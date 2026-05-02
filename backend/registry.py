"""
Profile registry — S4 fix.
Single JSON file tracks every profile (name, path, created_at).
All lifecycle operations read from here instead of ad-hoc path computation.
"""
import json
import os
import time
from typing import Optional

from config import REGISTRY_FILE, CHROME_PROFILES_DIR
from validator import validate_profile_name, safe_profile_path


def _load() -> dict:
    if os.path.exists(REGISTRY_FILE):
        try:
            with open(REGISTRY_FILE, "r") as f:
                return json.load(f)
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


def register_profile(name: str, path: str) -> dict:
    """Add a profile to the registry. Raises if already registered."""
    name = validate_profile_name(name)
    data = _load()
    if name in data:
        raise ValueError(f"Profile '{name}' already registered.")
    entry = {
        "name": name,
        "path": path,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    data[name] = entry
    _save(data)
    return entry


def unregister_profile(name: str) -> None:
    name = validate_profile_name(name)
    data = _load()
    data.pop(name, None)
    _save(data)


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
