#!/bin/bash
# stealth-launch.sh — container entrypoint.
# Wayland-native: no X11/DISPLAY dependency.
# Host desktop preferences are inherited via env vars set by docker_manager.py.

PROFILE_NAME="${CHROME_PROFILE:-default}"
SCRIPT_DIR="/home/chrome/scripts"

source "$SCRIPT_DIR/hardware-spoof.sh"    "$PROFILE_NAME"
source "$SCRIPT_DIR/user-agent-spoof.sh"  "$PROFILE_NAME"

# Isolated timezone from profile hardware-signature (not host tz)
TZ_VAL=$(python3 -c "
import json, os
f = '/home/chrome/.config/chromium/hardware-signature.json'
print(json.load(open(f)).get('system',{}).get('timezone','UTC')) if os.path.exists(f) else print('UTC')
" 2>/dev/null || echo UTC)
export TZ="$TZ_VAL"

export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-$LANG}"

# Build dark-mode feature flag based on host colour scheme injected by docker_manager
FEATURES="WaylandWindowDecorations,UseOzonePlatform"
if [[ "${HOST_COLOR_SCHEME:-dark}" == "dark" ]]; then
    FEATURES="$FEATURES,WebContentsForceDark"
fi

FLAGS=(
    # Wayland native rendering — no X11
    "--ozone-platform=wayland"
    "--enable-features=$FEATURES"
    "--enable-wayland-ime"

    # Required inside Docker: container IS the security boundary.
    # Chromium's internal sandbox needs kernel user-namespaces which
    # Docker blocks by default. The container itself provides isolation.
    "--no-sandbox"
    # Suppresses the "unsupported command-line flag" info-bar shown for --no-sandbox
    "--test-type"

    # Anti-detect / stealth
    "--no-first-run"
    "--no-default-browser-check"
    "--disable-sync"
    "--disable-translate"
    "--disable-dev-shm-usage"
    "--disable-logging"
    "--log-level=3"
    "--disable-blink-features=AutomationControlled"
    "--disable-infobars"
    "--start-maximized"
    "--user-agent=$CHROME_USER_AGENT"
    "--window-size=${CHROME_RESOLUTION/x/,}"
)

exec chromium-browser "${FLAGS[@]}" "$@"
