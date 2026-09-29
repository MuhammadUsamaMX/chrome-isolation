#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# uninstall.sh — remove Chrome Isolation Manager and all its data
#
# Removes (in order):
#   1. Profile containers + the isolated-chrome Docker image
#   2. App data: registry, proxy store, machine signatures (~/.local/share/
#      chrome-isolation-manager)
#   3. Launcher, .desktop entry, icon
#   4. Profile data (<isolation home>/profiles, plus the legacy ~/Chrome if
#      that still holds data) — back it up first!
#   5. .deb / .rpm package if installed via a package
#
# Usage: bash uninstall.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HOME_DIR="$HOME"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# =============================================================================
# THE rm -rf GUARD — DESIGN AND WHY
# =============================================================================
# The one irreversible thing this script does is `rm -rf` a directory. The
# previous guard on that operation was:
#
#     skip empty / "/" / non-directory / $HOME / ".."; require the leaf
#     basename to be "profiles" or "Chrome"; require depth */*/*.
#
# That reads like a guard and is not one. Both of its substantive conditions
# are satisfied by *any* directory a stranger cares to name: `mkdir -p
# /tmp/decoy/profiles` is three characters of work, is deep enough, and is
# called `profiles`. Verified against the guard by running this script under a
# hostile `CHROME_ISOLATION_HOME`: it deleted the decoy. A guard that a
# one-line `mkdir` defeats is not a guard, and the data behind it is the user's
# real cookies, logins and history.
#
# So the guard is rebuilt around two independent questions, and a directory
# must answer BOTH before anything is deleted:
#
#   (1) COULD THIS PATH HAVE BEEN PRODUCED BY THIS APP?
#       The candidate is not taken from configuration, a registry file or
#       anything else that a user, a stray environment variable or a corrupted
#       file can steer. It is *constructed*, from $HOME and this app's own
#       naming convention, in exactly the two shapes backend/paths.py can
#       produce:  <isolation home>/profiles  and  $HOME/Chrome. The isolation
#       home is `$CHROME_ISOLATION_HOME` when the user configured it, else
#       `~/Isolated-Chrome`. Every other spelling is discarded.
#       Symlinks are refused outright, and the resolved target must still equal
#       <isolation home>/profiles exactly — so `<hostile>/profiles -> /etc`
#       and `<hostile>/profiles -> $HOME` are both rejected, as is any
#       isolation home that is / , $HOME, or an ancestor of $HOME.
#       This question alone is defeated by a hostile $CHROME_ISOLATION_HOME,
#       which is precisely why question (2) exists.
#
#   (2) DOES THIS DIRECTORY CONTAIN DATA THIS APP CREATED?
#       Containment says where; this says what. The store is accepted only if
#       it actually holds a Chromium profile the app populated — a profile
#       directory containing `Default/` plus at least one of `Local State`
#       (written by Chromium) or `hardware-signature.json` (written only by
#       this app's generator). That is a conjunction no directory satisfies by
#       accident: a decoy has to be purpose-built by someone who has read this
#       file. An empty directory is also accepted, because deleting an empty
#       directory destroys nothing and refusing it would make the uninstaller
#       useless for a fresh install.
#       Everything else is REFUSED, loudly, with the exact path, what was found
#       inside it, and the command to remove it by hand.
#
# WHY NOT JUST ASK FOR CONFIRMATION (the alternative trade-off)
#   The prompt lists exactly what will be removed, profile by profile, before
#   anything is deleted — but it is a single up-front YES, not a second prompt
#   per store. A per-store prompt would be easy to defeat by the exact
#   accident we are guarding against (a script, a pipeline, a `yes` on stdin)
#   and would turn an unattended uninstall into a hang. Confirmation is
#   therefore the *reporting* layer on top of the structural checks, not a
#   substitute for them.
#
# WHY THIS DOES NOT SIMPLY REFUSE EVERYTHING (the failure mode it must avoid)
#   An uninstaller that cannot remove the real store is its own bug: the user
#   is left with orphan data and an app that is half-removed. Tiers 1 and 2
#   above remove the genuine store, including the legacy one, whenever it
#   holds real profile data — which is what every real install looks like,
#   because `Default/` and `Local State` appear the first time a profile is
#   ever launched. tests/test_uninstall_guard.py and check 11 of
#   scripts/verify.sh both assert removal of a realistic store, so "refuse
#   everything" cannot pass: the refusal path is only reachable for a
#   directory that fails a positive ownership test.
# =============================================================================

# --- The two canonical candidate locations ------------------------------------
# Computed the same way backend/paths.py computes them, so this script and the
# app can never disagree about the shape of the path. `cd`+`pwd` normalises
# `.`/`..` and expands `~` without trusting a relative path.
_norm() {  # _norm <path> -> absolute, symlink-resolved, or "" if unusable
    local p="$1" abs
    [[ -n "$p" ]] || return 0
    # paths.py expands `~`; a shell variable is not tilde-expanded, so do it
    # here or the two would disagree about what $CHROME_ISOLATION_HOME means.
    case "$p" in "~") p="$HOME_DIR" ;; "~/"*) p="$HOME_DIR/${p#\~/}" ;; esac
    abs="$(cd -- "$p" 2>/dev/null && pwd -P)" || abs=""
    printf '%s' "$abs"
}

# _norm_nolink — as above but WITHOUT resolving symlinks. Used only to compare
# against config.py, which uses os.path.abspath() and therefore reports the
# symlinked spelling. Comparing a symlink-resolved answer against a
# non-resolving one would make every symlinked store look like a disagreement
# and the store would be dropped from consideration entirely — which is safe,
# but silently, and the user would be told nothing.
_norm_nolink() {
    local p="$1" abs
    [[ -n "$p" ]] || return 0
    case "$p" in "~") p="$HOME_DIR" ;; "~/"*) p="$HOME_DIR/${p#\~/}" ;; esac
    abs="$(cd -- "$p" 2>/dev/null && pwd -L)" || abs=""
    printf '%s' "$abs"
}

ISOLATION_HOME_RAW="${CHROME_ISOLATION_HOME:-$HOME_DIR/Isolated-Chrome}"
ISOLATION_HOME="$(_norm "$ISOLATION_HOME_RAW")"
ISOLATION_HOME_SPELLING="$(_norm_nolink "$ISOLATION_HOME_RAW")"
PROFILES_DIR=""
[[ -n "$ISOLATION_HOME" ]] && PROFILES_DIR="$ISOLATION_HOME/profiles"
# Legacy location, for installs created before the relocation and never
# migrated. Removed only if it actually holds profile data.
HOME_REAL="$(_norm "$HOME_DIR")"
LEGACY_PROFILES_DIR="$HOME_REAL/Chrome"     # empty when $HOME cannot be resolved
APP_DATA="$HOME_DIR/.local/share/chrome-isolation-manager"
LAUNCHER="$HOME_DIR/.local/bin/chrome-isolation"
DESKTOP="$HOME_DIR/.local/share/applications/chrome-isolation.desktop"
ICON="$HOME_DIR/.local/share/icons/hicolor/256x256/apps/chrome-isolation.png"

# Cross-check against the app's own resolver. The app is the source of truth;
# this script is the safety net. If the two disagree, the app's answer is
# DISCARDED rather than trusted, because the whole point of the guard is that
# no configurable value gets to choose what `rm -rf` deletes. Disagreement is
# reported, not silently resolved.
CONFIG_PROFILES_DIR=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    CONFIG_PROFILES_DIR="$(cd "$SCRIPT_DIR" && "$cand" -c '
import os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "backend"))
import config
print(config.CHROME_PROFILES_DIR)
' 2>/dev/null)" || CONFIG_PROFILES_DIR=""
    [[ -n "$CONFIG_PROFILES_DIR" ]] && break
  fi
done
CONFIG_DISAGREEMENT=""
if [[ -n "$CONFIG_PROFILES_DIR" ]]; then
  _cfg_norm="$(_norm_nolink "$CONFIG_PROFILES_DIR")"
  if [[ "$_cfg_norm" != "$ISOLATION_HOME_SPELLING/profiles" && "$_cfg_norm" != "$LEGACY_PROFILES_DIR" ]]; then
    CONFIG_DISAGREEMENT="config.py reports the profile store as '$_cfg_norm', which is neither the canonical new store nor the documented legacy one; ignoring it and removing nothing outside the canonical locations."
    PROFILES_DIR=""
  fi
elif [[ -n "$ISOLATION_HOME" ]]; then
  CONFIG_DISAGREEMENT="could not read backend/config.py; proceeding with the documented locations computed above"
fi

# --- The positive ownership test ---------------------------------------------
# True when $1 (a profile store) holds at least one profile directory that this
# app populated: `Default/` AND (`Local State` OR `hardware-signature.json`).
# Both halves are required; either alone is too weak to establish ownership.
_store_holds_our_profiles() {
    local store="$1" prof
    [[ -d "$store" ]] || return 1
    for prof in "$store"/*/; do
        [[ -d "$prof" ]] || continue
        [[ -d "${prof}Default" ]] || continue
        if [[ -f "${prof}hardware-signature.json" || -f "${prof}Local State" ]]; then
            return 0
        fi
    done
    return 1
}

# True when $1 contains no entries at all (or only dotfiles). Deleting such a
# directory cannot destroy data, so it is not gated on the ownership test.
_dir_is_empty() {
    local d="$1" e
    [[ -d "$d" ]] || return 1
    for e in "$d"/* "$d"/.[!.]* "$d"/..?*; do
        [[ -e "$e" || -L "$e" ]] && return 1
    done
    return 0
}

# Summarise what is inside a store, for the pre-flight listing and for the
# refusal message. Bounded so a store with 5000 profiles cannot flood the
# terminal.
_store_summary() {
    local store="$1" n=0 line out=""
    while IFS= read -r line; do
        n=$((n + 1))
        [[ $n -le 8 ]] && out+="      $(basename -- "$line")/"
    done < <(find "$store" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)
    printf '%s' "$out"
    if [[ $n -gt 8 ]]; then printf '      … and %d more' "$((n - 8))"; fi
    return 0
}

# --- Decide what happens to one candidate ------------------------------------
# Sets D_VERDICT to "remove" or "refuse" and D_WHY to a one-line reason.
D_VERDICT="refuse"; D_WHY=""
_classify_store() {
    local dir="$1" kind="$2"      # kind: "new" | "legacy"
    D_VERDICT="refuse"; D_WHY=""

    # (0) Usable at all.
    if [[ -z "$dir" || "$dir" == "/" ]]; then
        D_WHY="the path is empty or the filesystem root"; return 0
    fi
    if [[ -L "$dir" ]]; then
        D_WHY="it is a symlink; this script only ever removes real directories it can name outright"; return 0
    fi
    if [[ ! -d "$dir" ]]; then
        D_WHY="it does not exist"; return 0
    fi

    # (1) Could this path have been produced by this app?
    local resolved canonical
    resolved="$(_norm "$dir")"
    case "$kind" in
        new)     canonical="$ISOLATION_HOME/profiles" ;;
        legacy)  canonical="$HOME_REAL/Chrome" ;;
    esac
    if [[ -z "$canonical" ]]; then
        D_WHY="the location it would have to equal cannot be resolved on this system"; return 0
    fi
    if [[ "$resolved" != "$canonical" ]]; then
        D_WHY="it resolves to '$resolved', which is not this app's profile store location '$canonical'"; return 0
    fi
    # The store must never be $HOME, an ancestor of it, or inside a system root
    # that no browser profile legitimately lives in.
    if [[ -n "$HOME_REAL" ]]; then
        case "$resolved" in
            "$HOME_REAL") D_WHY="it IS the home directory"; return 0 ;;
            /home|/root|/etc|/usr|/var|/opt|/srv|/bin|/sbin|/lib|/lib64|/proc|/sys|/dev|/boot|/run)
                D_WHY="it is a system root"; return 0 ;;
        esac
        case "$HOME_REAL/" in
            "$resolved/"*) D_WHY="it CONTAINS the home directory ($HOME_REAL)"; return 0 ;;
        esac
    fi
    # The isolation home itself must never be $HOME or an ancestor of it: that
    # is the shape a hostile $CHROME_ISOLATION_HOME=~ tries to produce. It also
    # may not be a system root, or a top-level directory: every location this
    # app has ever used is at least $HOME/<something>/profiles deep, so a
    # shallower answer came from a hand-typed environment variable rather than
    # from an install.
    if [[ -n "$ISOLATION_HOME" && "$kind" == "new" ]]; then
        case "$ISOLATION_HOME" in
            "$HOME_REAL") D_WHY="CHROME_ISOLATION_HOME is the home directory itself"; return 0 ;;
            /|/home|/root|/etc|/usr|/var|/opt|/srv|/bin|/sbin|/lib|/lib64|/libx32|/proc|/sys|/dev|/boot|/run)
                D_WHY="CHROME_ISOLATION_HOME is a system root or a top-level directory, which no install of this app has ever used"; return 0 ;;
        esac
        case "$HOME_REAL/" in
            "$ISOLATION_HOME/"*) D_WHY="CHROME_ISOLATION_HOME ($ISOLATION_HOME) contains the home directory"; return 0 ;;
        esac
        if [[ "$ISOLATION_HOME" != */*/* ]]; then
            D_WHY="CHROME_ISOLATION_HOME ($ISOLATION_HOME) is shallower than <home>/<something>, which no install of this app has ever used"; return 0
        fi
    fi

    # (2) Does it hold data this app created?
    if _store_holds_our_profiles "$dir"; then
        D_VERDICT="remove"; D_WHY="holds Chromium profiles created by this app"
    elif _dir_is_empty "$dir"; then
        D_VERDICT="remove"; D_WHY="is empty (nothing to lose)"
    else
        D_WHY="no profile inside it looks like one this app created (no profile directory holds both 'Default/' and 'Local State' or 'hardware-signature.json'), so it may be somebody else's data"
    fi
    return 0
}

# --- Work out the verdict for every candidate BEFORE asking anything ---------
# Classify, then present. Deciding after the user has already typed YES would
# make the prompt a rubber stamp.
STORES_TO_REMOVE=()
REFUSALS=()
for entry in "new:$PROFILES_DIR" "legacy:$LEGACY_PROFILES_DIR"; do
    kind="${entry%%:*}"; dir="${entry#*:}"
    [[ -n "$dir" ]] || continue
    _classify_store "$dir" "$kind"
    if [[ "$D_VERDICT" == "remove" ]]; then
        STORES_TO_REMOVE+=("$dir")
    elif [[ -e "$dir" ]]; then
        REFUSALS+=("$dir"$'\x01'"$D_WHY")
    fi
done

# --- App data: derived from $HOME only, but `rm -rf` deserves the same care --
APP_DATA_REMOVE=1
case "$APP_DATA" in
    ""|/|"$HOME_DIR") APP_DATA_REMOVE=0 ;;
    *) [[ -d "$APP_DATA" ]] || APP_DATA_REMOVE=0
       [[ "$(basename -- "$APP_DATA")" == "chrome-isolation-manager" ]] || APP_DATA_REMOVE=0 ;;
esac

echo "──────────────────────────────────────────────────────────────"
echo " Chrome Isolation Manager — uninstaller"
echo " This will DELETE:"
echo "   • Profile containers + Docker image (isolated-chrome)"
if [[ ${#STORES_TO_REMOVE[@]} -gt 0 ]]; then
    for dir in "${STORES_TO_REMOVE[@]}"; do
        echo "   • Profile data: $dir"
        _sum="$(_store_summary "$dir")"
        [[ -n "$_sum" ]] && printf '       containing:\n%s\n' "$_sum"
    done
else
    echo "   • Profile data: (nothing — see REFUSED below)"
fi
if [[ $APP_DATA_REMOVE -eq 1 ]]; then
    echo "   • App data: $APP_DATA"
fi
echo "   • Launcher, .desktop entry, app icon"
if [[ ${#REFUSALS[@]} -gt 0 ]]; then
    echo ""
    echo " NOT removed (left untouched on purpose):"
    for r in "${REFUSALS[@]}"; do
        echo "   • ${r%%$'\x01'*}  — ${r#*$'\x01'}"
    done
fi
[[ -n "$CONFIG_DISAGREEMENT" ]] && { echo ""; echo " note: $CONFIG_DISAGREEMENT"; }
echo "──────────────────────────────────────────────────────────────"
read -r -p "Type YES to continue: " ans
[[ "$ans" == "YES" ]] || { echo "Aborted."; exit 1; }

echo "→ Stopping and removing profile containers..."
docker ps -q --filter "name=chrome-" | xargs -r docker rm -f
docker rmi isolated-chrome >/dev/null 2>&1 || true

if [[ $APP_DATA_REMOVE -eq 1 ]]; then
    echo "→ Removing app data (registry, proxies, signatures)..."
    rm -rf "$APP_DATA"
else
    echo "→ Skipping app data ($APP_DATA): refusing to remove it."
fi

echo "→ Removing launcher, desktop entry, icon..."
rm -f "$LAUNCHER"
rm -f "$DESKTOP"
rm -f "$ICON"

if [[ ${#STORES_TO_REMOVE[@]} -gt 0 ]]; then
    for dir in "${STORES_TO_REMOVE[@]}"; do
        echo "→ Removing profile data ($dir)..."
        rm -rf -- "$dir"
    done
fi

# A refusal must never be silent: the user asked for everything to go, and the
# only thing standing between them and a `rm -rf` of somebody else's data is
# this script. Say exactly what was left and exactly how to remove it.
if [[ ${#REFUSALS[@]} -gt 0 ]]; then
  for r in "${REFUSALS[@]}"; do
    dir="${r%%$'\x01'*}"; why="${r#*$'\x01'}"
    echo "" >&2
    echo "!! LEFT IN PLACE: $dir" >&2
    echo "   reason: $why" >&2
    echo "   this script will not delete a directory it cannot prove belongs to" >&2
    echo "   this app. Inspect it, then remove it yourself if you are sure:" >&2
    echo "       rm -rf -- '$dir'" >&2
  done
fi

if command -v dpkg >/dev/null 2>&1 && dpkg -s chrome-isolation >/dev/null 2>&1; then
    echo "→ Removing .deb package..."
    sudo dpkg -r chrome-isolation
fi
if command -v rpm >/dev/null 2>&1 && rpm -q chrome-isolation >/dev/null 2>&1; then
    echo "→ Removing .rpm package..."
    sudo dnf remove -y chrome-isolation
fi

echo ""
echo "✓ Chrome Isolation Manager removed."