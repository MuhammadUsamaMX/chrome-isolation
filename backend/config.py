"""
Chrome Isolation — backend configuration.
All paths are computed from environment / home directory; no hardcoded user.
"""
import os

HOME = os.path.expanduser("~")

# Where profile data lives
CHROME_PROFILES_DIR = os.path.join(HOME, "Chrome")

# Per-app data directory (registry file, etc.)
APP_DATA_DIR = os.path.join(HOME, ".local", "share", "chrome-isolation-manager")

# Registry file — single source of truth for all profiles
REGISTRY_FILE = os.path.join(APP_DATA_DIR, "profiles.json")

# Per-profile host share directories (T1 / F1 fix).
# When a profile has no explicit host_mount, the container gets ONE of these
# empty, 0700 directories mounted read-only at /home/chrome/host — never the
# user's home directory. Layout: <HOST_SHARES_DIR>/<profile_name>/
HOST_SHARES_DIR = os.path.join(APP_DATA_DIR, "host-shares")

# Desktop entries
DESKTOP_ENTRIES_DIR = os.path.join(HOME, ".local", "share", "applications")

# Docker
DOCKER_IMAGE_NAME = "isolated-chrome"
CONTAINER_PREFIX = "chrome-"

# Icon installed by the app (set during install)
APP_ICON = os.path.join(APP_DATA_DIR, "icon.png")

# Ensure directories exist on import
for _d in (CHROME_PROFILES_DIR, APP_DATA_DIR, DESKTOP_ENTRIES_DIR, HOST_SHARES_DIR):
    os.makedirs(_d, exist_ok=True)

# The host-shares root must never be world- or group-readable; it is the parent
# of every per-profile share dir and is mounted into containers by path.
try:
    os.chmod(HOST_SHARES_DIR, 0o700)
except OSError:
    pass
