# Host-identity spoofing — what is real and what is not

Finding **F2** (work item **T4**). This file is deliberately blunt about the
limits of what the container can do, because a spoof that only *looks* like it
works is worse than no spoof at all.

## The machine we are pretending to be

Every profile owns a *machine blueprint*: a real CPU that actually shipped, with
its true physical core count and true SMT thread count, paired with the OEM
firmware a machine with that CPU actually shipped with. The blueprint is the
single source of truth — `cpu_cores` is **derived from the model**, never rolled
independently. That fixes the original contradiction where Fiverr-Demo claimed
14 cores while the CPU string said "8-Core".

The generated machine is persisted in `<profile>/hardware-signature.json` and
reused on every launch; missing fields are backfilled in place so an existing
profile keeps its identity.

## What each leak actually is, and what we actually did

| Leak | Can it be overridden unprivileged? | What we did | Verdict |
|---|---|---|---|
| `nproc` / `sched_getaffinity` / `navigator.hardwareConcurrency` | **Yes, genuinely** | `taskset -c 0-N-1` in `stealth-launch.sh`, unchanged | **REAL** |
| `/proc/cpuinfo` | **No** | LD_PRELOAD shim | **PARTIAL** |
| `/proc/meminfo` | **No** | LD_PRELOAD shim (`open`/`fopen` + `sysinfo()`) | **PARTIAL** |
| `/sys/class/dmi/id/*` | **No** | LD_PRELOAD shim | **PARTIAL** |
| `uname` | **No** | LD_PRELOAD shim (`uname()` + `/proc/version`) | **PARTIAL** |
| MAC address | **No** | LD_PRELOAD shim (`getifaddrs()`) | **PARTIAL** |
| hostname | n/a | **not ours** — see `docker_manager._profile_hostname()` | out of scope |

### Why the files themselves cannot be replaced

Measured inside the shipped image, as user `chrome` (uid 1000), not privileged:

```
$ touch /sys/class/dmi/id/x
touch: cannot touch '/sys/class/dmi/id/x': Read-only file system
$ touch /proc/x
touch: cannot touch '/proc/x': No such file or directory
$ unshare -Urm --propagation private true
unshare: unshare failed: Operation not permitted
$ grep -E " /proc | /sys " /proc/self/mounts
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
sysfs /sys sysfs ro,nosuid,nodev,noexec,relatime 0 0
```

* `/sys` is mounted **read-only** by Docker.
* `/proc` is read-only *for unprivileged users* — the kernel refuses file
  creation there, so there is nothing to `mv` a fake over.
* Unprivileged **user namespaces are blocked** by Docker's default seccomp
  profile, so the "mount a tmpfs over `/proc` inside a user namespace" trick is
  unavailable. `CAP_SYS_ADMIN` is not held either.

Therefore: **no genuine override of the on-disk `/proc` or `/sys` content is
possible in this container.** Anything claiming otherwise would require
`--privileged`, `--cap-add SYS_ADMIN` or a seccomp relaxation, i.e. giving up
the isolation that is the entire point of the container.

### What the shim actually does, and its real limits

`scripts/docker/libhwspoof.c` is built into the image at `/usr/lib/libhwspoof.so`
and activated by `hardware-spoof.sh` via `LD_PRELOAD`. It:

* redirects `open`/`open64`/`openat`/`openat64`/`fopen`/`fopen64` of a strict
  allow-list — `/proc/cpuinfo`, `/proc/meminfo`, `/proc/version`,
  `/sys/class/dmi/id/*`, `/sys/devices/virtual/dmi/id/*` — onto a per-profile
  fake tree materialised under `/home/chrome/.runtime/hwspoof`;
* overrides `sysinfo()` (total RAM), `uname()` (kernel release) and
  `getifaddrs()` (MAC).

Its limits, stated plainly:

1. **Only libc-mediated readers are affected.** A process that issues a raw
   `syscall(__NR_openat, ...)` bypasses the shim entirely. Chromium's own
   `base::` filesystem helpers do exactly this, so *Chromium's internal reads of
   these files are NOT masked*. We could not verify Chromium's behaviour from a
   web page because a page cannot read these paths at all.
2. **The kernel's view is unchanged.** `/proc/self/maps`, another process
   reading the same file, `ptrace`/`strace` of a process, and anything already
   cached in a running process all still see the real host.
3. **The realistic exposure was native code in the container all along.** Web
   content in a browser cannot read `/proc/cpuinfo` or `/sys/class/dmi`. These
   leaks matter to anything running a native binary inside the container.
4. The shim only ever *reduces* reported values (`sysinfo()` will not report
   more RAM than the host has), so it cannot be used to make the container look
   more capable than it is.

If you need the values genuinely masked — including from Chromium's own
process — the only real options are to run the container with
`--pid=host`-style isolation removed plus a `seccomp=unconfined` /
`--cap-add SYS_ADMIN` bind-mount of fakes over `/proc` and `/sys`, or to run
inside a VM. Both give up the container boundary. We did not do that, and we
did not write fake files that nothing would read.

## Graceful degradation

Nothing here is allowed to break a launch. `hardware-spoof.sh` was tested
against: shim missing, shim present but not a valid ELF, fake tree
unwritable, signature file corrupt, and profile directory read-only. In every
case the script falls back, logs a warning to stderr, and Chromium starts.
`stealth-launch.sh` additionally clamps a degenerate `CHROME_CPU_CORES` to 1
rather than letting `taskset` abort the container.

Note that musl's loader **silently ignores** an `LD_PRELOAD` it cannot load and
still execs the program with status 0 — so "the process started" is not proof
the shim is active. The load check therefore uses a probe file written by the
shim's constructor.

## Files

| File | Role |
|---|---|
| `scripts/docker/hardware-spoof.sh` | blueprint table, backfill, fake-tree generation, shim activation |
| `scripts/docker/libhwspoof.c` | the LD_PRELOAD shim |
| `scripts/docker/stealth-launch.sh` | `taskset` pinning (the real `hardwareConcurrency` control), clamps, banner |
| `tests/test_hardware_spoof.sh` | schema, coherence invariant, backfill, legacy upgrade, degradation |
| `Dockerfile` | `hwspoof-build` stage; copies the shim to `/usr/lib/libhwspoof.so` |
