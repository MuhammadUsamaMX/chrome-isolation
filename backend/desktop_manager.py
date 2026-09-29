"""
Desktop entry manager.
Generates per-profile .desktop files that launch via the installed Electron binary.

Desktop identity rules (enforced here):
  * These launchers are NOT web browsers. Each one opens exactly ONE fixed,
    isolated profile, so they must never carry the `WebBrowser` category and
    must never advertise `%U`/`%u` URL handling. Otherwise every "Set as
    default browser" chooser, GNOME search and xdg tiebreak sees a crowd of
    Chrome-branded browser candidates — which is what users report as "it
    messes with my browser".
  * They must never borrow Google Chrome's icon name. A missing icon file is
    a reason to degrade to a generic icon, never to impersonate Chrome.
  * `StartupWMClass` here is the single source of truth for the window class:
    docker_manager passes this exact string to Chromium's `--class=`, so the
    WM can associate the window with its own launcher.

Because the content is fully derived from the functions below,
`refresh_desktop_entries()` can regenerate an entry written by an older
build and heal it — the create-only path left stale entries on disk forever.
"""
import os
import re
import subprocess

from config import (
    DESKTOP_ENTRIES_DIR,
    APP_ICON,
    APP_DATA_DIR,
    APP_DESKTOP_FILENAME,
    APP_LAUNCHER_NAME,
)
from validator import validate_profile_name

# Launcher namespace. Also the prefix of the window class, so it can never
# collide with real Chrome's `google-chrome` class.
DESKTOP_PREFIX = "chrome-isolation"

# Per-profile launcher categories: NOT WebBrowser. Still filed sensibly in
# the app grid under Network, but never offered as "the browser".
PROFILE_CATEGORIES = "Network;Utility;"

# The app's own entry is a settings/profile-manager app, not a browser.
APP_CATEGORIES = "Utility;"


def _electron_exec() -> str:
    """Return the installed Electron app command."""
    # Installed as /usr/local/bin/chrome-isolation, ~/bin/chrome-isolation,
    # or via electron-builder .deb (/usr/bin or /opt/Chrome Isolation/)
    for candidate in (
        '/usr/local/bin/chrome-isolation',
        os.path.expanduser('~/bin/chrome-isolation'),
        os.path.expanduser('~/.local/bin/chrome-isolation'),
        '/usr/bin/chrome-isolation',
        '/opt/Chrome Isolation/chrome-isolation',
    ):
        if os.path.isfile(candidate):
            return candidate
    # Fallback — dev mode path
    return os.path.join(APP_DATA_DIR, 'chrome-isolation')


def _theme_icon_installed() -> bool:
    """True when install.sh/setup.sh installed the icon into the hicolor theme."""
    return os.path.exists(os.path.expanduser(
        '~/.local/share/icons/hicolor/256x256/apps/chrome-isolation.png'))


def _icon() -> str:
    """
    Return the icon to advertise in generated entries.

    Order of preference:
      1. `chrome-isolation` — the theme icon installed by install.sh/setup.sh.
         A stable icon *name*, so entries survive icon reinstalls.
      2. The absolute path of APP_ICON (installed, but not in the theme).
      3. `web-browser` — a generic, honest fallback.

    There is deliberately NO `google-chrome` fallback. Degrading to a generic
    icon is a cosmetic inconvenience; wearing another browser's icon is
    impersonation, and impersonation is the reported bug.
    """
    if _theme_icon_installed():
        return APP_LAUNCHER_NAME
    if os.path.exists(APP_ICON):
        return APP_ICON
    return 'web-browser'


def wm_class(profile_name: str) -> str:
    """
    The window class for a profile, used BOTH as `StartupWMClass=` in the
    .desktop entry and as Chromium's `--class=` in docker_manager. One
    function is what guarantees the two can never disagree again; they
    previously read `chrome-<name>` and `chrome-isolation-<name>`, so the WM
    could not match window to launcher and showed a wrong/generic icon.
    """
    return f"{DESKTOP_PREFIX}-{validate_profile_name(profile_name)}"


def desktop_file_path(profile_name: str) -> str:
    profile_name = validate_profile_name(profile_name)
    return os.path.join(DESKTOP_ENTRIES_DIR, f"{DESKTOP_PREFIX}-{profile_name}.desktop")


def profile_entry_content(profile_name: str) -> str:
    """Full body of a per-profile launcher. Pure function of the profile name."""
    profile_name = validate_profile_name(profile_name)
    return (
        "[Desktop Entry]\n"
        "Version=1.0\n"
        "Type=Application\n"
        f"Name=Chrome ({profile_name})\n"
        f"Comment=Isolated Chrome Profile: {profile_name}\n"
        # No %u / %U — this launcher takes no URL arguments.
        f"Exec={_electron_exec()} --profile {profile_name}\n"
        f"Icon={_icon()}\n"
        "Terminal=false\n"
        f"Categories={PROFILE_CATEGORIES}\n"
        f"StartupWMClass={wm_class(profile_name)}\n"
        # Marker: identifies the file as ours, so a refresh can regenerate it
        # and so tooling/tests can tell it from a hand-written entry.
        "X-Chrome-Isolation-Managed=true\n"
    )


def app_entry_content() -> str:
    """Body of the app's own launcher. Kept in sync with install.sh."""
    exec_cmd = _electron_exec()
    return (
        "[Desktop Entry]\n"
        "Version=1.0\n"
        "Type=Application\n"
        "Name=Chrome Isolation Manager\n"
        "GenericName=Isolated Profile Manager\n"
        "Comment=Manage isolated Chrome profiles in Docker containers\n"
        # No %u: the manager handles no URLs, so it is not a URL handler and
        # must not advertise being one.
        f"Exec={exec_cmd}\n"
        f"Icon={_icon()}\n"
        "Terminal=false\n"
        f"Categories={APP_CATEGORIES}\n"
        "Keywords=isolation;container;docker;profile;\n"
        "StartupNotify=true\n"
        f"StartupWMClass={DESKTOP_PREFIX}\n"
        "X-Chrome-Isolation-Managed=true\n"
    )


def _update_desktop_database() -> None:
    """
    Regenerate the menu cache for DESKTOP_ENTRIES_DIR, and nothing else.

    Necessary — our entries really do live in that directory — and scoped to
    that one directory. Called ONLY when a file actually changed, so a no-op
    launch never triggers a rebuild.

    Side effect worth being explicit about: `update-desktop-database` also
    rewrites the shared `mimeinfo.cache` in that directory. That is a
    property of the shared directory, not something this app authors — no
    generated entry declares `MimeType=` or an `x-scheme-handler/...` key
    (see the two *_entry_content functions above), so this app contributes
    no MIME association of its own and cannot claim default-browser status.

    The broad `xdg-desktop-menu forceupdate`, which rewrites the user's menu
    layout and menu caches well beyond this directory, is deliberately NOT
    used here or by install.sh/setup.sh any more.
    """
    # Security: subprocess.run with an argument list (shell=False), never
    # os.system — see tests/test_security_regressions.py (F7).
    try:
        subprocess.run(['update-desktop-database', DESKTOP_ENTRIES_DIR],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        pass


def _write_if_changed(path: str, content: str, mode: int = 0o644) -> bool:
    """
    Write `content` to `path`, but only when it differs. Returns True if the
    file was (re)written. This is what makes create and refresh idempotent: a
    second call is a no-op that does not even bump the mtime, so we neither
    churn the menu cache nor keep touching the user's home.
    """
    try:
        with open(path) as f:
            if f.read() == content:
                return False
    except (OSError, UnicodeDecodeError):
        pass

    tmp = f"{path}.tmp"
    with open(tmp, 'w') as f:
        f.write(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def create_desktop_entry(profile_name: str) -> dict:
    profile_name = validate_profile_name(profile_name)
    path = desktop_file_path(profile_name)
    changed = _write_if_changed(path, profile_entry_content(profile_name), mode=0o755)
    if changed:
        _update_desktop_database()
    return {"status": "created" if changed else "unchanged", "path": path}


def refresh_desktop_entries() -> dict:
    """
    Regenerate every desktop entry this app owns, without touching profiles.

    The create path only ever runs at profile-creation time, so an entry
    written by an older buggy build stays wrong forever — that is exactly how
    a launcher created before the icon was installed kept `Icon=google-chrome`
    on disk indefinitely. This re-derives the content for every file in our
    namespace and rewrites only what actually changed.

    It deliberately does NOT remove entries for profiles that no longer
    exist: removal is remove_desktop_entry()'s job, and this runs on app
    start, where the profile list is not this function's concern.
    """
    updated = []
    checked = 0
    if os.path.isdir(DESKTOP_ENTRIES_DIR):
        for name in sorted(os.listdir(DESKTOP_ENTRIES_DIR)):
            path = os.path.join(DESKTOP_ENTRIES_DIR, name)
            if not os.path.isfile(path):
                continue
            if name == APP_DESKTOP_FILENAME:
                checked += 1
                if _write_if_changed(path, app_entry_content()):
                    updated.append(path)
                continue
            m = re.fullmatch(rf"{re.escape(DESKTOP_PREFIX)}-(.+)\.desktop", name)
            if not m:
                continue
            try:
                profile = validate_profile_name(m.group(1))
            except ValueError:
                continue  # not one of ours / unusable name — leave it alone
            checked += 1
            if _write_if_changed(path, profile_entry_content(profile), mode=0o755):
                updated.append(path)

    if updated:
        _update_desktop_database()
    return {"status": "refreshed", "checked": checked, "updated": updated}


def remove_desktop_entry(profile_name: str) -> dict:
    profile_name = validate_profile_name(profile_name)
    path = desktop_file_path(profile_name)
    if os.path.exists(path):
        os.remove(path)
        _update_desktop_database()
        return {"status": "removed"}
    return {"status": "not_found"}


def desktop_entry_exists(profile_name: str) -> bool:
    return os.path.exists(desktop_file_path(profile_name))
