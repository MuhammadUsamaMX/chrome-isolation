"""
Profile name and path validation — S1 fix.
Applied to EVERY operation that accepts a profile_name.
"""
import os
import re
from config import CHROME_PROFILES_DIR, resolve_profiles_dir

# Only letters, digits, dash, underscore. 1–64 chars.
_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


def validate_profile_name(name: str) -> str:
    """
    Return the validated name or raise ValueError.
    Rejects empty, too-long, and any non-allowed characters.
    """
    if not name or not isinstance(name, str):
        raise ValueError("Profile name must be a non-empty string.")
    name = name.strip()
    if not _NAME_RE.match(name):
        raise ValueError(
            "Invalid profile name. Use only letters, numbers, dash (-) and underscore (_). "
            "Max 64 characters."
        )
    return name


def safe_profile_path(name: str, base: str = None) -> str:
    """
    Compute the profile directory path and confirm it is inside `base`.
    Raises ValueError if the resolved path escapes the base directory.
    This blocks any traversal attempt (e.g. name='../evil').

    `base` defaults to the profile store actually in use, resolved at call time
    (not import time) so a caller which rebinds config's constants — or a test
    which sets up a different store — gets consistent behaviour.
    """
    name = validate_profile_name(name)
    if base is None:
        base = resolve_profiles_dir()
    base = os.path.realpath(base)
    candidate = os.path.realpath(os.path.join(base, name))
    # Must start with base + separator to prevent base itself being returned
    if not candidate.startswith(base + os.sep) and candidate != base:
        raise ValueError(f"Profile path escapes managed directory: {candidate}")
    return candidate
