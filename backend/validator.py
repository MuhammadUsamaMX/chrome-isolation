"""
Profile name and path validation — S1 fix.
Applied to EVERY operation that accepts a profile_name.
"""
import os
import re
from config import CHROME_PROFILES_DIR

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


def safe_profile_path(name: str, base: str = CHROME_PROFILES_DIR) -> str:
    """
    Compute the profile directory path and confirm it is inside `base`.
    Raises ValueError if the resolved path escapes the base directory.
    This blocks any traversal attempt (e.g. name='../evil').
    """
    name = validate_profile_name(name)
    base = os.path.realpath(base)
    candidate = os.path.realpath(os.path.join(base, name))
    # Must start with base + separator to prevent base itself being returned
    if not candidate.startswith(base + os.sep) and candidate != base:
        raise ValueError(f"Profile path escapes managed directory: {candidate}")
    return candidate
