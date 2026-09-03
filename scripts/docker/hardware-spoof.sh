#!/bin/bash
# hardware-spoof.sh — generates a stable per-profile machine fingerprint.
# Each profile gets its own "machine": CPU core count, screen resolution,
# timezone, browser language, and GPU backend. Values are generated once and
# reused on every launch; fields missing from older signature files are
# backfilled in place so existing profiles keep their identity.
#
# CHROME_CONFIG_DIR can be overridden for testing (defaults to the container
# profile dir).
PROFILE_NAME="${1:-default}"
CONFIG_DIR="${CHROME_CONFIG_DIR:-/home/chrome/.config/chromium}"
HARDWARE_FILE="$CONFIG_DIR/hardware-signature.json"
mkdir -p "$CONFIG_DIR"

# Generate (or backfill) the signature and print the five resolved values on a
# single line (read splits them by IFS; none of the values contain spaces).
read -r CHROME_CPU_CORES CHROME_RESOLUTION CHROME_TIMEZONE CHROME_LANGUAGE CHROME_GPU_MODE <<< "$(python3 - "$PROFILE_NAME" "$HARDWARE_FILE" <<'PYEOF'
import json, os, random, sys

profile, path = sys.argv[1], sys.argv[2]

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

if "cpu_cores" not in hw:
    hw["cpu_cores"] = random.randint(2, 16)
if "screen_resolution" not in hw:
    hw["screen_resolution"] = random.choice(RESOLUTIONS)
if "timezone" not in sys_:
    sys_["timezone"] = random.choice(TIMEZONES)
if "language" not in br:
    br["language"] = random.choice(LANGUAGES)
if "gpu_mode" not in br:
    br["gpu_mode"] = random.choice(GPU_MODES)

with open(path, "w") as f:
    json.dump(data, f, indent=2)

print(hw["cpu_cores"], hw["screen_resolution"], sys_["timezone"], br["language"], br["gpu_mode"])
PYEOF
)"

export CHROME_CPU_CORES CHROME_RESOLUTION CHROME_TIMEZONE CHROME_LANGUAGE CHROME_GPU_MODE