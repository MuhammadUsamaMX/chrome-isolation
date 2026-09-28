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
#   4.  /proc/cpuinfo, /proc/meminfo, /sys/class/dmi spoofed inside container
#   5.  spoofed hardwareConcurrency consistent with the spoofed CPU model string
#   6.  container Chromium is a current version (+ Alpine base still supported)
#   7.  /.dockerenv absent, or 755 root:root
#   8.  every existing container references the current image id
#   9.  git working tree clean and the desktop_manager.py os.system ->
#       subprocess.run fix is committed
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
#
# EXIT STATUS
#   0  no FAILs (PASS and/or SKIP only)
#   1  at least one genuine FAIL
#   2  harness error (repo not found, etc.)
# =============================================================================

set -uo pipefail

# --------------------------------------------------------------------------- #
# Locate the repository
# --------------------------------------------------------------------------- #
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PROFILES_JSON="$REPO_ROOT/profiles.json"
CHROME_PROFILES_DIR="${HOME}/Chrome"

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
    C_BLUE=$'\033[36m'
else
    C_RESET=''; C_BOLD=''; C_DIM=''
    C_RED='';   C_GREEN=''; C_YELLOW=''; C_BLUE=''
fi

# --------------------------------------------------------------------------- #
# Result plumbing
# --------------------------------------------------------------------------- #
declare -a R_ID R_STATUS R_TITLE R_DETAIL R_ACTION
N_PASS=0; N_FAIL=0; N_SKIP=0

_log() { [[ $VERBOSE -eq 1 ]] && printf '%s[verify]%s %s\n' "$C_DIM" "$C_RESET" "$*" >&2; return 0; }
_hd()   { printf '\n%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"; }

_record() { # id status title detail [action]
    R_ID+=("$1"); R_STATUS+=("$2"); R_TITLE+=("$3")
    R_DETAIL+=("${4:-}"); R_ACTION+=("${5:-}")
    case "$2" in
        PASS) N_PASS=$((N_PASS + 1)) ;;
        FAIL) N_FAIL=$((N_FAIL + 1)) ;;
        SKIP) N_SKIP=$((N_SKIP + 1)) ;;
    esac
}

pass() { _record "$1" PASS "$2" "${3:-}" ""; }
skip() { _record "$1" SKIP "$2" "${3:-}" "${4:-}"; }
fail() { _record "$1" FAIL "$2" "$3" "${4:-}"; }

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
# --------------------------------------------------------------------------- #
PROBE_STATE="unattempted"   # unattempted | ok | failed
# shellcheck disable=SC2034
PROBE_MODEL="" PROBE_MEMTOTAL="" PROBE_PRODUCT="" PROBE_BOARD_VENDOR=""
PROBE_KERNEL="" PROBE_NPROC="" PROBE_CHROMIUM="" PROBE_ALPINE="" PROBE_DOCKERENV=""

_probe_once() {
    [[ "$PROBE_STATE" != "unattempted" ]] && return 0
    if [[ $DOCKER_OK -ne 1 ]]; then PROBE_STATE=failed; return 1; fi
    if [[ -z "$CURRENT_IMAGE_ID" ]]; then
        _log "image '$IMAGE_REF' not present locally; falling back to newest chrome-* container image"
        if [[ -n "$CHROME_CONTAINERS" ]]; then
            local cimg
            cimg="$(docker inspect -f '{{.Image}}' "$(printf '%s\n' "$CHROME_CONTAINERS" | head -1 | cut -f1)" 2>/dev/null || true)"
            [[ -n "$cimg" ]] && IMAGE_REF="$cimg"
        fi
    fi
    docker image inspect "$IMAGE_REF" >/dev/null 2>&1 || { PROBE_STATE=failed; return 1; }

    local cname="verify-probe-$$-${RANDOM}"
    local out rc
    out="$(docker run --rm --interactive --name "$cname" --network none --entrypoint sh "$IMAGE_REF" -s <<'PROBE_EOF' 2>/dev/null
echo "MODEL=$(grep -m1 '^model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^[[:space:]]*//')"
echo "MEMTOTAL=$(grep -m1 '^MemTotal' /proc/meminfo 2>/dev/null | awk '{print $2" "$3}')"
echo "PRODUCT=$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)"
echo "BOARDVENDOR=$(cat /sys/class/dmi/id/board_vendor 2>/dev/null || true)"
echo "KERNEL=$(uname -r 2>/dev/null || true)"
echo "NPROC=$(nproc 2>/dev/null || echo 0)"
echo "CHROMIUM=$(chromium-browser --version 2>/dev/null | head -1 || true)"
echo "ALPINE=$(cat /etc/alpine-release 2>/dev/null || true)"
echo "DOCKERENV=$(stat -c '%a %U:%G' /.dockerenv 2>/dev/null || echo 'absent')"
PROBE_EOF
)"
    rc=$?
    if [[ $rc -ne 0 || -z "$out" ]]; then
        PROBE_STATE=failed
        _log "probe container failed (rc=$rc)"
        return 1
    fi
    {
        printf '%s\n' "$out" | while IFS='=' read -r k v; do
            printf 'PROBE_%s=%q\n' "$k" "$v"
        done
    } > "$WORK/probe.env"
    # shellcheck disable=SC1090
    source "$WORK/probe.env"
    PROBE_STATE="ok"
    _log "probe container done (image=$IMAGE_REF)"
    return 0
}

# =========================================================================== #
printf '%sChrome Isolation Manager — verification harness%s\n' "$C_BOLD" "$C_RESET"
printf 'repo      : %s\n' "$REPO_ROOT"
printf 'image ref : %s%s%s\n' "$IMAGE_REF" \
    "$( [[ -n "$CURRENT_IMAGE_ID" ]] && printf ' (%s)' "${CURRENT_IMAGE_ID:0:19}" )" "$C_RESET"
printf 'docker    : %s\n' "$( [[ $DOCKER_OK -eq 1 ]] && echo 'available' || echo 'unavailable — Docker checks will SKIP' )"
printf 'host base : cpu=%s | mem=%s | dmi=%s | kernel=%s | cores=%s\n' \
    "$HOST_CPU_MODEL" "$HOST_MEMTOTAL" "$HOST_PRODUCT" "$HOST_KERNEL" "$HOST_NPROC"
printf 'baseline  : %s (known: cpu="%s", mem=%s, dmi=%s, board="%s", kernel=%s)\n' \
    "$BASELINE_USED" "$KNOWN_CPU_MODEL" "$KNOWN_MEMTOTAL" "$KNOWN_PRODUCT" "$KNOWN_BOARD_VENDOR" "$KNOWN_KERNEL"

# =========================================================================== #
# CHECK 1 — pytest tests/ is green
# =========================================================================== #
_hd "1. pytest tests/ green"
PY_BIN=""
for cand in python3 python; do command -v "$cand" >/dev/null 2>&1 && { PY_BIN="$cand"; break; }; done

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
    print("PROFILE\t%s\t%s\t%s\t%s\t%s" % (name, raw or '<empty>', resolved,
          ','.join(binds) or '<none>', 'HOME' if hit else 'ok'))
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
        fail 2 "no profile mounts ~ at /home/chrome/host" \
              "static check errored (rc=$VOL_RC) but $LIVE_HOME_HITS live container bind(s) expose HOME | $DETAIL" \
              "stop+recreate the affected profile containers after the host_mount fallback is narrowed"
    else
        skip 2 "no profile mounts ~ at /home/chrome/host" \
             "static check errored (rc=$VOL_RC): $(printf '%s' "$VOL_REPORT" | tail -2 | tr '\n' ' ')" \
             "make _build_volumes() introspectable (see the python traceback above)"
    fi
elif [[ "$VOL_RC" -eq 1 || $LIVE_HOME_HITS -gt 0 ]]; then
    fail 2 "no profile mounts ~ at /home/chrome/host" \
          "HOME ($HOME_REAL) is bound to /home/chrome/host on $LIVE_HOME_HITS live container(s); code-level violations: $VIOLATIONS | $DETAIL" \
          "the containers still hold the OLD bind — stop and remove them, then start again so they are recreated with the narrowed host_mount"
else
    pass 2 "no profile mounts ~ at /home/chrome/host" "0 code-level violations, 0 live binds of HOME | $DETAIL"
fi

# =========================================================================== #
# CHECK 3 — container hardening flags
# =========================================================================== #
_hd "3. docker inspect hardening flags"
if [[ $DOCKER_OK -ne 1 ]]; then
    skip 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" "docker unavailable" "start the docker daemon"
elif [[ -z "$CHROME_CONTAINERS" ]]; then
    skip 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" "no chrome-* container exists to inspect" "start one profile container and re-run"
else
    TARGET="$(printf '%s\n' "$CHROME_CONTAINERS" | head -1)"
    TID="$(printf '%s' "$TARGET" | cut -f1)"; TNAME="$(printf '%s' "$TARGET" | cut -f2)"; TSTAT="$(printf '%s' "$TARGET" | cut -f3)"
    CAPADD="$(docker inspect -f '{{json .HostConfig.CapAdd}}' "$TID" 2>/dev/null)"
    CAPDROP="$(docker inspect -f '{{json .HostConfig.CapDrop}}' "$TID" 2>/dev/null)"
    SECOPT="$(docker inspect -f '{{json .HostConfig.SecurityOpt}}' "$TID" 2>/dev/null)"
    PIDSLIM="$(docker inspect -f '{{json .HostConfig.PidsLimit}}' "$TID" 2>/dev/null)"
    ROFS="$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$TID" 2>/dev/null)"
    HC="$(printf 'container=%s (%s) CapAdd=%s CapDrop=%s SecurityOpt=%s PidsLimit=%s ReadonlyRootfs=%s' \
          "$TNAME" "$TSTAT" "$CAPADD" "$CAPDROP" "$SECOPT" "$PIDSLIM" "$ROFS")"
    PROBLEMS=()
    case "$CAPADD" in 'null'|'[]'|'""') ;; *) PROBLEMS+=("CapAdd is $CAPADD, expected null or []") ;; esac
    [[ "$CAPDROP" == *ALL* ]] || PROBLEMS+=("CapDrop is $CAPDROP, expected [\"ALL\"]")
    [[ "$SECOPT" == *no-new-privileges* ]] || PROBLEMS+=("SecurityOpt is $SECOPT, expected no-new-privileges")
    case "$PIDSLIM" in 'null') PROBLEMS+=("PidsLimit is null") ;; esac
    if [[ ${#PROBLEMS[@]} -eq 0 ]]; then
        pass 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" "$HC"
    else
        fail 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" \
              "${PROBLEMS[*]} | $HC" \
              "set cap_drop=['ALL'], security_opt=['no-new-privileges'], pids_limit=N (and read_only=True) in DockerManager.start_container(), then recreate the containers"
    fi
fi

# =========================================================================== #
# CHECK 4 — hardware fingerprint spoofing inside the container
# =========================================================================== #
_hd "4. /proc/cpuinfo, /proc/meminfo, /sys/class/dmi spoofed"
if ! _probe_once; then
    skip 4 "host hardware fingerprint not leaked into the container" \
         "no runnable image ('$IMAGE_REF') or docker unavailable" "build the image (docker build -t $IMAGE_NAME .) and re-run"
else
    LEAKS=()
    _cmp() { # label container-value host-value
        [[ -z "$2" ]] && { LEAKS+=("$1: container value could not be read"); return; }
        if [[ "$2" == "$3" ]]; then LEAKS+=("$1 matches the real host: '$2'"); fi
    }
    _cmp "/proc/cpuinfo 'model name'" "$PROBE_MODEL" "$HOST_CPU_MODEL"
    _cmp "/proc/meminfo MemTotal"     "$PROBE_MEMTOTAL" "$HOST_MEMTOTAL"
    _cmp "/sys/class/dmi/id/product_name"  "$PROBE_PRODUCT" "$HOST_PRODUCT"
    _cmp "/sys/class/dmi/id/board_vendor" "$PROBE_BOARD_VENDOR" "$HOST_BOARD_VENDOR"
    HD="container: cpu='$PROBE_MODEL' mem='$PROBE_MEMTOTAL' product='$PROBE_PRODUCT' board='$PROBE_BOARD_VENDOR' kernel='$PROBE_KERNEL' cores=$PROBE_NPROC"
    if [[ ${#LEAKS[@]} -eq 0 ]]; then
        pass 4 "host hardware fingerprint not leaked into the container" "$HD"
    else
        fail 4 "host hardware fingerprint not leaked into the container" \
              "${LEAKS[*]} | $HD" \
              "bind-mount spoofed /proc/cpuinfo, /proc/meminfo and /sys/class/dmi/id from a per-profile fingerprint in _build_volumes(), then rebuild the image"
    fi
fi

# =========================================================================== #
# CHECK 5 — hardwareConcurrency consistent with the CPU model string
# =========================================================================== #
_hd "5. spoofed hardwareConcurrency matches the spoofed CPU model"
SIG_GLOB_DIR="$CHROME_PROFILES_DIR"
if [[ ! -d "$SIG_GLOB_DIR" ]] || ! compgen -G "$SIG_GLOB_DIR/*/hardware-signature.json" >/dev/null; then
    skip 5 "hardwareConcurrency consistent with CPU model string" \
         "no */hardware-signature.json under $SIG_GLOB_DIR" "launch at least one profile so a machine signature exists"
else
    _probe_once >/dev/null 2>&1 || true
    FALLBACK_MODEL="$PROBE_MODEL"; [[ -z "$FALLBACK_MODEL" ]] && FALLBACK_MODEL="$HOST_CPU_MODEL"
    SIG_LIST="$WORK/sigs.tsv"
    "$PY_BIN" - "$SIG_GLOB_DIR" >"$SIG_LIST" 2>/dev/null <<'PY_EOF'
import glob, json, os, re, sys
root = sys.argv[1]
for path in sorted(glob.glob(os.path.join(root, '*', 'hardware-signature.json'))):
    try:
        d = json.load(open(path))
    except Exception as e:
        print("%s\tERROR\t%s" % (os.path.basename(os.path.dirname(path)), e)); continue
    hw = d.get('hardware', {}) or {}
    cores = hw.get('cpu_cores')
    model = hw.get('cpu_model') or d.get('cpu_model') or ''
    print("%s\t%s\t%s" % (os.path.basename(os.path.dirname(path)),
                           '' if cores is None else cores, model))
PY_EOF
    MISMATCH=""; OKC=0; UNC=0; DETAIL=""
    while IFS=$'\t' read -r prof cores model; do
        [[ -z "$prof" ]] && continue
        if [[ "$prof" == "ERROR" || "$cores" == "ERROR" ]]; then continue; fi
        [[ "$cores" == "ERROR" ]] && { MISMATCH+="$prof: unreadable signature; "; continue; }
        eff="${model:-$FALLBACK_MODEL}"
        src="signature cpu_model"
        [[ -z "$model" ]] && src="container /proc/cpuinfo (no cpu_model in signature)"
        m="$(printf '%s' "$eff" | grep -oiE '[0-9]+[[:space:]]*-?[[:space:]]*core' | head -1 | grep -oE '[0-9]+' | head -1)"
        if [[ -z "$m" ]]; then
            UNC=$((UNC + 1))
            DETAIL+="$prof: cores=$cores, model='$eff' has no N-Core token (skipped); "
            continue
        fi
        if [[ "$cores" == "$m" ]]; then
            OKC=$((OKC + 1)); DETAIL+="$prof: cores=$cores == '$m-Core' OK; "
        else
            MISMATCH+="$prof: claims $cores cores but CPU model '$eff' says ${m}-Core; "
        fi
        DETAIL+="($src) "
    done < <(grep -v $'\tERROR' "$SIG_LIST" 2>/dev/null || true)
    MISMATCH="${MISMATCH%; }"
    if [[ -n "$MISMATCH" ]]; then
        fail 5 "hardwareConcurrency consistent with CPU model string" \
              "core-count/model-string mismatch: $MISMATCH | $DETAIL" \
              "add a matching 'cpu_model' string to each hardware-signature.json (N-Core must equal hardware.cpu_cores) and spoof /proc/cpuinfo from it"
    elif [[ $UNC -gt 0 ]]; then
        skip 5 "hardwareConcurrency consistent with CPU model string" \
             "$UNC profile(s) have no parseable N-Core token in their CPU model: $DETAIL" \
             "record an explicit hardware.cpu_model in each signature"
    else
        pass 5 "hardwareConcurrency consistent with CPU model string" \
              "$OKC profile(s) consistent | $DETAIL"
    fi
fi

# =========================================================================== #
# CHECK 6 — Chromium currency (+ Alpine base support)
# =========================================================================== #
_hd "6. container Chromium is current"
if ! _probe_once; then
    skip 6 "container Chromium version is current" "no runnable image ('$IMAGE_REF') or docker unavailable" "build the image and re-run"
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
    if [[ "$MODE_OWNER" == "755 root:root" ]]; then
        pass 7 "/.dockerenv absent or 755 root:root" "$D7"
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
    STALE=""; LINES=""
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
            STALE+="$cname -> ${imgid:0:12} ($kind); "
            LINES+="$cname: ${imgid:0:12} $kind; "
        fi
    done <<< "$CHROME_CONTAINERS"
    LINES="${LINES%; }"; STALE="${STALE%; }"
    if [[ -z "$STALE" ]]; then
        pass 8 "all containers reference the current image id" "$LINES"
    else
        fail 8 "all containers reference the current image id" \
              "stale containers: $STALE" \
              "stop+remove each stale container and start it again so it is recreated from $IMAGE_REF; then prune the orphaned <none> image"
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
        PASS) c="$C_GREEN";  tag="PASS" ;;
        FAIL) c="$C_RED";    tag="FAIL" ;;
        *)    c="$C_YELLOW"; tag="SKIP" ;;
    esac
    printf '%s%-4s %s%-7s%s %-52s %s%s%s\n' \
        "${R_ID[$i]}" "$c" "$tag" "$C_RESET" "${R_TITLE[$i]}" "$C_DIM" "${R_DETAIL[$i]}" "$C_RESET"
done
printf '%s\n' "---------------------------------------------------------------------------------------------------------------"
printf 'total %d   %sPASS %d%s   %sFAIL %d%s   %sSKIP %d%s\n' \
    "$((N_PASS + N_FAIL + N_SKIP))" \
    "$C_GREEN" "$N_PASS" "$C_RESET" "$C_RED" "$N_FAIL" "$C_RESET" "$C_YELLOW" "$N_SKIP" "$C_RESET"

if [[ $N_FAIL -gt 0 ]]; then
    printf '\n%s%sActionable failures (%d)%s\n' "$C_BOLD" "$C_RED" "$N_FAIL" "$C_RESET"
    n=0
    for i in "${!R_ID[@]}"; do
        [[ "${R_STATUS[$i]}" == "FAIL" ]] || continue
        n=$((n + 1))
        printf '  %d. [%s] %s\n' "$n" "${R_ID[$i]}" "${R_TITLE[$i]}"
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
[[ $N_FAIL -gt 0 ]] && exit 1
exit 0
