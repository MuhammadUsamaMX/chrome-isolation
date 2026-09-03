#!/bin/bash
# user-agent-spoof.sh — generates a stable per-profile user-agent string.
PROFILE_NAME="${1:-default}"
CONFIG_DIR="/home/chrome/.config/chromium"
UA_FILE="$CONFIG_DIR/user-agent.txt"
mkdir -p "$CONFIG_DIR"

if [[ ! -f "$UA_FILE" ]]; then
    # Derive the version from the installed Chromium binary so the spoofed UA
    # matches the real engine version shipped in this container image.
    CHROME_VERSION="$(chromium-browser --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"
    if [[ -z "$CHROME_VERSION" ]]; then
        CHROME_VERSIONS=("120.0.6099.129" "121.0.6167.85" "122.0.6261.94" "119.0.6045.199")
        CHROME_VERSION=${CHROME_VERSIONS[$RANDOM % ${#CHROME_VERSIONS[@]}]}
    fi
    # Strictly Linux UA — Windows spoofing leaks via fonts/canvas
    USER_AGENT="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/$CHROME_VERSION Safari/537.36"
    echo "$USER_AGENT" > "$UA_FILE"
fi

export CHROME_USER_AGENT="$(cat "$UA_FILE")"
