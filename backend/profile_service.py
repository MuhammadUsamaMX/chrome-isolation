"""
Profile service — high-level operations used by the IPC bridge.
All inputs go through validator.py before any filesystem or Docker call.
"""
import json
import os
import shutil
import sys
import zoneinfo

import paths
from config import CHROME_PROFILES_DIR, CHROME_PROFILES_DIR_LEGACY
from validator import validate_profile_name, safe_profile_path
from host_share import validate_host_mount, host_share_dir, describe_default
from registry import (
    list_profiles, get_profile, register_profile, update_profile as _update_registry,
    unregister_profile, resolve_profile_path,
)
from docker_manager import DockerManager
from desktop_manager import (
    create_desktop_entry, remove_desktop_entry, desktop_entry_exists,
    refresh_desktop_entries as _refresh_desktop_entries,
)
from archive import import_profile as _import_archive, export_profile as _export_archive
from proxy_store import validate_url as _validate_proxy_url, _load as _load_proxies

_docker = DockerManager()

# Orphan-profile names already reported to stderr, so a long-running bridge
# polling get_all_profiles() does not repeat the same warning forever.
_WARNED_ORPHANS = set()


def active_profiles_dir() -> str:
    """
    The profile store in use, resolved from THIS module's globals so a caller
    (or test) that rebinds profile_service.CHROME_PROFILES_DIR gets consistent
    behaviour.
    """
    return paths.resolve_profiles_dir(CHROME_PROFILES_DIR, CHROME_PROFILES_DIR_LEGACY)


def _validate_proxy(proxy: str) -> str:
    """Accept a saved proxy name or a custom proxy URL."""
    proxy = proxy.strip()
    if proxy and proxy not in _load_proxies():
        _validate_proxy_url(proxy)
    return proxy


def _read_machine(path: str) -> dict:
    """Read the profile's machine signature (hardware-signature.json) so the
    UI can show its identity. Returns {} when absent or unreadable."""
    sig_path = os.path.join(path, 'hardware-signature.json')
    if not os.path.isfile(sig_path):
        return {}
    try:
        with open(sig_path) as f:
            sig = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    machine = {}
    machine.update(sig.get('hardware', {}))
    machine.update(sig.get('system', {}))
    machine.update(sig.get('browser', {}))
    return machine


def _warn_orphans(active_dir: str) -> None:
    """
    Tell the user, once per name, that profiles exist in the profile store this
    install is NOT using (i.e. an un-migrated ~/Chrome, or leftovers after a
    migration). Those profiles are never used, moved or deleted automatically —
    the fix is scripts/migrate_profiles_dir.py, which is dry-run by default.
    Printed to stderr, which the bridge already owns (stdout is the JSON
    protocol). Reported once per process so a polling UI cannot spam.
    """
    other = paths.other_profiles_dir(active_dir, CHROME_PROFILES_DIR,
                                    CHROME_PROFILES_DIR_LEGACY)
    for name in sorted(paths.orphan_profiles(active_dir, CHROME_PROFILES_DIR,
                                             CHROME_PROFILES_DIR_LEGACY)):
        if name in _WARNED_ORPHANS:
            continue
        _WARNED_ORPHANS.add(name)
        print(
            f"[chrome-isolation] profile '{name}' exists in {other} "
            f"but this install is using {active_dir}, so it is not "
            f"shown. Run scripts/migrate_profiles_dir.py to relocate it.",
            file=sys.stderr,
        )


def get_all_profiles() -> list:
    """
    Merge registry entries with live Docker status and on-disk size.
    Also picks up any on-disk profiles not yet in the registry.
    """
    # Ensure on-disk profiles that exist but aren't in registry are included.
    # Scans the *resolved* store, so a not-yet-migrated install still sees its
    # profiles in ~/Chrome.
    from registry import _load
    data = _load()
    active_dir = active_profiles_dir()
    _warn_orphans(active_dir)
    if os.path.isdir(active_dir):
        for name in os.listdir(active_dir):
            path = os.path.join(active_dir, name)
            if os.path.isdir(path) and name not in data:
                try:
                    validate_profile_name(name)
                    register_profile(name, path)
                    data = _load()
                except ValueError:
                    pass

    # Batch fetch all container statuses in a single Docker API call (O(1) HTTP requests)
    container_statuses = _docker.all_container_statuses()

    profiles = []
    for p in list_profiles():
        configured_host_mount = p.get('host_mount', '')
        profiles.append({
            'name': p['name'],
            'path': p['path'],
            'host_mount': configured_host_mount,
            # Resolved directory actually bind-mounted at /home/chrome/host.
            # Empty host_mount -> the safe, empty, 0700 per-profile share dir;
            # never the user's home directory. Surfaced so the UI can show the
            # real path instead of implying the home dir is mounted.
            'effective_host_mount': (configured_host_mount
                                     or host_share_dir(p['name'])),
            'host_mount_is_default': not bool(configured_host_mount.strip()),
            'host_mount_default_hint': describe_default(p['name']),
            'proxy': p.get('proxy', ''),
            'machine': _read_machine(p['path']),
            'created_at': p.get('created_at', ''),
            'status': container_statuses.get(p['name'], 'not_found'),
            'size_mb': _docker.profile_size_mb(p['name']),
            'has_desktop_entry': desktop_entry_exists(p['name']),
        })
    return profiles


def refresh_desktop_entries() -> dict:
    """
    Re-derive every desktop entry this app owns. Cheap and idempotent: files
    whose content is already correct are not touched, so this is safe to call
    on every app start. Exists because entries used to be created only at
    profile-creation time, so anything written by an older build (e.g. an
    entry that fell back to Icon=google-chrome) stayed wrong on disk forever.
    """
    return _refresh_desktop_entries()


def create_profile(name: str, custom_path: str = '', host_mount: str = '', proxy: str = '') -> dict:
    name = validate_profile_name(name)

    if custom_path:
        path = os.path.realpath(os.path.expanduser(custom_path))
    else:
        # active_profiles_dir(), not the constant: a not-yet-migrated install
        # must keep putting new profiles next to its existing ones rather than
        # half-populating the new store.
        path = safe_profile_path(name, active_profiles_dir())

    if os.path.exists(path):
        raise ValueError(f"Directory already exists: {path}")

    # Optional host folder mounted read-only into the container at
    # /home/chrome/host. Empty means "safe empty per-profile share directory",
    # NOT the user's home directory (T1 / F1 fix). An explicit value is a
    # deliberate user choice but is still validated: no `/`, no system roots,
    # no credential stores, no traversal or symlink escape into them.
    host_mount = validate_host_mount(host_mount)

    proxy = _validate_proxy(proxy)

    os.makedirs(path, exist_ok=True)
    os.makedirs(os.path.join(path, 'Downloads'), exist_ok=True)

    entry = register_profile(name, path, host_mount, proxy)
    create_desktop_entry(name)
    return entry


def _update_signature_timezone(path: str, timezone: str) -> None:
    """Write the timezone into the profile's hardware-signature.json, creating
    the file if the profile has not been launched yet (hardware-spoof.sh
    backfills the remaining machine fields on first launch)."""
    sig_path = os.path.join(path, 'hardware-signature.json')
    sig = {}
    if os.path.isfile(sig_path):
        try:
            with open(sig_path) as f:
                sig = json.load(f)
        except (json.JSONDecodeError, OSError):
            sig = {}
    sig.setdefault('system', {})['timezone'] = timezone
    with open(sig_path, 'w') as f:
        json.dump(sig, f, indent=2)


def update_profile(name: str, host_mount: str = None, proxy: str = None,
                   timezone: str = None) -> dict:
    """Update a profile's read-only host mount, proxy, and/or timezone.
    Empty string clears a field (host mount falls back to the safe empty
    per-profile share directory, timezone to the machine signature default).
    A non-empty host mount is validated: `/` and other system roots, credential
    stores, and traversal/symlink escapes into them are rejected.
    See backend/host_share.py."""
    name = validate_profile_name(name)

    if host_mount is not None:
        host_mount = validate_host_mount(host_mount)

    if proxy is not None:
        proxy = _validate_proxy(proxy)

    if timezone is not None:
        timezone = timezone.strip()
        if timezone and timezone not in zoneinfo.available_timezones():
            raise ValueError(f"Invalid timezone: {timezone}")
        if timezone:
            _update_signature_timezone(resolve_profile_path(name), timezone)

    return _update_registry(name, host_mount=host_mount, proxy=proxy)


def delete_profile(name: str) -> dict:
    name = validate_profile_name(name)
    _docker.stop_container(name)
    _docker.remove_container(name)
    remove_desktop_entry(name)

    path = resolve_profile_path(name)
    if os.path.isdir(path):
        shutil.rmtree(path)

    unregister_profile(name)
    return {"status": "deleted", "name": name}


def start_profile(name: str) -> dict:
    name = validate_profile_name(name)
    return _docker.start_container(name)


def stop_profile(name: str) -> dict:
    name = validate_profile_name(name)
    return _docker.stop_container(name)


def profile_status(name: str) -> dict:
    name = validate_profile_name(name)
    return {
        "name": name,
        "status": _docker.container_status(name),
        "size_mb": _docker.profile_size_mb(name),
    }


def import_profile(archive_path: str) -> dict:
    return _import_archive(archive_path)


def export_profile(name: str, dest_path: str) -> dict:
    name = validate_profile_name(name)
    out = _export_archive(name, dest_path)
    return {"status": "exported", "path": out}
