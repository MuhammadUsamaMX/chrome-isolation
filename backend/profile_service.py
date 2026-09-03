"""
Profile service — high-level operations used by the IPC bridge.
All inputs go through validator.py before any filesystem or Docker call.
"""
import os
import shutil

from config import CHROME_PROFILES_DIR
from validator import validate_profile_name, safe_profile_path
from registry import (
    list_profiles, get_profile, register_profile,
    unregister_profile, resolve_profile_path,
)
from docker_manager import DockerManager
from desktop_manager import (
    create_desktop_entry, remove_desktop_entry, desktop_entry_exists,
)
from archive import import_profile as _import_archive, export_profile as _export_archive

_docker = DockerManager()


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
            'created_at': p.get('created_at', ''),
            'status': _docker.container_status(p['name']),
            'size_mb': _docker.profile_size_mb(p['name']),
            'has_desktop_entry': desktop_entry_exists(p['name']),
        })
    return profiles


def create_profile(name: str, custom_path: str = '', host_mount: str = '') -> dict:
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

    os.makedirs(path, exist_ok=True)
    os.makedirs(os.path.join(path, 'Downloads'), exist_ok=True)

    entry = register_profile(name, path, host_mount)
    create_desktop_entry(name)
    return entry


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
