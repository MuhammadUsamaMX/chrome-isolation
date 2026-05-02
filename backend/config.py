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

# Desktop entries
DESKTOP_ENTRIES_DIR = os.path.join(HOME, ".local", "share", "applications")

# Docker
DOCKER_IMAGE_NAME = "isolated-chrome"
CONTAINER_PREFIX = "chrome-"

# Icon installed by the app (set during install)
APP_ICON = os.path.join(APP_DATA_DIR, "icon.png")

# Ensure directories exist on import
for _d in (CHROME_PROFILES_DIR, APP_DATA_DIR, DESKTOP_ENTRIES_DIR):
    os.makedirs(_d, exist_ok=True)
