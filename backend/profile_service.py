"""
Profile service — high-level operations used by the IPC bridge.
All inputs go through validator.py before any filesystem or Docker call.
"""
import json
import os
import re
import shutil
import zoneinfo

from config import CHROME_PROFILES_DIR
from validator import validate_profile_name, safe_profile_path
from registry import (
    list_profiles, get_profile, register_profile, update_profile as _update_registry,
    unregister_profile, resolve_profile_path,
)
from docker_manager import DockerManager
from desktop_manager import (
    create_desktop_entry, remove_desktop_entry, desktop_entry_exists,
)
from archive import import_profile as _import_archive, export_profile as _export_archive

_docker = DockerManager()

# socks5://host:port, socks5h://host:port, http://host:port, https://host:port
# with optional user:pass@ credentials.
_PROXY_RE = re.compile(
    r'^(socks5|socks5h|http|https)://([^:@/]+(:[^:@/]+)?@)?[a-zA-Z0-9._-]+:\d{1,5}$'
)


def _validate_proxy(proxy: str) -> str:
    proxy = proxy.strip()
    if proxy and not _PROXY_RE.match(proxy):
        raise ValueError(
            "Invalid proxy. Use e.g. socks5://127.0.0.1:1080 or http://user:pass@host:8080"
        )
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


def get_all_profiles() -> list:
    """
    Merge registry entries with live Docker status and on-disk size.
    Also picks up any on-disk profiles not yet in the registry.
    """
    # Ensure on-disk profiles that exist but aren't in registry are included
    from registry import _load
    data = _load()
    if os.path.isdir(CHROME_PROFILES_DIR):
        for name in os.listdir(CHROME_PROFILES_DIR):
            path = os.path.join(CHROME_PROFILES_DIR, name)
            if os.path.isdir(path) and name not in data:
                try:
                    validate_profile_name(name)
                    register_profile(name, path)
                    data = _load()
                except ValueError:
                    pass

    profiles = []
    for p in list_profiles():
        profiles.append({
            'name': p['name'],
            'path': p['path'],
            'host_mount': p.get('host_mount', ''),
            'proxy': p.get('proxy', ''),
            'machine': _read_machine(p['path']),
            'created_at': p.get('created_at', ''),
            'status': _docker.container_status(p['name']),
            'size_mb': _docker.profile_size_mb(p['name']),
            'has_desktop_entry': desktop_entry_exists(p['name']),
        })
    return profiles


def create_profile(name: str, custom_path: str = '', host_mount: str = '', proxy: str = '') -> dict:
    name = validate_profile_name(name)

    if custom_path:
        path = os.path.realpath(os.path.expanduser(custom_path))
    else:
        path = safe_profile_path(name, CHROME_PROFILES_DIR)

    if os.path.exists(path):
        raise ValueError(f"Directory already exists: {path}")

    # Optional host folder mounted read-only into the container. Empty means
    # the user's home directory is mounted instead.
    if host_mount:
        host_mount = os.path.realpath(os.path.expanduser(host_mount))
        if not os.path.isdir(host_mount):
            raise ValueError(f"Host folder not found: {host_mount}")

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
    Empty string clears a field (host mount falls back to the home directory,
    timezone falls back to the machine signature default)."""
    name = validate_profile_name(name)

    if host_mount is not None:
        host_mount = host_mount.strip()
        if host_mount:
            host_mount = os.path.realpath(os.path.expanduser(host_mount))
            if not os.path.isdir(host_mount):
                raise ValueError(f"Host folder not found: {host_mount}")

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
