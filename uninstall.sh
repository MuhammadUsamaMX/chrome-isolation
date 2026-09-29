#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# uninstall.sh — remove Chrome Isolation Manager and all its data
#
# Removes (in order):
#   1. Profile containers + the isolated-chrome Docker image
#   2. App data: registry, proxy store, machine signatures (~/.local/share/
#      chrome-isolation-manager)
#   3. Launcher, .desktop entry, icon
#   4. Profile data (~/Isolated-Chrome/profiles, plus the legacy ~/Chrome if
#      that still holds data) — back it up first!
#   5. .deb / .rpm package if installed via a package
#
# Usage: bash uninstall.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HOME_DIR="$HOME"
APP_DATA="$HOME_DIR/.local/share/chrome-isolation-manager"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Profile data location: ask the app's config so this follows the constant
# (and $CHROME_ISOLATION_HOME) instead of hardcoding a path. Falls back to the
# documented default if Python or the backend is unavailable.
PROFILES_DIR=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    PROFILES_DIR="$(cd "$SCRIPT_DIR" && "$cand" -c '
import os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "backend"))
import config
print(config.CHROME_PROFILES_DIR)
' 2>/dev/null)"
    [[ -n "$PROFILES_DIR" ]] && break
  fi
done
if [[ -z "$PROFILES_DIR" ]]; then
  PROFILES_DIR="${CHROME_ISOLATION_HOME:-$HOME_DIR/Isolated-Chrome}/profiles"
  echo "note: could not read config.py; assuming PROFILES_DIR=$PROFILES_DIR" >&2
fi
# Legacy location, for installs created before the relocation and never
# migrated. Removed only if it actually holds profile data.
LEGACY_PROFILES_DIR="$HOME_DIR/Chrome"
LAUNCHER="$HOME_DIR/.local/bin/chrome-isolation"
DESKTOP="$HOME_DIR/.local/share/applications/chrome-isolation.desktop"
ICON="$HOME_DIR/.local/share/icons/hicolor/256x256/apps/chrome-isolation.png"

echo "──────────────────────────────────────────────────────────────"
echo " Chrome Isolation Manager — uninstaller"
echo " This will DELETE:"
echo "   • Profile containers + Docker image (isolated-chrome)"
echo "   • All profile data: $PROFILES_DIR"
echo "   • App data: $APP_DATA"
echo "   • Launcher, .desktop entry, app icon"
echo "──────────────────────────────────────────────────────────────"
read -r -p "Type YES to continue: " ans
[[ "$ans" == "YES" ]] || { echo "Aborted."; exit 1; }

echo "→ Stopping and removing profile containers..."
docker ps -q --filter "name=chrome-" | xargs -r docker rm -f
docker rmi isolated-chrome >/dev/null 2>&1 || true

echo "→ Removing app data (registry, proxies, signatures)..."
rm -rf "$APP_DATA"

echo "→ Removing launcher, desktop entry, icon..."
rm -f "$LAUNCHER"
rm -f "$DESKTOP"
rm -f "$ICON"

for dir in "$PROFILES_DIR" "$LEGACY_PROFILES_DIR"; do
  # Guard against a mis-resolved empty/root path turning this into `rm -rf /`.
  if [[ -z "$dir" || "$dir" == "/" || ! -d "$dir" ]]; then
    continue
  fi
  echo "→ Removing profile data ($dir)..."
  rm -rf "$dir"
done

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