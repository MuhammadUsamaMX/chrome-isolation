<div align="center">

<img src="assets/icons/icon.png" width="96" height="96" alt="Chrome Isolation Icon"/>

# Chrome Isolation Manager

**Run fully isolated Chrome browser profiles in Docker containers — managed from a native GNOME desktop app.**

[![License: Non-Commercial](https://img.shields.io/badge/License-Non--Commercial%20(All%20Rights%20Reserved)-dc2626.svg)](LICENSE)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux%20%28GNOME%29-10b981.svg)](#requirements)
[![Electron](https://img.shields.io/badge/Electron-35-47848F.svg)](https://electronjs.org)
[![Docker](https://img.shields.io/badge/Docker-required-2496ED.svg)](https://docs.docker.com/get-docker/)

</div>

---

## What it does

Each browser profile lives in its own Docker container. Profiles are completely isolated from each other and from your host browser — no shared cookies, history, sessions, or fingerprints.

The app is a **native Electron desktop application** for GNOME/Wayland Linux. No web server, no Flask, no localhost port — the UI talks directly to the backend via a secure stdio IPC bridge.

Chrome that opens via this manager reads your host's fonts, icons, GTK theme, and dark/light mode preference (read-only) so it feels native — while remaining fully isolated at the data and identity level.

---

## Features

- **Full profile isolation** — Docker container per profile, separate data directories
- **Wayland-native** — runs on Wayland via `--ozone-platform=wayland`, no X11 needed
- **Stealth mode** — each profile presents as its own machine: stable per-profile CPU core count, browser language, GPU backend (real GPU or software), timezone, user-agent, hostname, and window size
- **Host folder sharing is opt-in and narrow** — apart from the curated appearance paths listed below, no host directory is mounted into a container unless *you* explicitly pick one. A folder you choose is mounted **read-only** at `/home/chrome/host`. Your home directory is **never** mounted implicitly; leaving **Host Folder** empty mounts an empty, per-profile `0700` directory instead (see [S7](#security) and finding F1)
- **Per-profile proxy** — route a profile's traffic through a SOCKS5/HTTP proxy for a different egress IP; manage a shared proxy list in the app (sidebar → **Proxies**), pick one per profile from a dropdown, or enter a custom URL; loopback addresses are rewritten to the host automatically, WebRTC is forced through the proxy, and authenticated proxies (`socks5://user:pass@host:port`) are handled by an in-container auth wrapper
- **Dark / Light mode** — follows host GNOME colour scheme; toggle in titlebar
- **Host preferences inherited** — a fixed, curated set of paths (system fonts/icons/themes and your `~/.local/share/fonts`, `~/.icons`, `~/.themes`, `~/.config/gtk-*`) is mounted read-only, so Chrome looks native without granting access to the rest of your home directory
- **No localhost attack surface** — all IPC is over stdio JSON, no HTTP server
- **Profile import / export** — safe ZIP/TAR archive with path-traversal protection
- **GNOME integration** — `.desktop` entry, app icon, `chrome-isolation --profile <name>` CLI
- **Hardened security** — no `--privileged`, no `--ipc=host`; containers run with `cap_drop=ALL`, `no-new-privileges`, a `pids_limit`, a memory limit and a read-only root filesystem; `.desktop` entry refresh no longer goes through a shell, and per-profile launchers cannot impersonate a browser or claim a MIME type. **Chromium itself still runs `--no-sandbox` — see [Security](#security) for why, and what it costs**

---

## Architecture

```
GNOME App (Electron)
  └── electron/main.js          — BrowserWindow, IPC handlers, window controls
  └── electron/preload.js       — contextBridge (narrow API only)
  └── electron/renderer/        — UI (HTML + CSS + JS, no Node access)
        └── index.html
        └── style.css           — dark/light design system
        └── app.js

  │  stdio JSON (newline-delimited)
  ▼

Python Backend (bridge.py)
  └── bridge.py                 — stdin/stdout JSON-RPC dispatcher
  └── profile_service.py        — high-level profile operations
  └── docker_manager.py         — Docker container lifecycle
  └── host_share.py             — resolves + validates what host dir (if any) is shared
  └── registry.py               — profile path registry (JSON)
  └── proxy_store.py            — shared proxy list
  └── archive.py                — safe import/export
  └── validator.py              — input validation (path traversal prevention)
  └── desktop_manager.py        — .desktop entry generation
  └── config.py                 — paths & constants

  │  docker run / docker stop
  ▼

Docker Container (isolated-chrome image)
  └── Alpine Linux + Chromium
  └── scripts/docker/stealth-launch.sh   — entrypoint
  └── scripts/docker/hardware-spoof.sh   — stable fingerprint
  └── scripts/docker/user-agent-spoof.sh — stable UA
```

> **Which Chromium you are actually running.** The `Dockerfile` now targets
> `alpine:3.24` and a current Chromium (finding F3), but building the image and
> *promoting* it are separate acts. `isolated-chrome:latest` on a given machine
> is whatever was last promoted there, and it can lag the `Dockerfile` by
> weeks. Check what you have rather than assuming:
>
> ```bash
> docker image inspect isolated-chrome:latest --format '{{.Id}} {{.Created}}'
> docker run --rm --entrypoint sh isolated-chrome:latest \
>   -c 'cat /etc/alpine-release; chromium-browser --version'
> ```
>
> A Launch from the app recreates the container; it does **not** rebuild the
> image. `scripts/verify.sh` check 6 measures this and tells you which of the
> two you are looking at.

---

## Requirements

| Dependency | Version | Notes |
|---|---|---|
| Linux (GNOME + Wayland) | Any modern | Arch, Fedora, Ubuntu 22.04+ |
| Node.js | 18+ | [nodejs.org](https://nodejs.org) |
| npm | 8+ | Bundled with Node |
| Python | 3.10+ | Usually pre-installed |
| Docker | 24+ | [docs.docker.com](https://docs.docker.com/get-docker/) |
| `python-docker` | latest | `pip install docker` |

> **Docker access is root-equivalent.** On first launch the app can offer to install
> Docker and add your user to the `docker` group. Membership in the `docker` group is
> equivalent to full root on this machine: anyone able to talk to the Docker socket can
> start a container with a bind mount of `/` and take over the machine. The app now
> warns about this before running the `usermod -aG docker` step — see finding
> [F7](#remediation-audit--2026-09-29) below. If you do not accept that trade-off,
> run the containers under a dedicated non-privileged user or a rootless Docker daemon.
>
> This is not a one-time prompt. The consent dialog appears once, when Docker is
> installed, but group membership persists — so the app **also** re-checks on every
> start and shows a startup warning while your user is in the `docker` group. That
> repeated warning is the one that covers the live risk.
>
> The `usermod` call is passed as an argument vector, never interpolated into a
> `bash -c` string. An earlier version of this app built a shell string from
> `process.env.USER`, which is attacker-influenceable (login shells, `su` without
> `-l`, PAM/environment, a `.bashrc`) and reached `root` via `pkexec`. That was
> reported fixed once while it was still open. It is closed now, and
> `scripts/verify.sh` check 10 greps the generator rather than trusting this
> paragraph.

---

## Installation

### Option A — Standalone binary (recommended, any Linux)

Download the latest **`.AppImage`** from the [Releases page](https://github.com/MuhammadUsamaMX/chrome-isolation/releases). It runs on **any Linux distribution** — Node.js, Python, and npm are bundled inside; no compilation or package managers needed. The only system requirement is **Docker** (the app runs one container per profile — first launch offers to install it if missing).

```bash
# Download, make executable, run
wget https://github.com/MuhammadUsamaMX/chrome-isolation/releases/latest/download/chrome-isolation-1.1.0-linux-x64.AppImage
chmod +x chrome-isolation-1.1.0-linux-x64.AppImage
./chrome-isolation-1.1.0-linux-x64.AppImage
```

First launch: the app checks for Docker, and builds the `isolated-chrome` container image automatically (one-time, a few minutes).

### Option B — `.deb` / `.rpm` (Debian/Ubuntu, Fedora/RHEL)

```bash
# Debian / Ubuntu
sudo dpkg -i chrome-isolation-1.1.0-linux-x64.deb && sudo apt-get install -f

# Fedora / RHEL
sudo dnf install chrome-isolation-1.1.0-linux-x64.rpm
```

### Option C — From source (developers)

```bash
# 1. Clone
git clone https://github.com/MuhammadUsamaMX/chrome-isolation.git
cd chrome-isolation

# 2. Install npm deps
npm install

# 3. Install Python dep
pip install docker

# 4. Run the installer (copies app, builds Docker image, creates .desktop entry)
bash install.sh
```

The installer:
- Copies the app to `~/.local/share/chrome-isolation-manager/`
- Builds the `isolated-chrome` Docker image
- Creates `~/.local/bin/chrome-isolation` launcher
- Installs `.desktop` entry for the GNOME app grid
- Stops and disables the old `chrome-manager.service` if present

After install, launch from the GNOME app grid or:

```bash
chrome-isolation
```

---

## Usage

### Create a profile
Click **New Profile** in the sidebar. Give it a name (letters, numbers, dash, underscore). Optionally set a custom storage path, a **Host Folder** — a directory from your machine mounted read-only into the container at `/home/chrome/host` — and a **Proxy** chosen from the saved list or entered as a custom URL such as `socks5://127.0.0.1:1080` or `socks5://user:pass@proxy.example.com:1080`.

**Leaving Host Folder empty mounts nothing from your home directory.** The container gets an empty per-profile directory (`~/.local/share/chrome-isolation-manager/host-shares/<profile>/`, mode `0700`) at `/home/chrome/host` instead. To share something, type or paste a path into the field or press **Browse…**; to undo, use the **✕** button. The fields are ordinary editable text inputs — folder picking is bound to the **Browse…** buttons only, so you can type and paste paths as well.

On a profile card the host folder reads `Host: None (private empty folder)` when it
is unset — never `Host: ~`, which is what the old backend default actually did and
the single most misleading thing this app used to display.

An explicit host folder is still allowed, and "the home directory is never
mounted" means exactly that: `$HOME` itself is rejected in every spelling (`~`,
`~/`, the expanded path, `~/.gnupg/..`), as are `~/.ssh`, `~/.gnupg`, `~/.aws`,
`~/.docker`, keyring stores, `/etc`, `/usr`, and symlinks resolving into any of
them. **A subfolder of your home is still shareable** — `~/share`, for instance —
and so is any other dedicated directory. Read-only does not stop exfiltration, so
the control is "don't mount it", not "mount it read-only".

> Previously an empty **Host Folder** silently mounted your entire home directory
> read-only, which exposed SSH keys, `.gnupg`, keyring files and your other browser
> profiles to every container. That default was removed (finding F1). The in-app
> copy was updated to match; what the dialog says is:
> *"Type or paste a path, or use Browse to pick a folder. Mounted read-only at
> `/home/chrome/host` inside the container. **Your home directory is never
> mounted.** If you leave this empty, the container gets a private, empty folder
> just for this profile — nothing from your home is shared."*

### Manage proxies
Click **Proxies** in the sidebar to add, view, and delete named proxies shared by all profiles (e.g. `tunnel-alan1` → `socks5://127.0.0.1:10080`). Profiles reference them by name, so updating a proxy in the list updates every profile using it.

### Edit a profile
Click **Edit** on any profile card to change the host folder, proxy, and machine timezone after creation. As with creation, an empty host folder means *no host directory is shared*. Changes apply the next time the profile is launched — the host mount is baked in at `docker run` time, so editing a running profile's host folder has no effect until it is stopped and relaunched.

### Launch Chrome
Click **Launch** on any profile card. A Docker container starts and Chromium opens on your Wayland display. The container uses your host fonts, icons, and GTK theme (read-only), and presents the profile's own machine fingerprint: CPU model and core count, DMI/board strings, total RAM, browser language, GPU backend (real GPU or software), timezone, user-agent, hostname, and window size. The spoofed values are mutually consistent — the advertised core count always matches the spoofed CPU string — and they are stable for the life of the profile.

**How much of this is real, and what is not.** Each profile draws a *blueprint*:
one of 13 real CPUs, paired with the OEM firmware that shipped with it, with
`cpu_cores` / `cpu_physical_cores` **derived from the model** rather than rolled
independently. That is why the advertised core count always matches the CPU
string — an earlier version generated a "14-core" profile whose model string said
"8-Core", which is a fingerprint tell that gives itself away. Measured over 40
fresh profiles: 11 models, 11 boards, 5 core counts, 40 unique MAC addresses.

What is *genuinely* hidden vs. only partially, because "your CPU model is a
string on a web page" is a much weaker claim than it sounds:

| Leak | Hidden for real? |
|---|---|
| `navigator.hardwareConcurrency`, `sched_getaffinity` | **Yes** |
| `/proc/cpuinfo`, `/proc/meminfo`, `/sys/class/dmi/id/*` | **Partially** — libc-mediated readers only |
| MAC address | Partially |
| `uname` / kernel release | **No.** busybox uses a raw syscall |

This is an `LD_PRELOAD` shim that redirects libc `open()`/`fopen()`/`uname()`/
`sysinfo()`. A raw `syscall(__NR_openat, ...)` bypasses it, and Chromium's own
`base::` file helpers issue exactly those — so **Chromium's internal reads of
these files are not masked**. It cannot be tested from a web page, because a page
cannot read those paths at all.

It is also not fixable by bind-mounting fakes over `/proc` and `/sys`, and this is
a deliberate refusal rather than an oversight: `/proc` refuses unprivileged file
creation, `/sys` is mounted read-only, and Docker's seccomp profile blocks
unprivileged user namespaces (`unshare -Urm` → `EPERM`). Overriding them for real
requires `--privileged`, `--cap-add SYS_ADMIN`, or `seccomp=unconfined` — i.e.
giving up the isolation the app exists to provide. **Do not "fix" this by adding a
`/proc` or `/sys` bind mount.** The measured evidence is in
`scripts/docker/HOST-SPOOFING.md`.

The kernel's own view is unchanged, and `sysinfo()` can only ever *reduce* the
reported RAM, never invent it.

### Stop Chrome
Click **Stop** on the card.

### Export / Import profiles
Use **Export** to save a profile as a ZIP archive. Use **Import** in the sidebar to restore from an archive. Archives are validated for path traversal and symlink attacks before extraction.

### CLI (per-profile desktop entry)
After creating a profile, a `.desktop` entry is added to GNOME so you can launch it directly:

```bash
chrome-isolation --profile Work
```

### Light / Dark mode
Click the moon/sun icon in the titlebar. Preference is saved across sessions.

---

## Security

### Design-level mitigations

| Issue | Fix |
|---|---|
| **S1 — Path traversal in profile names** | `validator.py` — regex allowlist + `os.path.realpath` prefix check |
| **S2 — Archive extraction traversal** | `archive.py` — member-by-member validation, temp-dir staging, `tarfile filter="data"` |
| **S3 — Localhost HTTP attack surface** | Eliminated — no Flask, all IPC via stdio JSON bridge |
| **S4 — Orphaned custom-path profiles** | `registry.py` — JSON registry tracks all profiles with actual paths |
| **S5 — Container over-privilege** | No `--privileged`, no `--ipc=host`, no `SYS_ADMIN`. Containers run with `cap_drop=ALL`, `no-new-privileges:true`, a `pids_limit` and a read-only root filesystem, with only explicit `tmpfs` paths writable. **Chromium runs `--no-sandbox` — there is no browser sandbox; see below.** The container is the boundary, so those flags are what limit the blast radius of a renderer RCE |
| **S6 — Flask dev server in production** | Eliminated — Electron replaces Flask entirely |
| **S7 — Host files exposed to container** | **Nothing is mounted unless the user asks for it**, apart from a fixed list of curated appearance paths (system + user fonts, icons, themes, GTK config) and the audio cookie. An explicit host folder is mounted read-only at `/home/chrome/host`; an empty setting mounts an empty per-profile `0700` directory, never `$HOME`. Read-only still permits exfiltration, so the control is "don't mount it" rather than "mount it read-only". `$HOME` itself is refused in every spelling, as are `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.docker`, keyring stores, `/etc`, `/usr`, and symlinks resolving into any of them — **a subfolder of your home is still shareable**, which is a deliberate widening, and a deliberate limit, not an oversight in either direction |
| **S8 — Per-profile proxy** | Optional SOCKS5/HTTP proxy per profile; loopback rewritten to `host.docker.internal` (host-gateway alias), WebRTC forced through the proxy to prevent IP leaks, and `user:pass@` credentials handled by an in-container auth wrapper (Chromium ignores credentials in proxy URLs) |
| **S9 — Shell injection in desktop-entry refresh** | `desktop_manager.py` — `update-desktop-database` is invoked via `subprocess.run([...])` with `shell=False`; profile names are additionally constrained by the S1 validator |

### There is no Chromium sandbox — and there cannot be one

Chromium is launched with `--no-sandbox`. This is not a mitigation that is merely
imperfect; **the sandbox is genuinely off**, and nothing else in this README
substitutes for it.

Why: Chromium's sandbox needs unprivileged user namespaces, and Docker's default
seccomp profile blocks the syscalls that create them. Restoring it therefore needs
either `--cap-add SYS_ADMIN` or `--security-opt seccomp=unconfined` — each of
which weakens the container hardening above, which is the isolation the whole app
exists to provide. Given the choice between "Chromium's own sandbox" and "the
container boundary", this project keeps the container boundary and drops the
sandbox. That is a real reduction in defence in depth and is stated here rather
than buried.

**It is technically restorable, and the trade-off is genuinely open.**
`--security-opt seccomp=unconfined` has been verified to compose with the full
hardening — capabilities still zero, `NoNewPrivs` still 1, the sandbox does start,
the page renders — and this host already has `kernel.unprivileged_userns_clone=1`,
so no sysctl change is needed. It is **not** enabled, because `seccomp=unconfined`
removes *all* syscall filtering rather than just the blocking of
`clone`/`unshare`/`setns`. The right version of this change is a narrow custom
seccomp profile permitting only those calls with `CLONE_NEWUSER`, and that needs a
deliberate decision rather than a one-line edit.

`scripts/verify.sh` does not assert that the sandbox is enabled, because it is
not. A check that asserted it would be permanently red and therefore ignored —
which is worse than no check at all.

### `/.dockerenv` is deliberately left exposed

The container is **not** masked against `/.dockerenv`. Inside it, the file is
Docker's own, with its genuine `0755 root:root` metadata. Anything that stats it
can tell immediately that it is inside a container.

This was a decision, taken against evidence rather than assumed:

| | mode | owner |
|---|---|---|
| real Docker `/.dockerenv` | `755` | `0:0` |
| this app's mask, bound over it | `644` | `1000:1000` |

The mask could only ever be half-fixed. The host user can `chmod 755`, but the
owner stays `1000:1000`, because the container runs as non-root `chrome` and `0:0`
is unreachable without a root helper. Real Docker's file is *always* `0:0`, so a
mode-only match still reads as anomalous to anything that stats the owner.
Running the container as UID 0 to obtain `0:0` would undo the `cap_drop=ALL`
hardening directly.

**So the mask was removed. A correctly-statted file beats a visibly-fake one.**
This is an accepted trade-off, not an oversight, and the tell remains — it is
simply one tell among several that are addressed properly rather than
cosmetically (the `uname` release, the cgroup v2 line, the Alpine `os-release`,
and the DMI/cpuinfo rows described under **Launch Chrome**). An earlier version of
this application masked `/.dockerenv` with a user-owned empty file; if you are
reading this on a machine where that mask was never removed, `scripts/verify.sh`
check 7 will tell you, and will distinguish a live container still carrying the
mask from the code having regressed.

### Desktop identity — why you may have many of these entries and none of them are "a browser"

Each profile gets a `.desktop` launcher, which means a desktop full of
Chrome-named-looking entries. The generated entries are constrained so that they
cannot compete with your browser:

- **No `WebBrowser` category.** Every generated entry is `Network;Utility;`.
- **No `%u`/`%U` in `Exec`.** These launchers take no URL arguments, so they
  never appear in "open link with…" as a browser.
- **No `MimeType=` and no `x-scheme-handler/`.** They claim no file or protocol
  association, so they neither raise nor answer a default-browser prompt.
- **No borrowed icon.** `google-chrome` is deliberately absent from the fallback
  chain. If the app's own icon is missing, the entry degrades to a generic
  `web-browser` icon — a cosmetic inconvenience. Wearing Chrome's icon is
  impersonation, and impersonation is the bug being fixed here.
- **`StartupWMClass` equals Chromium's `--class`.** Both come from one function,
  so a window can always be matched to its own launcher and never displays a
  neighbouring entry's icon.

`scripts/verify.sh` check 10 asserts all five, against the generator *and* against
the entries on disk, and fails if `docker_manager` ever stops deriving `--class`
from the same source.

### Chromium update — detect and inform, rebuild on consent

Chromium is not installed on the host — it lives inside the `isolated-chrome`
Docker image, so "updating Chromium" means rebuilding that image from the
`Dockerfile`. A cached build is ~31 s; a cold build is minutes and pulls
~300 MB. `image_update.py` therefore treats a rebuild as a background job, not
a startup step.

**By default it does not rebuild. It checks, reports, and waits for you.**

On app start the updater checks whether a newer Chromium is published, records
the result, and stops. Nothing is built until you click **Apply now**.

This is the default because the operation is not reversible:

- **A profile cannot be downgraded.** Chromium migrates a browser profile
  *up* and will not bring it back down. Once you move 124 → 152 you cannot
  return to 124 with your profile intact. The image layers are recoverable —
  nothing is pruned — but profile data may not be.
- **Verification is weaker than it looks.** The pre-promotion check proves
  Chromium *runs* inside the image. It does not prove the full hardened launch
  works: the tmpfs mounts are sized by trial, and a Chromium/Alpine major bump
  changes what Chromium writes at first start. Your first real launch after an
  update is the actual test.
- **The build is root-adjacent.** It compiles C for the `hwspoof` shim.

A browser silently replacing itself is not an acceptable default, so a human
crosses that door. If you have automation around this app and genuinely want
the unattended behaviour, `CHROME_ISOLATION_AUTO_REBUILD=1` restores it.

**Consent is tied to a version, not to updates in general.** The **Apply now**
action carries the version the check reported. If Alpine publishes a different
build in the interval between the check and the click, the approval no longer
applies, nothing is built, and the app asks you again. You will never approve
153 and silently receive 154.

**It never blocks.** The bridge method hands the rebuild to a daemon thread and
returns immediately. No IPC request, Electron call or UI action waits on it.

**It never risks your working image.** The build targets a staging tag
(`isolated-chrome:staging-<pid>-<ts>`). The staging image is then verified —
Chromium runs as the `chrome` user, `versions.env` is present, and the
`LD_PRELOAD` shim actually loads — and only a fully verified image is promoted
onto `isolated-chrome:latest` with a single `docker tag`. On any failure the
staging tag is deleted and `:latest` is left byte-for-byte untouched. This
matters because `_ensure_image()` only rebuilds on `ImageNotFound`: a broken
image left on `:latest` would be launched forever with no recovery path.

Also: builds are serialised by an `flock` on
`~/.local/share/chrome-isolation-manager/image-update.lock` (cross-process —
Electron restarts the bridge on crash, so an in-process flag is not enough);
a build is deferred while any `chrome-*` container is running; and attempts
are debounced (6 h normally, 24 h after a failure) so a persistently failing
update cannot become a retry loop.

| Variable | Effect |
|---|---|
| *(unset — the default)* | **Detect and inform only.** Availability is checked and reported; nothing is built without your consent |
| `CHROME_ISOLATION_AUTO_REBUILD=1` | **Opt-in for unattended rebuilds.** Restores the old "just keep it current" behaviour. Still respects the single-flight lock, the failure cooldown and the running-container deferral — those protect the install, they are not politeness knobs |
| `CHROME_ISOLATION_NO_AUTO_UPDATE=1` | **Kill switch.** Disables detection, the consent path and both opt-ins above. Nothing is built, no Docker command runs. Beats everything else in this table |
| `CHROME_ISOLATION_FORCE_IMAGE_UPDATE=1` | Documented manual opt-in: rebuild at the next app start even when the version check says there is no update. Still respects the lock and the running-container deferral |
| `CHROME_ISOLATION_VERSION_DETECTOR=module:callable` | Registers the version-detection module (see below) |

`image_update_status` returns the current `state` plus the fields the UI needs
to render it:

| `state` | Meaning |
|---|---|
| `idle` | Nothing has happened yet |
| `checking` | A check is running in the background |
| `up_to_date` | **Positively determined:** the installed version is current |
| `update_available` | An update exists. `consent_required` is `true`; show **Apply now** |
| `unknown` | **Could not determine** — no network, no image, an unreadable index. This is *not* `up_to_date` |
| `building` / `verifying` | A consented rebuild is running. `:latest` is still untouched |
| `ready` | A verified image was promoted onto `:latest` |
| `failed` | The attempt failed. `:latest` is untouched; `error` says why |
| `deferred` | A `chrome-*` container is running; the build was postponed |
| `disabled` | `CHROME_ISOLATION_NO_AUTO_UPDATE` is set |
| `debounced` / `interrupted` | Waiting out the interval, or a previous attempt died |

Also returned: `update_available` (tri-state `true`/`false`/`null` — `null`
means *could not determine*, and must not be rendered as "up to date"),
`current_version`, `latest_version`, `consent_required`, `consent_version`,
`consent_warning`, `policy` (`notify_only`/`unattended`/`disabled`),
`error`, `last_result`, and `next_attempt_at`/`retry_in_seconds`.

The `up_to_date` / `unknown` distinction is deliberate and load-bearing.
Collapsing them is how a browser stays stale behind a green UI: "we could not
check" and "you are current" are opposite facts.

**The version-detection seam.** `image_update.py` does not know how to ask
"is a newer Chromium available?" — that belongs to a separate module, and
guessing its API would be worse than declaring a contract. It needs one
callable:

```python
def detector(image_name: str) -> dict:
    return {"current": "152.0.7977.82", "latest": "153.0.2.1"}
```

`latest` of `None`, a raised exception, or a malformed return all mean
"cannot tell". **The default is fail-closed**: with no detector registered,
nothing is rebuilt. "Cannot tell" must never be read as "yes, rebuild" —
a false positive costs minutes and 1.31 GB of I/O on every app start, and
gets it wrong every single time. The *current* version needs no detector and is
read straight from the image's `versions.env`, which the `Dockerfile` writes at
build time so reporting cannot drift from reality.

> Progress is written to **stderr** only. `stdout` is the newline-delimited
> JSON IPC protocol; one stray `print()` from the background thread corrupts
> the stream for the renderer.

### Remediation audit — 2026-09-29

An independent pass over the running installation on 2026-09-29 raised the findings
below. Every remediation has now landed and been committed; an adversarial review
afterwards found four further defects in the fixes themselves, all also closed.
This table is the audit trail.

**Read the *Status* column as a statement about the code, not about your
machine.** A fix in the code changes nothing about a container that already
exists. `scripts/verify.sh` distinguishes the two and reports "stale live state"
separately, which is the only honest way to present this: the code and the
runtime genuinely diverge until every profile is relaunched from the app.

| # | Finding | Remediation | Status |
|---|---|---|---|
| **F1** | `host_mount` fell back to `$HOME`, mounting the whole home directory read-only at `/home/chrome/host` for every profile — SSH keys, `.gnupg`, keyring files, other browser profiles all readable from a compromised page | New `host_share.py`: empty `host_mount` resolves to an empty per-profile `0700` share dir; explicit mounts validated, and `$HOME` itself refused in every spelling | **Fixed** (code). Tests pass, and reverting the fix turns them red. In-app copy updated. **Containers created before this still bind `$HOME` until relaunched** |
| **F2** | DMI, `/proc/cpuinfo` and `/proc/meminfo` leaked the real machine (`MS-7E27`, `AMD Ryzen 7 9700X`, 32 GB), and the spoofed core count contradicted the CPU string | `LD_PRELOAD` shim for the libc-mediated readers; core counts derived from the CPU blueprint. **Partial by construction — see the honest limits under Launch Chrome** | **Fixed for what is fixable.** The residue (`uname`, Chromium's own raw-syscall reads) is documented, not hidden. Silently ignored if the shim fails to load |
| **F3** | Image was built 2026-09-13 on EOL `alpine:3.19` shipping Chromium 124 (Apr 2024) — a long, unsupported CVE tail | Rebuilt on `alpine:3.24` with Chromium 152, plus a build-time guard that fails the build if Chromium is missing | **`Dockerfile` fixed.** Whether your machine runs it is separate — `:latest` only changes when an image is promoted, and a Launch does not rebuild. Check with the command above |
| **F4** | `--no-sandbox` with a weak container profile: no `cap_drop`, no `no-new-privileges`, no `pids_limit`, writable rootfs | See S5 — `cap_drop=ALL`, `no-new-privileges`, `pids_limit=2048`, read-only rootfs, `mem_limit`, memory and file-descriptor limits | **Fixed**, and verified behaviourally free (an A/B against an unhardened container with identical mounts produced byte-identical logs). **The `--no-sandbox` half is not fixed and cannot be — see the sandbox section** |
| **F5** | `/.dockerenv` was masked with a `0644` user-owned empty file instead of Docker's real `0755 root:root` — a fingerprint tell in itself | **Mask dropped.** A `0:0` owner is unreachable from a non-root container, so any mask this app can create is a *worse* tell than the genuine file | **Decided and fixed.** Accepted trade-off, documented in its own section. Not an oversight |
| **F6** | `chrome-Fiverr-Read` and `chrome-Fiverr-Demo` were left running on untagged image `b226dbacf344`, two builds behind `isolated-chrome:latest` | Remove the stale containers so every profile runs the current image | **Done** — the containers are gone. Note the original urgency here was *inverted*: those containers were the only thing holding ~1 GB of dead image alive, and prune skips any image a container references (stopped ones included), so there was never an emergency |
| **F7** | The app installs Docker and runs `usermod -aG docker <user>` without explaining that Docker socket access is root-equivalent | Consent gate before the privilege-granting step, plus a repeated startup warning while membership persists; the `usermod` call argument-vectored | **Fixed — after two false "fixed" reports.** The `bash -c` string-injection surface was reported closed while still open; it is closed now, and the fix was verified rather than re-reported |
| **F8** | The Python bridge restarted 1 s after any exit, forever, dropping in-flight request payloads and re-running `_ensure_image()` each time | Exponential backoff (1s→30s cap, give up after 5) with the counter reset by a stable run; queued-but-unsent requests retried, genuinely in-flight ones rejected | **Fixed — after the first attempt made it worse.** The original fix rejected nothing and retried nothing, leaving promises hanging forever; that is strictly worse than the original and was caught in review |
| **F9** | Security fixes sat uncommitted in a dirty working tree, and the two stale containers still ran the pre-fix image layer | Commit the fixes | **Fixed.** The `os.system` → `subprocess.run` fix is at `HEAD`; `scripts/verify.sh` check 9b asserts it against the commit, not the working tree |

> **Two lessons from this audit, recorded because they recur.** First, twice a
> green result was reported that was an artifact — once from a test file caught
> mid-write, once from a test that could never pass. Both were caught by
> re-verifying against an immutable artefact rather than the dirty working tree.
> **Test counts are not evidence; test content is.** Second, the same
> review pass found two hollow assertions that passed regardless of the value
> they claimed to check, and a third test gap that let the F1 fix be silently
> reverted. A green suite is not the same as a verified property.
>
> One item is **deliberately not fixed** and is not a pending task: the renderer
> shipped with `aria-modal` and no focus trap, and `readonly` on path inputs
> that removed the ability to type. Both were reverted rather than completed —
> an ARIA contract the app does not keep is worse than no contract, and
> `readonly` broke typing and paste for everyone.

### Cleaning up stale containers and images

A stopped container keeps its image alive, including one that is otherwise
dangling. Removing the container is what makes that image prunable — so the
usual "prune now or lose it" urgency here is **inverted**, and acting on it is how
people delete an image a live container still needs.

Removing a container does **not** touch profile data. Every profile's data lives
in `~/Isolated-Chrome/profiles/<profile>/` on the host (or wherever
`$CHROME_ISOLATION_HOME` points) and is bind-mounted into the container, so the
container's own writable layer holds only disposable cache and logs, and that is
discarded on removal anyway. There are no Docker volumes holding your data.

Verify before removing anything:

```bash
# Which image does each container actually use?
docker ps -a --filter 'name=chrome-' --format '{{.Names}}\t{{.Image}}\t{{.Status}}'

# How many containers reference each image? "CONTAINERS" must be 0 before you prune.
docker system df -v | sed -n '/^Images/,/^Containers/p'

# Confirm the profile data is a host bind mount, not a Docker volume
docker inspect chrome-<name> --format '{{range .Mounts}}{{.Type}} {{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
```

You want to see
`bind /home/<you>/Isolated-Chrome/profiles/<name> -> /home/chrome/.config/chromium`
and `Local Volumes: 0`. Only then proceed — and re-run the second command
*after* any rebuild, so you are not pruning the image a live container needs.

> **Note on a claim this document previously made.** An earlier version of this
> runbook said `docker image prune` would destroy the layer those stale
> containers depended on. That was wrong, and the wrong version was the
> dangerous one, because it implied a fake urgency to act immediately. Prune
> skips any image held by a container; `--force` is required to break that.


---

## Project structure

```
chrome-isolation/
├── electron/                  # Electron frontend
│   ├── main.js                # Main process
│   ├── preload.js             # contextBridge API
│   └── renderer/              # UI
│       ├── index.html
│       ├── style.css
│       └── app.js
├── backend/                   # Python backend
│   ├── bridge.py              # stdio JSON-RPC
│   ├── paths.py               # profile-store location (pure, no side effects)
│   ├── profile_service.py
│   ├── docker_manager.py
│   ├── image_update.py         # Background Chromium auto-update
│   ├── host_share.py
│   ├── registry.py
│   ├── proxy_store.py
│   ├── archive.py
│   ├── validator.py
│   ├── desktop_manager.py
│   └── config.py
├── tests/                     # Backend tests
├── scripts/
│   ├── build/after-pack.js    # electron-builder post-pack hook
│   ├── migrate_profiles_dir.py  # relocate profile data (dry run by default)
│   └── docker/                # Container entrypoint scripts
│       ├── stealth-launch.sh
│       ├── hardware-spoof.sh
│       └── user-agent-spoof.sh
├── assets/icons/icon.png
├── bridge.spec                # PyInstaller spec for the Python bridge
├── Dockerfile
├── build.sh                   # AppImage / .deb / .rpm build script
├── install.sh                 # Desktop app installer
├── uninstall.sh               # Full uninstaller (all install methods)
├── setup.sh                   # Dev environment setup
├── package.json
└── requirements.txt
```

---

## Development

```bash
# Run without installing
npm start

# Watch bridge stderr (Python tracebacks) in terminal
npm start 2>&1
```

The renderer has zero Node.js access — all calls go through `window.api` (contextBridge) → IPC → main process → Python bridge → Docker.

---

## Where profile data lives

Profile data lives in **`~/Isolated-Chrome/profiles/<profile>`**. The name is
deliberately not something any browser creates, so the directory can never be
confused with real Google Chrome's storage (`~/.config/google-chrome`) or swept
away as "browser junk". Nothing in this app ever reads or writes inside
`~/.config/google-chrome`.

Override the root with the `CHROME_ISOLATION_HOME` environment variable, which
is what the location is derived from:

```bash
export CHROME_ISOLATION_HOME=/mnt/data/isolated-chrome   # -> /mnt/data/isolated-chrome/profiles
```

### Relocating from the old `~/Chrome`

`~/Chrome` was the original location. It has been renamed, and on a migrated
install it no longer exists.

The rename is backward compatible, but only deliberately so: if the new location
holds no profiles **and** the isolation home is the default `~/Isolated-Chrome`,
the app falls back to `~/Chrome`, so an un-migrated install keeps its profiles,
its logins and its history with no action from you. That gate is a safety
property, not tidiness — without it, any caller pointing the store at a custom
directory (the test suite does) would be handed the real `~/Chrome` as a
fallback and start writing real user data into it. When both locations hold
profiles the new one wins, and whatever is left behind in the other is *reported*,
never used and never deleted.

**"Holds profiles", not "exists"** is the test on purpose: the app creates the new
directory on startup, so a mere existence check would let that empty directory
shadow a perfectly good legacy store and make every existing profile silently
vanish from the UI.

To migrate when you are ready:

```bash
cd <repo>
python3 scripts/migrate_profiles_dir.py            # dry run: prints the plan, changes nothing
python3 scripts/migrate_profiles_dir.py --apply    # moves, verifying each profile first
```

The migration is conservative by design:

- **Dry run by default.** Nothing moves without `--apply`.
- **Verified before anything is deleted.** Each profile's file count and total
  bytes are recorded before the move and re-checked after. On any mismatch the
  profile is reported and its source is left intact.
- **`rename` when possible** (same filesystem — atomic and instant), otherwise
  copy into a `.partial` staging directory, verify, promote with an atomic
  rename, verify again, and only then remove the source.
- **Refuses a non-empty destination** it cannot fully account for.
- **Backs up `profiles.json`** before rewriting it, and rewrites it atomically.
- **Leaves an empty `~/Chrome` behind** for you to `rmdir` yourself.

If a profile moves but the registry rewrite then fails, that is reported as an
error naming every new path — never swallowed.

Run it against a different pair of locations with `--from` / `--to`, and
resume an interrupted run with `--apply --resume`.

> **This location is resolved, not hardcoded, everywhere it is used** — including
> in `uninstall.sh` and in `scripts/verify.sh`, which both ask
> `backend/paths.py` for the answer rather than assuming a path. That is not
> fastidiousness: an earlier version of the verification harness hardcoded
> `~/Chrome`, and on a host where the move had already happened it reported
> confident, wrong results instead of failing loudly. A hardcoded path does not
> announce that it is stale.

---

## Uninstall

### Any install method — use the script

```bash
bash <(curl -s https://raw.githubusercontent.com/MuhammadUsamaMX/chrome-isolation/main/uninstall.sh)
# or, from a clone:
bash uninstall.sh
```

The script confirms first, then removes: profile containers + the `isolated-chrome` Docker image, all profile data (`~/Isolated-Chrome/profiles`, and the legacy `~/Chrome` if that still holds data), the app data dir (registry, proxies, signatures), launcher, `.desktop` entry, icon, and the `.deb`/`.rpm` package if installed.

**What the `rm -rf` guard actually covers.** Because the path comes from
`$CHROME_ISOLATION_HOME`, a mis-resolved value could point `rm -rf` somewhere it
should not. The script refuses to remove `/`, `$HOME` itself, anything reached
through `$HOME/..`, and anything whose leaf name is not `profiles` or `Chrome` or
which is too shallow to be a profile store.

**Known limit, so you can judge it rather than discover it.** The guard keys on
the leaf name, and any directory can be named `profiles`. If
`$CHROME_ISOLATION_HOME` points at a directory that is *not* this app's isolation
home, the directory it names will be removed. It has to be set deliberately to
do that, and anything able to set the variable could remove the directory
anyway — but a typo, or an uninstaller disagreeing with the app about where the
store is, is the realistic way to get there. `scripts/verify.sh` check 11
executes the real script in a sandbox with sentinel files and reports this as a
FAIL until the guard is anchored on containment rather than on the leaf name.

### Manual uninstall

```bash
rm -rf ~/.local/share/chrome-isolation-manager
rm ~/.local/bin/chrome-isolation
rm ~/.local/share/applications/chrome-isolation.desktop
rm ~/.local/share/icons/hicolor/256x256/apps/chrome-isolation.png
rm -rf ~/Isolated-Chrome             # profile data — back it up first!
rm -rf ~/Chrome                      # only if a pre-relocation install left data
docker ps -aq --filter 'name=chrome-' | xargs -r docker rm   # containers hold image refs
docker rmi isolated-chrome
# .deb:  sudo dpkg -r chrome-isolation
# .rpm:  sudo dnf remove chrome-isolation
```

---

## Verifying an installation

```bash
scripts/verify.sh              # add -v to echo each sub-command
```

This checks the code, not your intentions. It has three statuses rather than two,
because conflating "the code is wrong" with "a container built before the fix is
still here" is how a harness starts crying wolf and gets ignored — and an ignored
harness is how a real regression slips through:

| Result | Meaning |
|---|---|
| **PASS** | The code, and the image, are correct |
| **FAIL** | A genuine defect — in the code, or in a live container or image the current code would not have produced |
| **STALE** | The code is correct, but a pre-existing container, image or on-disk signature was made by an older version. It closes by **relaunching from the app**, not by changing code |
| **SKIP** | Could not be evaluated on this host |

**`STALE` never affects the exit status**, and is always printed in its own
section. It is reported as STALE only *after* the corresponding code has been
introspected and found correct; if the code itself is wrong, the result is a FAIL
regardless of what the containers look like.

`VERIFY_SKIP_DOCKER=1` skips every Docker check for CI.
`VERIFY_IMAGE_REF=some:tag` points the probe at a build under test without
touching the live tag. `VERIFY_CHROMIUM_MAX_AGE_MONTHS=n` relaxes the Chromium
age gate.

Two properties the harness holds itself to, both of which it previously violated:

- **It writes nothing outside its `mktemp` scratch directory.** Every check that
  touches the filesystem redirects into that scratch dir, and check 13 hashes
  `profiles.json`, the proxy store, the profile store, every hardware signature,
  the host-share root and the generated `.desktop` files before and after the run
  to prove it. An earlier version created a junk profile in the real registry
  while probing.
- **Every check can pass on its own terms.** No check requires a container to
  already exist in order to evaluate the code. An earlier version reported
  "9 FAIL" on a host where two of the nine could not have passed under any code,
  because they asserted things the current code deliberately does *not* do.

Neither claim is taken on trust: both were mutation-tested — deliberately
breaking the guard and the write-redirect, confirming the harness notices, then
restoring it.

---

## License

Non-Commercial — All rights reserved by MuhammadUsamaMX. See [LICENSE](LICENSE) for full terms.
