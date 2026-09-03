"""
Desktop entry manager.
Generates per-profile .desktop files that launch via the installed Electron binary.
"""
import os
import re

from config import DESKTOP_ENTRIES_DIR, APP_ICON, APP_DATA_DIR
from validator import validate_profile_name


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


def _icon() -> str:
    if os.path.exists(APP_ICON):
        return APP_ICON
    return 'google-chrome'


def desktop_file_path(profile_name: str) -> str:
    profile_name = validate_profile_name(profile_name)
    return os.path.join(DESKTOP_ENTRIES_DIR, f"chrome-isolation-{profile_name}.desktop")


def create_desktop_entry(profile_name: str) -> dict:
    profile_name = validate_profile_name(profile_name)
    exec_cmd = _electron_exec()
    icon = _icon()

    content = (
        "[Desktop Entry]\n"
        "Version=1.0\n"
        "Type=Application\n"
        f"Name=Chrome ({profile_name})\n"
        f"Comment=Isolated Chrome Profile: {profile_name}\n"
        f"Exec={exec_cmd} --profile {profile_name}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Network;WebBrowser;\n"
        f"StartupWMClass=chrome-isolation-{profile_name}\n"
    )

    path = desktop_file_path(profile_name)
    with open(path, 'w') as f:
        f.write(content)
    os.chmod(path, 0o755)
    os.system(f'update-desktop-database "{DESKTOP_ENTRIES_DIR}" > /dev/null 2>&1')
    return {"status": "created", "path": path}


def remove_desktop_entry(profile_name: str) -> dict:
    profile_name = validate_profile_name(profile_name)
    path = desktop_file_path(profile_name)
    if os.path.exists(path):
        os.remove(path)
        os.system(f'update-desktop-database "{DESKTOP_ENTRIES_DIR}" > /dev/null 2>&1')
        return {"status": "removed"}
    return {"status": "not_found"}


def desktop_entry_exists(profile_name: str) -> bool:
    return os.path.exists(desktop_file_path(profile_name))
