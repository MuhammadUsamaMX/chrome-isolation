#!/bin/bash
# hardware-spoof.sh — generates a stable per-profile hardware fingerprint.
# Run once; subsequent launches reuse the stored values.
PROFILE_NAME="${1:-default}"
CONFIG_DIR="/home/chrome/.config/chromium"
HARDWARE_FILE="$CONFIG_DIR/hardware-signature.json"
mkdir -p "$CONFIG_DIR"

if [[ ! -f "$HARDWARE_FILE" ]]; then
    CPU_CORES=$((RANDOM % 15 + 2))
    RAM_SIZES=(4 8 16 32)
    RAM_GB=${RAM_SIZES[$RANDOM % ${#RAM_SIZES[@]}]}
    RESOLUTIONS=("1920x1080" "2560x1440" "1366x768" "1440x900")
    RESOLUTION=${RESOLUTIONS[$RANDOM % ${#RESOLUTIONS[@]}]}
    TIMEZONES=("America/New_York" "America/Los_Angeles" "Europe/London" "Asia/Tokyo")
    TIMEZONE=${TIMEZONES[$RANDOM % ${#TIMEZONES[@]}]}

    cat > "$HARDWARE_FILE" <<EOF
{
  "profile": "$PROFILE_NAME",
  "hardware": {
    "cpu_cores": $CPU_CORES,
    "ram_gb": $RAM_GB,
    "screen_resolution": "$RESOLUTION"
  },
  "system": {
    "timezone": "$TIMEZONE"
  }
}
EOF
fi

# Export resolved values for the caller
CPU_CORES=$(python3 -c "import json; print(json.load(open('$HARDWARE_FILE')).get('hardware',{}).get('cpu_cores',4))")
RAM_GB=$(python3    -c "import json; print(json.load(open('$HARDWARE_FILE')).get('hardware',{}).get('ram_gb',8))")
RESOLUTION=$(python3 -c "import json; print(json.load(open('$HARDWARE_FILE')).get('hardware',{}).get('screen_resolution','1920x1080'))")
export CHROME_CPU_CORES="$CPU_CORES"
export CHROME_RAM_GB="$RAM_GB"
export CHROME_RESOLUTION="$RESOLUTION"
