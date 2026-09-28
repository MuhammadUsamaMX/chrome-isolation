"""
Profile registry — S4 fix.
Single JSON file tracks every profile (name, path, created_at).
All lifecycle operations read from here instead of ad-hoc path computation.

T1 (F1) migration note
----------------------
`host_mount == ""` used to mean "mount the user's home directory". It now
means "mount a safe, empty, per-profile share directory" (see host_share.py).
Load-time migration is deliberately NON-DESTRUCTIVE: profiles.json is never
rewritten, the stored "" is left untouched, and the affected profiles are only
*tracked* (and reported once on stderr, which the bridge already owns — stdout
carries the JSON protocol).
"""
import json
import os
import sys
import time
from typing import Optional

from config import REGISTRY_FILE, CHROME_PROFILES_DIR, HOST_SHARES_DIR
from validator import validate_profile_name, safe_profile_path

# Profiles found with an empty host_mount on the most recent load, plus the
# subset we have already reported, so a long-running bridge does not spam.
_default_host_mount_profiles = set()
_reported_default_host_mount = set()


def _audit_default_host_mounts(data: dict) -> None:
    """
    Track profiles whose host_mount is empty (i.e. now using the safe default
    share dir). Non-destructive: nothing is written back to profiles.json.
    """
    _default_host_mount_profiles.clear()
    for name, entry in data.items():
        if not isinstance(entry, dict):
            continue
        if str(entry.get('host_mount', '') or '').strip():
            continue
        _default_host_mount_profiles.add(name)
        if name in _reported_default_host_mount:
            continue
        _reported_default_host_mount.add(name)
        print(
            f"[chrome-isolation] profile '{name}': host_mount is empty, so the "
            f"container now gets a dedicated empty share directory "
            f"({os.path.join(HOST_SHARES_DIR, name)}) mounted read-only at "
            f"/home/chrome/host. Your home directory is NO LONGER mounted. "
            f"Set an explicit Host Folder in the app if you want to share files.",
            file=sys.stderr,
        )


def default_host_mount_profiles() -> list:
    """Names of profiles currently relying on the safe default share dir."""
    return sorted(_default_host_mount_profiles)


def _load() -> dict:
    if os.path.exists(REGISTRY_FILE):
        try:
            with open(REGISTRY_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _audit_default_host_mounts(data)
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(data: dict) -> None:
    tmp = REGISTRY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, REGISTRY_FILE)


def list_profiles() -> list:
    """Return list of profile dicts from the registry."""
    data = _load()
    return list(data.values())


def get_profile(name: str) -> Optional[dict]:
    name = validate_profile_name(name)
    return _load().get(name)


def register_profile(name: str, path: str, host_mount: str = '', proxy: str = '') -> dict:
    """Add a profile to the registry. Raises if already registered."""
    name = validate_profile_name(name)
    data = _load()
    if name in data:
        raise ValueError(f"Profile '{name}' already registered.")
    entry = {
        "name": name,
        "path": path,
        # "" means "safe empty per-profile share dir", never the home dir (T1).
        "host_mount": host_mount,
        "proxy": proxy,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    data[name] = entry
    _save(data)
    if not str(host_mount or '').strip():
        _default_host_mount_profiles.add(name)
    return entry


def update_profile(name: str, host_mount: str = None, proxy: str = None) -> dict:
    """Update mutable profile settings. None leaves a field unchanged."""
    name = validate_profile_name(name)
    data = _load()
    if name not in data:
        raise ValueError(f"Profile '{name}' not found.")
    entry = data[name]
    if host_mount is not None:
        entry['host_mount'] = host_mount
    if proxy is not None:
        entry['proxy'] = proxy
    _save(data)
    return entry


def unregister_profile(name: str) -> None:
    name = validate_profile_name(name)
    data = _load()
    data.pop(name, None)
    _save(data)
    _default_host_mount_profiles.discard(name)
    _reported_default_host_mount.discard(name)


def resolve_profile_path(name: str) -> str:
    """
    Return the canonical path for a profile.
    First checks the registry (handles custom locations correctly).
    Falls back to the default managed path if not yet registered.
    """
    name = validate_profile_name(name)
    entry = _load().get(name)
    if entry:
        return entry["path"]
    # Not in registry yet — use default path (but do NOT create it here)
    return safe_profile_path(name, CHROME_PROFILES_DIR)
