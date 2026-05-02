"""
Safe archive import/export — S2 fix.
ZIP and TAR archives are validated member-by-member before extraction.
Path traversal, absolute paths, symlinks, and device files are all rejected.
Extraction happens into a temp directory first; only on success is it moved.
"""
import os
import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import PurePosixPath

from config import CHROME_PROFILES_DIR
from validator import validate_profile_name
from registry import register_profile, resolve_profile_path

_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')
_EXCLUDE_DIRS = {'Cache', 'Code Cache', 'GPUCache', 'ShaderCache', 'GrShaderCache'}
_EXCLUDE_FILES = {'SingletonLock', 'SingletonCookie', 'Lock', 'LOCK'}


def _safe_member_path(member_path: str) -> str:
    """
    Validate and normalize an archive member path.
    Raises ValueError on any traversal or absolute path.
    Returns the normalized relative path string.
    """
    # Reject absolute paths
    if os.path.isabs(member_path):
        raise ValueError(f"Absolute path in archive member: {member_path}")
    # Normalize without the OS filesystem
    parts = PurePosixPath(member_path).parts
    # Reject any '..' component
    if any(p == '..' for p in parts):
        raise ValueError(f"Path traversal in archive member: {member_path}")
    return member_path


def _extract_profile_name_from_members(names: list) -> str:
    """
    Derive the profile name from archive member paths.
    Requires exactly one top-level directory that matches the name rules.
    """
    top_level = set()
    for n in names:
        parts = PurePosixPath(n).parts
        if parts:
            top_level.add(parts[0])
    # Remove anything that looks like a file (has extension at top level)
    top_dirs = {t for t in top_level if '.' not in t or '/' in t.replace('\\', '/')}
    if len(top_dirs) != 1:
        raise ValueError(
            f"Archive must contain exactly one top-level profile directory. "
            f"Found: {top_dirs}"
        )
    name = top_dirs.pop()
    return validate_profile_name(name)


def import_profile(archive_path: str) -> dict:
    """
    Safely import a profile archive (.zip or .tar.gz/.tgz).
    Returns the profile info dict on success.
    """
    if not os.path.isfile(archive_path):
        raise FileNotFoundError(f"Archive not found: {archive_path}")

    tmp_dir = tempfile.mkdtemp(prefix="chrome-isolation-import-")
    try:
        if zipfile.is_zipfile(archive_path):
            _import_zip(archive_path, tmp_dir)
        elif tarfile.is_tarfile(archive_path):
            _import_tar(archive_path, tmp_dir)
        else:
            raise ValueError("Unsupported archive format. Use .zip or .tar.gz")

        # Exactly one directory should be present in tmp_dir
        entries = os.listdir(tmp_dir)
        dirs = [e for e in entries if os.path.isdir(os.path.join(tmp_dir, e))]
        if len(dirs) != 1:
            raise ValueError(f"Expected one profile directory, found: {dirs}")

        profile_name = validate_profile_name(dirs[0])
        dest = os.path.join(CHROME_PROFILES_DIR, profile_name)

        if os.path.exists(dest):
            raise ValueError(f"Profile '{profile_name}' already exists.")

        # Move the validated temp dir into the real location
        shutil.move(os.path.join(tmp_dir, profile_name), dest)

        # Ensure Downloads subdir
        os.makedirs(os.path.join(dest, "Downloads"), exist_ok=True)

        entry = register_profile(profile_name, dest)
        return entry

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _import_zip(archive_path: str, dest_dir: str) -> None:
    with zipfile.ZipFile(archive_path, 'r') as zf:
        members = zf.infolist()
        if not members:
            raise ValueError("Archive is empty.")

        for info in members:
            _safe_member_path(info.filename)

            # Reject symlinks inside zip (external_attr encodes unix mode)
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if unix_mode and (unix_mode & 0xA000) == 0xA000:  # S_ISLNK
                raise ValueError(f"Symlink in archive: {info.filename}")

        # Extract validated archive
        zf.extractall(dest_dir)


def _import_tar(archive_path: str, dest_dir: str) -> None:
    with tarfile.open(archive_path, 'r:*') as tf:
        members = tf.getmembers()
        if not members:
            raise ValueError("Archive is empty.")

        for m in members:
            _safe_member_path(m.name)

            # Reject symlinks, hard links, device and block files
            if m.issym() or m.islnk():
                raise ValueError(f"Symlink/hardlink in archive: {m.name}")
            if m.isdev() or m.ischr() or m.isblk():
                raise ValueError(f"Device file in archive: {m.name}")

        tf.extractall(dest_dir, filter="data")


def export_profile(profile_name: str, dest_path: str) -> str:
    """
    Export a profile as a .zip archive to dest_path.
    Returns the path to the created zip file.
    """
    profile_name = validate_profile_name(profile_name)
    profile_dir = resolve_profile_path(profile_name)
    if not os.path.isdir(profile_dir):
        raise FileNotFoundError(f"Profile directory not found: {profile_dir}")

    if not dest_path.endswith('.zip'):
        dest_path += '.zip'

    with zipfile.ZipFile(dest_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(profile_dir):
            # Skip cache dirs
            dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS]

            for fname in files:
                if fname in _EXCLUDE_FILES:
                    continue
                fpath = os.path.join(root, fname)

                # Skip symlinks pointing outside profile
                if os.path.islink(fpath):
                    target = os.path.realpath(fpath)
                    if not target.startswith(os.path.realpath(profile_dir) + os.sep):
                        continue

                if not os.path.isfile(fpath):
                    continue

                try:
                    arcname = os.path.join(
                        profile_name,
                        os.path.relpath(fpath, profile_dir)
                    )
                    zf.write(fpath, arcname)
                except (OSError, PermissionError):
                    continue

    return dest_path
