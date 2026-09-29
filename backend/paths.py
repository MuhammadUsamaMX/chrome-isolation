"""
Profile-store path resolution — pure and side-effect free.

This module is the single source of truth for WHERE this app keeps profile
data. It deliberately does nothing at import time: no mkdir, no chmod, no
environment capture. That lets dry-run tooling (scripts/migrate_profiles_dir.py)
import it and be certain it changed nothing on disk. `config.py` imports these
helpers and re-exports the results as its named constants; everything else in
the backend should import the constants from `config`.

WHY THE PROFILE STORE IS NOT "~/Chrome"
--------------------------------------
The original location was ``~/Chrome``. That name is a lie twice over: it reads
like a browser profile store, so it is confusable with real Google Chrome
(``~/.config/google-chrome``), and it is exactly the sort of generic directory
a user eventually `rm -rf`s as "browser junk" — 177 MB of real cookies, logins
and history with no warning. Nothing Chrome, Chromium or any other browser
ever creates a directory called ``Isolated-Chrome``, so the new name cannot be
confused with a browser and cannot be deleted by a browser-junk sweep.

The location is overridable through the ``CHROME_ISOLATION_HOME`` environment
variable so the path is never hardcoded to one user. It is intentionally NOT
``~/.config/google-chrome`` (or anything beneath it): this app must never write
into, move, or shadow a real browser's own storage.

BACKWARD COMPATIBILITY
----------------------
Installs from before the relocation still have their data in ``~/Chrome``.
Rather than hard-fail (which would make the app unusable until the user ran a
migration), `resolve_profiles_dir()` picks a location at runtime:

  * the new location, if it holds any profile;
  * otherwise the legacy location, if it holds any profile;
  * otherwise the new location (the default for freshly created profiles).

The "holds any profile" test — rather than merely "exists" — matters because
`config.py` creates the new directory eagerly on import. If mere existence won,
that empty directory would shadow a perfectly good legacy store the moment the
app started, and every existing profile would silently vanish from the UI.
When BOTH locations hold profiles the new one wins; profiles left behind in the
other location are reported (see `other_profiles_dir` / `orphan_profiles`) rather
than being used or deleted.
"""
import os

#: Environment variable that overrides the profile-store root.
ISOLATION_HOME_ENV = "CHROME_ISOLATION_HOME"

#: Directory name created under $HOME when the env var is unset.
DEFAULT_ISOLATION_HOME_NAME = "Isolated-Chrome"

#: Subdirectory of the isolation home that actually holds the profiles.
PROFILES_SUBDIR = "profiles"

#: The pre-relocation location, kept for backward compatibility + migration.
LEGACY_PROFILES_DIR_NAME = "Chrome"


# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #
def home() -> str:
    """The user's home directory (re-read every call, so tests can patch it)."""
    return os.path.expanduser("~")


def isolation_home() -> str:
    """
    Root of this app's own storage. `$CHROME_ISOLATION_HOME` when set to a
    non-empty value, otherwise `~/Isolated-Chrome`.
    """
    raw = os.environ.get(ISOLATION_HOME_ENV, "")
    if isinstance(raw, str) and raw.strip():
        return os.path.abspath(os.path.expanduser(raw.strip()))
    return os.path.join(home(), DEFAULT_ISOLATION_HOME_NAME)


def default_profiles_dir() -> str:
    """The new, relocation-safe profile store: `~/Isolated-Chrome/profiles`."""
    return os.path.join(isolation_home(), PROFILES_SUBDIR)


def legacy_profiles_dir() -> str:
    """The pre-relocation profile store: `~/Chrome`."""
    return os.path.join(home(), LEGACY_PROFILES_DIR_NAME)


def app_data_dir() -> str:
    """Per-app data directory (registry file, proxies, signatures)."""
    return os.path.join(home(), ".local", "share", "chrome-isolation-manager")


def registry_file() -> str:
    """The profile registry — the single source of truth for all profiles."""
    return os.path.join(app_data_dir(), "profiles.json")


# --------------------------------------------------------------------------- #
# Runtime resolution
# --------------------------------------------------------------------------- #
def _profile_dirs(base: str) -> set:
    """Names of the profile directories directly under `base`."""
    try:
        return {n for n in os.listdir(base)
                if os.path.isdir(os.path.join(base, n)) and not os.path.islink(os.path.join(base, n))}
    except OSError:
        return set()


def has_profiles(base: str) -> bool:
    """True when `base` exists and holds at least one profile directory."""
    return bool(_profile_dirs(base))


def resolve_profiles_dir(new_dir: str = None, legacy_dir: str = None,
                         allow_legacy: bool = None) -> str:
    """
    The profile store the app should use right now.

    Precedence: the new location if it holds profiles, else the legacy location
    if it holds profiles, else the new location. Both locations holding profiles
    is not an error — the new one wins, and `orphan_profiles()` reports what was
    left behind in the other.

    `allow_legacy` gates the legacy fallback, which is the subtle part. The
    fallback exists to serve an UN-MIGRATED INSTALL, and such an install is by
    definition running the default location: it never set
    $CHROME_ISOLATION_HOME and nobody redirected it. So the fallback is only
    consulted when the isolation home is the default `~/Isolated-Chrome`.

    That gate matters for safety, not just tidiness. A caller that passes a
    non-default `new_dir` — the test suite redirecting the store into a tmp dir,
    or a caller managing its own location — would otherwise be handed the real
    `~/Chrome` as a fallback and start writing real user data into it, which is
    precisely the hazard this module exists to prevent.

    The arguments default to the current environment's locations, but
    config.resolve_profiles_dir() passes its import-time constants so the app
    always agrees with the single constant it exports.
    """
    if new_dir is None:
        new_dir = default_profiles_dir()
    if legacy_dir is None:
        legacy_dir = legacy_profiles_dir()
    if allow_legacy is None:
        allow_legacy = (new_dir == default_profiles_dir()
                        and isolation_home() == os.path.join(
                            home(), DEFAULT_ISOLATION_HOME_NAME))
    if has_profiles(new_dir):
        return new_dir
    if allow_legacy and has_profiles(legacy_dir):
        return legacy_dir
    return new_dir


def other_profiles_dir(active_dir: str = None, new_dir: str = None,
                       legacy_dir: str = None) -> str:
    """The location that is NOT in use (the one to migrate away from)."""
    if new_dir is None:
        new_dir = default_profiles_dir()
    if legacy_dir is None:
        legacy_dir = legacy_profiles_dir()
    if active_dir is None:
        active_dir = resolve_profiles_dir(new_dir, legacy_dir)
    if os.path.normpath(active_dir) == os.path.normpath(new_dir):
        return legacy_dir
    return new_dir


def orphan_profiles(active_dir: str = None, new_dir: str = None,
                    legacy_dir: str = None) -> set:
    """
    Profile names that exist in the location the app is NOT using. These are
    invisible to the app and are never deleted or moved automatically — they
    exist so the caller can tell the user "you have profiles here that this
    install will not show; run scripts/migrate_profiles_dir.py".
    """
    other = other_profiles_dir(active_dir, new_dir, legacy_dir)
    return _profile_dirs(other) - _profile_dirs(
        active_dir if active_dir is not None else resolve_profiles_dir(new_dir, legacy_dir))
