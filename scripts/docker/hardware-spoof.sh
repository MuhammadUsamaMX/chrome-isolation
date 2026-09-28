#!/bin/bash
# hardware-spoof.sh — generates a stable per-profile machine fingerprint.
#
# Each profile gets its own "machine": a CPU model, a core count DERIVED from
# that model, screen resolution, RAM size, DMI/firmware identity, MAC address,
# kernel identity, timezone, browser language and GPU backend. Values are
# generated once and reused on every launch; fields missing from older
# signature files are backfilled in place so existing profiles keep their
# identity.
#
# The machine is described by a small table of *blueprints* — real-world CPU
# models paired with firmware that shipped alongside them. The core count is
# never rolled independently of the model, so the fake machine cannot end up
# claiming 14 cores while its CPU string says "8-Core" (finding F2).
#
# HONESTY NOTE — what is actually overridden inside the container
# (see scripts/docker/libhwspoof.c and scripts/docker/HOST-SPOOFING.md):
#   * nproc / sched_getaffinity  -> REAL, via taskset (see stealth-launch.sh)
#   * /proc/cpuinfo, /proc/meminfo, /proc/version, /sys/class/dmi/id/*, uname
#     -> OVERRIDDEN, but only through the optional LD_PRELOAD shim, and only
#        for consumers that go through libc. The container is unprivileged with
#        user-namespace creation blocked, so procfs/sysfs CANNOT be bind-mounted
#        over and the files CANNOT be written. A process issuing raw syscalls
#        (which is what Chromium's own base:: file helpers do) still sees the
#        real host, so this remains a partial mitigation. If the shim is
#        missing or not functional the entrypoint logs a warning and continues
#        with LD_PRELOAD unset — it never aborts and never blocks the launch.
#
# CHROME_CONFIG_DIR can be overridden for testing (defaults to the container
# profile dir). CHROME_SPOOF_DIR controls where the faked sysfs/procfs view is
# materialised (default /home/chrome/.runtime/hwspoof).
PROFILE_NAME="${1:-default}"
CONFIG_DIR="${CHROME_CONFIG_DIR:-/home/chrome/.config/chromium}"
SPOOF_DIR="${CHROME_SPOOF_DIR:-/home/chrome/.runtime/hwspoof}"
HARDWARE_FILE="$CONFIG_DIR/hardware-signature.json"
mkdir -p "$CONFIG_DIR"

# ---------------------------------------------------------------- defaults --
# Used only if the generator itself is broken; the profile must still launch.
CHROME_CPU_CORES=4
CHROME_RESOLUTION=1920x1080
CHROME_TIMEZONE=UTC
CHROME_LANGUAGE=en-US
CHROME_GPU_MODE=swiftshader

# ------------------------------------------------------- generate/backfill --
# Output: line 1 is the historical five-value contract (no spaces in any
# value). Following lines are `export`-style assignments carrying the richer
# machine identity. Failures degrade to the defaults above.
__hw_out="$(python3 - "$PROFILE_NAME" "$HARDWARE_FILE" "$SPOOF_DIR" <<'PYEOF' 2>/dev/null
import json, os, random, shlex, sys

profile, path, spoof_dir = sys.argv[1], sys.argv[2], sys.argv[3]

# ---------------------------------------------------------------- blueprints
# Each blueprint is a coherent, real-world machine: a CPU that actually existed,
# with its true physical core count and true SMT thread count, paired with the
# OEM/firmware a machine with that CPU actually shipped with, plus a plausible
# kernel. `cores` is the physical core count, `threads` the logical one.
#   threads == cores * 2  -> SMT
#   threads == cores      -> no SMT
BLUEPRINTS = [
    dict(model="Intel(R) Core(TM) i5-12400F", cores=6, threads=12,
         vendor_id="GenuineIntel", dmi=dict(
             sys_vendor="LENOVO", product_name="82M9",
             board_vendor="LENOVO", board_name="3291 M9",
             bios_vendor="LENOVO", chassis_type="3"),
         kernel="6.8.0-45-generic", mem=[16, 32]),
    dict(model="Intel(R) Core(TM) i5-10400", cores=6, threads=12,
         vendor_id="GenuineIntel", dmi=dict(
             sys_vendor="Dell Inc.", product_name="OptiPlex 3080",
             board_vendor="Dell Inc.", board_name="0KV2V8",
             bios_vendor="Dell Inc.", chassis_type="3"),
         kernel="5.15.0-119-generic", mem=[8, 16]),
    dict(model="Intel(R) Core(TM) i7-8550U", cores=4, threads=8,
         vendor_id="GenuineIntel", dmi=dict(
             sys_vendor="LENOVO", product_name="ThinkPad E490",
             board_vendor="LENOVO", board_name="20N8S29T00",
             bios_vendor="LENOVO", chassis_type="10"),
         kernel="5.15.0-119-generic", mem=[8, 16]),
    dict(model="Intel(R) Core(TM) i5-9600K", cores=6, threads=6,
         vendor_id="GenuineIntel", dmi=dict(
             sys_vendor="ASUSTeK COMPUTER INC.", product_name="TUF Z390-P GAMING II",
             board_vendor="ASUSTeK COMPUTER INC.", board_name="TUF Z390-P GAMING II",
             bios_vendor="American Megatrends Inc.", chassis_type="3"),
         kernel="6.8.0-45-generic", mem=[16, 32]),
    # i3-8100 is 4C/4T — this part has no Hyper-Threading. Encoding it as 6
    # threads produced an impossible 1.5x ratio, which is itself a fingerprint
    # tell (and violated the threads==cores|threads==cores*2 rule above).
    dict(model="Intel(R) Core(TM) i3-8100", cores=4, threads=4,
         vendor_id="GenuineIntel", dmi=dict(
             sys_vendor="GIGA-BYTE TECHNOLOGY CO., LTD.", product_name="H310M DS2V",
             board_vendor="GIGA-BYTE TECHNOLOGY CO., LTD.", board_name="H310M DS2V",
             bios_vendor="American Megatrends International, LLC.", chassis_type="3"),
         kernel="5.15.0-119-generic", mem=[8, 16]),
    dict(model="Intel(R) Core(TM) i5-7500", cores=4, threads=4,
         vendor_id="GenuineIntel", dmi=dict(
             sys_vendor="Hewlett-Packard", product_name="HP 260 G3 Desktop PC",
             board_vendor="Hewlett-Packard", board_name="8378 G3",
             bios_vendor="Hewlett-Packard", chassis_type="3"),
         kernel="4.15.0-192-generic", mem=[8, 16]),
    dict(model="Intel(R) Celeron(R) CPU N4020", cores=2, threads=2,
         vendor_id="GenuineIntel", dmi=dict(
             sys_vendor="ASUSTeK COMPUTER INC.", product_name="Chromebox CX5",
             board_vendor="ASUSTeK COMPUTER INC.", board_name="CX5",
             bios_vendor="American Megatrends Inc.", chassis_type="3"),
         kernel="6.8.0-45-generic", mem=[8]),
    dict(model="AMD Ryzen 7 9700X 8-Core Processor", cores=8, threads=16,
         vendor_id="AuthenticAMD", dmi=dict(
             sys_vendor="Micro-Star International Co., Ltd.", product_name="MAG B650 TOMAHAWK",
             board_vendor="Micro-Star International Co., Ltd.", board_name="MAG B650 TOMAHAWK",
             bios_vendor="American Megatrends International, LLC.", chassis_type="3"),
         kernel="6.8.0-45-generic", mem=[32]),
    dict(model="AMD Ryzen 5 5600", cores=6, threads=12,
         vendor_id="AuthenticAMD", dmi=dict(
             sys_vendor="ASUSTeK COMPUTER INC.", product_name="TUF GAMING B550M-PLUS",
             board_vendor="ASUSTeK COMPUTER INC.", board_name="TUF B550M-PLUS",
             bios_vendor="American Megatrends Inc.", chassis_type="3"),
         kernel="6.8.0-45-generic", mem=[16, 32]),
    dict(model="AMD Ryzen 5 3600 6-Core Processor", cores=6, threads=12,
         vendor_id="AuthenticAMD", dmi=dict(
             sys_vendor="GIGA-BYTE TECHNOLOGY CO., LTD.", product_name="B450M DS3H",
             board_vendor="GIGA-BYTE TECHNOLOGY CO., LTD.", board_name="B450M DS3H",
             bios_vendor="American Megatrends International, LLC.", chassis_type="3"),
         kernel="6.8.0-45-generic", mem=[8, 16, 32]),
    dict(model="AMD Ryzen 5 2600", cores=6, threads=12,
         vendor_id="AuthenticAMD", dmi=dict(
             sys_vendor="ASUSTeK COMPUTER INC.", product_name="PRIME B450M-A II",
             board_vendor="ASUSTeK COMPUTER INC.", board_name="B450M-A II",
             bios_vendor="American Megatrends Inc.", chassis_type="3"),
         kernel="5.15.0-119-generic", mem=[8, 16]),
    dict(model="AMD Ryzen 3 3200G with Radeon Vega Graphics", cores=4, threads=4,
         vendor_id="AuthenticAMD", dmi=dict(
             sys_vendor="Micro-Star International Co., Ltd.", product_name="B450M PRO-VDH WIFI",
             board_vendor="Micro-Star International Co., Ltd.", board_name="B450M PRO-VDH WIFI",
             bios_vendor="American Megatrends International, LLC.", chassis_type="3"),
         kernel="5.15.0-119-generic", mem=[8, 16]),
    dict(model="AMD Ryzen 5 2400G with Radeon Vega Graphics", cores=4, threads=8,
         vendor_id="AuthenticAMD", dmi=dict(
             sys_vendor="Micro-Star International Co., Ltd.", product_name="B450M-A PRO MAX",
             board_vendor="Micro-Star International Co., Ltd.", board_name="B450M-A PRO MAX",
             bios_vendor="American Megatrends International, LLC.", chassis_type="3"),
         kernel="5.15.0-119-generic", mem=[8, 16]),
]

RESOLUTIONS = ["1920x1080", "2560x1440", "1366x768", "1440x900"]
TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "Europe/London", "Europe/Berlin", "Europe/Paris", "Asia/Tokyo", "Asia/Karachi",
    "Asia/Dubai", "Asia/Kolkata", "Australia/Sydney", "America/Sao_Paulo", "Africa/Cairo",
]
LANGUAGES = [
    "en-US", "en-GB", "en-AU", "en-CA", "de-DE", "fr-FR", "es-ES", "pt-BR",
    "hi-IN", "ur-PK", "ar-SA", "tr-TR", "nl-NL", "it-IT",
]
# 2:1 weighting — most real machines have a GPU, some don't
GPU_MODES = ["vulkan", "vulkan", "swiftshader"]


def pick_mac():
    """Locally-administered, unicast MAC (bit 1 of the first octet set, bit 0
    clear) so it can never collide with a real vendor's OUI."""
    b = [random.getrandbits(8) for _ in range(6)]
    b[0] = (b[0] | 0x02) & 0xFE
    return ":".join("%02x" % x for x in b)


# ------------------------------------------------------------------- load
data = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        data = {}

data.setdefault("profile", profile)
hw = data.setdefault("hardware", {})
sys_ = data.setdefault("system", {})
br = data.setdefault("browser", {})

legacy_cores = hw.get("cpu_cores")

# ------------------------------------------------- blueprint / core coherence
# If a CPU model already exists we keep it and re-derive nothing. Otherwise we
# pick a blueprint, preferring one whose thread count equals a core count a
# pre-T4 signature already stored, so `cpu_cores` keeps its old meaning where
# that is still achievable. When the legacy value cannot be honoured (e.g. 14,
# 11 or 7 threads is not a real configuration) cpu_cores is re-derived from the
# model — coherence wins over preserving an incoherent number.
if "cpu_model" not in hw:
    matching = [b for b in BLUEPRINTS if isinstance(legacy_cores, int)
                and b["threads"] == legacy_cores]
    blueprint = random.choice(matching) if matching else random.choice(BLUEPRINTS)
    hw["cpu_model"] = blueprint["model"]
    hw["cpu_vendor_id"] = blueprint["vendor_id"]
    hw["cpu_physical_cores"] = blueprint["cores"]
    hw["cpu_cores"] = blueprint["threads"]
else:
    # A partially backfilled signature (model present, counts missing).
    known = [b for b in BLUEPRINTS if b["model"] == hw["cpu_model"]]
    if known:
        blueprint = known[0]
        hw.setdefault("cpu_vendor_id", blueprint["vendor_id"])
        hw["cpu_physical_cores"] = blueprint["cores"]
        hw["cpu_cores"] = blueprint["threads"]
    else:
        blueprint = None

if "cpu_vendor_id" not in hw:
    hw["cpu_vendor_id"] = ("AuthenticAMD" if hw["cpu_model"].startswith("AMD")
                           else "GenuineIntel")
# Repair a pre-T4 core count that was rolled independently of the model.
if "cpu_cores" not in hw:
    hw["cpu_cores"] = hw.get("cpu_physical_cores", 4) * 2
if "cpu_physical_cores" not in hw:
    # AMD encodes the core count in the string ("... 8-Core Processor").
    n = None
    if "-Core" in hw["cpu_model"]:
        tok = hw["cpu_model"].split("-Core")[0].split()[-1]
        if tok.isdigit():
            n = int(tok)
    hw["cpu_physical_cores"] = n or max(1, hw["cpu_cores"] // 2)
if hw["cpu_cores"] < hw["cpu_physical_cores"]:
    hw["cpu_cores"] = hw["cpu_physical_cores"]

# --------------------------------------------------------- remaining fields
if "mem_total_gb" not in hw:
    choices = blueprint["mem"] if blueprint else [8, 16, 32]
    hw["mem_total_gb"] = random.choice(sorted(set(choices)))
if "mac_address" not in hw:
    hw["mac_address"] = pick_mac()

dmi = hw.setdefault("dmi", {})
if blueprint:
    for k, v in blueprint["dmi"].items():
        dmi.setdefault(k, v)
else:
    dmi.setdefault("sys_vendor", "LENOVO")
    dmi.setdefault("product_name", "ThinkCentre")
    dmi.setdefault("board_vendor", dmi["sys_vendor"])

if "uname" not in sys_:
    sys_["uname"] = {
        "sysname": "Linux",
        "release": (blueprint["kernel"] if blueprint else "6.8.0-45-generic"),
        "machine": "x86_64",
    }
if "screen_resolution" not in hw:
    hw["screen_resolution"] = random.choice(RESOLUTIONS)
if "timezone" not in sys_:
    sys_["timezone"] = random.choice(TIMEZONES)
if "language" not in br:
    br["language"] = random.choice(LANGUAGES)
if "gpu_mode" not in br:
    br["gpu_mode"] = random.choice(GPU_MODES)

# Mask the legacy one-word field; everything authoritative is in the sub-tables.
data["schema_version"] = 2

# Never let a bad write destroy a profile's identity.
try:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
except OSError:
    pass

# ------------------------------------------- materialise the faked sys view
# These are consumed ONLY by the optional LD_PRELOAD shim. They are never
# mounted over /proc or /sys (impossible unprivileged) and nothing reads them
# unless the shim redirects a libc open() onto them. Best-effort by design.
mem_kb = hw["mem_total_gb"] * 1024 * 1024
try:
    os.makedirs(os.path.join(spoof_dir, "proc"), exist_ok=True)
    os.makedirs(os.path.join(spoof_dir, "sys/class/dmi/id"), exist_ok=True)

    flags = ("fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat "
             "pse36 clflush mmx fxsr sse sse2 ss ht syscall nx pdpe1gb rdtscp "
             "lm constant_tsc arch_perfmon rep_good nopl xtopology pni pclmulqdq "
             "monitor ssse3 cx16 sse4_1 sse4_2 x2apic movbe popcnt aes xsave avx "
             "f16c rdrand lahf_lm abm 3dnowprefetch invpcid ssbd ibrs ibpb stibp "
             "fsgsbase tsc_adjust bmi1 avx2 smep bmi2 erms invpcid mpx rdseed adx "
             "smap clflushopt xsaveopt xsavec xgetbv1 arat")
    is_amd = hw.get("cpu_vendor_id") == "AuthenticAMD"
    if is_amd:
        flags += (" osvw ibs xop skinit wdt lwp fma4 tce nodev_msr tbm topoext ")
    # CPU family/model must match the vendor, or the string is self-contradictory.
    family, model_no = ("25", "33") if is_amd else ("6", "151")

    def cpuinfo():
        out = []
        for i in range(hw["cpu_cores"]):
            if i:
                out.append("")
            out += [
                "processor\t: %d" % i,
                "vendor_id\t: %s" % hw["cpu_vendor_id"],
                "cpu family\t: %s" % family,
                "model\t\t: %s" % model_no,
                "model name\t: %s" % hw["cpu_model"],
                "stepping\t: 0",
                "microcode\t: 0x%x" % random.getrandbits(24),
                "cpu MHz\t\t: %.3f" % (2.5 + i * 0.05),
                "cache size\t: 1024 KB",
                "physical id\t: 0",
                "siblings\t: %d" % hw["cpu_cores"],
                "core id\t\t: %d" % i,
                "cpu cores\t: %d" % hw["cpu_physical_cores"],
                "flags\t\t: %s" % flags,
                "bugs\t\t:",
                "address sizes\t: 43 bits physical, 48 bits virtual",
                "power management:",
            ]
        return "\n".join(out) + "\n"

    def meminfo():
        # Built as an ordered list so the totals can never drift out of sync
        # with the declared MemTotal.
        total = mem_kb
        free = int(total * 0.45)
        swap = total // 4
        rows = [
            ("MemTotal", total), ("MemFree", free),
            ("MemAvailable", int(total * 0.55)),
            ("Buffers", int(total * 0.03)), ("Cached", int(total * 0.30)),
            ("SwapCached", 0),
            ("Active", int(total * 0.40)), ("Inactive", int(total * 0.55)),
            ("Active(anon)", int(total * 0.25)), ("Inactive(anon)", int(total * 0.30)),
            ("Active(file)", int(total * 0.15)), ("Inactive(file)", int(total * 0.25)),
            ("Unevictable", 0), ("Mlocked", int(total * 0.05)),
            ("SwapTotal", swap), ("SwapFree", int(swap * 0.98)),
            ("Dirty", 0), ("Writeback", 0),
            ("AnonPages", int(total * 0.28)), ("Mapped", int(total * 0.12)),
            ("Shmem", int(total * 0.02)),
            ("Slab", int(total * 0.07)), ("SReclaimable", int(total * 0.05)),
            ("SUnreclaim", int(total * 0.02)),
            ("KStack", 4096), ("PageTables", int(total * 0.005)),
            ("NFS_Unstable", 0), ("HardwareCorrupted", 0),
        ]
        # /proc/meminfo pads every value to a fixed 15-column field; keep that
        # so a parser that hard-codes the layout still works.
        out = "".join("%-15s%11d kB\n" % (k + ":", v) for k, v in rows)
        out += "HugePages_Total:%8d\nHugePages_Free:%9d\nHugepagesize:%10d kB\n" % (
            0, 0, 2048)
        return out

    def w(rel, text):
        p = os.path.join(spoof_dir, rel)
        with open(p, "w") as f:
            f.write(text)

    w("proc/cpuinfo", cpuinfo())
    w("proc/meminfo", meminfo())
    w("proc/version", "Linux version %s (buildd@lcy02-amd64-051) "
                      "(gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0) "
                      "#45-Ubuntu SMP Tue Aug 30 12:02:04 UTC 2024\n"
      % sys_["uname"]["release"])
    # Every blueprint field is written verbatim, so chassis_type stays a
    # SMBIOS number as the real sysfs attribute does.
    for k, v in dmi.items():
        w("sys/class/dmi/id/%s" % k, "%s\n" % v)
    w("sys/class/dmi/id/bios_version", "N2HET52W (1.60)\n")
    w("sys/class/dmi/id/chassis_asset_tag", "To be filled by O.E.M.\n")
    w("sys/class/dmi/id/chassis_serial", "\n")
except OSError:
    pass

# ------------------------------------------------------------------ output
print(hw["cpu_cores"], hw["screen_resolution"], sys_["timezone"],
      br["language"], br["gpu_mode"])
env = {
    "CHROME_CPU_MODEL": hw["cpu_model"],
    "CHROME_CPU_VENDOR_ID": hw.get("cpu_vendor_id", ""),
    "CHROME_CPU_PHYSICAL_CORES": hw["cpu_physical_cores"],
    "CHROME_MEM_TOTAL_GB": hw["mem_total_gb"],
    "CHROME_MEM_TOTAL_KB": mem_kb,
    "CHROME_MAC_ADDRESS": hw["mac_address"],
    "CHROME_DMI_SYS_VENDOR": dmi.get("sys_vendor", ""),
    "CHROME_DMI_PRODUCT_NAME": dmi.get("product_name", ""),
    "CHROME_DMI_BOARD_VENDOR": dmi.get("board_vendor", ""),
    "CHROME_KERNEL_RELEASE": sys_["uname"]["release"],
    "CHROME_SPOOF_ROOT": spoof_dir,
}
for k, v in env.items():
    print("export %s=%s" % (k, shlex.quote(str(v))))
PYEOF
)"

if [[ -z "$__hw_out" ]]; then
    echo "hardware-spoof: generator failed, using safe defaults" >&2
else
    # Line 1 keeps the historical five-value contract.
    read -r CHROME_CPU_CORES CHROME_RESOLUTION CHROME_TIMEZONE CHROME_LANGUAGE CHROME_GPU_MODE \
        <<< "$(printf '%s\n' "$__hw_out" | head -n1)"
    # Remaining lines carry the machine identity as shell assignments.
    while IFS= read -r __line; do
        [[ "$__line" == export\ * ]] || continue
        eval "$__line"
    done <<< "$(printf '%s\n' "$__hw_out" | tail -n +2)"
    unset __line __hw_out
fi

export CHROME_CPU_CORES CHROME_RESOLUTION CHROME_TIMEZONE CHROME_LANGUAGE CHROME_GPU_MODE
export CHROME_CPU_MODEL CHROME_CPU_VENDOR_ID CHROME_CPU_PHYSICAL_CORES \
       CHROME_MEM_TOTAL_GB CHROME_MEM_TOTAL_KB CHROME_MAC_ADDRESS \
       CHROME_DMI_SYS_VENDOR CHROME_DMI_PRODUCT_NAME CHROME_DMI_BOARD_VENDOR \
       CHROME_KERNEL_RELEASE CHROME_SPOOF_ROOT

# ------------------------------------------------------------- LD_PRELOAD ---
# The shim is OPTIONAL and is only effective if it was built into the image.
# It redirects libc open()/fopen() of /proc/cpuinfo, /proc/meminfo,
# /proc/version and /sys/class/dmi/id/* onto $CHROME_SPOOF_ROOT, and overrides
# sysinfo()/uname()/getifaddrs(). It cannot help a process that issues raw
# syscalls, and it is useless for anything Chromium reads via its own base::
# file helpers — see scripts/docker/HOST-SPOOFING.md. Never fatal.
HWSHIM_LIB="${CHROME_SPOOF_LIB:-/usr/lib/libhwspoof.so}"
if [[ -r "$HWSHIM_LIB" && -d "$CHROME_SPOOF_ROOT/proc" ]]; then
    # Two separate things are being verified here, and conflating them is what
    # previously made a broken shim look healthy:
    #
    #   1. Did the loader accept LD_PRELOAD at all? musl silently ignores an
    #      entry it cannot load and still exits 0, so the process succeeding
    #      proves nothing. The shim's constructor writes a probe file.
    #   2. Are the shim's hooks actually functional? A shim can load perfectly
    #      and still fail every dlsym(RTLD_NEXT, ...). The old "fail closed"
    #      fallback in that case made open() return -1 for EVERY path, so the
    #      dynamic loader could not map the browser and Chromium never started:
    #      `/bin/sh: can't open '/usr/bin/chromium-browser'` (exit 2). The shim
    #      now writes "unusable" instead of "loaded" in that situation, and we
    #      refuse to export LD_PRELOAD — the browser then runs uninstrumented,
    #      which is slow and leaky but always works.
    __probe="$CHROME_SPOOF_ROOT/.shim-probe.$$"
    rm -f "$__probe" 2>/dev/null
    if LD_PRELOAD="$HWSHIM_LIB" CHROME_SPOOF_PROBE="$__probe" \
       /bin/true >/dev/null 2>&1 && grep -qx 'loaded' "$__probe" 2>/dev/null; then
        export LD_PRELOAD="$HWSHIM_LIB"
    else
        echo "hardware-spoof: $HWSHIM_LIB is not usable; DMI/cpuinfo/meminfo NOT masked" \
             "(running without it so the browser still starts)" >&2
    fi
    rm -f "$__probe" 2>/dev/null
    unset __probe
else
    echo "hardware-spoof: no LD_PRELOAD shim ($HWSHIM_LIB);" \
         "/proc/cpuinfo, /proc/meminfo and /sys/class/dmi remain the host's" >&2
fi
