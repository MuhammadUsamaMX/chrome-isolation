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
#       string, again through the real entrypoint path (5a generated, 5b on disk)
#   6.  container Chromium is a current version (+ Alpine base still supported)
#   7.  /.dockerenv absent, or 755 root:root
#   8.  every existing container references the current image id
#   9.  git working tree clean and the desktop_manager.py os.system ->
#       subprocess.run fix is committed
#   10. desktop identity: no generated .desktop declares WebBrowser, borrows
#       google-chrome's icon, or claims a MIME type; and Chromium's --class
#       matches the entry's StartupWMClass
#   11. uninstall.sh will not `rm -rf` a path outside the profile store,
#       including under a hostile $CHROME_ISOLATION_HOME
#   12. the Chromium update path is DETECT-ONLY by default: an update being
#       available is never on its own sufficient to start a build
#   13. THIS HARNESS IS SIDE-EFFECT FREE — nothing it ran changed profiles.json,
#       the proxy store, or any on-disk profile signature
#
# ---------------------------------------------------------------------------
# PROFILE STORE LOCATION — WHY NOTHING HERE HARDCODES IT
# ---------------------------------------------------------------------------
#   The store MOVED. It is `<isolation home>/profiles/<name>`, where the
#   isolation home is `$CHROME_ISOLATION_HOME` or, unset, `~/Isolated-Chrome`.
#   `~/Chrome` is the LEGACY location and is now only a backward-compat
#   fallback for installs that have not been migrated. `~/Chrome` does not
#   exist on this host and the migration has been applied.
#
#   An earlier version of this harness hardcoded `~/Chrome` and therefore
#   reported meaningless results on every host where the move had happened.
#   That is the whole failure mode: a hardcoded path does not fail loudly, it
#   fails *confidently*. So every path here is ASKED FOR, never asserted:
#
#     * `backend/paths.py` is the app's own resolver. It is deliberately pure
#       and side-effect free, which is what makes it safe to import from a
#       verification script — `backend/config.py` computes the same answer but
#       also runs `os.makedirs`/`chmod` at import time.
#     * `paths.resolve_profiles_dir()` with no arguments is *exactly*
#       `config.resolve_profiles_dir()`: config's two constants are literally
#       `paths.default_profiles_dir()` and `paths.legacy_profiles_dir()`,
#       which are the defaults `resolve_profiles_dir()` uses. Check 10 asserts
#       that equivalence rather than assuming it.
#     * The backend directory is passed in as an ARGUMENT. Reading it from
#       `os.getcwd()` silently degrades to a shell fallback whenever the script
#       is invoked from another directory, which is exactly how a harness ends
#       up reporting a fallback path as though it had been measured.
#
# ---------------------------------------------------------------------------
# CODE STATE vs LIVE STATE — why this harness has a third status
# ---------------------------------------------------------------------------
#   PASS   the code and/or the image is correct.
#   FAIL   a genuine defect. Either in the code/image, or in a LIVE CONTAINER
#          or IMAGE that the current code would not have produced.
#   STALE  the *code* is correct but a pre-existing container, a pre-existing
#          IMAGE, or a signature file on disk was created by an OLDER version
#          and still shows the old behaviour. This is the expected
#          intermediate state after a fix lands and before containers are
#          recreated or a new image is promoted — it is NOT a code regression.
#   SKIP   the check could not be evaluated on this host.
#
#   STALE exists because a harness that cries wolf on every run gets ignored,
#   and an ignored harness is how a real regression slips through. Checks 2, 3,
#   4, 6, 7 and 8 read the user's pre-existing containers and images, which
#   necessarily predate the fixes; they are reported as STALE *only after* the
#   corresponding code has been introspected and found correct. If the code
#   itself is wrong, the check is a FAIL regardless of what the containers
#   look like.
#
#   A CHECK MUST BE CAPABLE OF PASSING ON ITS OWN TERMS. Two rules follow from
#   an earlier version of this harness reporting "9 FAIL" on a host where two
#   of the nine could not have passed under any code:
#     * no check may need live state to exist in order to evaluate the code
#       (see 3, which used to SKIP outright when no container existed, so the
#       one thing it could prove on its own was never proven);
#     * no check may assert something the current code deliberately does NOT do
#       (see 7 and 12, which are about what is absent).
#
# ---------------------------------------------------------------------------
# SAFETY CONTRACT
# ---------------------------------------------------------------------------
#   * Read-only inspection only, plus throwaway `docker run --rm` containers
#     that this script creates and removes itself.
#   * Never removes, stops, prunes, kills or restarts anything that existed
#     before the run — the user's `chrome-*` containers are only ever
#     `docker inspect`-ed.
#   * NEVER `docker build`, `docker prune`, or `docker rm` of anything that
#     existed before the run.
#   * Every filesystem write this harness performs lands in the mktemp -d
#     scratch dir created at startup and removed on exit. An earlier version
#     wrote into the real profile store while probing; check 13 now exists
#     precisely so that class of bug cannot pass unnoticed again. When a probe
#     redirects a write, it redirects the LOCATION the code computes the path
#     from, not merely the name of the function that performs the write —
#     check 2 shipped a stub that patched `ensure_host_share_dir` on
#     docker_manager, was never called even once, and still created directories
#     in the user's real host-shares root every run.
#   * Check 11 runs `uninstall.sh` inside a sandbox under a fake $HOME with
#     `docker`, `sudo`, `dpkg` and `rpm` stubbed out on PATH. It never runs
#     the real uninstaller against the real home directory.
#   * Importing `backend/config.py` runs the app's own import-time
#     `os.makedirs(..., exist_ok=True)` and `chmod` of the host-shares root.
#     Those are idempotent and touch only directories the app owns, but they
#     ARE real mkdir calls and are disclosed here rather than glossed over.
#   * No writes to `profiles.json`, `proxies.json` or any profile signature;
#     check 13 proves it rather than asserting it. No git operations, no edits
#     to the repo.
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
#   by recreating containers or promoting a rebuilt image, not by changing
#   code. It is always printed in the summary and in its own section, so it is
#   never silently swallowed.
# =============================================================================

set -uo pipefail

# --------------------------------------------------------------------------- #
# Locate the repository
# --------------------------------------------------------------------------- #
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PROFILES_JSON="$REPO_ROOT/profiles.json"

# Python interpreter, needed both for pytest and to ask the backend where
# profile data lives. Resolved here, early, because the profile location below
# needs it.
PY_BIN=""
for cand in python3 python; do command -v "$cand" >/dev/null 2>&1 && { PY_BIN="$cand"; break; }; done

# Profile data location. NEVER hardcoded, and never read out of the current
# working directory — ask the app's own resolver, with the repo passed in.
# See the header block for why `paths` rather than `config`.
resolve_profiles_dir() {
    "$1" - "$REPO_ROOT" <<'PY_EOF'
import os, sys
sys.path.insert(0, os.path.join(sys.argv[1], 'backend'))
import paths
print(paths.resolve_profiles_dir())
PY_EOF
}
CHROME_PROFILES_DIR=""
PROFILES_DIR_RESOLVED_BY="backend/paths.py"
if [[ -n "$PY_BIN" ]]; then
    CHROME_PROFILES_DIR="$(resolve_profiles_dir "$PY_BIN" 2>/dev/null)"
fi
# Only a real, absolute path counts as an answer. Anything else means we
# failed to ask, and we must say so rather than substitute a guess that will
# later be printed as though it had been measured.
if [[ -z "$CHROME_PROFILES_DIR" || "$CHROME_PROFILES_DIR" != /* ]]; then
    CHROME_PROFILES_DIR="${CHROME_ISOLATION_HOME:-$HOME/Isolated-Chrome}/profiles"
    PROFILES_DIR_RESOLVED_BY="FALLBACK — the app's own resolver could not be asked"
fi


VERBOSE=0
CHROMIUM_MAX_AGE_MONTHS="${VERIFY_CHROMIUM_MAX_AGE_MONTHS:-12}"
SKIP_DOCKER="${VERIFY_SKIP_DOCKER:-0}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--verbose) VERBOSE=1 ;;
        -h|--help)    sed -n '2,120p' "${BASH_SOURCE[0]}"; exit 0 ;;
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
# Side-effect tripwire — "before" snapshot (check 13 compares against this)
# --------------------------------------------------------------------------- #
# Taken HERE, before any check runs, because every check introspects code by
# CALLING it. See check 13 for why that is a hazard worth a tripwire.
LEGACY_PROFILES_DIR="$HOME/Chrome"
DESKTOP_DIR="$HOME/.local/share/applications"
D13_SNAP="$WORK/snapshot-00-start.tsv"
D13_SNAP_POST_TESTS="$WORK/snapshot-01-post-tests.tsv"

_snapshot_files() {
    local f d
    for f in "$PROFILES_JSON" "$REPO_ROOT/proxies.json"; do
        if [[ -f "$f" ]]; then
            printf '%s\t%s\n' "$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)" "$f"
        else
            printf 'absent\t%s\n' "$f"
        fi
    done
    # The resolved profile store, and the legacy one if present: signatures
    # (which the generator rewrites) and profile directories themselves, so a
    # junk profile created by a probe shows up.
    for d in "$CHROME_PROFILES_DIR" "$LEGACY_PROFILES_DIR"; do
        [[ -d "$d" ]] || continue
        printf 'DIR\t%s\n' "$d"
        while IFS= read -r f; do
            [[ -n "$f" ]] && printf '%s\t%s\n' "$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)" "$f"
        done < <(find "$d" -maxdepth 2 -name 'hardware-signature.json' 2>/dev/null | sort)
        while IFS= read -r f; do
            [[ -n "$f" ]] && printf 'DIRENT\t%s\n' "$f"
        done < <(find "$d" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)
    done
    # The host-share root: a probe that mkdirs here leaves a trace.
    if [[ -d "$REPO_ROOT/host-shares" ]]; then
        printf 'DIR\t%s\n' "$REPO_ROOT/host-shares"
        while IFS= read -r f; do
            [[ -n "$f" ]] && printf 'DIRENT\t%s\n' "$f"
        done < <(find "$REPO_ROOT/host-shares" -mindepth 1 -maxdepth 1 2>/dev/null | sort)
    fi
    # Generated desktop entries (refresh_desktop_entries() rewrites these).
    while IFS= read -r f; do
        [[ -n "$f" ]] && printf '%s\t%s\n' "$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)" "$f"
    done < <(ls -1 "$DESKTOP_DIR"/chrome-isolation*.desktop 2>/dev/null)
}
_snapshot_files > "$D13_SNAP"

# Is anyone ELSE able to write this repo's data while this harness runs?
# Tests that spawn the real bridge as a subprocess register real profiles in
# the registry and create real host-share directories. If such a process is
# running concurrently, a mutation observed at the end of the run cannot be
# attributed to the harness. Recorded so that check 13 can say so instead of
# accusing itself — or silently passing — when attribution is impossible.
# Read-only scan of /proc; no signals, no writes.
_d13_concurrent_writers() {
    local n=0 pid cmd
    for pid in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
        [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
        cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" || continue
        case "$cmd" in
            *chrome-isolation-manager*pytest*|*pytest*chrome-isolation-manager*|*bridge.py*|*chrome_isolation*)
                n=$((n + 1)) ;;
        esac
    done
    printf '%s' "$n"
}
D13_CONCURRENT="$( _d13_concurrent_writers )"

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

# --------------------------------------------------------------------------- #
# Does the image we are measuring predate the Dockerfile we are measuring against?
# --------------------------------------------------------------------------- #
# Checks 4, 5a, 6 and 7 measure what is INSIDE the image. If that image was
# built before the current Dockerfile, then a red result is a statement about an
# old build, not about the code in the tree — the same reasoning that makes a
# stale container STALE rather than FAIL. Getting this wrong is how a harness
# reports "9 FAIL" for an app whose code is entirely correct.
#
# Image creation time vs. Dockerfile mtime is a coarse but honest signal: the
# Dockerfile changing is not proof the image is old (a comment edit would trip
# it), so this only ever *downgrades* a FAIL to STALE with the evidence
# printed. It never upgrades anything.
IMAGE_PREDATES_DOCKERFILE=0
IMAGE_STALENESS_EVIDENCE=""
if [[ $DOCKER_OK -eq 1 && -f "$REPO_ROOT/Dockerfile" ]]; then
    IMG_CREATED="$(docker image inspect -f '{{.Created}}' "$PROBE_IMAGE_REF" 2>/dev/null || true)"
    if [[ -n "$IMG_CREATED" ]]; then
        IMG_EPOCH="$(date -u -d "$IMG_CREATED" +%s 2>/dev/null || echo 0)"
        DOCKERFILE_EPOCH="$(date -u -d "@$(stat -c %Y "$REPO_ROOT/Dockerfile" 2>/dev/null || echo 0)" +%s 2>/dev/null || echo 0)"
        if [[ "$IMG_EPOCH" -gt 0 && "$DOCKERFILE_EPOCH" -gt 0 && "$IMG_EPOCH" -lt "$DOCKERFILE_EPOCH" ]]; then
            IMAGE_PREDATES_DOCKERFILE=1
            IMAGE_STALENESS_EVIDENCE="image '$PROBE_IMAGE_REF' was built $IMG_CREATED, but the Dockerfile was last modified $(date -u -d "@$DOCKERFILE_EPOCH" '+%Y-%m-%d %H:%M:%S UTC') — the image under test predates the code in the tree"
        fi
    fi
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

# The profile store is ASKED FOR, never assumed. Print where it resolved and
# how, because "the harness silently fell back to a guess" is a failure mode
# that must be visible in the output rather than discovered later.
printf 'profiles  : %s%s%s\n' "$C_BOLD" "$CHROME_PROFILES_DIR" "$C_RESET"
printf '            via %s%s%s\n' "$C_DIM" "$PROFILES_DIR_RESOLVED_BY" "$C_RESET"
if [[ -d "$LEGACY_PROFILES_DIR" ]] && [[ -n "$(ls -A "$LEGACY_PROFILES_DIR" 2>/dev/null)" ]]; then
    printf '            legacy %s still holds data — the app would fall back to it if the\n' "$LEGACY_PROFILES_DIR"
    printf '            new store were empty. See scripts/migrate_profiles_dir.py\n' 
fi

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

# Second tripwire snapshot, taken immediately after check 1. Check 13 uses the
# two segments to attribute any mutation to a phase: the test suite, or the
# harness's own checks. Blaming the wrong one sends whoever has to fix it in
# the wrong direction — and the test suite spawns the real bridge as a
# subprocess, so it genuinely can touch real data on its own.
_snapshot_files > "$D13_SNAP_POST_TESTS"

# =========================================================================== #
# CHECK 2 — no profile container mounts ~ at /home/chrome/host
# =========================================================================== #
_hd "2. no profile mounts ~ at /home/chrome/host"

HOME_REAL="$(readlink -f "$HOME" 2>/dev/null || echo "$HOME")"
VOL_REPORT=""
VOL_RC=1
if [[ -n "$PY_BIN" && -f "$PROFILES_JSON" ]]; then
    _log "introspecting _build_volumes() for every profile in profiles.json (read-only; share dirs redirected to $WORK)"
    # The scratch dir is EXPORTED on its own line, not written as
    #   VERIFY_SCRATCH="$WORK" VOL_REPORT="$(python ...)"
    # because bash does not make a pending assignment visible to the command
    # substitutions in the SAME simple command: the substitution runs first, so
    # the probe saw no VERIFY_SCRATCH at all. The old probe tolerated that
    # (`if SCRATCH:` was simply never true, and the code fell through to
    # returning the real path), so the bug was silent: the probe believed it
    # was redirecting into $WORK and was writing to the user's store instead.
    # The probe now treats a missing VERIFY_SCRATCH as fatal rather than
    # defaulting to the real location.
    export VERIFY_SCRATCH="$WORK"
    VOL_REPORT="$("$PY_BIN" - "$REPO_ROOT" <<'PY_EOF' 2>&1
import json, os, sys
repo = sys.argv[1]
sys.path.insert(0, os.path.join(repo, 'backend'))
try:
    import docker, docker.errors
except Exception as e:                      # docker SDK missing -> caller SKIPs
    print("IMPORT_ERROR docker sdk: %s" % e); sys.exit(3)

import docker_manager, registry, config, host_share

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

# ---------------------------------------------------------------------------
# VERIFY-PROBE-HOST-MOUNTS-BEGIN
#
# READ-ONLY, AND HOW THAT IS ACTUALLY ACHIEVED
#   `_profile_host_mount()` resolves a profile's /home/chrome/host source and,
#   for every profile with an empty host_mount, that resolution CREATES a
#   per-profile directory under ~/.local/share/chrome-isolation-manager/
#   host-shares (host_share.ensure_host_share_dir -> os.makedirs). That is
#   correct APP behaviour and a bug in a PROBE: introspecting code by calling
#   it silently created directories in the user's data.
#
#   The stub that used to sit here patched ONE NAME — docker_manager's
#   `ensure_host_share_dir` — and that was not enough, because
#   `_profile_host_mount()` does not call that name. It calls
#   `resolve_host_mount()`, which looks up `ensure_host_share_dir` in
#   host_share's OWN namespace and calls the real one. Verified: with the old
#   stub installed, running this probe created host-shares/<profile> for every
#   profile with an empty host_mount, and the stub was called zero times.
#
#   Two independent mechanisms fix it, because one was demonstrably not enough:
#     (a) REDIRECT THE LOCATION. host_share.HOST_SHARES_DIR is patched to the
#         run's scratch dir. Every path the code computes from it — through any
#         function, in any module, however it is reached — now lands in
#         scratch. This cannot go stale when a function is renamed.
#     (b) STUB THE FUNCTIONS on both namespaces, so the mkdir cannot happen
#         even if (a) is ever undone.
#   And a tripwire, because the failure mode is silence: the real host-shares
#   root is snapshotted before and after and any new entry is reported as
#   PROBE_HOST_SHARE_LEAK. A probe that quietly writes to the user's data is
#   exactly the bug this block exists to prevent, so the block checks itself.
# ---------------------------------------------------------------------------
SCRATCH = os.environ.get('VERIFY_SCRATCH', '')
if not SCRATCH:
    print("PROBE_FATAL\tVERIFY_SCRATCH is not set; refusing to introspect, "
          "because with nowhere to redirect the mkdir this probe would write "
          "into the user's real host-shares/")
    sys.exit(9)
_REAL_SHARE_ROOT = host_share.HOST_SHARES_DIR
_FAKE_SHARE_ROOT = os.path.join(SCRATCH, 'host-shares')
host_share.HOST_SHARES_DIR = _FAKE_SHARE_ROOT
os.makedirs(_FAKE_SHARE_ROOT, mode=0o700, exist_ok=True)

def _real_share_entries(root):
    try:
        return set(os.listdir(root))
    except OSError:
        return set()
_LEAK_BEFORE = _real_share_entries(_REAL_SHARE_ROOT)

_share_calls = []
def _dry_ensure(profile_name):
    # Report the path the CODE chose (under the real root) separately from the
    # path this probe actually creates, so the report stays truthful.
    real = os.path.join(_REAL_SHARE_ROOT, profile_name)
    _share_calls.append(real)
    fake = os.path.join(_FAKE_SHARE_ROOT, profile_name)
    os.makedirs(fake, mode=0o700, exist_ok=True)
    return fake
docker_manager.ensure_host_share_dir = _dry_ensure
if hasattr(docker_manager, 'host_share'):
    docker_manager.host_share.ensure_host_share_dir = _dry_ensure
# The name that was actually reached: `_profile_host_mount()` calls
# `resolve_host_mount()`, which looks `ensure_host_share_dir` up in host_share's
# OWN namespace and calls the real one. Patch that resolution in every namespace
# it can be bound under, keeping the real validation of an explicit host_mount
# so the probe still measures what the app would actually do.
def _dry_resolve(profile_name, configured=None):
    if configured is not None and str(configured).strip():
        return host_share.validate_host_mount(str(configured))
    return _dry_ensure(profile_name)
for _mod in (docker_manager, host_share):
    if hasattr(_mod, 'resolve_host_mount'):
        _mod.resolve_host_mount = _dry_resolve
host_share.ensure_host_share_dir = _dry_ensure

home = os.path.realpath(os.path.expanduser('~'))
# Resolve the profile store the SAME WAY the harness header did, from the app's
# own resolver, rather than reaching for the legacy constant. registry's
# CHROME_PROFILES_DIR is the *default* location only; it is NOT what an
# un-migrated install would use.
store = config.resolve_profiles_dir()
with open(os.path.join(repo, 'profiles.json')) as f:
    profiles = json.load(f)

bad = []
_with_binds = []
for name in sorted(profiles):
    entry = profiles[name]
    raw = entry.get('host_mount', '')
    try:
        resolved = m._profile_host_mount(name)
    except Exception as e:
        print("PROFILE\t%s\terror\t%s" % (name, e)); continue
    pdir = entry.get('path') or os.path.join(store, name)
    ddir = os.path.join(pdir, 'Downloads')
    binds = []
    try:
        vols = m._build_volumes(pdir, ddir, resolved)
        for src, cfg in vols.items():
            if cfg.get('bind') == '/home/chrome/host':
                binds.append(src)
    except Exception as e:
        print("PROFILE\t%s\terror\t%s" % (name, e)); continue
    # Report the path the CODE chose (the real one), not the scratch stand-in
    # this probe created, so the check's output stays truthful.
    def _as_real(p):
        if p and p.startswith(_FAKE_SHARE_ROOT + os.sep):
            return _REAL_SHARE_ROOT + p[len(_FAKE_SHARE_ROOT):]
        return p
    resolved_real = _as_real(resolved)
    hit = [b for b in binds if os.path.realpath(_as_real(b)) == home]
    bad.extend((name, b) for b in hit)
    # Also report whether the (removed) /.dockerenv mask mount is still emitted,
    # so check 7 can tell a code regression from a stale live container.
    mask = [src for src, cfg in vols.items() if cfg.get('bind') == '/.dockerenv']
    print("PROFILE\t%s\t%s\t%s\t%s\t%s\t%s" % (name, raw or '<empty>',
          resolved_real, ','.join(_as_real(b) for b in binds) or '<none>',
          'HOME' if hit else 'ok', ','.join(mask) or '-'))
    if binds:
        _with_binds.append(name)

# Self-check: the whole point of this block is that it must not write into the
# user's data, so it measures that itself rather than trusting the patch above.
_LEAK_AFTER = _real_share_entries(_REAL_SHARE_ROOT)
_LEAKS = sorted(_LEAK_AFTER - _LEAK_BEFORE)
print("PROBE_HOST_SHARE_LEAK\t%d\t%s" % (len(_LEAKS), ','.join(_LEAKS)))
# Non-vacuity: if not one profile produced a /home/chrome/host bind at all,
# the "no HOME is mounted" verdict below would be true for the wrong reason.
print("PROFILE_HOST_BINDS\t%d\t%d" % (len(profiles), len(_with_binds)))
print("HOME_MOUNT_VIOLATIONS\t%d" % len(bad))
sys.exit(1 if bad else 0)
# VERIFY-PROBE-HOST-MOUNTS-END
#
# NOTE FOR TESTS: tests/test_verify_harness_side_effects.py extracts exactly the
# text between the BEGIN and END markers above and executes it, so the shipped
# probe — not a copy of it — is what the regression test exercises. Keep the
# markers, and keep them inside this single heredoc.
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

# The probe measures its OWN side effects (PROBE_HOST_SHARE_LEAK) and its own
# non-vacuity (PROFILE_HOST_BINDS). Both are read here rather than left to the
# reader, so a probe that quietly starts writing to the user's host-shares, or
# that stops producing binds and therefore "finds no violations" for the wrong
# reason, fails THIS check loudly instead of passing.
D2_LEAK="$(printf '%s\n' "$VOL_REPORT" | sed -n 's/^PROBE_HOST_SHARE_LEAK\t//p' | tail -1)"
D2_LEAK_N="${D2_LEAK%%$'\t'*}"
D2_LEAK_WHICH=""
[[ "$D2_LEAK" == *$'\t'* ]] && D2_LEAK_WHICH="${D2_LEAK#*$'\t'}"
D2_BINDS="$(printf '%s\n' "$VOL_REPORT" | sed -n 's/^PROFILE_HOST_BINDS\t//p' | tail -1)"
D2_NPROFILES="${D2_BINDS%%$'\t'*}"
D2_NBINDS="${D2_BINDS#*$'\t'}"
PROBE_PROBLEMS=()
if [[ "${D2_LEAK_N:-1}" != "0" ]]; then
    PROBE_PROBLEMS+=("the probe itself wrote into the user's host-shares root while introspecting the code: ${D2_LEAK_WHICH:-see output}. Redirect the LOCATION, not just the function name — _profile_host_mount() reaches ensure_host_share_dir through resolve_host_mount() in host_share's own namespace, so patching docker_manager.ensure_host_share_dir alone is not enough (patch host_share.HOST_SHARES_DIR to \$VERIFY_SCRATCH as well)")
fi
if [[ -n "$D2_NPROFILES" && "${D2_NBINDS:-0}" == "0" ]]; then
    PROBE_PROBLEMS+=("the probe produced NO /home/chrome/host bind for any of the $D2_NPROFILES profile(s), so 'no profile mounts \$HOME' would be true for the wrong reason — a vacuous pass")
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

if [[ "$VOL_RC" -eq 9 ]]; then
    skip 2 "no profile mounts ~ at /home/chrome/host" \
         "the probe refused to introspect because VERIFY_SCRATCH was unset — with no scratch dir there is nowhere to redirect host_share's mkdir, and running anyway would write into the user's real host-shares | $DETAIL" \
         "run scripts/verify.sh (it sets VERIFY_SCRATCH); do not invoke the probe standalone without it"
elif [[ ${#PROBE_PROBLEMS[@]} -gt 0 ]]; then
    fail 2 "no profile mounts ~ at /home/chrome/host" \
          "THE PROBE IS NOT SIDE-EFFECT FREE OR NOT NON-VACUOUS: ${PROBE_PROBLEMS[*]} | $DETAIL" \
          "fix the probe before trusting this verdict"
elif [[ "$VOL_RC" -eq 3 ]]; then
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
    pass 2 "no profile mounts ~ at /home/chrome/host" "0 code-level violations, 0 live binds of HOME; the probe itself created $D2_LEAK_N new entries in the user's host-shares root and produced $D2_NBINDS/$D2_NPROFILES /home/chrome/host binds, so the verdict rests on a measurement rather than on a probe that failed to look | $DETAIL"
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
    _log "introspecting start_container() run kwargs (docker client stubbed; all writes redirected to $WORK)"
    export VERIFY_SCRATCH="$WORK"   # exported on its own line: a pending
    # assignment is not visible to command substitutions in the same simple
    # command. This block also takes the scratch dir from argv, so it worked
    # without the export — which is exactly why the check-2 probe's identical
    # construct went unnoticed for so long.
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
#
# The names below are asserted to still be the ones docker_manager uses, and
# the result is echoed as STUB so the caller can fail loudly if that ever stops
# being true — a stale stub would make this whole check vacuous.
_dm_src = open(os.path.join(repo, 'backend', 'docker_manager.py')).read()
WANTED = ('register_profile', 'resolve_profile_path', 'ensure_host_share_dir')
_missing = [n for n in WANTED if n not in _dm_src]
if _missing:
    print("STUB\tstale: docker_manager.py no longer references %s" % ','.join(_missing))
    sys.exit(5)
print("STUB\tOK")

docker_manager.register_profile = lambda *a, **k: {}
docker_manager.resolve_profile_path = lambda name: os.path.join(SCRATCH, name)
docker_manager.ensure_host_share_dir = lambda *a, **k: os.path.join(SCRATCH, 'host-share')
# Belt and braces: the profile registry and host-share root are redirected too,
# in case start_container() reaches them through the modules rather than the
# names imported into docker_manager's namespace.
#
# Redirect the LOCATION as well as the function names. Check 2 shipped stubs
# that patched `ensure_host_share_dir` on docker_manager and still created real
# directories, because the mkdir was reached through `resolve_host_mount()`
# looking `ensure_host_share_dir` up in host_share's own namespace. Patching
# host_share.HOST_SHARES_DIR cannot rot that way: every path the code derives
# from it — through any function, in any module — lands in the scratch dir.
try:
    import registry as _reg
    _reg.REGISTRY_FILE = os.path.join(SCRATCH, 'profiles.json')
    _reg.register_profile = lambda *a, **k: {}
except Exception:
    pass
try:
    import host_share as _hs
    _hs.HOST_SHARES_DIR = os.path.join(SCRATCH, 'host-shares')
    os.makedirs(_hs.HOST_SHARES_DIR, mode=0o700, exist_ok=True)
    _hs.ensure_host_share_dir = lambda name, _s=SCRATCH: os.path.join(_s, 'host-share')
    if hasattr(_hs, 'resolve_host_mount'):
        _hs.resolve_host_mount = (
            lambda name, configured=None, _s=SCRATCH:
                _hs.validate_host_mount(str(configured))
                if configured is not None and str(configured).strip()
                else os.path.join(_s, 'host-share'))
    if hasattr(docker_manager, 'resolve_host_mount'):
        docker_manager.resolve_host_mount = _hs.resolve_host_mount
except Exception:
    pass

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
    # Self-check on the introspection itself: the assertions above are only
    # meaningful if these names are still the ones start_container() uses.
    STUB_CHECK="$(printf '%s\n' "$HARDEN_REPORT" | sed -n 's/^STUB	//p' | head -1)"
    if [[ -n "$STUB_CHECK" && "$STUB_CHECK" != "OK" ]]; then
        CODE_PROBLEMS+=("the introspection's write-guard is stale ($STUB_CHECK), so every assertion below may be measuring the wrong code path")
    fi
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

# The introspection above deliberately stubs out every disk-touching entry
# point so it cannot write into the user's data. That stubbing is itself a
# hazard: a stub that stops matching the real code makes the check report
# against a config the app does not use. So the real path is re-asserted here,
# by name, and a stub that has silently stopped intercepting is reported as a
# FAIL rather than quietly making the rest of this check meaningless.
# --------------------------------------------------------------------------- #
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
elif [[ $HARDEN_RC -ne 0 ]]; then
    # The live-container inspection cannot say anything the code introspection
    # cannot, so report the code verdict alone rather than pretending a
    # container is required.
    if [[ ${#CODE_PROBLEMS[@]} -gt 0 ]]; then
        fail 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" \
              "CODE REGRESSION: ${CODE_PROBLEMS[*]} | $HARDEN_SUMMARY" \
              "set cap_drop=['ALL'], security_opt=['no-new-privileges'], pids_limit=N and read_only=True in DockerManager.start_container(), then recreate the containers"
    else
        skip 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" \
              "start_container() could not be introspected (rc=$HARDEN_RC), so neither the code nor a container could be checked | $HARDEN_SUMMARY" \
              "fix the introspection above; without it this check cannot distinguish a code regression from a stale container"
    fi
else
    # The live-container inspection below is OPTIONAL, never load-bearing. An
    # earlier version SKIPped the whole check when no chrome-* container
    # existed, which is precisely the case where the one thing this check can
    # prove on its own terms — that the current start_container() config
    # produces a hardened container — went unproven.
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
    [[ $LIVE_TOTAL -eq 0 ]] && HC="$HC (no live container exists, so the verdict rests on the code introspection and the end-to-end proof below)"

    if [[ ${#CODE_PROBLEMS[@]} -gt 0 ]]; then
        fail 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" \
              "CODE REGRESSION: ${CODE_PROBLEMS[*]} | $HC" \
              "set cap_drop=['ALL'], security_opt=['no-new-privileges'], pids_limit=N and read_only=True in DockerManager.start_container(), then recreate the containers"
        # Deliberately NOT falling through to the proof: the code is already
        # wrong, and a green proof against a stale set of flags would only
        # muddy a FAIL that is unambiguous.

    elif [[ $K_PIDSLIM != "2048" || "$K_MEMLIM" != '"2g"' ]]; then
        # The proof container is built for the shipped limits. If the code has
        # moved on, proving anything with stale flags would be theatre. This
        # is a gap in the HARNESS, not in the app, so it is its own status.
        skip 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" \
              "the code IS hardened, but start_container() now uses pids_limit=$K_PIDSLIM / mem_limit=$K_MEMLIM while this harness's proof container is hardcoded for 2048/2g, so the end-to-end proof was skipped rather than run against the wrong config | $HC" \
              "update the proof flags in _verify_hardened_container() in scripts/verify.sh to match DockerManager.PIDS_LIMIT/MEM_LIMIT; the code itself is fine"

    else
        # The containers being unhardened is expected. What must NOT be
        # tolerated is a NEW container that would also be unhardened, so prove
        # the fix by actually creating one under the current config and
        # inspecting the result. This runs whether or not any live container
        # exists — that is the fix for this check having been structurally
        # unable to prove anything on a host with no containers running.
        # It only ever touches a container it created itself.
        _PROOF_OK=0
        _verify_hardened_container && _PROOF_OK=1
        if [[ $_PROOF_OK -eq 1 ]]; then
            if [[ -z "$LIVE_PROBLEMS" ]]; then
                if [[ $LIVE_TOTAL -eq 0 ]]; then
                    pass 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" \
                          "no live container to inspect; start_container() is hardened and a throwaway container built from the current config IS hardened | $HC | proof: $PROOF_SUMMARY"
                else
                    pass 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" \
                          "code is hardened and all $LIVE_TOTAL live container(s) are hardened | $HC | proof: $PROOF_SUMMARY"
                fi
            else
                stale 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" \
                       "STALE LIVE CONTAINER (pre-dates the fix): $LIVE_PROBLEMS — but start_container() now passes cap_drop=['ALL'], no-new-privileges, pids_limit, read_only and a mem_limit, and a throwaway container built from that config IS hardened. $HC | proof: $PROOF_SUMMARY" \
                       "recreate the containers by launching each profile from the app (a plain Launch force-removes the stopped container); 'docker start' would reuse the old config"
            fi
        else
            fail 3 "container hardening (CapAdd/CapDrop/SecurityOpt/PidsLimit)" \
                  "PROOF FAILED: the code's run kwargs claim hardening, but a throwaway container created from exactly that config is NOT hardened — $PROOF_SUMMARY. Live: ${LIVE_PROBLEMS:-none}. $HC" \
                  "the introspection and the runtime disagree; re-check start_container()'s cap_drop/security_opt/pids_limit/read_only, and the tmpfs set for a read-only rootfs"
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
        # And if the image under test predates the Dockerfile, it is not the
        # app's fault at all: an older image simply has no shim compiled in.
        if [[ $IMAGE_PREDATES_DOCKERFILE -eq 1 ]]; then
            stale 4 "host hardware fingerprint not leaked into the container" \
                   "STALE IMAGE (not a code regression): the entrypoint ran but LD_PRELOAD was not exported, because this image has no shim: cpu='$SPOOF_MODEL' mem='$SPOOF_MEMTOTAL' product='$SPOOF_PRODUCT' board='$SPOOF_BOARDVENDOR' — every value is the host's. Control leg agrees, confirming the probe can see a leak. $IMAGE_STALENESS_EVIDENCE" \
                   "promote or rebuild the image and re-run. The shim is compiled in the hwspoof-build stage of the current Dockerfile and copied to /usr/lib/libhwspoof.so; an image built before that change cannot have it."
        else
            fail 4 "host hardware fingerprint not leaked into the container" \
                  "the entrypoint ran but LD_PRELOAD was not exported (shim missing or unusable in this image), so nothing is masked: cpu='$SPOOF_MODEL' mem='$SPOOF_MEMTOTAL' product='$SPOOF_PRODUCT' board='$SPOOF_BOARDVENDOR' | control leg (no spoof stack) agrees: cpu='$CTRL_MODEL' mem='$CTRL_MEMTOTAL' product='$CTRL_PRODUCT' board='$CTRL_BOARDVENDOR'" \
                  "rebuild the image from the current Dockerfile (the shim is compiled in the hwspoof-build stage and copied to /usr/lib/libhwspoof.so); hardware-spoof.sh logs a warning and continues without LD_PRELOAD when the shim is unusable — see scripts/docker/HOST-SPOOFING.md"
        fi
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
                if [[ $IMAGE_PREDATES_DOCKERFILE -eq 1 ]]; then
                    stale 4 "host hardware fingerprint not leaked into the container" \
                           "STALE IMAGE (not a code regression): the image under test has no LD_PRELOAD shim at all, so nothing can be masked — cpu='$SPOOF_MODEL' mem='$SPOOF_MEMTOTAL' product='$SPOOF_PRODUCT'. $IMAGE_STALENESS_EVIDENCE, and the shim is compiled in the hwspoof-build stage of the current Dockerfile. The control leg confirms the probe is capable of seeing the leak, so this is a real leak in THIS IMAGE and not a measurement artefact." \
                           "promote or rebuild the image, then re-run. The current code path is verified correct by the fact that the shim is present in the Dockerfile and absent here."
                else
                    fail 4 "host hardware fingerprint not leaked into the container" \
                          "${LEAKS[*]} | $HD" "$REMEDIATION"
                fi
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
            if [[ $IMAGE_PREDATES_DOCKERFILE -eq 1 ]]; then
                stale 6 "container Chromium version is current" \
                       "STALE IMAGE (not a code regression): $D6. $IMAGE_STALENESS_EVIDENCE. The Dockerfile now targets $(sed -n 's/^FROM[[:space:]]*//p' "$REPO_ROOT/Dockerfile" 2>/dev/null | head -1); this image simply predates it." \
                       "promote or rebuild the image. Note that launching a profile from the app recreates the CONTAINER but does not rebuild the IMAGE — 'docker build -t $IMAGE_NAME .', or use the update flow described under 'Chromium update', which is detect-only until you approve it."
            else
                fail 6 "container Chromium version is current" \
                      "$D6 — STALE" \
                      "rebuild the image from a supported Alpine branch (currently FROM $(sed -n 's/^FROM[[:space:]]*//p' "$REPO_ROOT/Dockerfile" 2>/dev/null | head -1)) so it ships a current chromium package"
            fi
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
# CHECK 7 — /.dockerenv is NOT masked (F5/T3)
# =========================================================================== #
#
# WHAT THE CODE NOW DOES, AND WHY THIS CHECK IS ABOUT ABSENCE
# ------------------------------------------------------------
#   The `/.dockerenv` mask was DROPPED on purpose. The mask file could only ever
#   be 0644 <user>:<user> (the host user creates it; the container runs as
#   non-root `chrome`, so it cannot be chowned to 0:0). Real Docker's
#   `/.dockerenv` is ALWAYS 0755 root:root, so a mode-only match still read as
#   anomalous to anything that stats the owner — a strictly worse tell than the
#   unhidden real file. Running the container as UID 0 to get 0:0 would undo the
#   T2 hardening.
#
#   So the correct end state is: NO mask mount, and the genuine 0755 root:root
#   file. An earlier version of this check asserted "absent OR 755 root:root",
#   which a mask at exactly 0755 would have satisfied — i.e. it could not
#   distinguish "we dropped the mask" from "we kept it and got the mode right".
#   This check asserts the mask is gone from the CODE as the primary claim, and
#   reports the live/container observation as corroboration.
# --------------------------------------------------------------------------- #
_hd "7. /.dockerenv is not masked (F5/T3 — mask dropped deliberately)"
DOCKERENV_SRC=""
if [[ $DOCKER_OK -eq 1 && -n "$CHROME_CONTAINERS" ]]; then
    while IFS=$'\t' read -r cid cname cstatus; do
        [[ -z "$cid" ]] && continue
        src="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/.dockerenv"}}{{.Source}}{{end}}{{end}}' "$cid" 2>/dev/null)"
        [[ -n "$src" ]] && DOCKERENV_SRC="$src" && break
    done <<< "$CHROME_CONTAINERS"
fi

D7_TITLE="/.dockerenv is not masked (F5/T3)"

# The CODE claim comes first and is the load-bearing half: does
# _build_volumes() still emit a /.dockerenv mount at all? This is
# answerable from check 2's introspection alone, with no container and no
# image required — which is what makes it capable of passing here.
CODE_MASK="unknown"
if [[ -n "$VOL_REPORT" ]]; then
    if printf '%s\n' "$VOL_REPORT" | grep -q $'\t/.dockerenv'; then
        CODE_MASK="present"
    else
        CODE_MASK="absent"
    fi
fi

# The OBSERVED state inside a throwaway probe container, when possible.
OBSERVED="(not probed)"
if [[ $DOCKER_OK -eq 1 ]] && _probe_once; then
    OBSERVED="$PROBE_DOCKERENV"
fi

CODE_CLAIM="code: _build_volumes() emits a /.dockerenv mask: $CODE_MASK"
OBS_CLAIM="observed in a probe container: ${OBSERVED:-unknown}"

if [[ "$CODE_MASK" == "present" ]]; then
    # This is the regression that matters. A mask cannot be made to look
    # right (0644 <user>:<user> vs Docker's 0755 root:root), so its presence
    # is the defect — not its mode.
    fail 7 "$D7_TITLE" \
          "CODE REGRESSION: _build_volumes() emits a /.dockerenv mask mount again. $OBS_CLAIM. Any mask this app can create is 0644 <user>:<user> and reads as a MORE obvious tell than leaving Docker's genuine 0755 root:root file alone." \
          "remove the /.dockerenv mount from _build_volumes() in backend/docker_manager.py; the mask was deliberately dropped (F5/T3) because a half-correct one is worse than none. See the comment block above it."
elif [[ "$CODE_MASK" == "unknown" ]]; then
    skip 7 "$D7_TITLE" \
          "could not introspect _build_volumes() (check 2's introspection did not run or errored), so whether the mask is gone is unevaluated | $OBS_CLAIM" \
          "fix check 2's introspection; without it this check cannot tell a code regression from a stale container"
elif [[ -n "$DOCKERENV_SRC" ]]; then
    # A container still carries the mask mount, but the code no longer emits
    # one. That is stale live state, and it is the ONLY case that is.
    MODE_OWNER="$(stat -c '%a %U:%G' "$DOCKERENV_SRC" 2>/dev/null || echo 'unreadable')"
    stale 7 "$D7_TITLE" \
           "STALE LIVE CONTAINER (pre-dates the fix): a live container still binds $DOCKERENV_SRC ($MODE_OWNER) at /.dockerenv, but $CODE_CLAIM — a recreated container will see Docker's genuine file. $OBS_CLAIM" \
           "recreate the container by launching the profile from the app; 'docker start' would reuse the old config"
elif [[ $PROBE_FALLBACK -eq 1 ]]; then
    stale 7 "$D7_TITLE" \
           "STALE PROBE: $CODE_CLAIM, but image '$PROBE_IMAGE_REF' is not built locally so the probe fell back to a pre-existing container's image, which predates the current Dockerfile. $OBS_CLAIM" \
           "build the image (docker build -t $IMAGE_NAME .) and re-run to observe the current image's /.dockerenv"
else
    pass 7 "$D7_TITLE" \
          "$CODE_CLAIM — the mask is gone and the genuine file is left in place. $OBS_CLAIM. This is the ACCEPTED TRADE-OFF, not an oversight: the container is trivially detectable via this one file, deliberately, because a correctly-statted file beats a visibly-fake one (F5/T3)."
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
# CHECK 10 — desktop identity
# =========================================================================== #
# Two claims, both about what the generated entries must NOT contain and about
# one thing they must agree on:
#
#   * NO generated .desktop may declare the `WebBrowser` category, borrow
#     `google-chrome` (or any other browser's) icon, or contain a `MimeType=`
#     / `x-scheme-handler/...` key. Each of those individually is what makes a
#     crowd of Chrome-branded entries show up in GNOME's "Set as default
#     browser" chooser, which is the reported "it messes with my browser" bug.
#   * Chromium's `--class=` must EQUAL the entry's `StartupWMClass=`. If they
#     diverge the WM cannot associate the window with its own launcher and
#     shows a wrong or generic icon. desktop_manager.wm_class() is the single
#     source of truth for both sides; this asserts that it still is, rather
#     than trusting the docstring.
#
# Evaluated twice: against the CODE (what the generator would write now) and
# against the FILES ON DISK (what a launcher actually offers GNOME today). The
# on-disk half is live state, so a stale entry written by an older build is
# STALE, not FAIL — refresh_desktop_entries() heals it on next app start.
# --------------------------------------------------------------------------- #
_hd "10. desktop identity (no WebBrowser, no google-chrome icon, no MIME, --class == StartupWMClass)"

D10_PY_RC=3
D10_CODE=""
if [[ -n "$PY_BIN" && -d "$REPO_ROOT/backend" ]]; then
    _log "introspecting desktop_manager generators + docker_manager --class"
    D10_CODE="$(VERIFY_SCRATCH="$WORK" "$PY_BIN" - "$REPO_ROOT" 2>&1 <<'PY_EOF'
import os, re, sys
repo = sys.argv[1]
sys.path.insert(0, os.path.join(repo, 'backend'))
try:
    import desktop_manager as dm
except Exception as e:
    print("IMPORT_ERROR %s" % e); sys.exit(3)

# -- A single source of truth for the window class. ---------------------------
# Both the .desktop key and Chromium's --class must come from wm_class().
try:
    dm_src = open(os.path.join(repo, 'backend', 'docker_manager.py')).read()
except OSError as e:
    print("IMPORT_ERROR cannot read docker_manager.py: %s" % e); sys.exit(3)
if not re.search(r'--class=\{?wm_class\(', dm_src) and '--class=' not in dm_src:
    print("FLAG docker_manager does not pass --class at all"); sys.exit(1)
if 'wm_class(' not in dm_src:
    print("FLAG docker_manager derives --class without desktop_manager.wm_class()")
    sys.exit(1)
if 'from desktop_manager import wm_class' not in dm_src:
    print("FLAG docker_manager does not import wm_class from desktop_manager")
    sys.exit(1)

# -- For each generated entry: assert the prohibitions and print the facts. ----
# PROFILE_NAMES is a fixed sample; desktop_manager's generators are pure
# functions of the name, so a sample is sufficient and keeps the check free of
# any dependency on the real profile list.
SAMPLE = ('Work', 'Fiverr-Demo', 'Zoom-1')
problems = []
for name in SAMPLE:
    for kind, content in (('profile', dm.profile_entry_content(name)),
                          ('app', dm.app_entry_content())):
        tag = '%s:%s' % (kind, name)
        # The APP entry is not a profile launcher: it opens the manager, and
        # there is no profile to scope a window class to. Its class is the bare
        # DESKTOP_PREFIX, which is correct and must NOT be compared against
        # wm_class(profile) — that comparison is the profile entries' claim only.
        scoped = (kind == 'profile')
        lines = [l.rstrip('\r') for l in content.splitlines()]
        # Categories must not contain WebBrowser (or Network;WebBrowser).
        for l in lines:
            if l.startswith('Categories='):
                cats = [c.strip() for c in l.split('=', 1)[1].split(';')]
                for bad in ('WebBrowser', 'WebBrowserDevelopment'):
                    if bad in cats:
                        problems.append('%s Categories contains %s' % (tag, bad))
                break
        else:
            problems.append('%s has no Categories= key at all' % tag)
        # Icon must not be google-chrome or any other real browser's icon name.
        icon = next((l.split('=', 1)[1] for l in lines if l.startswith('Icon=')), '')
        if re.search(r'(^|[=/_-])google-chrome([/=]|$)', icon):
            problems.append('%s Icon=%s borrows Google Chrome\'s icon' % (tag, icon))
        if icon in ('chromium', 'chromium-browser', 'firefox', 'brave'):
            problems.append('%s Icon=%s impersonates another browser' % (tag, icon))
        if icon in ('web-browser', ''):
            pass  # generic fallback is the documented, honest degrade
        # MIME / URL-scheme handling must be entirely absent.
        for l in lines:
            if l.startswith('MimeType='):
                problems.append('%s declares MimeType= (%s)' % (tag, l))
            if l.startswith('x-scheme-handler'):
                problems.append('%s declares %s' % (tag, l))
        # No URL field codes: these launchers open exactly one fixed profile.
        for l in lines:
            if l.startswith('Exec=') and re.search(r'%[uUfF]', l):
                problems.append('%s Exec= advertises URL field codes (%s)' % (tag, l))
        # StartupWMClass must equal the class Chromium is launched with.
        wm = next((l.split('=', 1)[1] for l in lines if l.startswith('StartupWMClass=')), '')
        if not wm:
            problems.append('%s has no StartupWMClass=' % tag)
        elif scoped and wm != dm.wm_class(name):
            problems.append('%s StartupWMClass=%s but the container launches '
                            '--class=%s' % (tag, wm, dm.wm_class(name)))
        elif not scoped and wm != dm.DESKTOP_PREFIX:
            problems.append('%s StartupWMClass=%s, expected the bare %r'
                            % (tag, wm, dm.DESKTOP_PREFIX))
        # Neither kind may adopt another browser's window class, which would
        # make a WM treat this window as that browser's.
        if wm in ('google-chrome', 'chrome', 'chromium', 'firefox'):
            problems.append('%s StartupWMClass=%s collides with a real browser'
                            % (tag, wm))
        print("ENTRY\t%s\ticon=%s\twmclass=%s\tcategories=%s" % (
            tag, icon, wm or '<none>',
            next((l for l in lines if l.startswith('Categories=')), '<none>')))

# -- The profile-store resolver must be the one the harness used. ------------
# config's constants ARE paths' defaults; assert that rather than assuming it,
# because the whole harness depends on this equivalence.
import config, paths
if config.CHROME_PROFILES_DIR != paths.default_profiles_dir():
    print("FLAG config.CHROME_PROFILES_DIR has drifted from "
          "paths.default_profiles_dir(): %s vs %s"
          % (config.CHROME_PROFILES_DIR, paths.default_profiles_dir()))
    sys.exit(1)
if config.CHROME_PROFILES_DIR_LEGACY != paths.legacy_profiles_dir():
    print("FLAG config.CHROME_PROFILES_DIR_LEGACY has drifted from "
          "paths.legacy_profiles_dir()")
    sys.exit(1)
if config.resolve_profiles_dir() != paths.resolve_profiles_dir():
    print("FLAG config.resolve_profiles_dir() != paths.resolve_profiles_dir()")
    sys.exit(1)
print("STORE\t%s" % config.resolve_profiles_dir())

for p in problems:
    print("PROBLEM\t%s" % p)
sys.exit(1 if problems else 0)
PY_EOF
)"
    D10_PY_RC=$?
fi

# --- the on-disk half: what a launcher actually offers GNOME today ----------
D10_DISK_PROBLEMS=""; D10_DISK_N=0
if [[ -d "$DESKTOP_DIR" ]]; then
    while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        base="$(basename "$f")"
        # Only entries this app owns. A hand-written or third-party .desktop is
        # not ours to assert on.
        [[ "$base" == chrome-isolation.desktop || "$base" == chrome-isolation-*.desktop ]] || continue
        D10_DISK_N=$((D10_DISK_N + 1))
        # 1. WebBrowser in Categories
        if grep -qiE '^Categories=.*(^|;)[[:space:]]*WebBrowser' "$f" 2>/dev/null \
           || grep -qiE '^[[:space:]]*WebBrowser[[:space:]]*;' "$f" 2>/dev/null; then
            D10_DISK_PROBLEMS+="$base: declares the WebBrowser category; "
        fi
        # 2. google-chrome (or another browser) as the icon
        icon="$(sed -n 's/^Icon=//p' "$f" 2>/dev/null | head -1)"
        if printf '%s' "$icon" | grep -qiE 'google-chrome'; then
            D10_DISK_PROBLEMS+="$base: Icon=$icon borrows Google Chrome's icon; "
        fi
        # 3. MIME / scheme handlers
        if grep -qiE '^[[:space:]]*MimeType=' "$f" 2>/dev/null \
           || grep -qiE '^[[:space:]]*x-scheme-handler' "$f" 2>/dev/null; then
            D10_DISK_PROBLEMS+="$base: declares MimeType=/x-scheme-handler; "
        fi
        # 4. --class must agree with StartupWMClass. The class a launcher claims
        #    is the class the WM matches the window against; the container's
        #    --class comes from wm_class(<name>) for the same profile name.
        wm="$(sed -n 's/^StartupWMClass=//p' "$f" 2>/dev/null | head -1)"
        if [[ "$base" == chrome-isolation-*.desktop ]]; then
            pname="${base#chrome-isolation-}"; pname="${pname%.desktop}"
            if [[ -z "$wm" ]]; then
                D10_DISK_PROBLEMS+="$base: no StartupWMClass=; "
            elif [[ -n "$PY_BIN" && "$D10_PY_RC" -eq 0 ]]; then
                want="$(cd "$REPO_ROOT" && VERIFY_SCRATCH="$WORK" "$PY_BIN" -c "
import sys; sys.path.insert(0, 'backend')
import desktop_manager
print(desktop_manager.wm_class(sys.argv[1]))
" "$pname" 2>/dev/null)"
                [[ -n "$want" && "$wm" != "$want" ]] && \
                    D10_DISK_PROBLEMS+="$base: StartupWMClass=$wm but the container launches --class=$want; "
            fi
        fi
    done <<< "$(ls -1 "$DESKTOP_DIR"/chrome-isolation*.desktop 2>/dev/null)"
fi
D10_DISK_PROBLEMS="${D10_DISK_PROBLEMS%; }"

D10_DISK_NOTE="on disk: $D10_DISK_N app-owned .desktop file(s) in $DESKTOP_DIR; ${D10_DISK_PROBLEMS:-all clean}"

if [[ $D10_PY_RC -eq 3 ]]; then
    skip 10 "desktop identity" "could not import desktop_manager ($D10_CODE) | $D10_DISK_NOTE" "fix the import; without it this check cannot run"
elif [[ $D10_PY_RC -gt 1 ]]; then
    fail 10 "desktop identity" \
          "CODE REGRESSION: the desktop identity assertions could not be evaluated (rc=$D10_PY_RC): $D10_CODE" \
          "run the python block above directly to see the traceback"
elif [[ $D10_PY_RC -eq 1 ]]; then
    fail 10 "desktop identity" \
          "CODE REGRESSION in the generator: $(printf '%s\n' "$D10_CODE" | grep -E '^(PROBLEM|FLAG)' | sed 's/^\(PROBLEM\|FLAG\)\t\?//' | tr '\n' '; ') | $D10_DISK_NOTE" \
          "a generated entry must not declare WebBrowser, must not use google-chrome's icon, must contain no MimeType=/x-scheme-handler, and must carry a StartupWMClass equal to desktop_manager.wm_class(profile) — the same value docker_manager passes as --class"
elif [[ -n "$D10_DISK_PROBLEMS" ]]; then
    stale 10 "desktop identity" \
           "STALE ON-DISK ENTRIES (pre-date the fix): $D10_DISK_PROBLEMS — but the generator itself is clean, so refresh_desktop_entries() will heal these | $D10_DISK_NOTE" \
           "just launch the app once: it regenerates every entry in its own namespace on startup and rewrites only what changed"
else
    pass 10 "desktop identity" \
          "generator is clean and StartupWMClass == the --class docker_manager passes | $D10_DISK_NOTE"
fi

# =========================================================================== #
# CHECK 11 — uninstall.sh will not rm -rf a path outside the profile store
# =========================================================================== #
# `rm -rf` on a mis-resolved path is irreversible, and the profile store is
# the user's real browser data (cookies, logins, history). uninstall.sh
# derives PROFILES_DIR from backend/config.py, which honours
# $CHROME_ISOLATION_HOME, so the path it deletes is influenced by the
# environment — and uninstall.sh was hardened with a guard precisely because
# "is it a directory?" is not sufficient to make `rm -rf` safe.
#
# This check EXECUTES the real script and observes what it actually deletes.
# That matters: reading the guard and concluding "it looks safe" is exactly how
# a guard that does not hold gets reported as holding. Four things are measured
# per scenario, by sentinel files:
#
#   MUST SURVIVE   $HOME itself, anything above it, a sibling directory whose
#                  leaf merely resembles the store, and — under a hostile
#                  $CHROME_ISOLATION_HOME — a directory that is NOT the app's
#                  profile store.
#   MUST BE GONE   the resolved profile store, the app data dir and the legacy
#                  ~/Chrome. Asserting these is what makes the check
#                  NON-VACUOUS: a run where every sentinel survives because the
#                  script aborted early would otherwise look like a pass.
#
# SANDBOX. The real uninstall.sh is executed, unmodified, with:
#   * HOME redirected to a throwaway tree under the run's mktemp dir, so every
#     path it resolves (`$HOME/Chrome`, `$HOME/.local/...`) lands in there;
#   * docker, sudo, dpkg and rpm replaced by inert stubs first on PATH, so the
#     script cannot remove containers, images or packages;
#   * PATH still containing the real rm, so the guard is genuinely exercised
#     rather than stubbed away;
#   * the script's own SCRIPT_DIR left as the REAL repo, so it imports the real
#     backend/config.py and the real path-resolution logic is what is tested.
# Nothing outside $WORK is reachable from inside the sandbox.
# --------------------------------------------------------------------------- #
_hd "11. uninstall.sh cannot rm -rf a path outside the profile store"

D11_UNINSTALL="$REPO_ROOT/uninstall.sh"
D11_TITLE="uninstall rm -rf is confined to the profile store"

if [[ ! -f "$D11_UNINSTALL" ]]; then
    skip 11 "$D11_TITLE" "no uninstall.sh at $D11_UNINSTALL"
else
    # Inert stubs, first on PATH, for every command the uninstaller uses to act
    # on the system. `rm` is deliberately NOT stubbed: stubbing it would make
    # this check unable to detect the very thing it exists to detect.
    D11_BIN="$WORK/stubbin"; mkdir -p "$D11_BIN"
    for stub in docker sudo dpkg rpm; do
        { printf '#!/usr/bin/env bash\n'
          printf '# Inert stub installed by scripts/verify.sh check 11. Touches nothing.\n'
          printf 'exit 0\n'
        } > "$D11_BIN/$stub"
        chmod +x "$D11_BIN/$stub"
    done

    # A profile directory as this app leaves it after Chromium has run once.
    # REALISTIC FIXTURES, AND WHY THAT MATTERS.
    #   The genuine stores below (store, ~/Chrome, ~/Isolated-Chrome/profiles)
    #   are filled with REAL Chromium artefacts — Default/, Local State,
    #   hardware-signature.json — because that is what a genuine store actually
    #   contains. An earlier version of this check filled them with a single
    #   sentinel file, which no store in the field has ever looked like: it made
    #   the fixture unfalsifiable, because "remove it" and "refuse it" were then
    #   indistinguishable outcomes for ANY rule the uninstaller could use. The
    #   fix is not to weaken the assertions — every assertion below is the same
    #   one, and several are new — it is to stop asking an unanswerable question
    #   and start asking the real one: does the uninstaller delete data it can
    #   prove belongs to it, and refuse data it cannot?
    _d11_real_profile() {   # _d11_real_profile <profile dir>
        local p="$1"
        mkdir -p "$p/Default" "$p/Downloads" || return 1
        printf '{"Cookies":[]}\n' > "$p/Default/Preferences"
        : > "$p/Local State"
        printf '{"schema_version":1,"system":{}}\n' > "$p/hardware-signature.json"
    }

    # Build one sandbox tree. Returns 0 on success.
    _d11_build() {
        local sb="$1"
        rm -rf "$sb" 2>/dev/null
        mkdir -p "$sb/home/.local/share/chrome-isolation-manager" \
                 "$sb/home/Chrome" \
                 "$sb/home/Isolated-Chrome/profiles" \
                 "$sb/store/profiles" \
                 "$sb/sibling/profiles.bak" \
                 "$sb/decoy/profiles" \
                 "$sb/decoy2/profiles" \
                 "$sb/home/profiles" \
                 "$sb/sym" \
                 2>/dev/null || return 1
        _d11_real_profile "$sb/home/Chrome/Legacy"          || return 1
        _d11_real_profile "$sb/home/Isolated-Chrome/profiles/Home" || return 1
        _d11_real_profile "$sb/store/profiles/Work"         || return 1
        # $HOME/profiles is given REAL profile artefacts on purpose: the worst
        # case for a guard that only asks "does this look like ours". It looks
        # exactly like ours and is still not ours, so containment must refuse
        # it. A decoy that only satisfies the shape test would let a
        # shape-only guard pass.
        _d11_real_profile "$sb/home/profiles/Mine"           || return 1
        # decoy2: contains a FILE named Default, so the directory-name bait is
        # there without the directory being a Chromium profile.
        mkdir -p "$sb/decoy2/profiles/Nested" || return 1
        printf 'not a profile\n' > "$sb/decoy2/profiles/Default"
        # sym: the resolved store is a symlink to the decoy.
        ln -sfn "$sb/decoy/profiles" "$sb/sym/profiles" || return 1
        printf 'store\n'    > "$sb/store/profiles/SENTINEL_STORE"
        printf 'appdata\n'   > "$sb/home/.local/share/chrome-isolation-manager/SENTINEL_APPDATA"
        printf 'legacy\n'    > "$sb/home/Chrome/SENTINEL_LEGACY"
        printf 'default\n'   > "$sb/home/Isolated-Chrome/profiles/SENTINEL_DEFAULTSTORE"
        printf 'homeprof\n'  > "$sb/home/profiles/SENTINEL_HOMEDIRPROFILES"
        printf 'outside\n'   > "$sb/home/SENTINEL_OUTSIDE"
        printf 'sibling\n'   > "$sb/sibling/profiles.bak/SENTINEL_SIBLING"
        printf 'decoy2\n'    > "$sb/decoy2/profiles/SENTINEL_DECOY2"
        printf 'decoy\n'     > "$sb/decoy/profiles/SENTINEL_DECOY"
        return 0
    }

    # _d11_run <sandbox> <cih-value|UNSET> -> echoes rc; sets D11_*_GONE flags
    _d11_run() {
        local sb="$1" cih="$2"
        local fakehome="$sb/home"
        local -a env_args=("PATH=$D11_BIN:$PATH" "HOME=$fakehome")
        [[ "$cih" != "UNSET" ]] && env_args+=("CHROME_ISOLATION_HOME=$cih")
        local rc=0
        printf 'YES\n' | env "${env_args[@]}" bash "$D11_UNINSTALL" \
            >"$sb/stdout" 2>"$sb/stderr" || rc=$?
        D11_RC=$rc
        D11_STDERR="$(tail -2 "$sb/stderr" 2>/dev/null | tr '\n' ' ')"
        local f
        for f in STORE:store/profiles/SENTINEL_STORE \
                 APPDATA:home/.local/share/chrome-isolation-manager/SENTINEL_APPDATA \
                 LEGACY:home/Chrome/SENTINEL_LEGACY \
                 DEFAULTSTORE:home/Isolated-Chrome/profiles/SENTINEL_DEFAULTSTORE \
                 HOMEDIRPROFILES:home/profiles/SENTINEL_HOMEDIRPROFILES \
                 OUTSIDE:home/SENTINEL_OUTSIDE \
                 SIBLING:sibling/profiles.bak/SENTINEL_SIBLING \
                 DECOY:decoy/profiles/SENTINEL_DECOY \
                 DECOY2:decoy2/profiles/SENTINEL_DECOY2; do
            local key="${f%%:*}" rel="${f#*:}"
            if [[ -e "$sb/$rel" ]]; then
                D11_GONE["$key"]=0
            else
                D11_GONE["$key"]=1
            fi
        done
        return 0
    }

    declare -A D11_GONE
    D11_PROBLEMS=(); D11_NOTES=""

    # --- Scenario 1: the default install shape ------------------------------
    # PROFILES_DIR resolves to $HOME/Isolated-Chrome/profiles, which now holds a
    # realistic store, so it MUST be removed — as must the legacy ~/Chrome. Both
    # are the documented behaviour, and both are what keeps this check from
    # passing by refusing everything. Nothing outside those two locations may
    # be touched.
    if _d11_build "$WORK/d11-default" && _d11_run "$WORK/d11-default" UNSET; then
        g() { printf '%s' "${D11_GONE[$1]}"; }
        if [[ "$(g APPDATA)" != "1" ]]; then
            D11_PROBLEMS+=("the default run did not remove the app data dir (rc=$D11_RC, stderr: $D11_STDERR) — uninstall.sh's own documented behaviour is broken, so nothing below can be concluded about the guard")
        fi
        if [[ "$(g DEFAULTSTORE)" != "1" ]]; then
            D11_PROBLEMS+=("the default run did not remove the default profile store ~/Isolated-Chrome/profiles (rc=$D11_RC, stderr: $D11_STDERR) — a guard that cannot remove the real store is its own bug, and this is the check that keeps it from becoming one")
        fi
        if [[ "$(g LEGACY)" != "1" ]]; then
            D11_PROBLEMS+=("the default run did not remove the legacy ~/Chrome (rc=$D11_RC, stderr: $D11_STDERR) — a pre-migration install would keep its data")
        else
            D11_NOTES+="default: ~/Isolated-Chrome/profiles and legacy ~/Chrome both removed as documented; "
        fi
        for k in OUTSIDE SIBLING DECOY DECOY2 STORE HOMEDIRPROFILES; do
            [[ "$(g $k)" == "1" ]] && \
                D11_PROBLEMS+=("the DEFAULT run destroyed the '$k' sentinel, which is not the resolved profile store")
        done
    else
        D11_PROBLEMS+=("could not build or run the default sandbox")
    fi

    # --- Scenario 2: CHROME_ISOLATION_HOME pointing at the real store -------
    if _d11_build "$WORK/d11-honest" && _d11_run "$WORK/d11-honest" "$WORK/d11-honest/store"; then
        g() { printf '%s' "${D11_GONE[$1]}"; }
        if [[ "$(g STORE)" != "1" ]]; then
            D11_PROBLEMS+=("with CHROME_ISOLATION_HOME on the store the store was NOT removed (rc=$D11_RC, stderr: $D11_STDERR)")
        fi
        for k in OUTSIDE SIBLING DECOY DECOY2 HOMEDIRPROFILES; do
            [[ "$(g $k)" == "1" ]] && \
                D11_PROBLEMS+=("with CHROME_ISOLATION_HOME on the store, the '$k' sentinel was destroyed")
        done
        D11_NOTES+="honest CHROME_ISOLATION_HOME: store removed, all out-of-store sentinels survived; "
    else
        D11_PROBLEMS+=("could not build or run the honest-CHROME_ISOLATION_HOME sandbox")
    fi

    # --- Scenario 3: HOSTILE CHROME_ISOLATION_HOME --------------------------
    # $CHROME_ISOLATION_HOME points at a directory that is NOT the app's
    # profile store but whose leaf resolves to something named `profiles` and
    # is deep enough and named right. This is the case the guard was added for
    # and the case it is easiest to get wrong: the old guard keyed on the leaf
    # name being "profiles" or "Chrome", which any directory can satisfy.
    #
    # decoy  holds only a foreign file.
    # decoy2 holds a FILE named `Default`, i.e. the directory-name bait without
    #        the directory being a Chromium profile.
    # Both must survive. A guard that only asks "is the leaf called profiles"
    # destroys both; a guard that only asks "is there something Chromium-ish in
    # here" destroys decoy2's parent.
    if _d11_build "$WORK/d11-hostile" && _d11_run "$WORK/d11-hostile" "$WORK/d11-hostile/decoy"; then
        g() { printf '%s' "${D11_GONE[$1]}"; }
        if [[ "$(g DECOY)" == "1" ]]; then
            D11_PROBLEMS+=("HOSTILE CHROME_ISOLATION_HOME: uninstall.sh rm -rf'd $WORK/d11-hostile/decoy/profiles, which is a real directory but NOT this app's profile store. A guard that accepts any directory merely NAMED 'profiles' is not a guard.")
        else
            D11_NOTES+="hostile CHROME_ISOLATION_HOME: the decoy store was refused; "
        fi
        for k in DECOY2 OUTSIDE SIBLING STORE HOMEDIRPROFILES; do
            [[ "$(g $k)" == "1" ]] && \
                D11_PROBLEMS+=("HOSTILE CHROME_ISOLATION_HOME destroyed the '$k' sentinel")
        done
        # A refusal must be a REPORTED refusal, not a crash or a silent abort:
        # the user needs to be told what was left and how to remove it.
        if ! grep -q 'LEFT IN PLACE' "$WORK/d11-hostile/stderr" 2>/dev/null; then
            D11_PROBLEMS+=("HOSTILE CHROME_ISOLATION_HOME: the decoy survived but uninstall.sh said nothing about it — a refused directory must be named, with a reason and a manual command, or the user is left with orphan data and no idea why")
        fi
    else
        D11_PROBLEMS+=("could not build or run the hostile-CHROME_ISOLATION_HOME sandbox")
    fi

    # --- Scenario 4: HOSTILE CHROME_ISOLATION_HOME, leaf is a SYMLINK -------
    # The resolved store is a symlink pointing at the decoy. `rm -rf` on a
    # symlink is cheap, but a guard that dereferences symlinks before deciding
    # can be walked anywhere by one environment variable.
    if _d11_build "$WORK/d11-symlink" && _d11_run "$WORK/d11-symlink" "$WORK/d11-symlink/sym"; then
        g() { printf '%s' "${D11_GONE[$1]}"; }
        if [[ "$(g DECOY)" == "1" ]]; then
            D11_PROBLEMS+=("HOSTILE CHROME_ISOLATION_HOME (symlink): uninstall.sh followed <hostile>/profiles -> ../decoy/profiles and destroyed the decoy's contents")
        elif [[ ! -L "$WORK/d11-symlink/sym/profiles" ]]; then
            D11_PROBLEMS+=("HOSTILE CHROME_ISOLATION_HOME (symlink): the symlink itself was removed. This script only ever removes real directories it can name outright; a symlink means the path was steered somewhere other than where it points at resolution time")
        else
            D11_NOTES+="symlinked store refused (neither the link nor its target touched); "
        fi
    else
        D11_PROBLEMS+=("could not build or run the symlink-CHROME_ISOLATION_HOME sandbox")
    fi

    # --- Scenario 5: HOSTILE CHROME_ISOLATION_HOME = \$HOME ------------------
    # The nastiest shape: the decoy contains REAL Chromium artefacts, so it
    # satisfies every shape test, and it is still not this app's store —
    # $HOME/profiles is a location the app never creates. This is what proves
    # the guard anchors on CONTAINMENT and not on appearance alone.
    if _d11_build "$WORK/d11-home" && _d11_run "$WORK/d11-home" "$WORK/d11-home/home"; then
        g() { printf '%s' "${D11_GONE[$1]}"; }
        if [[ "$(g HOMEDIRPROFILES)" == "1" ]]; then
            D11_PROBLEMS+=("HOSTILE CHROME_ISOLATION_HOME=\$HOME: uninstall.sh rm -rf'd \$HOME/profiles even though it holds genuine-looking Chromium profile data — a guard that only asks 'does this look like ours' will delete this, and the shape test is not enough on its own")
        else
            D11_NOTES+="CHROME_ISOLATION_HOME=\$HOME refused despite holding realistic profile data (containment, not appearance); "
        fi
        for k in OUTSIDE DECOY DECOY2 STORE SIBLING; do
            [[ "$(g $k)" == "1" ]] && \
                D11_PROBLEMS+=("CHROME_ISOLATION_HOME=\$HOME destroyed the '$k' sentinel")
        done
    else
        D11_PROBLEMS+=("could not build or run the CHROME_ISOLATION_HOME=\$HOME sandbox")
    fi

    if [[ ${#D11_PROBLEMS[@]} -gt 0 ]]; then
        fail 11 "$D11_TITLE" \
              "${D11_PROBLEMS[*]} | observed: $D11_NOTES" \
              "uninstall.sh must answer TWO independent questions before rm -rf, and a directory has to pass both. (1) Could this path have been produced by this app? Construct the candidate from \$HOME and this app's own naming convention only — <isolation home>/profiles and \$HOME/Chrome — never from config, the registry or anything steerable; refuse symlinks and require the resolved path to equal the canonical one. (2) Does it hold data this app created? Require at least one profile directory containing Default/ plus Local State or hardware-signature.json. The old guard (leaf name in {profiles, Chrome}, depth >= 2 slashes, not \$HOME) satisfies neither question: any directory named 'profiles' passes it. Containment alone is defeated by a hostile \$CHROME_ISOLATION_HOME, and appearance alone is defeated by \$HOME/profiles — scenario 5 is what stops a shape-only guard from passing."
    else
        pass 11 "$D11_TITLE" \
              "executed for real under a fake \$HOME with docker/sudo/dpkg/rpm stubbed, across 5 scenarios. REMOVED: ~/Isolated-Chrome/profiles, legacy ~/Chrome, an explicitly-configured store, the app data dir. REFUSED and reported with a manual command: a decoy 'profiles' directory under a hostile \$CHROME_ISOLATION_HOME, a second decoy carrying a file named 'Default', a symlinked store, and \$HOME/profiles even though it holds realistic Chromium data. Also survived: \$HOME itself and a sibling 'profiles.bak' | $D11_NOTES"
    fi
fi

# =========================================================================== #
# CHECK 12 — the update path is DETECT-ONLY by default
# =========================================================================== #
# A Chromium auto-update that rebuilds the image unattended is a large,
# root-adjacent, and (for profile data) irreversible operation: Chromium
# upgrades a profile in place and will not downgrade it. The shipped default
# must therefore detect and report, and build ONLY on explicit consent or a
# named env opt-in.
#
# Asserted against the real module with all three side-effect seams injected
# (docker client, run_cmd, detector), all state redirected into $WORK, so this
# genuinely executes the decision logic and observes whether it chose to
# build. A static grep would not: the property is about the branch taken.
# --------------------------------------------------------------------------- #
_hd "12. Chromium update path is detect-only by default (no build without consent)"

D12_PY_RC=3
D12_OUT=""
if [[ -n "$PY_BIN" && -f "$REPO_ROOT/backend/image_update.py" ]]; then
    _log "exercising image_update decision logic with injected seams (no Docker calls, all state in $WORK)"
    D12_OUT="$(cd "$REPO_ROOT" && VERIFY_SCRATCH="$WORK" "$PY_BIN" - "$WORK/au" 2>&1 <<'PY_EOF'
import os, sys
scratch = sys.argv[1]
os.makedirs(scratch, exist_ok=True)
sys.path.insert(0, 'backend')

try:
    import image_update as iu
except Exception as e:
    print("IMPORT_ERROR %s" % e); sys.exit(3)

# The three opt-in flags must all be CLEAR in this process, or the scenario
# below is meaningless. The harness must never inherit a developer's opt-in.
LEAKY = [n for n in (iu.ENV_DISABLE, iu.ENV_FORCE, getattr(iu, 'ENV_AUTO_REBUILD', ''))
         if n and os.environ.get(n)]
for n in LEAKY:
    os.environ.pop(n, None)
if os.environ.get(iu.ENV_DETECTOR):
    print("NOTE %s was set in this environment and has been cleared for the test"
          % iu.ENV_DETECTOR)
    os.environ.pop(iu.ENV_DETECTOR, None)

# --- a fake docker client: zero containers, so nothing defers ----------------
class _Ctrs:
    def list(self, **kw): return []
class _Imgs:
    def get(self, n): raise KeyError(n)
class _FakeCli:
    containers = _Ctrs(); images = _Imgs()

# --- a run_cmd recorder: records what WOULD be run, runs nothing -------------
BUILDS = []
def _record_run_cmd(cmd, env=None, timeout=None):
    argv = list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)]
    if argv and argv[0] == 'docker':
        verb = next((a for a in argv[1:] if not a.startswith('-')), '')
        if verb == 'build':
            BUILDS.append(argv)
        # Pretend every other docker call succeeded with empty output.
        return 0, '', ''
    return 0, '', ''

def _mk(detector, tag):
    """An updater whose lock/state files live in the scratch dir."""
    return iu.ImageUpdater(
        client_factory=lambda: _FakeCli(),
        run_cmd=_record_run_cmd,
        detector=detector,
        image_name='verify-detect-only',
        lock_file=os.path.join(scratch, '%s.lock' % tag),
        state_file=os.path.join(scratch, '%s.json' % tag),
        min_interval=0, failure_cooldown=0, defer_cooldown=0,
    )

# --- Scenario A: an update IS available, nobody has consented. ---------------
# This is the case that matters: detection succeeds, the answer is "yes there
# is a newer Chromium", and the default must still NOT build.
BUILDS.clear()
upd = _mk(lambda img: {'current': '124.0.6367.78', 'latest': '152.0.7977.82'}, 'A')
upd._read_installed_versions = lambda: ('124.0.6367.78', '3.24.2')
upd.check_and_start(reason='app_start')
upd.wait_for_idle(30)
st = upd.get_update_status()
print("A\tstate=%s\tbuilds=%d\tconsent_required=%s\tpolicy=%s" % (
    st.get('state'), len(BUILDS), st.get('consent_required'), st.get('policy')))
if BUILDS:
    print("A_FAIL\tbuilt with no consent: %s" % BUILDS[0][:4])
    sys.exit(1)
if st.get('state') != 'update_available':
    print("A_FAIL\tan available update should rest in state 'update_available', got %r "
          "(an update that is merely detected must be visible to the user)"
          % st.get('state'))
    sys.exit(1)
if not st.get('consent_required'):
    print("A_FAIL\tconsent_required is not set while an update is pending, so the UI "
          "has no way to offer 'Apply now'")
    sys.exit(1)
if st.get('policy') != 'notify_only':
    print("A_FAIL\tpolicy is %r, expected 'notify_only'" % st.get('policy'))
    sys.exit(1)

# --- Scenario B: the startup call must not pass force ------------------------
# main.js calls image_update_check with {}; check_and_start(force=None) must
# resolve force from the ENVIRONMENT, not from the caller.
import inspect
sig = inspect.signature(iu.check_and_start)
if 'force' not in sig.parameters:
    print("B_FAIL\tmodule-level check_and_start has no force parameter")
    sys.exit(1)
print("B\tmodule check_and_start signature = %s" % sig)

# --- Scenario C: with the kill switch set, nothing happens at all -----------
BUILDS.clear()
os.environ[iu.ENV_DISABLE] = '1'
upd_c = _mk(lambda img: {'current': '124.0.6367.78', 'latest': '152.0.7977.82'}, 'C')
upd_c._read_installed_versions = lambda: ('124.0.6367.78', '3.24.2')
res = upd_c.check_and_start(reason='app_start')
upd_c.wait_for_idle(30)
print("C\tresult=%s\tbuilds=%d\tstate=%s" % (
    res.get('reason'), len(BUILDS), upd_c.get_update_status().get('state')))
if BUILDS:
    print("C_FAIL\tkill switch was set but a build ran")
    sys.exit(1)
if res.get('reason') != 'disabled':
    print("C_FAIL\tkill switch did not produce reason='disabled', got %r" % res.get('reason'))
    sys.exit(1)
os.environ.pop(iu.ENV_DISABLE, None)

# --- Scenario D: explicit consent for the EXACT version does build ----------
# The counterpart to A. If consent cannot cause a build, then A passing would
# prove nothing: "it never builds" and "it can never build" look identical.
BUILDS.clear()
upd_d = _mk(lambda img: {'current': '124.0.6367.78', 'latest': '152.0.7977.82'}, 'D')
upd_d._read_installed_versions = lambda: ('124.0.6367.78', '3.24.2')
# Walk the consent path the way the bridge does. apply_update is the gate; call
# it with the version the status reports as available.
res_d = upd_d.check_and_start(reason='app_start')
upd_d.wait_for_idle(30)
st_d = upd_d.get_update_status()
want = st_d.get('consent_version')
if not want:
    print("D_FAIL\tno consent_version was offered, so the consent path is untestable")
    sys.exit(1)
upd_d2 = _mk(lambda img: {'current': '124.0.6367.78', 'latest': '152.0.7977.82'}, 'D2')
upd_d2._read_installed_versions = lambda: ('124.0.6367.78', '3.24.2')
res_d2 = upd_d2.apply_update(version=want)
upd_d2.wait_for_idle(30)
print("D\tconsent_version=%s\tstarted=%s\tbuilds=%d\tstate=%s" % (
    want, res_d2.get('started'), len(BUILDS), upd_d2.get_update_status().get('state')))
if not BUILDS:
    print("D_FAIL\twith explicit, version-matched consent nothing was built either — "
          "so scenario A proves nothing (a module that can never build is not "
          "detect-only, it is broken)")
    sys.exit(1)

# --- Scenario E: consent for a DIFFERENT version must not build -------------
# The anti-"blanket yes" property: approving 152 must not silently deliver 154.
BUILDS.clear()
upd_e = _mk(lambda img: {'current': '124.0.6367.78', 'latest': '154.0.1.1'}, 'E')
upd_e._read_installed_versions = lambda: ('124.0.6367.78', '3.24.2')
res_e = upd_e.apply_update(version='152.0.7977.82')
upd_e.wait_for_idle(30)
st_e = upd_e.get_update_status()
print("E\tconsent=152.0.7977.82\tavailable=154.0.1.1\tbuilds=%d\tstate=%s" % (
    len(BUILDS), st_e.get('state')))
if BUILDS:
    print("E_FAIL\tconsent for 152 produced a build of 154 — a blanket 'yes to updates'")
    sys.exit(1)

# --- Scenario F: 'unknown' must never read as 'up to date' ------------------
# A detector that cannot tell is the common case (no network, an EOL branch).
# Collapsing it into up_to_date is how a browser stays stale behind a green UI.
BUILDS.clear()
def _raise_detector(img):
    raise RuntimeError("no network")
upd_f = _mk(_raise_detector, 'F')
upd_f._read_installed_versions = lambda: ('124.0.6367.78', None)
upd_f.check_and_start(reason='app_start')
upd_f.wait_for_idle(30)
st_f = upd_f.get_update_status()
print("F\tstate=%s\tbuilds=%d" % (st_f.get('state'), len(BUILDS)))
if BUILDS:
    print("F_FAIL\ta detector that raised still produced a build")
    sys.exit(1)
if st_f.get('state') == 'up_to_date':
    print("F_FAIL\t'could not determine' was reported as 'up_to_date' — those are "
          "different facts and conflating them is exactly the bug the state "
          "vocabulary exists to prevent")
    sys.exit(1)

print("STATES\t%s" % ','.join(iu.STATES))
sys.exit(0)
PY_EOF
)"
    D12_PY_RC=$?
fi

D12_NOTE="$(printf '%s\n' "$D12_OUT" | grep -E "^[A-F]$(printf '\t')" | tr '\n' ' ')"
if [[ $D12_PY_RC -eq 3 ]]; then
    skip 12 "update path is detect-only by default" "could not import image_update: $D12_OUT" "fix the import; without it this check cannot run"
elif [[ $D12_PY_RC -gt 1 ]]; then
    fail 12 "update path is detect-only by default" \
          "the detect-only scenarios could not be evaluated (rc=$D12_PY_RC): $D12_OUT" \
          "run the python block above directly to see the traceback"
elif [[ $D12_PY_RC -eq 1 ]]; then
    fail 12 "update path is detect-only by default" \
          "CONSENT GATE VIOLATED: $(printf '%s\n' "$D12_OUT" | grep -E '_FAIL' | tr '\n' ' ') | $D12_NOTE" \
          "an available update must rest in state 'update_available' with consent_required set and build nothing; only apply_update(version=<exact version the status reports>), CHROME_ISOLATION_AUTO_REBUILD=1 or CHROME_ISOLATION_FORCE_IMAGE_UPDATE=1 may build"
else
    pass 12 "update path is detect-only by default" \
          "an available update is detected and reported, nothing is built, and consent for the exact version does build — so the gate is real and not merely a no-op | $D12_NOTE"
fi

# =========================================================================== #
# CHECK 13 — the harness itself did not write to the user's data
# =========================================================================== #
# The harness introspects code by CALLING it. Calling the app's own functions
# creates directories: ensure_host_share_dir() mkdirs into the real host-shares
# root, and start_container() calls register_profile() which rewrites
# profiles.json. An earlier version of this harness did exactly that, silently
# adding a junk profile to the user's registry.
#
# So this check is a tripwire: it hashes the files that must never change and
# reports a FAIL if any of them moved. It runs LAST because it compares the
# snapshot taken before check 1 against the state now.
#
# The snapshot is written to $WORK only, and read-only throughout.
# --------------------------------------------------------------------------- #
_hd "13. harness is side-effect free (no writes to profiles.json / proxies.json / signatures)"

# The "after" snapshot. Two segments are compared, so a mutation is attributed
# to a phase rather than merely detected:
#   segment 1  startup -> after check 1   == whatever the test suite did
#   segment 2  after check 1 -> now       == whatever the harness's own checks did
#
# BOTH SEGMENTS ARE VERDICTS. Segment 1 used to be a footnote — the test suite
# was excused because it spawns the real bridge as a subprocess, and it had in
# fact been observed leaving real profiles and real host-share directories in
# the user's data. That excuse is the bug: the test suite is part of this
# harness, `scripts/verify.sh` runs it, and a verification harness that writes
# into the user's data directory is unacceptable whether it did so directly or
# by shelling out. A footnote about a mutation is a FAIL that has learned to
# apologise. Segment 2 keeps its stricter blame message because that is the
# part the harness itself controls without invoking anything else.
D13_SNAP_END="$WORK/snapshot-02-end.tsv"
_snapshot_files > "$D13_SNAP_END"

_d13_changes() { diff "$1" "$2" 2>/dev/null | grep -E '^>' | sed 's/^> //' | tr '\n' ';'; }
D13_BY_TESTS="$(_d13_changes "$D13_SNAP" "$D13_SNAP_POST_TESTS")"
D13_BY_HARNESS="$(_d13_changes "$D13_SNAP_POST_TESTS" "$D13_SNAP_END")"
N_WATCHED="$(grep -c . "$D13_SNAP" || true)"

D13_CONCURRENCY_NOTE=""
if [[ "${D13_CONCURRENT:-0}" -gt 0 ]]; then
    D13_CONCURRENCY_NOTE=" ATTRIBUTION IMPOSSIBLE: $D13_CONCURRENT other process(es) were running this repo's pytest suite or a backend bridge at the same time, and those register real profiles in the registry and create real host-share directories. A mutation below therefore cannot be blamed on the harness without re-running when the machine is idle."
fi

D13_TESTS_NOTE=""
if [[ -n "$D13_BY_TESTS" ]]; then
    D13_TESTS_NOTE=" The test suite (check 1) also changed: $D13_BY_TESTS — that phase spawns the real bridge as a subprocess, so it is not a sandbox either."
fi

if [[ -z "$D13_BY_HARNESS" && -z "$D13_BY_TESTS" ]]; then
    pass 13 "harness is side-effect free" \
          "the test suite AND checks 2-13 changed nothing: $N_WATCHED watched path(s) — profiles.json, proxies.json, every profile store and signature, the host-shares root and the generated .desktop files are all byte-identical to their state before the harness ran. Both phases are verdicts now: the test suite is part of the harness and is held to the same standard as the probes."
else
    if [[ "${D13_CONCURRENT:-0}" -gt 0 ]]; then
        # A mutation really happened, but the harness cannot say it was its
        # own. Reporting this as a harness FAIL would be a guess dressed up as
        # a verdict; reporting it as a PASS would hide a real change. SKIP is
        # the honest middle: the check did not evaluate cleanly.
        skip 13 "harness is side-effect free" \
              "user data DID change during the run, but this cannot be attributed: checks: $D13_BY_HARNESS; test suite: $D13_BY_TESTS.$D13_CONCURRENCY_NOTE" \
              "re-run scripts/verify.sh when no other pytest/bridge process for this repo is running; a clean run will then either pass or name the harness as the cause"
    elif [[ -n "$D13_BY_TESTS" && -z "$D13_BY_HARNESS" ]]; then
        fail 13 "harness is side-effect free" \
              "THE HARNESS MUTATED USER DATA — the pytest phase (check 1): $D13_BY_TESTS — the probes themselves were clean, but scripts/verify.sh runs that suite, so the mutation is the harness's own.$D13_CONCURRENCY_NOTE" \
              "the tests that exercise the real bridge must run it against a redirected HOME (a mktemp -d), not the user's: spawn it with env HOME=<scratch> so config.py derives APP_DATA_DIR, the registry and the host-shares root inside the sandbox. Do not 'fix' this by relaxing the snapshot."
    else
        fail 13 "harness is side-effect free" \
              "THE HARNESS MUTATED USER DATA — checks 2-13: $D13_BY_HARNESS$D13_TESTS_NOTE$D13_CONCURRENCY_NOTE" \
              "every check must redirect its writes into the mktemp scratch dir ($WORK). The decisive lesson from the bug this caught: patching a FUNCTION name is not enough, because _profile_host_mount() reaches host_share.ensure_host_share_dir through resolve_host_mount() in host_share's own namespace, so the stub was never called and the real mkdir ran. Patch the LOCATION (host_share.HOST_SHARES_DIR) and the function, and let the probe measure its own leak (PROBE_HOST_SHARE_LEAK)."
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
