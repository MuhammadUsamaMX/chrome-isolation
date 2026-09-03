#!/bin/bash
# stealth-launch.sh — container entrypoint.
# Wayland-native: no X11/DISPLAY dependency.
# Host desktop preferences are inherited via env vars set by docker_manager.py.
# Each profile presents as its own machine: CPU core count (via CPU affinity),
# language, GPU backend, timezone, user-agent, hostname, and window size.

PROFILE_NAME="${CHROME_PROFILE:-default}"
SCRIPT_DIR="/home/chrome/scripts"

source "$SCRIPT_DIR/hardware-spoof.sh"    "$PROFILE_NAME"
source "$SCRIPT_DIR/user-agent-spoof.sh"  "$PROFILE_NAME"

# Isolated timezone from the profile's machine signature (not host tz)
export TZ="$CHROME_TIMEZONE"

# Locale from the profile's machine signature (de-DE -> de_DE.UTF-8)
export LANG="${CHROME_LANGUAGE//-/_}.UTF-8"
export LC_ALL="$LANG"

# Build dark-mode feature flag based on host colour scheme injected by docker_manager
FEATURES="WaylandWindowDecorations,UseOzonePlatform,VulkanFromANGLE,DefaultANGLEVulkan"
if [[ "${HOST_COLOR_SCHEME:-dark}" == "dark" ]]; then
    FEATURES="$FEATURES,WebContentsForceDark"
fi

# Per-profile GPU backend: real GPU via ANGLE/Vulkan, or software SwiftShader
# (a machine without a usable GPU). Renderer string differs accordingly.
case "${CHROME_GPU_MODE:-vulkan}" in
    swiftshader) GL_FLAGS=("--use-gl=angle" "--use-angle=swiftshader") ;;
    *)           GL_FLAGS=("--use-gl=angle" "--use-angle=vulkan") ;;
esac

# Clamp the spoofed core count to what the host actually exposes, then pin the
# process to that many CPUs. Chromium reads sched_getaffinity, so
# navigator.hardwareConcurrency reports the spoofed count.
ACTUAL_CORES=$(nproc)
SPOOF_CORES=$(( CHROME_CPU_CORES < ACTUAL_CORES ? CHROME_CPU_CORES : ACTUAL_CORES ))

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
    "--disable-logging"
    "--log-level=3"
    "--disable-blink-features=AutomationControlled"
    "--disable-infobars"
    "--lang=$CHROME_LANGUAGE"
    "--user-agent=$CHROME_USER_AGENT"
    "--window-size=${CHROME_RESOLUTION/x/,}"
    "${GL_FLAGS[@]}"
)

# Note: no --start-maximized — the window opens at the profile's spoofed size
# so window dimensions stay consistent with the machine signature.
exec taskset -c "0-$((SPOOF_CORES-1))" chromium-browser "${FLAGS[@]}" "$@"