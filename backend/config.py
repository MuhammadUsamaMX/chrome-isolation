"""
Chrome Isolation — backend configuration.
All paths are computed from environment / home directory; no hardcoded user.

The path *logic* lives in paths.py (pure, no import-time side effects) so that
dry-run tooling can share it; the constants below are this app's single named
answer to "where does profile data live" and everything imports them from here.
"""
import os

import paths

HOME = paths.home()

# --------------------------------------------------------------------------- #
# Profile data — THE one location, and the legacy one.
# --------------------------------------------------------------------------- #
# Root of this app's own storage. Override with $CHROME_ISOLATION_HOME.
CHROME_ISOLATION_HOME = paths.isolation_home()

# Where NEW profile data lives: ~/Isolated-Chrome/profiles (by default).
# Deliberately not a name any browser creates, and never ~/.config/google-chrome
# or anything under it — this app must not be confusable with, or write into,
# real Google Chrome's storage.
CHROME_PROFILES_DIR = os.path.join(CHROME_ISOLATION_HOME, "profiles")

# The pre-relocation location, ~/Chrome. Never used for new profiles; only
# honoured by resolve_profiles_dir() for installs that have not migrated yet,
# and as the source directory of scripts/migrate_profiles_dir.py.
CHROME_PROFILES_DIR_LEGACY = os.path.join(HOME, "Chrome")


def resolve_profiles_dir() -> str:
    """
    The profile store to use right now, with backward compatibility.

    Returns CHROME_PROFILES_DIR when it holds profiles, else
    CHROME_PROFILES_DIR_LEGACY when that holds profiles, else
    CHROME_PROFILES_DIR. See paths.resolve_profiles_dir() for why "holds
    profiles" rather than merely "exists" is the test.
    """
    return paths.resolve_profiles_dir(CHROME_PROFILES_DIR, CHROME_PROFILES_DIR_LEGACY)


def other_profiles_dir() -> str:
    """The location currently NOT in use (i.e. what a migration moves from)."""
    return paths.other_profiles_dir(resolve_profiles_dir(),
                                    CHROME_PROFILES_DIR, CHROME_PROFILES_DIR_LEGACY)


def orphan_profiles() -> set:
    """Profile names present in the unused location; never used, never deleted."""
    active = resolve_profiles_dir()
    return paths.orphan_profiles(active, CHROME_PROFILES_DIR, CHROME_PROFILES_DIR_LEGACY)


# Per-app data directory (registry file, etc.)
APP_DATA_DIR = paths.app_data_dir()

# Registry file — single source of truth for all profiles
REGISTRY_FILE = os.path.join(APP_DATA_DIR, "profiles.json")

# Per-profile host share directories (T1 / F1 fix).
# When a profile has no explicit host_mount, the container gets ONE of these
# empty, 0700 directories mounted read-only at /home/chrome/host — never the
# user's home directory. Layout: <HOST_SHARES_DIR>/<profile_name>/
HOST_SHARES_DIR = os.path.join(APP_DATA_DIR, "host-shares")

# Desktop entries
DESKTOP_ENTRIES_DIR = os.path.join(HOME, ".local", "share", "applications")

# The app's own launcher. Also the launcher/window-class prefix shared by
# desktop_manager and docker_manager, and the basename of the icon installed
# into the hicolor theme by install.sh / setup.sh.
APP_LAUNCHER_NAME = "chrome-isolation"
APP_DESKTOP_FILENAME = f"{APP_LAUNCHER_NAME}.desktop"

# Docker
DOCKER_IMAGE_NAME = "isolated-chrome"
CONTAINER_PREFIX = "chrome-"

# Icon installed by the app (set during install)
APP_ICON = os.path.join(APP_DATA_DIR, "icon.png")

# Ensure directories exist on import. The profile store that gets created is
# the one actually in use, so an un-migrated install keeps working and never
# ends up with an empty new directory shadowing its real data (resolve_profiles_dir
# ignores empty directories anyway, but there is no reason to create one).
for _d in (resolve_profiles_dir(), APP_DATA_DIR, DESKTOP_ENTRIES_DIR, HOST_SHARES_DIR):
    os.makedirs(_d, exist_ok=True)

# The host-shares root must never be world- or group-readable; it is the parent
# of every per-profile share dir and is mounted into containers by path.
try:
    os.chmod(HOST_SHARES_DIR, 0o700)
except OSError:
    pass
