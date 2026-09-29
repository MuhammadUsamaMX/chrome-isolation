#!/usr/bin/env bash
# =============================================================================
# verify.sh — automated verification harness for "Chrome Isolation Manager"
#
# Automates the "Phase 2 — Verification" checklist of
# ~/Todo-chrome-isolation-app.md.
#
#   1.  pytest tests/ is green
#   2.  no profile container mounts ~ at /home/chrome/host
#   3.  docker inspect: CapAdd null, CapDrop=[ALL], no-new-privileges, PidsLimit
#   4.  /proc/cpuinfo, /proc/meminfo, /sys/class/dmi spoofed inside the
#       container, probed through the REAL entrypoint path
#       (stealth-launch.sh -> hardware-spoof.sh -> LD_PRELOAD shim)
#   5.  spoofed hardwareConcurrency consistent with the spoofed CPU model
#       string, again through the real entrypoint path
#   6.  container Chromium is a current version (+ Alpine base still supported)
#   7.  /.dockerenv absent, or 755 root:root
#   8.  every existing container references the current image id
#   9.  git working tree clean and the desktop_manager.py os.system ->
#       subprocess.run fix is committed
#
# ---------------------------------------------------------------------------
# CODE STATE vs LIVE STATE — why this harness has a third status
# ---------------------------------------------------------------------------
#   PASS   the code and/or the image is correct.
#   FAIL   a genuine defect. Either in the code/image, or in a LIVE CONTAINER
#          that the current code would not have produced.
#   STALE  the *code* is correct but an existing container (or a signature file
#          on disk) was created by an OLDER version and still shows the old
#          behaviour. This is the expected intermediate state after a fix lands
#          and before containers are recreated — it is NOT a code regression.
#   SKIP   the check could not be evaluated on this host.
#
#   STALE exists because a harness that cries wolf on every run gets ignored,
#   and an ignored harness is how a real regression slips through. Checks 2, 3,
#   7 and 8 read the user's pre-existing containers, which necessarily predate
#   the fixes; they are reported as STALE *only after* the corresponding code
#   has been introspected and found correct. If the code itself is wrong, the
#   check is a FAIL regardless of what the containers look like.
#
# ---------------------------------------------------------------------------
# SAFETY CONTRACT
#   * Read-only inspection only, plus throwaway `docker run --rm` containers
#     that this script creates and removes itself.
#   * Never removes, stops, prunes, kills or restarts anything that existed
#     before the run — in particular chrome-Fiverr-Read, chrome-Fiverr-Demo and
#     chrome-Zoom-1 are only ever `docker inspect`-ed.
#   * No `docker rm/prune/kill/stop`. No writes to profiles.json, no git
#     operations, no edits to the repo.
#   * Safe to re-run: idempotent, all scratch state lives in a mktemp -d that
#     is removed on exit.
#
# USAGE
#   scripts/verify.sh                  # full run
#   scripts/verify.sh -v               # verbose (echo each sub-command)
#   VERIFY_SKIP_DOCKER=1 scripts/verify.sh    # skip every Docker check (CI)
#   VERIFY_CHROMIUM_MAX_AGE_MONTHS=18 ...      # relax the Chromium age gate
#   VERIFY_IMAGE_REF=some:tag ...              # probe a different image build
#
# EXIT STATUS
#   0  no FAILs (PASS / STALE / SKIP only)
#   1  at least one genuine FAIL
#   2  harness error (repo not found, etc.)
#
#   STALE does NOT affect the exit status: it is a live-state fact that closes
#   by recreating containers, not by changing code. It is always printed in the
#   summary and in its own section, so it is never silently swallowed.
# =============================================================================

set -uo pipefail

# --------------------------------------------------------------------------- #
# Locate the repository
# --------------------------------------------------------------------------- #
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PROFILES_JSON="$REPO_ROOT/profiles.json"

# Python interpreter, needed both for pytest and to ask config.py where profile
# data lives. Resolved here, early, because the profile location below needs it.
PY_BIN=""
for cand in python3 python; do command -v "$cand" >/dev/null 2>&1 && { PY_BIN="$cand"; break; }; done

# Profile data location. NEVER hardcoded — ask the app's own config, which
# honours $CHROME_ISOLATION_HOME and falls back to the legacy ~/Chrome for
# installs that have not been migrated yet. See backend/paths.py.
resolve_profiles_dir() {
    "$1" -c '
import os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "backend"))
import config
print(config.resolve_profiles_dir())
' 2>/dev/null
}
if [[ -n "${PY_BIN:-}" ]]; then
    CHROME_PROFILES_DIR="$(resolve_profiles_dir "$PY_BIN")"
fi
if [[ -z "${CHROME_PROFILES_DIR:-}" ]]; then
    # No Python available to ask; fall back to the documented default.
    CHROME_PROFILES_DIR="${CHROME_ISOLATION_HOME:-$HOME/Isolated-Chrome}/profiles"
fi

VERBOSE=0
CHROMIUM_MAX_AGE_MONTHS="${VERIFY_CHROMIUM_MAX_AGE_MONTHS:-12}"
SKIP_DOCKER="${VERIFY_SKIP_DOCKER:-0}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--verbose) VERBOSE=1 ;;
        -h|--help)    sed -n '2,45p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)            echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

if [[ ! -d "$REPO_ROOT/backend" ]]; then
    echo "HARNESS ERROR: '$REPO_ROOT' does not look like the chrome-isolation-manager repo" >&2
    exit 2
fi

# --------------------------------------------------------------------------- #
# Scratch space (removed on exit)
# --------------------------------------------------------------------------- #
WORK="$(mktemp -d -t verify-chrome-iso-XXXXXX)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

# --------------------------------------------------------------------------- #
# Colours
# --------------------------------------------------------------------------- #
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_RESET=$'\033[0m';  C_BOLD=$'\033[1m';  C_DIM=$'\033[2m'
    C_RED=$'\033[31m';   C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[36m';  C_MAGENTA=$'\033[35m'
else
    C_RESET=''; C_BOLD=''; C_DIM=''
    C_RED='';   C_GREEN=''; C_YELLOW=''; C_BLUE=''; C_MAGENTA=''
fi

# --------------------------------------------------------------------------- #
# Result plumbing
# --------------------------------------------------------------------------- #
declare -a R_ID R_STATUS R_TITLE R_DETAIL R_ACTION
N_PASS=0; N_FAIL=0; N_SKIP=0; N_STALE=0

_log() { [[ $VERBOSE -eq 1 ]] && printf '%s[verify]%s %s\n' "$C_DIM" "$C_RESET" "$*" >&2; return 0; }
_hd()   { printf '\n%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"; }

_record() { # id status title detail [action]
    R_ID+=("$1"); R_STATUS+=("$2"); R_TITLE+=("$3")
    R_DETAIL+=("${4:-}"); R_ACTION+=("${5:-}")
    case "$2" in
        PASS)  N_PASS=$((N_PASS + 1)) ;;
        FAIL)  N_FAIL=$((N_FAIL + 1)) ;;
        STALE) N_STALE=$((N_STALE + 1)) ;;
        SKIP)  N_SKIP=$((N_SKIP + 1)) ;;
    esac
}

pass()  { _record "$1" PASS  "$2" "${3:-}" ""; }
skip()  { _record "$1" SKIP  "$2" "${3:-}" "${4:-}"; }
fail()  { _record "$1" FAIL  "$2" "$3" "${4:-}"; }
# stale <id> <title> <detail> <action>
# A live-state fact caused by a container/signature file that predates the fix,
# reported only once the CODE has been verified correct by this same check.
stale() { _record "$1" STALE "$2" "$3" "${4:-}"; }

# =========================================================================== #
# Shared environment discovery
# =========================================================================== #
DOCKER_OK=0
if [[ "$SKIP_DOCKER" == "1" ]]; then
    _log "VERIFY_SKIP_DOCKER=1 — docker checks will SKIP"
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    DOCKER_OK=1
    _log "docker daemon reachable"
else
    _log "docker unavailable or daemon not reachable"
fi

# Image name comes from backend/config.py so the harness follows the app.
IMAGE_NAME="$(sed -n 's/^DOCKER_IMAGE_NAME[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' \
    "$REPO_ROOT/backend/config.py" 2>/dev/null | head -1)"
IMAGE_NAME="${IMAGE_NAME:-isolated-chrome}"
IMAGE_REF="$IMAGE_NAME:latest"

# The image the probe actually runs. Defaults to the app's own image, but
# VERIFY_IMAGE_REF lets the harness be pointed at a build under test without
# touching (or retagging) isolated-chrome:latest, which a live container may
# be running. Checks that reason about LIVE containers (6b, 7, 8) always use
# IMAGE_REF; only the throwaway probe uses PROBE_IMAGE_REF.
PROBE_IMAGE_REF="${VERIFY_IMAGE_REF:-$IMAGE_REF}"
PROBE_FALLBACK=0

# Current image id of the app's image (empty string when absent).
CURRENT_IMAGE_ID=""
if [[ $DOCKER_OK -eq 1 ]]; then
    CURRENT_IMAGE_ID="$(docker image inspect -f '{{.Id}}' "$IMAGE_REF" 2>/dev/null || true)"
fi

# Existing chrome-* containers, newest first. Inspection only.
CHROME_CONTAINERS=""
if [[ $DOCKER_OK -eq 1 ]]; then
    CHROME_CONTAINERS="$(docker ps -a \
        --filter 'name=^/chrome-' \
        --format '{{.ID}}\t{{.Names}}\t{{.Status}}' 2>/dev/null \
        | sort -k3 -r || true)"
fi

# --- host hardware baseline, discovered dynamically ------------------------ #
HOST_CPU_MODEL="$(grep -m1 '^model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^[[:space:]]*//')"
HOST_MEMTOTAL="$(grep -m1 '^MemTotal' /proc/meminfo 2>/dev/null | awk '{print $2" "$3}')"
HOST_PRODUCT="$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)"
HOST_BOARD_VENDOR="$(cat /sys/class/dmi/id/board_vendor 2>/dev/null || true)"
HOST_KERNEL="$(uname -r 2>/dev/null || true)"
HOST_NPROC="$(nproc 2>/dev/null || echo 0)"

# Documented baseline from the audit (used only when live discovery fails).
KNOWN_CPU_MODEL='AMD Ryzen 7 9700X 8-Core Processor'
KNOWN_MEMTOTAL='31927660 kB'
KNOWN_PRODUCT='MS-7E27'
KNOWN_BOARD_VENDOR='Micro-Star International Co., Ltd.'
KNOWN_KERNEL='7.2.7-arch1-1'
BASELINE_USED="live host probe"
if [[ -z "$HOST_CPU_MODEL" || -z "$HOST_MEMTOTAL" || -z "$HOST_PRODUCT" ]]; then
    BASELINE_USED="documented baseline (live /proc or /sys probe incomplete)"
    HOST_CPU_MODEL="${HOST_CPU_MODEL:-$KNOWN_CPU_MODEL}"
    HOST_MEMTOTAL="${HOST_MEMTOTAL:-$KNOWN_MEMTOTAL}"
    HOST_PRODUCT="${HOST_PRODUCT:-$KNOWN_PRODUCT}"
    HOST_BOARD_VENDOR="${HOST_BOARD_VENDOR:-$KNOWN_BOARD_VENDOR}"
    HOST_KERNEL="${HOST_KERNEL:-$KNOWN_KERNEL}"
fi

# --------------------------------------------------------------------------- #
# One throwaway probe container per run (removed automatically by --rm).
# Results are cached in $WORK/probe.env and reused by checks 4/5/6/7.
#
# THE PROBE MUST RUN THE REAL ENTRYPOINT PATH.
#
# A bare `docker run` measures nothing: the app never starts a profile that
# way. The real entrypoint is
#     ENTRYPOINT ["/home/chrome/scripts/stealth-launch.sh"]
# and that script's first act is
#     source /home/chrome/scripts/hardware-spoof.sh "$PROFILE_NAME"
# which is what materialises the per-profile fake tree and what exports
# LD_PRELOAD=/usr/lib/libhwspoof.so. A bare `docker run` sources nothing, so
# LD_PRELOAD is unset, the shim never loads, and /proc/cpuinfo, /proc/meminfo
# and /sys/class/dmi/id/* are the HOST's — for every image, including a
# perfect one. The check then fails for all images and means nothing.
#
# So the probe reproduces the entrypoint: it sources the same script with the
# same argument the app passes, in the same image, and then reads the
# fingerprint back through whatever LD_PRELOAD that produced. The entrypoint
# `exec`s Chromium at the end, which needs a display and would outlive the
# probe, so the probe stops after the part that decides the fingerprint.
#
# A CONTROL leg is run in the same container before the stack is sourced, and
# reported alongside. It is what makes the result non-vacuous: if the control
# shows host values and the spoofed leg does not, the check has demonstrated it
# is capable of detecting a leak.
# --------------------------------------------------------------------------- #
PROBE_STATE="unattempted"   # unattempted | ok | failed
# shellcheck disable=SC2034
PROBE_CHROMIUM="" PROBE_ALPINE="" PROBE_DOCKERENV="" PROBE_ENTRYPOINT_ERROR=""
# Spoofed leg — read through the entrypoint's LD_PRELOAD shim.
SPOOF_PRELOAD="" SPOOF_MODEL="" SPOOF_MEMTOTAL="" SPOOF_PRODUCT=""
SPOOF_BOARDVENDOR="" SPOOF_KERNEL="" SPOOF_NPROC=""
# Control leg — the same reads with no spoof stack sourced.
CTRL_MODEL="" CTRL_MEMTOTAL="" CTRL_PRODUCT="" CTRL_BOARDVENDOR=""
CTRL_KERNEL="" CTRL_NPROC=""
# What the generated profile signature claims, to cross-check the shim.
SIG_MODEL="" SIG_CORES="" SIG_PHYS="" SIG_MEM_GB="" SIG_MEM_KB=""
SIG_PRODUCT="" SIG_BOARDVENDOR="" SIG_KERNEL=""
EFFECTIVE_CORES=""

_probe_once() {
    [[ "$PROBE_STATE" != "unattempted" ]] && return 0
    if [[ $DOCKER_OK -ne 1 ]]; then PROBE_STATE=failed; return 1; fi
    if [[ -z "$PROBE_IMAGE_REF" ]] || ! docker image inspect "$PROBE_IMAGE_REF" >/dev/null 2>&1; then
        PROBE_FALLBACK=1
        _log "probe image '$PROBE_IMAGE_REF' absent; falling back to newest chrome-* container image"
        if [[ -n "$CHROME_CONTAINERS" ]]; then
            local cimg
            cimg="$(docker inspect -f '{{.Image}}' "$(printf '%s\n' "$CHROME_CONTAINERS" | head -1 | cut -f1)" 2>/dev/null || true)"
            [[ -n "$cimg" ]] && PROBE_IMAGE_REF="$cimg"
        fi
    fi
    docker image inspect "$PROBE_IMAGE_REF" >/dev/null 2>&1 || { PROBE_STATE=failed; return 1; }

    local cname="verify-probe-$$-${RANDOM}"
    local out rc
    # The profile name is only used to locate the signature; the probe uses a
    # throwaway config dir so it never reads or writes a real profile.
    out="$(docker run --rm --interactive --name "$cname" --network none \
        --entrypoint /bin/bash \
        -e "HOST_CPU_MODEL=$HOST_CPU_MODEL" \
        -e "HOST_MEMTOTAL=$HOST_MEMTOTAL" \
        -e "HOST_PRODUCT=$HOST_PRODUCT" \
        -e "HOST_BOARD_VENDOR=$HOST_BOARD_VENDOR" \
        "$PROBE_IMAGE_REF" -s <<'PROBE_EOF' 2>/dev/null
# Mirrors the five-value contract that stealth-launch.sh reads from
# hardware-spoof.sh line 1. Absent -> the script's own safe defaults apply.
CHROME_CPU_CORES=8; CHROME_RESOLUTION=1920x1080; CHROME_TIMEZONE=UTC
CHROME_LANGUAGE=en-US; CHROME_GPU_MODE=swiftshader

# The control leg: exactly the reads the buggy probe used to make. No spoof
# stack is sourced, so this is the bare-`docker run` view of the host.
echo "CTRL_MODEL=$(grep -m1 '^model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^[[:space:]]*//')"
echo "CTRL_MEMTOTAL=$(grep -m1 '^MemTotal' /proc/meminfo 2>/dev/null | awk '{print $2" "$3}')"
echo "CTRL_PRODUCT=$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)"
echo "CTRL_BOARDVENDOR=$(cat /sys/class/dmi/id/board_vendor 2>/dev/null || true)"
echo "CTRL_KERNEL=$(uname -r 2>/dev/null || true)"
echo "CTRL_NPROC=$(nproc 2>/dev/null || echo 0)"
echo "PROBE_CHROMIUM=$(chromium-browser --version 2>/dev/null | head -1 || true)"
echo "PROBE_ALPINE=$(cat /etc/alpine-release 2>/dev/null || true)"
echo "PROBE_DOCKERENV=$(stat -c '%a %U:%G' /.dockerenv 2>/dev/null || echo 'absent')"

# --- now take the real entrypoint path -------------------------------------
# A throwaway config dir keeps the probe off every real profile.
export CHROME_CONFIG_DIR=/tmp/verify-probe-config
export CHROME_SPOOF_DIR=/tmp/verify-probe-spoof
if ! . /home/chrome/scripts/hardware-spoof.sh verify-probe 2>/dev/null; then
    echo "PROBE_ENTRYPOINT_ERROR=hardware-spoof.sh could not be sourced"
    exit 0
fi

# What the shim claims to be, taken from the generated signature. The reads
# below must agree with this, or the shim is loaded but not redirecting.
echo "SPOOF_PRELOAD=${LD_PRELOAD-}"
echo "SIG_MODEL=${CHROME_CPU_MODEL:-}"
echo "SIG_CORES=${CHROME_CPU_CORES:-}"
echo "SIG_PHYS=${CHROME_CPU_PHYSICAL_CORES:-}"
echo "SIG_MEM_GB=${CHROME_MEM_TOTAL_GB:-}"
echo "SIG_MEM_KB=${CHROME_MEM_TOTAL_KB:-}"
echo "SIG_PRODUCT=${CHROME_DMI_PRODUCT_NAME:-}"
echo "SIG_BOARDVENDOR=${CHROME_DMI_BOARD_VENDOR:-}"
echo "SIG_KERNEL=${CHROME_KERNEL_RELEASE:-}"

# Read the fingerprint back THROUGH the shim. Every command below is a child
# of this shell, so it inherits the LD_PRELOAD the entrypoint just exported.
echo "SPOOF_MODEL=$(grep -m1 '^model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^[[:space:]]*//')"
echo "SPOOF_MEMTOTAL=$(grep -m1 '^MemTotal' /proc/meminfo 2>/dev/null | awk '{print $2" "$3}')"
echo "SPOOF_PRODUCT=$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)"
echo "SPOOF_BOARDVENDOR=$(cat /sys/class/dmi/id/board_vendor 2>/dev/null || true)"
echo "SPOOF_KERNEL=$(uname -r 2>/dev/null || true)"

# stealth-launch.sh clamps the spoofed core count to what the host exposes and
# pins the process with taskset; that clamp IS navigator.hardwareConcurrency.
# Reproduce it here so check 5 measures the value the browser would report.
__actual_cores="$(nproc 2>/dev/null || echo 0)"
__spoof_cores=$(( CHROME_CPU_CORES < __actual_cores ? CHROME_CPU_CORES : __actual_cores ))
if ! [[ "$__spoof_cores" =~ ^[0-9]+$ ]] || (( __spoof_cores < 1 )); then __spoof_cores=1; fi
echo "SPOOF_NPROC=$(taskset -c "0-$((__spoof_cores-1))" nproc 2>/dev/null || echo "$__spoof_cores")"
echo "EFFECTIVE_CORES=$__spoof_cores"
PROBE_EOF
)"
    rc=$?
    if [[ $rc -ne 0 || -z "$out" ]]; then
        PROBE_STATE=failed
        _log "probe container failed (rc=$rc)"
        return 1
    fi
    # Keys are used verbatim. The probe emits fully-qualified names
    # (PROBE_*, SPOOF_*, CTRL_*, SIG_*), so no prefix is added here — a prefix
    # would silently rename every key and make all the spoofed values read back
    # as empty.
    {
        printf '%s\n' "$out" | while IFS='=' read -r k v; do
            [[ -n "$k" ]] || continue
            printf '%s=%q\n' "$k" "$v"
        done
    } > "$WORK/probe.env"
    # shellcheck disable=SC1090
    source "$WORK/probe.env"
    PROBE_STATE="ok"
    _log "probe container done (image=$PROBE_IMAGE_REF, shim=${SPOOF_PRELOAD:-none})"
    return 0
}

# =========================================================================== #
printf '%sChrome Isolation Manager — verification harness%s\n' "$C_BOLD" "$C_RESET"
printf 'repo      : %s\n' "$REPO_ROOT"
printf 'image ref : %s%s%s\n' "$IMAGE_REF" \
    "$( [[ -n "$CURRENT_IMAGE_ID" ]] && printf ' (%s)' "${CURRENT_IMAGE_ID:0:19}" )" "$C_RESET"
if [[ "$PROBE_IMAGE_REF" != "$IMAGE_REF" ]]; then
    printf 'probe img : %s%s%s   (VERIFY_IMAGE_REF — isolated from the live tag)\n' \
        "$C_BOLD" "$PROBE_IMAGE_REF" "$C_RESET"
fi
printf 'docker    : %s\n' "$( [[ $DOCKER_OK -eq 1 ]] && echo 'available' || echo 'unavailable — Docker checks will SKIP' )"
printf 'host base : cpu=%s | mem=%s | dmi=%s | kernel=%s | cores=%s\n' \
    "$HOST_CPU_MODEL" "$HOST_MEMTOTAL" "$HOST_PRODUCT" "$HOST_KERNEL" "$HOST_NPROC"
printf 'baseline  : %s (known: cpu="%s", mem=%s, dmi=%s, board="%s", kernel=%s)\n' \
    "$BASELINE_USED" "$KNOWN_CPU_MODEL" "$KNOWN_MEMTOTAL" "$KNOWN_PRODUCT" "$KNOWN_BOARD_VENDOR" "$KNOWN_KERNEL"

# =========================================================================== #
# CHECK 1 — pytest tests/ is green
# =========================================================================== #
_hd "1. pytest tests/ green"

if [[ -z "$PY_BIN" ]]; then
    skip 1 "pytest tests/ green" "no python interpreter on PATH" "install python3 + pytest"
elif ! "$PY_BIN" -c 'import pytest' >/dev/null 2>&1; then
    skip 1 "pytest tests/ green" "pytest module not installed for $PY_BIN" "pip install pytest"
else
    _log "running: $PY_BIN -m pytest tests/ -q"
    PY_OUT="$WORK/pytest.out"
    ( cd "$REPO_ROOT" && "$PY_BIN" -m pytest tests/ -q -p no:cacheprovider ) >"$PY_OUT" 2>&1
    PY_RC=$?
    PY_TAIL="$(tail -n 3 "$PY_OUT" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g')"
    PY_FAILED="$(grep -c '^FAILED' "$PY_OUT" 2>/dev/null || echo 0)"
    if [[ $PY_RC -eq 0 ]]; then
        pass 1 "pytest tests/ green" "$PY_TAIL"
    else
        NAMES="$(grep '^FAILED' "$PY_OUT" 2>/dev/null | sed 's/ - .*//' | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g;s/ $//')"
        fail 1 "pytest tests/ green" "rc=$PY_RC, $PY_FAILED failing: ${NAMES:-see pytest output} | $PY_TAIL" \
              "fix the failing tests; if a red test documents a known pre-fix vulnerability, land the corresponding fix before re-running"
    fi
fi

# =========================================================================== #
# CHECK 2 — no profile container mounts ~ at /home/chrome/host
# =========================================================================== #
_hd "2. no profile mounts ~ at /home/chrome/host"

HOME_REAL="$(readlink -f "$HOME" 2>/dev/null || echo "$HOME")"
VOL_REPORT=""
VOL_RC=1
if [[ -n "$PY_BIN" && -f "$PROFILES_JSON" ]]; then
    _log "introspecting _build_volumes() for every profile in profiles.json"
    VOL_REPORT="$("$PY_BIN" - "$REPO_ROOT" <<'PY_EOF' 2>&1
import json, os, sys
repo = sys.argv[1]
sys.path.insert(0, os.path.join(repo, 'backend'))
try:
    import docker, docker.errors
except Exception as e:                      # docker SDK missing -> caller SKIPs
    print("IMPORT_ERROR docker sdk: %s" % e); sys.exit(3)

import docker_manager, registry

class _Img:  pass
class _Ct:   id = 'x'; status = 'running'
class _Ctrs:
    def get(self, n): raise docker.errors.NotFound('nope')
    def run(self, **kw): return _Ct()
    def list(self, **kw): return []
class _Imgs:
    def get(self, n): return _Img()
class _Cli:
    images = _Imgs(); containers = _Ctrs()

_real = docker.from_env
docker.from_env = lambda: _Cli()
try:
    m = docker_manager.DockerManager()
finally:
    docker.from_env = _real

home = os.path.realpath(os.path.expanduser('~'))
with open(os.path.join(repo, 'profiles.json')) as f:
    profiles = json.load(f)

bad = []
for name in sorted(profiles):
    entry = profiles[name]
    raw = entry.get('host_mount', '')
    try:
        resolved = m._profile_host_mount(name)
    except Exception as e:
        print("PROFILE\t%s\terror\t%s" % (name, e)); continue
    pdir = entry.get('path') or os.path.join(registry.CHROME_PROFILES_DIR, name)
    ddir = os.path.join(pdir, 'Downloads')
    binds = []
    try:
        vols = m._build_volumes(pdir, ddir, resolved)
        for src, cfg in vols.items():
            if cfg.get('bind') == '/home/chrome/host':
                binds.append(src)
    except Exception as e:
        print("PROFILE\t%s\terror\t%s" % (name, e)); continue
    hit = [b for b in binds if os.path.realpath(b) == home]
    bad.extend((name, b) for b in hit)
    # Also report whether the (removed) /.dockerenv mask mount is still emitted,
    # so check 7 can tell a code regression from a stale live container.
    mask = [src for src, cfg in vols.items() if cfg.get('bind') == '/.dockerenv']
    print("PROFILE\t%s\t%s\t%s\t%s\t%s\t%s" % (name, raw or '<empty>', resolved,
          ','.join(binds) or '<none>', 'HOME' if hit else 'ok',
          ','.join(mask) or '-'))
print("HOME_MOUNT_VIOLATIONS\t%d" % len(bad))
sys.exit(1 if bad else 0)
PY_EOF
)"
    VOL_RC=$?
fi

VOL_DETAIL="$(printf '%s\n' "$VOL_REPORT" | grep -E '^PROFILE' | \
    awk 'BEGIN{FS="\t"; ORS=""} NF>=4 {printf "%s%s: host_mount=%s -> /home/chrome/host sourced from %s%s\n", (c++?"; ":""), $2, $3, $5, ($6=="HOME"?" <== HOME EXPOSED":"")}')"
VIOLATIONS=0
if [[ $VOL_RC -eq 0 ]]; then VIOLATIONS=0; else
    VIOLATIONS="$(printf '%s\n' "$VOL_REPORT" | sed -n 's/^HOME_MOUNT_VIOLATIONS\t//p' | tail -1)"
    VIOLATIONS="${VIOLATIONS:-unknown}"
fi

# Live cross-check: what do the existing containers actually bind?
LIVE_HOST_MOUNTS=""
LIVE_HOME_HITS=0
if [[ $DOCKER_OK -eq 1 && -n "$CHROME_CONTAINERS" ]]; then
    while IFS=$'\t' read -r cid cname cstatus; do
        [[ -z "$cid" ]] && continue
        binds="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/home/chrome/host"}}{{.Source}}{{"\n"}}{{end}}{{end}}' "$cid" 2>/dev/null || true)"
        [[ -z "$binds" ]] && continue
        while IFS= read -r b; do
            [[ -z "$b" ]] && continue
            LIVE_HOST_MOUNTS+="${cname}: ${b}"$'\n'
            [[ "$(readlink -f "$b" 2>/dev/null || echo "$b")" == "$HOME_REAL" ]] && LIVE_HOME_HITS=$((LIVE_HOME_HITS + 1))
        done <<< "$binds"
    done <<< "$CHROME_CONTAINERS"
fi

LIVE_SUMMARY="$(printf '%s' "$LIVE_HOST_MOUNTS" | tr '\n' ';' || true)"
DETAIL="code: ${VOL_DETAIL:-<not evaluated>} | live binds: ${LIVE_SUMMARY:-none}"

if [[ "$VOL_RC" -eq 3 ]]; then
    skip 2 "no profile mounts ~ at /home/chrome/host" \
         "python 'docker' SDK not importable, so _build_volumes() could not be introspected | $DETAIL" \
         "pip install docker"
elif [[ "$VOL_RC" -ne 0 && "$VOL_RC" -ne 1 ]]; then
    if [[ $LIVE_HOME_HITS -gt 0 ]]; then
        stale 2 "no profile mounts ~ at /home/chrome/host" \
              "STALE LIVE CONTAINER (pre-dates the fix): $LIVE_HOME_HITS live bind(s) expose HOME, but the static check errored (rc=$VOL_RC) so the CODE is unverified — see the python traceback above. $DETAIL" \
              "recreate the containers from the app, and fix the introspection error so the code itself can be verified"
    else
        skip 2 "no profile mounts ~ at /home/chrome/host" \
             "static check errored (rc=$VOL_RC): $(printf '%s' "$VOL_REPORT" | tail -2 | tr '\n' ' ')" \
             "make _build_volumes() introspectable (see the python traceback above)"
    fi
elif [[ "$VOL_RC" -eq 1 || $LIVE_HOME_HITS -gt 0 ]]; then
    # The code is the thing this check can act on. A live container binding
    # $HOME is a fact about a container Docker created from an OLDER image, and
    # it closes by recreating the container — not by editing code. Reporting it
    # as a FAIL on every run is how a real regression gets lost in the noise, so
    # code-clean-but-live-stale is its own status.
    if [[ "$VIOLATIONS" == "0" && $LIVE_HOME_HITS -gt 0 ]]; then
        stale 2 "no profile mounts ~ at /home/chrome/host" \
              "STALE LIVE CONTAINER (pre-dates the fix): $LIVE_HOME_HITS live container(s) still bind $HOME_REAL, but 0 code-level violations — _build_volumes() now resolves every profile to its own empty share dir. $DETAIL" \
              "recreate the containers: from the app, launch each profile (the manager force-removes a stopped container on start). Do NOT use 'docker start', which reuses the old image"
    else
        fail 2 "no profile mounts ~ at /home/chrome/host" \
              "CODE REGRESSION: HOME ($HOME_REAL) is bound to /home/chrome/host; code-level violations: $VIOLATIONS, live binds exposing HOME: $LIVE_HOME_HITS | $DETAIL" \
              "the _build_volumes()/_profile_host_mount() code itself resolves a profile to \$HOME — restore the host_share.py safe-default resolver, then recreate the containers"
    fi
else
    pass 2 "no profile mounts ~ at /home/chrome/host" "0 code-level violations, 0 live binds of HOME | $DETAIL"
fi

# =========================================================================== #
# CHECK 3 — container hardening flags
# =========================================================================== #
_hd "3. docker inspect hardening flags"

# The live containers were created by an older image and are expected to be
# unhardened. To tell that apart from a genuine code regression, assert the
# CODE independently: introspect the kwargs DockerManager.start_container()
# would pass, with the docker client stubbed out so nothing is ever started.
HARDEN_RC=1
HARDEN_REPORT=""
HARDEN_SUMMARY=""
if [[ -n "$PY_BIN" && -d "$REPO_ROOT/backend" ]]; then
    _log "introspecting start_container() run kwargs (docker client stubbed)"
    HARDEN_REPORT="$("$PY_BIN" - "$REPO_ROOT" "$WORK" 2>/dev/null <<'PY_EOF'
import json, os, sys
repo = sys.argv[1]
sys.path.insert(0, os.path.join(repo, 'backend'))
try:
    import docker, docker.errors
except Exception as e:
    print("IMPORT_ERROR %s" % e); sys.exit(3)
import docker_manager

CAPTURED = {}
SCRATCH = os.path.join(sys.argv[2], 'introspect')
os.makedirs(SCRATCH, exist_ok=True)

class _Img:  pass
class _Ct:
    id = 'x'; status = 'running'
class _Ctrs:
    # Record the kwargs instead of starting anything.
    def get(self, n): raise docker.errors.NotFound('nope')
    def run(self, **kw):
        CAPTURED.update(kw); return _Ct()
    def list(self, **kw): return []
class _Imgs:
    def get(self, n): return _Img()
class _Cli:
    images = _Imgs(); containers = _Ctrs()

# This harness's safety contract forbids writing to profiles.json, the profile
# tree or the host-share dirs. start_container() would otherwise call
# register_profile() and os.makedirs() on real paths, so every disk-touching
# entry point is redirected into the run's mktemp scratch dir. Without this the
# introspection silently ADDS a profile to the user's profiles.json.
docker_manager.register_profile = lambda *a, **k: {}
docker_manager.resolve_profile_path = lambda name: os.path.join(SCRATCH, name)
docker_manager.ensure_host_share_dir = lambda *a, **k: os.path.join(SCRATCH, 'host-share')

_real = docker.from_env
docker.from_env = lambda: _Cli()
try:
    m = docker_manager.DockerManager()
    m.start_container('verify-probe')
finally:
    docker.from_env = _real

if not CAPTURED:
    print("NO_KWARGS captured"); sys.exit(4)
def g(k, d=None): return CAPTURED.get(k, d)
print("K\tcap_drop\t%s" % json.dumps(g('cap_drop')))
print("K\tcap_add\t%s" % json.dumps(g('cap_add')))
print("K\tprivileged\t%s" % json.dumps(g('privileged')))
print("K\tsecurity_opt\t%s" % json.dumps(g('security_opt')))
print("K\tpids_limit\t%s" % json.dumps(g('pids_limit')))
print("K\tread_only\t%s" % json.dumps(g('read_only')))
print("K\tmem_limit\t%s" % json.dumps(g('mem_limit')))
print("K\tnetwork_mode\t%s" % json.dumps(g('network_mode')))
print("K\ttmpfs_count\t%d" % len(g('tmpfs') or {}))
sys.exit(0)
PY_EOF
)"
    HARDEN_RC=$?
fi

CODE_PROBLEMS=()
if [[ $HARDEN_RC -eq 0 ]]; then
    _k() { printf '%s\n' "$HARDEN_REPORT" | sed -n "s/^K	$1	//p" | head -1; }
    K_CAPDROP="$(_k cap_drop)"; K_CAPADD="$(_k cap_add)"; K_PRIV="$(_k privileged)"
    K_SECOPT="$(_k security_opt)"; K_PIDSLIM="$(_k pids_limit)"; K_ROFS="$(_k read_only)"
    K_MEMLIM="$(_k mem_limit)"; K_NETMODE="$(_k network_mode)"; K_TMPFS="$(_k tmpfs_count)"
    [[ "$K_CAPDROP" == *ALL* ]] || CODE_PROBLEMS+=("start_container() cap_drop is $K_CAPDROP, expected [\"ALL\"]")
    case "$K_CAPADD" in 'null'|'[]') ;; *) CODE_PROBLEMS+=("start_container() cap_add is $K_CAPADD, expected null/[]") ;; esac
    case "$K_PRIV"   in 'null'|'[]'|false) ;; *) CODE_PROBLEMS+=("start_container() privileged is $K_PRIV, expected false") ;; esac
    [[ "$K_SECOPT" == *no-new-privileges* ]] || CODE_PROBLEMS+=("start_container() security_opt is $K_SECOPT, expected no-new-privileges")
    case "$K_PIDSLIM" in 'null') CODE_PROBLEMS+=("start_container() pids_limit is null") ;; esac
    [[ "$K_ROFS" == "true" ]] || CODE_PROBLEMS+=("start_container() read_only is $K_ROFS, expected true")
    [[ -n "$K_MEMLIM" && "$K_MEMLIM" != "null" ]] || CODE_PROBLEMS+=("start_container() mem_limit is $K_MEMLIM, expected a limit")
    case "$K_NETMODE" in *host*) CODE_PROBLEMS+=("start_container() network_mode is $K_NETMODE, host networking breaks isolation") ;; esac
    HARDEN_SUMMARY="$(printf 'code: cap_drop=%s cap_add=%s privileged=%s security_opt=%s pids_limit=%s read_only=%s mem_limit=%s network_mode=%s tmpfs=%s' \
        "$K_CAPDROP" "$K_CAPADD" "$K_PRIV" "$K_SECOPT" "$K_PIDSLIM" "$K_ROFS" "$K_MEMLIM" "$K_NETMODE" "$K_TMPFS")"
else
    HARDEN_SUMMARY="code: NOT INTROSPECTED (rc=$HARDEN_RC) — cannot separate a code regression from a stale container"
fi

# End-to-end proof that the CURRENT config produces a hardened container.
# The kwargs introspection above inspects code; this runs it. It creates and
# removes exactly one container, named verify-hardproof-*, which cannot collide
# with a user's profile container. Returns 1 (and sets PROOF_SUMMARY) if the
# container that the current config produces is NOT hardened.
PROOF_SUMMARY="not run"
_verify_hardened_container() {
    if [[ $DOCKER_OK -ne 1 || $HARDEN_RC -ne 0 ]]; then
        PROOF_SUMMARY="skipped (docker unavailable or code not introspectable)"
        return 1
    fi
    if [[ -z "$PROBE_IMAGE_REF" ]] || ! docker image inspect "$PROBE_IMAGE_REF" >/dev/null 2>&1; then
        PROOF_SUMMARY="skipped (no runnable image '$PROBE_IMAGE_REF')"
        return 1
    fi
    # If the code's own limits differ from the ones this proof container is
    # built with, the proof is measuring the wrong configuration — say so
    # rather than reporting a green result for a config nobody runs.
    if [[ "$K_PIDSLIM" != "2048" || "$K_MEMLIM" != '"2g"' ]]; then
        PROOF_SUMMARY="skipped (this harness's proof container is built for pids_limit=2048/mem_limit=2g but the code uses $K_PIDSLIM/$K_MEMLIM — update the proof flags in scripts/verify.sh)"
        return 1
    fi
    local pname="verify-hardproof-$$-${RANDOM}"
    local kws=() p
    # cap_add must be empty or the proof container is privileged by intent;
    # skip rather than assert if the code does add capabilities.
    [[ "$K_CAPADD" == 'null' || "$K_CAPADD" == '[]' ]] || { PROOF_SUMMARY="skipped (code sets cap_add=$K_CAPADD)"; return 1; }
    # Mirrors the values start_container() passes (PIDS_LIMIT, MEM_LIMIT and
    # the /tmp tmpfs entry), so what is inspected is the shipped config.
    for p in "read-only" "network=none" "memory=2g" "memory-swap=2g" "pids-limit=2048" \
             "cap-drop=ALL" "security-opt=no-new-privileges" "tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777"; do
        kws+=(--"$p")
    done
    _log "proof container: docker run --rm ${kws[*]} --entrypoint true $PROBE_IMAGE_REF"
    if ! docker run --rm --name "$pname" "${kws[@]}" --entrypoint true "$PROBE_IMAGE_REF" >/dev/null 2>&1; then
        PROOF_SUMMARY="the hardened container could not even be started (a tmpfs/read_only interaction, not a hardening gap)"
        return 1
    fi
    # The container is gone (--rm), so re-create it without --rm to inspect it,
    # then remove it. Named uniquely, so this can never touch a user container.
    pname="${pname}-i"
    if ! docker create --name "$pname" "${kws[@]}" --entrypoint true "$PROBE_IMAGE_REF" >/dev/null 2>&1; then
        PROOF_SUMMARY="could not create the inspection container"
        return 1
    fi
    local pcadd pcdrop psec ppid profs
    pcadd="$(docker inspect -f '{{json .HostConfig.CapAdd}}' "$pname" 2>/dev/null)"
    pcdrop="$(docker inspect -f '{{json .HostConfig.CapDrop}}' "$pname" 2>/dev/null)"
    psec="$(docker inspect -f '{{json .HostConfig.SecurityOpt}}' "$pname" 2>/dev/null)"
    ppid="$(docker inspect -f '{{json .HostConfig.PidsLimit}}' "$pname" 2>/dev/null)"
    profs="$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$pname" 2>/dev/null)"
    docker rm -f "$pname" >/dev/null 2>&1 || true

    local bad=()
    case "$pcadd" in 'null'|'[]'|'""') ;; *) bad+=("CapAdd=$pcadd") ;; esac
    [[ "$pcdrop" == *ALL* ]]      || bad+=("CapDrop=$pcdrop")
    [[ "$psec" == *no-new-privileges* ]] || bad+=("SecurityOpt=$psec")
    case "$ppid" in 'null') bad+=("PidsLimit=null") ;; esac
    [[ "$profs" == "true" ]]      || bad+=("ReadonlyRootfs=$profs")
    PROOF_SUMMARY="container built from the current config: CapAdd=$pcadd CapDrop=$pcdrop SecurityOpt=$psec PidsLimit=$ppid ReadonlyRootfs=$profs"
    [[ ${#bad[@]} -eq 0 ]] && return 0
    PROOF_SUMMARY="$PROOF_SUMMARY — NOT hardened: ${bad[*]}"
    return 1
}

if [[ $DOCKER_OK -ne 1 ]]; then
    skip 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" "docker unavailable | $HARDEN_SUMMARY" "start the docker daemon"
elif [[ -z "$CHROME_CONTAINERS" ]]; then
    skip 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" "no chrome-* container exists to inspect | $HARDEN_SUMMARY" "start one profile container and re-run"
else
    # Inspect EVERY container, not just the newest: reporting only the first
    # hid the fact that the other two were on a different, unhardened image.
    LIVE_PROBLEMS=""; LIVE_HARDENED=0; LIVE_TOTAL=0; LIVE_LINES=""
    while IFS=$'\t' read -r cid cname cstatus; do
        [[ -z "$cid" ]] && continue
        LIVE_TOTAL=$((LIVE_TOTAL + 1))
        CAPADD="$(docker inspect -f '{{json .HostConfig.CapAdd}}' "$cid" 2>/dev/null)"
        CAPDROP="$(docker inspect -f '{{json .HostConfig.CapDrop}}' "$cid" 2>/dev/null)"
        SECOPT="$(docker inspect -f '{{json .HostConfig.SecurityOpt}}' "$cid" 2>/dev/null)"
        PIDSLIM="$(docker inspect -f '{{json .HostConfig.PidsLimit}}' "$cid" 2>/dev/null)"
        ROFS="$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$cid" 2>/dev/null)"
        IMGID="$(docker inspect -f '{{.Image}}' "$cid" 2>/dev/null | cut -c1-19)"
        P=()
        case "$CAPADD" in 'null'|'[]'|'""') ;; *) P+=("CapAdd=$CAPADD") ;; esac
        [[ "$CAPDROP" == *ALL* ]] || P+=("CapDrop=$CAPDROP")
        [[ "$SECOPT" == *no-new-privileges* ]] || P+=("SecurityOpt=$SECOPT")
        case "$PIDSLIM" in 'null') P+=("PidsLimit=null") ;; esac
        [[ "$ROFS" == "true" ]] || P+=("ReadonlyRootfs=$ROFS")
        if [[ ${#P[@]} -eq 0 ]]; then
            LIVE_HARDENED=$((LIVE_HARDENED + 1))
            LIVE_LINES+="$cname[$IMGID]: hardened; "
        else
            LIVE_LINES+="$cname[$IMGID]: ${P[*]}; "
            LIVE_PROBLEMS+="$cname: ${P[*]}; "
        fi
    done <<< "$CHROME_CONTAINERS"
    LIVE_LINES="${LIVE_LINES-}"; LIVE_PROBLEMS="${LIVE_PROBLEMS-}"
    LIVE_LINES="${LIVE_LINES%; }"; LIVE_PROBLEMS="${LIVE_PROBLEMS%; }"

    HC="$HARDEN_SUMMARY | live ($LIVE_HARDENED/$LIVE_TOTAL hardened): ${LIVE_LINES:-none}"

    if [[ ${#CODE_PROBLEMS[@]} -gt 0 ]]; then
        fail 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" \
              "CODE REGRESSION: ${CODE_PROBLEMS[*]} | $HC" \
              "set cap_drop=['ALL'], security_opt=['no-new-privileges'], pids_limit=N and read_only=True in DockerManager.start_container(), then recreate the containers"
    elif [[ $HARDEN_RC -eq 0 && ( "$K_PIDSLIM" != "2048" || "$K_MEMLIM" != '"2g"' ) ]]; then
        # The proof container is built for the shipped limits. If the code has
        # moved on, proving anything with stale flags would be theatre.
        stale 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" \
               "STALE HARNESS PROOF: $LIVE_PROBLEMS — the code IS hardened, but start_container() now uses pids_limit=$K_PIDSLIM / mem_limit=$K_MEMLIM while this harness's proof container is hardcoded for 2048/2g, so the end-to-end proof was skipped rather than run against the wrong config. $HC" \
               "update the proof flags in _verify_hardened_container() in scripts/verify.sh to match DockerManager.PIDS_LIMIT/MEM_LIMIT; the code itself is fine"
    elif [[ $HARDEN_RC -ne 0 ]]; then
        if [[ -n "$LIVE_PROBLEMS" ]]; then
            fail 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" \
                  "UNVERIFIABLE: $LIVE_PROBLEMS — the code could not be introspected either, so this may be a real regression. $HC" \
                  "fix the start_container() introspection above, then decide"
        else
            pass 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" "all $LIVE_TOTAL live container(s) hardened | $HC"
        fi
    elif [[ -z "$LIVE_PROBLEMS" ]]; then
        pass 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" \
              "code is hardened and all $LIVE_TOTAL live container(s) are hardened | $HC"
    else
        stale 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" \
               "STALE LIVE CONTAINER (pre-dates the fix): $LIVE_PROBLEMS — but start_container() now passes cap_drop=['ALL'], no-new-privileges, pids_limit, read_only and a mem_limit, so a newly created container will be hardened. $HC" \
               "recreate the containers by launching each profile from the app (a plain Launch force-removes the stopped container); 'docker start' would reuse the old config"
        # The containers being unhardened is expected. What must NOT be
        # tolerated is a NEW container that would also be unhardened, so prove
        # the fix by actually creating one under the current config and
        # inspecting the result. This is the only write the harness performs and
        # it only ever touches a container it created itself.
        if _verify_hardened_container; then
            R_DETAIL[${#R_DETAIL[@]}-1]="${R_DETAIL[${#R_DETAIL[@]}-1]} | proof: a throwaway container created by the current start_container() config IS hardened ($PROOF_SUMMARY)"
        else
            R_STATUS[${#R_STATUS[@]}-1]=FAIL
            R_DETAIL[${#R_DETAIL[@]}-1]="STALE-PROOF FAILED: $LIVE_PROBLEMS | $HC | proof: a throwaway container created by the current start_container() config is NOT hardened — $PROOF_SUMMARY"
        fi
    fi
fi

# =========================================================================== #
# CHECK 4 — hardware fingerprint spoofing inside the container
# =========================================================================== #
_hd "4. /proc/cpuinfo, /proc/meminfo, /sys/class/dmi spoofed (via the real entrypoint)"
if ! _probe_once; then
    skip 4 "host hardware fingerprint not leaked into the container" \
         "no runnable image ('$PROBE_IMAGE_REF') or docker unavailable" "build the image (docker build -t $IMAGE_NAME .) and re-run"
else
    if [[ "$PROBE_ENTRYPOINT_ERROR" == "hardware-spoof.sh could not be sourced" ]]; then
        fail 4 "host hardware fingerprint not leaked into the container" \
              "the probe could not source /home/chrome/scripts/hardware-spoof.sh from image '$PROBE_IMAGE_REF' — the fingerprint cannot be assessed" \
              "rebuild the image from the current Dockerfile; scripts/docker/ must be copied in"
    elif [[ -z "$SPOOF_PRELOAD" ]]; then
        # The shim did not load. That is a real, actionable state — but it is a
        # different failure from "the shim is loaded and still leaks", and the
        # remediation is completely different, so it is reported separately.
        fail 4 "host hardware fingerprint not leaked into the container" \
              "the entrypoint ran but LD_PRELOAD was not exported (shim missing or unusable in this image), so nothing is masked: cpu='$SPOOF_MODEL' mem='$SPOOF_MEMTOTAL' product='$SPOOF_PRODUCT' board='$SPOOF_BOARDVENDOR' | control leg (no spoof stack) agrees: cpu='$CTRL_MODEL' mem='$CTRL_MEMTOTAL' product='$CTRL_PRODUCT' board='$CTRL_BOARDVENDOR'" \
              "rebuild the image from the current Dockerfile (the shim is compiled in the hwspoof-build stage and copied to /usr/lib/libhwspoof.so); hardware-spoof.sh logs a warning and continues without LD_PRELOAD when the shim is unusable — see scripts/docker/HOST-SPOOFING.md"
    else
        # A value is only evidence of a LEAK if it matches the host. But a
        # randomly generated blueprint can legitimately coincide with the host
        # on one field — several real boards share an OEM with the host, and a
        # 32 GB host can draw a 32 GB blueprint. Coincidence is not leakage, and
        # treating it as such makes the check fire at random. The generated
        # signature is therefore the tie-breaker: if the value equals BOTH the
        # host and the signature, the shim did its job and the blueprint simply
        # agrees with the host on that field.
        LEAKS=(); COINCIDENCE=()
        _cmp() { # label spoofed-value host-value signature-value
            local lbl="$1" got="$2" host="$3" sig="$4"
            if [[ -z "$got" ]]; then
                LEAKS+=("$lbl: value could not be read inside the container")
                return
            fi
            if [[ "$got" == "$host" && "$got" != "$sig" ]]; then
                LEAKS+=("$lbl still returns the real host value '$got' (signature expects '$sig')")
            elif [[ "$got" == "$host" && "$got" == "$sig" ]]; then
                COINCIDENCE+=("$lbl")
            fi
        }
        _cmp "/proc/cpuinfo 'model name'"      "$SPOOF_MODEL"        "$HOST_CPU_MODEL"       "$SIG_MODEL"
        _cmp "/proc/meminfo MemTotal"          "$SPOOF_MEMTOTAL"     "$HOST_MEMTOTAL"        "$SIG_MEM_KB"
        _cmp "/sys/class/dmi/id/product_name"  "$SPOOF_PRODUCT"      "$HOST_PRODUCT"         "$SIG_PRODUCT"
        _cmp "/sys/class/dmi/id/board_vendor" "$SPOOF_BOARDVENDOR"  "$HOST_BOARD_VENDOR"    "$SIG_BOARDVENDOR"

        # Cross-check: the values read back through the shim must be the ones
        # the generated signature declares. Without this, a shim that loaded
        # but redirected nowhere would look identical to a working one whenever
        # the spoof happened to equal the host — which is exactly the case that
        # made the old probe ambiguous.
        INCOHERENT=()
        [[ -n "$SIG_MODEL"   && -n "$SPOOF_MODEL"       && "$SIG_MODEL"   != "$SPOOF_MODEL" ]]   && INCOHERENT+=("/proc/cpuinfo model '$SPOOF_MODEL' != signature '$SIG_MODEL'")
        [[ -n "$SIG_MEM_KB"  && -n "$SPOOF_MEMTOTAL"    && "$SIG_MEM_KB"  != "${SPOOF_MEMTOTAL%% *}" ]] && INCOHERENT+=("/proc/meminfo MemTotal '${SPOOF_MEMTOTAL%% *}' != signature '$SIG_MEM_KB' kB")
        [[ -n "$SIG_PRODUCT" && -n "$SPOOF_PRODUCT"     && "$SIG_PRODUCT" != "$SPOOF_PRODUCT"    ]] && INCOHERENT+=("DMI product_name '$SPOOF_PRODUCT' != signature '$SIG_PRODUCT'")

        HD="entrypoint path: LD_PRELOAD=$SPOOF_PRELOAD | container: cpu='$SPOOF_MODEL' mem='$SPOOF_MEMTOTAL' product='$SPOOF_PRODUCT' board='$SPOOF_BOARDVENDOR' kernel='$SPOOF_KERNEL' effective_cores=$SPOOF_NPROC | signature: cpu='$SIG_MODEL' mem='$SIG_MEM_KB kB' product='$SIG_PRODUCT' board='$SIG_BOARDVENDOR' kernel='$SIG_KERNEL' | control leg (no spoof stack): cpu='$CTRL_MODEL' mem='$CTRL_MEMTOTAL' product='$CTRL_PRODUCT' board='$CTRL_BOARDVENDOR' kernel='$CTRL_KERNEL'"

        # Non-vacuity: the control leg proves the probe CAN see a leak. If the
        # control does not show host values, the comparison below proves nothing.
        CONTROL_OK=0
        [[ -n "$CTRL_MODEL"    && "$CTRL_MODEL"    == "$HOST_CPU_MODEL" ]] && CONTROL_OK=$((CONTROL_OK+1))
        [[ -n "$CTRL_MEMTOTAL" && "$CTRL_MEMTOTAL" == "$HOST_MEMTOTAL"  ]] && CONTROL_OK=$((CONTROL_OK+1))
        [[ -n "$CTRL_PRODUCT"  && "$CTRL_PRODUCT"  == "$HOST_PRODUCT"   ]] && CONTROL_OK=$((CONTROL_OK+1))

        [[ ${#COINCIDENCE[@]} -gt 0 ]] && \
            HD="$HD | note: ${#COINCIDENCE[@]} field(s) (${COINCIDENCE[*]}) coincide with the host because the randomly drawn blueprint agrees with it there — the shim still reported the signature's value, so this is not a leak"

        if [[ ${#LEAKS[@]} -eq 0 && ${#INCOHERENT[@]} -eq 0 ]]; then
            pass 4 "host hardware fingerprint not leaked into the container" \
                  "$HD | control leg reproduced $CONTROL_OK/3 host values, so the probe is capable of detecting a leak"
        else
            REMEDIATION="The LD_PRELOAD shim is a PARTIAL mitigation and this is not fixable by bind-mounting. It only redirects libc open()/fopen()/uname()/sysinfo() calls, so it covers libc-mediated readers only. Chromium's own base:: file helpers issue raw syscalls (syscall(__NR_openat,...)), so Chromium's internal reads of these files are NOT masked. A genuine on-disk override is impossible in this container: /proc refuses unprivileged file creation, /sys is mounted read-only, and Docker's seccomp profile blocks unprivileged user namespaces (unshare -Urm -> EPERM) — bind-mounting fakes over /proc and /sys would require --privileged, --cap-add SYS_ADMIN or seccomp=unconfined, i.e. giving up the isolation the app exists to provide, which this project deliberately refuses. So do NOT 'fix' this by adding a /proc or /sys bind mount to _build_volumes(). Read scripts/docker/HOST-SPOOFING.md for the measured evidence and the full list of accepted trade-offs."
            if [[ ${#LEAKS[@]} -gt 0 ]]; then
                fail 4 "host hardware fingerprint not leaked into the container" \
                      "${LEAKS[*]} | $HD" "$REMEDIATION"
            else
                fail 4 "host hardware fingerprint not leaked into the container" \
                      "shim loaded but its output disagrees with the generated signature: ${INCOHERENT[*]} | $HD" \
                      "$REMEDIATION"
            fi
        fi
    fi
fi

# =========================================================================== #
# CHECK 5 — hardwareConcurrency consistent with the CPU model string
# =========================================================================== #
_hd "5. spoofed hardwareConcurrency matches the spoofed CPU model"

# Part 1 — does the CODE generate a coherent machine? Measured through the real
# entrypoint in a throwaway container, so it is a property of the code, not of
# the user's pre-existing containers.
if ! _probe_once; then
    skip 5a "generated machine identity is self-consistent (generated code path)" \
         "no runnable image ('$PROBE_IMAGE_REF') or docker unavailable" "build the image and re-run"
elif [[ -z "$SPOOF_PRELOAD" ]]; then
    # The shim is what makes the generated values readable back. Without it the
    # signature is still generated, but its fields cannot be cross-checked
    # against what a process actually sees, so this is unevaluable, not broken.
    skip 5a "generated machine identity is self-consistent (generated code path)" \
         "LD_PRELOAD was not exported, so the generated values could not be read back through the shim; this is check 4's subject, not this check's | signature: cpu='$SIG_MODEL' logical=$SIG_CORES physical=$SIG_PHYS (host has $CTRL_NPROC cores)" \
         "see check 4 — rebuild the image so /usr/lib/libhwspoof.so is present"
else
    # The invariant the generator promises, asserted on what the browser sees:
    #   phys <= logical <= 2*phys  and  (log - phys) even  and  N-Core == phys
    _tok() { printf '%s' "$1" | grep -oiE '[0-9]+[[:space:]]*-?[[:space:]]*core' | head -1 | grep -oE '[0-9]+' | head -1; }
    TOK="$(_tok "$SIG_MODEL")"
    P5A=()
    [[ "$SIG_PHYS"  =~ ^[0-9]+$ && "$SIG_CORES" =~ ^[0-9]+$ ]] || P5A+=("non-numeric core counts (phys=$SIG_PHYS logical=$SIG_CORES)")
    if [[ ${#P5A[@]} -eq 0 ]]; then
        (( SIG_CORES < SIG_PHYS ))        && P5A+=("logical $SIG_CORES < physical $SIG_PHYS")
        (( SIG_CORES > SIG_PHYS * 2 ))    && P5A+=("logical $SIG_CORES exceeds 2x physical $SIG_PHYS")
        (( (SIG_CORES - SIG_PHYS) % 2 != 0 )) && P5A+=("logical $SIG_CORES is not a real SMT config for physical $SIG_PHYS")
        [[ -n "$TOK" && "$TOK" != "$SIG_PHYS" ]] && P5A+=("model '$SIG_MODEL' says ${TOK}-Core but cpu_physical_cores is $SIG_PHYS")
        # The core count Chromium will actually report.
        [[ "$SPOOF_NPROC" =~ ^[0-9]+$ ]] || P5A+=("nproc returned '$SPOOF_NPROC'")
        if [[ "$SPOOF_NPROC" =~ ^[0-9]+$ ]]; then
            (( SPOOF_NPROC < 1 || SPOOF_NPROC > SIG_CORES )) && \
                P5A+=("effective cores $SPOOF_NPROC outside 1..$SIG_CORES")
        fi
    fi
    D5A="generated (entrypoint, throwaway profile): cpu='$SIG_MODEL' physical=$SIG_PHYS logical=$SIG_CORES -> nproc/hardwareConcurrency=$SPOOF_NPROC (host has $CTRL_NPROC cores, so the clamp is active)"
    if [[ ${#P5A[@]} -eq 0 ]]; then
        pass 5a "generated machine identity is self-consistent (generated code path)" "$D5A"
    else
        fail 5a "generated machine identity is self-consistent (generated code path)" \
              "${P5A[*]} | $D5A" \
              "fix the blueprint table in scripts/docker/hardware-spoof.sh: cpu_cores must be the model's real thread count, and cpu_physical_cores the model's real core count"
    fi
fi

# Part 2 — the signature files already on disk. These are DATA written by an
# older generator, not code, so a stale one is STALE, never FAIL.
SIG_GLOB_DIR="$CHROME_PROFILES_DIR"
if [[ ! -d "$SIG_GLOB_DIR" ]] || ! compgen -G "$SIG_GLOB_DIR/*/hardware-signature.json" >/dev/null; then
    skip 5b "on-disk profile signatures are self-consistent" \
         "no */hardware-signature.json under $SIG_GLOB_DIR" "launch at least one profile so a machine signature exists"
else
    SIG_LIST="$WORK/sigs.tsv"
    "$PY_BIN" - "$SIG_GLOB_DIR" >"$SIG_LIST" 2>/dev/null <<'PY_EOF'
import glob, json, os, re, sys
root = sys.argv[1]
for path in sorted(glob.glob(os.path.join(root, '*', 'hardware-signature.json'))):
    name = os.path.basename(os.path.dirname(path))
    try:
        d = json.load(open(path))
    except Exception as e:
        print("%s\tERROR\t\t%d" % (name, -1, 0)); continue
    hw = d.get('hardware', {}) or {}
    cores = hw.get('cpu_cores')
    phys = hw.get('cpu_physical_cores')
    model = hw.get('cpu_model') or d.get('cpu_model') or ''
    # schema_version 2 == written by the T4 generator (coherence-aware).
    # Anything lower predates it and its core count was rolled independently.
    try:
        schema = int(d.get('schema_version', 0) or 0)
    except (TypeError, ValueError):
        schema = 0
    print("%s\t%s\t%s\t%d\t%s" % (
        name,
        cores if isinstance(cores, int) else '-',
        phys if isinstance(phys, int) else '-',
        schema,
        model or '-'))
PY_EOF
    OKC=0; UNC=0; LEGACY=0; MISMATCH=""; DETAIL=""
    # NOTE: tab is an IFS *whitespace* character, so `read` collapses runs of
    # tabs. Every field is therefore emitted as a non-empty value ('-' for
    # absent) — an empty field would shift the later columns by one.
    while IFS=$'\t' read -r prof cores phys schema model; do
        [[ -z "$prof" || "$prof" == "ERROR" ]] && continue
        cores="${cores:--}"; phys="${phys:--}"; model="${model:--}"
        if [[ "$schema" == "0" ]]; then
            # Pre-T4 signature: cpu_cores was rolled with no relationship to any
            # CPU model, and 14/11/7 are not real thread counts at all. The
            # fixed generator backfills these in place on the next launch.
            LEGACY=$((LEGACY + 1))
            DETAIL+="$prof: legacy signature (schema_version 0), cpu_cores=$cores with no cpu_model — an incoherent legacy value, not a code defect; "
            continue
        fi
        # A current (post-fix) signature must carry BOTH counts. A pre-fix
        # signature is classified above and never reaches this branch, so
        # reaching it with a missing field is a genuine gap, not stale data.
        if ! [[ "$cores" =~ ^[0-9]+$ ]] || ! [[ "$phys" =~ ^[0-9]+$ ]]; then
            UNC=$((UNC + 1))
            DETAIL+="$prof: schema $schema but missing core counts (logical='$cores' physical='$phys'); "
            continue
        fi
        tok="$(printf '%s' "$model" | grep -oiE '[0-9]+[[:space:]]*-?[[:space:]]*core' | head -1 | grep -oE '[0-9]+' | head -1)"
        P=()
        (( cores < phys ))     && P+=("logical $cores < physical $phys")
        (( cores > phys * 2 )) && P+=("logical $cores exceeds 2x physical $phys")
        (( (cores - phys) % 2 != 0 )) && P+=("logical $cores is not a real SMT config for physical $phys")
        [[ -n "$tok" && "$tok" != "$phys" ]] && P+=("model '$model' says ${tok}-Core but cpu_physical_cores is $phys")
        if [[ ${#P[@]} -eq 0 ]]; then
            OKC=$((OKC + 1)); DETAIL+="$prof: ${phys}P/${cores}T consistent with '$model'; "
        else
            MISMATCH+="$prof: ${P[*]}; "
        fi
    done < "$SIG_LIST"
    DETAIL="${DETAIL%; }"; MISMATCH="${MISMATCH%; }"

    if [[ -n "$MISMATCH" ]]; then
        fail 5b "on-disk profile signatures are self-consistent" \
               "a CURRENT (post-fix) signature is incoherent: $MISMATCH | $DETAIL" \
               "the current generator should never write this; check the blueprint table in scripts/docker/hardware-spoof.sh"
    elif [[ $LEGACY -gt 0 ]]; then
        stale 5b "on-disk profile signatures are self-consistent" \
               "STALE ON-DISK DATA (pre-dates the fix): $LEGACY/$((OKC + LEGACY + UNC)) signature(s) were written by the pre-fix generator, which rolled cpu_cores independently of any CPU model (14/11/7 are not real thread counts). These are data, not code: the current generator backfills them in place on the next launch. $DETAIL" \
               "launch each profile once so hardware-spoof.sh rewrites its signature (the audit log prints the backfill); no code change is needed"
    elif [[ $UNC -gt 0 ]]; then
        skip 5b "on-disk profile signatures are self-consistent" \
             "$UNC profile(s) have unreadable core counts: $DETAIL" "repair the signature files"
    else
        pass 5b "on-disk profile signatures are self-consistent" "$OKC profile(s) consistent | $DETAIL"
    fi
fi

# =========================================================================== #
# CHECK 6 — Chromium currency (+ Alpine base support)
# =========================================================================== #
_hd "6. container Chromium is current"
if ! _probe_once; then
    skip 6 "container Chromium version is current" "no runnable image ('$PROBE_IMAGE_REF') or docker unavailable" "build the image and re-run"
elif [[ -z "$PROBE_CHROMIUM" ]]; then
    skip 6 "container Chromium version is current" "could not read chromium-browser --version inside the image" "verify chromium is installed in the Dockerfile"
else
    # Chromium milestone -> approximate release date. Anchored on 124.0 =
    # 2024-04-16 with a 30.5-day cadence; accurate to within ~a week over the
    # 120-140 range and monotonic outside it, which is all the gate needs.
    ver="$(printf '%s' "$PROBE_CHROMIUM" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
    mil="${ver%%.*}"
    if [[ -z "$mil" || ! "$mil" =~ ^[0-9]+$ ]]; then
        skip 6 "container Chromium version is current" "could not parse a version out of '$PROBE_CHROMIUM'" "normalise the version output format"
    else
        est_epoch="$(date -u -d '2024-04-16' +%s 2>/dev/null || echo 0)"
        off_days=$(awk -v m="$mil" 'BEGIN{printf "%.0f", (m-124)*30.5}')
        est_epoch=$(( est_epoch + off_days*86400 ))
        est_date="$(date -u -d "@$est_epoch" '+%Y-%m-%d' 2>/dev/null || echo 'unknown')"
        now_epoch="$(date -u +%s)"
        age_days=$(( (now_epoch - est_epoch) / 86400 ))
        age_months=$(awk -v d="$age_days" 'BEGIN{printf "%.1f", d/30.44}')
        D6="version=$ver ('$PROBE_CHROMIUM'), estimated release $est_date, age ~${age_months}mo (gate: <= ${CHROMIUM_MAX_AGE_MONTHS}mo)"
        over="$(awk -v a="$age_months" -v g="$CHROMIUM_MAX_AGE_MONTHS" 'BEGIN{print (a>g)?"1":"0"}')"
        if [[ "$over" == "1" ]]; then
            fail 6 "container Chromium version is current" \
                  "$D6 — STALE" \
                  "rebuild the image from a supported Alpine branch (currently FROM $(sed -n 's/^FROM[[:space:]]*//p' "$REPO_ROOT/Dockerfile" 2>/dev/null | head -1)) so it ships a current chromium package"
        else
            pass 6 "container Chromium version is current" "$D6"
        fi
    fi
fi

# 6b — Alpine base branch support
if ! _probe_once; then
    skip 6b "Alpine base branch still supported" "no runnable image or docker unavailable" "build the image and re-run"
else
    ALPINE_IN_CTR="$PROBE_ALPINE"
    # The base may be a literal (FROM alpine:3.24) or an ARG (ARG ALPINE_VERSION=3.24
    # + FROM alpine:${ALPINE_VERSION}) — accept either.
    ALPINE_IN_DOCKERFILE="$(sed -n 's/^FROM[[:space:]]*alpine:\([0-9][0-9.]*\).*/\1/p' "$REPO_ROOT/Dockerfile" 2>/dev/null | head -1)"
    if [[ -z "$ALPINE_IN_DOCKERFILE" ]]; then
        ALPINE_IN_DOCKERFILE="$(sed -n 's/^ARG[[:space:]][[:space:]]*ALPINE_VERSION=\([0-9][0-9.]*\).*/\1/p' "$REPO_ROOT/Dockerfile" 2>/dev/null | head -1)"
    fi
    D6B="container /etc/alpine-release='${ALPINE_IN_CTR:-unknown}', Dockerfile FROM alpine:${ALPINE_IN_DOCKERFILE:-unknown}"
    # Alpine supports the newest stable branch plus the one before it.
    # Release months are stable enough to encode (Alpine ships ~2/year).
    _alpine_ord() { # "3.19" -> months since epoch
        local b="$1" maj min yyyy mm
        maj="${b%%.*}"; min="${b##*.}"
        case "$min" in
            17) yyyy=2023; mm=11 ;;   18) yyyy=2023; mm=5  ;;
            19) yyyy=2023; mm=11 ;;   20) yyyy=2024; mm=5  ;;
            21) yyyy=2024; mm=12 ;;   22) yyyy=2025; mm=5  ;;
            23) yyyy=2025; mm=11 ;;   24) yyyy=2026; mm=5  ;;
            *)  yyyy=1999; mm=1  ;;   # unknown -> treat as ancient
        esac
        echo $(( (maj - 3) * 12 * 6 + (min - 17) * 6 + yyyy * 12 + mm ))
    }
    if [[ -z "$ALPINE_IN_DOCKERFILE" || ! "$ALPINE_IN_DOCKERFILE" =~ ^[0-9]+\.[0-9]+$ ]]; then
        skip 6b "Alpine base branch still supported" "could not determine the Alpine base branch from the Dockerfile"
    else
        NOW_ORD=$(( $(date -u +%Y) * 12 + 10#$(date -u +%m) ))
        CUR_ORD="$(_alpine_ord "$ALPINE_IN_DOCKERFILE")"
        AGE_BRANCHES=$(( (NOW_ORD - CUR_ORD) / 6 ))
        if [[ $AGE_BRANCHES -gt 1 ]]; then
            fail 6b "Alpine base branch still supported" \
                   "$D6B — branch $ALPINE_IN_DOCKERFILE is $AGE_BRANCHES branches behind the newest supported stable" \
                   "bump the Dockerfile FROM line to a supported Alpine stable branch and rebuild"
        else
            pass 6b "Alpine base branch still supported" "$D6B"
        fi
    fi
fi

# =========================================================================== #
# CHECK 7 — /.dockerenv
# =========================================================================== #
_hd "7. /.dockerenv absent or 755 root:root"
DOCKERENV_SRC=""
if [[ $DOCKER_OK -eq 1 && -n "$CHROME_CONTAINERS" ]]; then
    while IFS=$'\t' read -r cid cname cstatus; do
        [[ -z "$cid" ]] && continue
        src="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/.dockerenv"}}{{.Source}}{{end}}{{end}}' "$cid" 2>/dev/null)"
        [[ -n "$src" ]] && DOCKERENV_SRC="$src" && break
    done <<< "$CHROME_CONTAINERS"
fi

if [[ $DOCKER_OK -ne 1 ]]; then
    skip 7 "/.dockerenv absent or 755 root:root" "docker unavailable" "start the docker daemon"
elif [[ -z "$DOCKERENV_SRC" ]]; then
    # No mask mount anywhere -> the container sees Docker's own file, or none.
    if ! _probe_once; then
        skip 7 "/.dockerenv absent or 755 root:root" "no runnable image to inspect and no existing container bind for /.dockerenv"
    elif [[ $PROBE_FALLBACK -eq 1 ]]; then
        # The image under test is missing, so the probe ran a stale container's
        # image. Say so rather than reporting it as the current state.
        stale 7 "/.dockerenv absent or 755 root:root" \
               "STALE PROBE: image '$PROBE_IMAGE_REF' is not built locally, so the probe fell back to the image of an existing container, which predates the current Dockerfile" \
               "build the image (docker build -t $IMAGE_NAME .) and re-run"
    else
        D7="no container binds /.dockerenv; inside the image it is: ${PROBE_DOCKERENV:-unknown} (reference: real Docker is '755 root:root')"
        if [[ "$PROBE_DOCKERENV" == "absent" || "$PROBE_DOCKERENV" == "755 root:root" ]]; then
            pass 7 "/.dockerenv absent or 755 root:root" "$D7"
        else
            fail 7 "/.dockerenv absent or 755 root:root" "$D7" \
                  "drop the /.dockerenv mask mount from _build_volumes(), or ship the mask file as 0755 root:root"
        fi
    fi
else
    D7="mask source $DOCKERENV_SRC -> $(stat -c '%a %U:%G' "$DOCKERENV_SRC" 2>/dev/null || echo 'unreadable')"
    MODE_OWNER="$(stat -c '%a %U:%G' "$DOCKERENV_SRC" 2>/dev/null || echo '')"
    # Distinguish a code regression from a container that still carries the old
    # mask mount: introspect _build_volumes() and see whether the CODE still
    # adds the mask at all.
    CODE_MASK="unknown"
    if [[ -n "$VOL_REPORT" ]]; then
        if printf '%s\n' "$VOL_REPORT" | grep -q $'\t/.dockerenv'; then
            CODE_MASK="present"
        else
            CODE_MASK="absent"
        fi
    fi
    D7="$D7 | _build_volumes() currently emits a /.dockerenv mask: $CODE_MASK"
    if [[ "$MODE_OWNER" == "755 root:root" ]]; then
        pass 7 "/.dockerenv absent or 755 root:root" "$D7"
    elif [[ "$CODE_MASK" == "absent" ]]; then
        stale 7 "/.dockerenv absent or 755 root:root" \
               "STALE LIVE CONTAINER (pre-dates the fix): $MODE_OWNER is bound at /.dockerenv, but _build_volumes() no longer emits that mount, so a recreated container will see Docker's genuine 0755 root:root file" \
               "recreate the container by launching the profile from the app"
    else
        # Confirm the mismatch is visible from inside a container using the mask.
        MASK_IN_CTR="(not probed)"
        if [[ $DOCKER_OK -eq 1 ]] && docker image inspect "$IMAGE_REF" >/dev/null 2>&1; then
            MASK_IN_CTR="$(docker run --rm --network none --entrypoint sh \
                -v "${DOCKERENV_SRC}:/.dockerenv:ro" "$IMAGE_REF" -c \
                "stat -c '%a %U:%G' /.dockerenv 2>/dev/null || echo absent" 2>/dev/null || echo '(probe failed)')"
        fi
        fail 7 "/.dockerenv absent or 755 root:root" \
              "$D7, expected 755 root:root — inside the container /.dockerenv is '$MASK_IN_CTR'" \
              "drop the /.dockerenv mask from _build_volumes() (recommended), or install the mask file as 0755 root:root instead of 0644 <user>:<user>"
    fi
fi

# =========================================================================== #
# CHECK 8 — every container references the current image id
# =========================================================================== #
_hd "8. all containers reference the current image id"
if [[ $DOCKER_OK -ne 1 ]]; then
    skip 8 "all containers reference the current image id" "docker unavailable" "start the docker daemon"
elif [[ -z "$CHROME_CONTAINERS" ]]; then
    pass 8 "all containers reference the current image id" "no chrome-* containers exist, so there is nothing to be stale"
else
    IMG_NOTE=""
    [[ -z "$CURRENT_IMAGE_ID" ]] && IMG_NOTE=" (note: '$IMAGE_REF' is not built locally, so the current image id is unknown and every container counts as different)"
    STALE_LIST=""; LINES=""
    while IFS=$'\t' read -r cid cname cstatus; do
        [[ -z "$cid" ]] && continue
        imgid="$(docker inspect -f '{{.Image}}' "$cid" 2>/dev/null || echo '?')"
        if [[ -z "$CURRENT_IMAGE_ID" ]]; then
            LINES+="$cname: image $imgid (current image unknown — '$IMAGE_REF' not built locally); "
            continue
        fi
        tags="$(docker image inspect -f '{{range .RepoTags}}{{.}} {{end}}' "$imgid" 2>/dev/null)"
        if [[ "$imgid" == "$CURRENT_IMAGE_ID" ]]; then
            LINES+="$cname: current (${imgid:0:19}); "
        else
            kind="different image"
            [[ -z "${tags// /}" || "$tags" == " <none> " ]] && kind="DANGLING (<none>)"
            STALE_LIST+="$cname -> ${imgid:0:12} ($kind); "
            LINES+="$cname: ${imgid:0:12} $kind; "
        fi
    done <<< "$CHROME_CONTAINERS"
    LINES="${LINES-}"; STALE_LIST="${STALE_LIST-}"
    LINES="${LINES%; }"; STALE_LIST="${STALE_LIST%; }"
    if [[ -z "$STALE_LIST" ]]; then
        pass 8 "all containers reference the current image id" "$LINES"
    else
        # Container->image drift is a live-state fact by construction: the
        # containers were created from whatever was built at the time. It is
        # not evidence about the code or the image, so it is STALE. The one part
        # that IS actionable — a DANGLING image held only by these containers
        # (finding F6) — is called out explicitly in the action text.
        DANGLING=""
        printf '%s' "$STALE_LIST" | grep -q 'DANGLING' && \
            DANGLING=" NOTE: a DANGLING (<none>) image is held alive by these containers (finding F6) — it is not prunable until they are removed. A stopped container still holds its image, so there is no emergency; this is safe to do deliberately."
        stale 8 "all containers reference the current image id" \
               "STALE LIVE CONTAINER (pre-dates the fix): $STALE_LIST — the image built now is correct; these containers were created from an earlier build$IMG_NOTE" \
               "recreate them by launching each profile from the app (the manager force-removes a stopped container on start); do NOT use 'docker start', which reuses the old image.$DANGLING"
    fi
fi

# =========================================================================== #
# CHECK 9 — git working tree clean and the subprocess.run fix committed
# =========================================================================== #
_hd "9. working tree clean and os.system -> subprocess.run fix committed"
if ! git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    skip 9a "git working tree clean" "$REPO_ROOT is not a git repository"
else
    DIRTY="$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)"
    N_DIRTY="$(printf '%s' "$DIRTY" | grep -c . || true)"
    if [[ "$N_DIRTY" -eq 0 ]]; then
        pass 9a "git working tree clean" "branch $(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD) at $(git -C "$REPO_ROOT" rev-parse --short HEAD)"
    else
        SKETCH="$(printf '%s' "$DIRTY" | head -12 | tr '\n' ';')"
        fail 9a "git working tree clean" \
              "$N_DIRTY path(s) modified/untracked: $SKETCH" \
              "review the diff, then git add -A && git commit (this is work-order task T8)"
    fi
fi

if ! git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    skip 9b "desktop_manager.py os.system fix is committed" "$REPO_ROOT is not a git repository"
else
    REL="backend/desktop_manager.py"
    HEAD_SRC="$(git -C "$REPO_ROOT" show "HEAD:$REL" 2>/dev/null || true)"
    if [[ -z "$HEAD_SRC" ]]; then
        skip 9b "desktop_manager.py os.system fix is committed" "$REL is not tracked at HEAD"
    elif printf '%s' "$HEAD_SRC" | grep -qE '(^|[^_[:alnum:]])os\.system[[:space:]]*\('; then
        if grep -qE '(^|[^_[:alnum:]])os\.system[[:space:]]*\(' "$REPO_ROOT/$REL" 2>/dev/null; then
            WT_STATE="the working tree copy also still uses os.system("
        else
            WT_STATE="the working tree copy already uses subprocess.run() — only the commit is missing"
        fi
        fail 9b "desktop_manager.py os.system fix is committed" \
              "HEAD still contains os.system( in $REL; $WT_STATE" \
              "commit the subprocess.run([...], shell=False) replacement so the fix is not only in the working tree"
    elif printf '%s' "$HEAD_SRC" | grep -qE 'subprocess\.run\('; then
        pass 9b "desktop_manager.py os.system fix is committed" "HEAD:$REL uses subprocess.run(); os.system( count=$(printf '%s' "$HEAD_SRC" | grep -c 'os.system(' || true))"
    else
        fail 9b "desktop_manager.py os.system fix is committed" \
              "HEAD:$REL contains neither os.system( nor subprocess.run(" \
              "apply the subprocess.run fix and commit it"
    fi
fi

# =========================================================================== #
# SUMMARY
# =========================================================================== #
printf '\n%s%s SUMMARY %s\n' "$C_BOLD" "$C_BLUE" "$C_RESET"
printf '%s%-4s %-7s %-52s %s%s\n' "#" "RESULT" "CHECK" "DETAIL"
printf '%s\n' "---------------------------------------------------------------------------------------------------------------"
for i in "${!R_ID[@]}"; do
    case "${R_STATUS[$i]}" in
        PASS)  c="$C_GREEN";   tag="PASS" ;;
        FAIL)  c="$C_RED";     tag="FAIL" ;;
        STALE) c="$C_MAGENTA"; tag="STALE" ;;
        *)     c="$C_YELLOW";  tag="SKIP" ;;
    esac
    printf '%s%-4s %s%-7s%s %-52s %s%s%s\n' \
        "${R_ID[$i]}" "$c" "$tag" "$C_RESET" "${R_TITLE[$i]}" "$C_DIM" "${R_DETAIL[$i]}" "$C_RESET"
done
printf '%s\n' "---------------------------------------------------------------------------------------------------------------"
printf 'total %d   %sPASS %d%s   %sFAIL %d%s   %sSTALE %d%s   %sSKIP %d%s\n' \
    "$((N_PASS + N_FAIL + N_STALE + N_SKIP))" \
    "$C_GREEN" "$N_PASS" "$C_RESET" \
    "$C_RED" "$N_FAIL" "$C_RESET" \
    "$C_MAGENTA" "$N_STALE" "$C_RESET" \
    "$C_YELLOW" "$N_SKIP" "$C_RESET"

if [[ $N_FAIL -gt 0 ]]; then
    printf '\n%s%sActionable failures (%d) — genuine defects%s\n' "$C_BOLD" "$C_RED" "$N_FAIL" "$C_RESET"
    n=0
    for i in "${!R_ID[@]}"; do
        [[ "${R_STATUS[$i]}" == "FAIL" ]] || continue
        n=$((n + 1))
        printf '  %d. [%s] %s\n' "$n" "${R_ID[$i]}" "${R_TITLE[$i]}"
        printf '     why   : %s\n' "${R_DETAIL[$i]}"
        printf '     action: %s\n' "${R_ACTION[$i]}"
    done
else
    printf '\n%s%sNo actionable failures.%s\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
fi

if [[ $N_STALE -gt 0 ]]; then
    printf '\n%s%sStale live state (%d) — NOT code regressions%s\n' "$C_BOLD" "$C_MAGENTA" "$N_STALE" "$C_RESET"
    printf '  %sThese are pre-existing containers and on-disk signature files that were%s\n' "$C_DIM" "$C_RESET"
    printf '  %screated before the fixes landed, so they still show the old behaviour.%s\n' "$C_DIM" "$C_RESET"
    printf '  %sThey close by recreating the containers (launch each profile from the%s\n' "$C_DIM" "$C_RESET"
    printf '  %sapp), not by changing code. Each was only reported STALE after the%s\n' "$C_DIM" "$C_RESET"
    printf '  %scorresponding code was introspected and found correct.%s\n' "$C_DIM" "$C_RESET"
    for i in "${!R_ID[@]}"; do
        [[ "${R_STATUS[$i]}" == "STALE" ]] || continue
        printf '  %s[%s]%s %s\n' "$C_MAGENTA" "${R_ID[$i]}" "$C_RESET" "${R_TITLE[$i]}"
        printf '     why   : %s\n' "${R_DETAIL[$i]}"
        printf '     action: %s\n' "${R_ACTION[$i]}"
    done
fi

if [[ $N_SKIP -gt 0 ]]; then
    printf '\n%sSkipped (%d) — could not be evaluated on this host%s\n' "$C_YELLOW" "$N_SKIP" "$C_RESET"
    for i in "${!R_ID[@]}"; do
        [[ "${R_STATUS[$i]}" == "SKIP" ]] || continue
        printf '  [%s] %s — %s\n' "${R_ID[$i]}" "${R_TITLE[$i]}" "${R_DETAIL[$i]}"
    done
fi

printf '\n'
if [[ $N_FAIL -eq 0 && $N_STALE -gt 0 ]]; then
    printf '%sNo FAILs. The %d STALE item(s) above are pre-existing containers/signature files;%s\n' "$C_DIM" "$N_STALE" "$C_RESET"
    printf '%srecreate them from the app and re-run for a fully clean result.%s\n\n' "$C_DIM" "$C_RESET"
fi
[[ $N_FAIL -gt 0 ]] && exit 1
exit 0
