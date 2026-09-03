#!/usr/bin/env python3
"""Tests for backend/archive.py — run with: python3 tests/test_archive.py

Monkeypatches the module-level CHROME_PROFILES_DIR / REGISTRY_FILE bindings
(archive.py and registry.py import them from config at module load, so the
effective way to redirect them is to patch the importing modules).
"""
import os
import shutil
import stat
import sys
import tempfile
import traceback
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backend'))

import archive
import registry


def _setup():
    tmp = tempfile.mkdtemp(prefix='archive-test-')
    archive.CHROME_PROFILES_DIR = os.path.join(tmp, 'profiles')
    registry.REGISTRY_FILE = os.path.join(tmp, 'profiles.json')
    registry.CHROME_PROFILES_DIR = archive.CHROME_PROFILES_DIR
    os.makedirs(archive.CHROME_PROFILES_DIR, exist_ok=True)
    return tmp


def _make_zip(path, members):
    """members: list of (arcname, data, symlink_target_or_None)."""
    with zipfile.ZipFile(path, 'w') as zf:
        for arcname, data, symlink in members:
            if symlink is not None:
                info = zipfile.ZipInfo(arcname)
                info.create_system = 3  # unix
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, symlink)
            else:
                zf.writestr(arcname, data)


def test_traversal_zip_rejected():
    tmp = _setup()
    try:
        zpath = os.path.join(tmp, 'evil.zip')
        _make_zip(zpath, [('../evil', 'pwned', None)])
        try:
            archive.import_profile(zpath)
            assert False, "expected ValueError for traversal zip"
        except ValueError:
            pass
        # nothing may have been extracted into the profiles dir
        assert os.listdir(archive.CHROME_PROFILES_DIR) == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_symlink_zip_rejected():
    tmp = _setup()
    try:
        zpath = os.path.join(tmp, 'symlink.zip')
        _make_zip(zpath, [
            ('profile1', '', None),
            ('profile1/link', '', '/etc/passwd'),
        ])
        try:
            archive.import_profile(zpath)
            assert False, "expected ValueError for symlink zip"
        except ValueError:
            pass
        assert os.listdir(archive.CHROME_PROFILES_DIR) == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_tar_traversal_rejected():
    import tarfile
    tmp = _setup()
    try:
        tpath = os.path.join(tmp, 'evil.tar')
        with tarfile.open(tpath, 'w') as tf:
            info = tarfile.TarInfo('../evil')
            data = b'pwned'
            info.size = len(data)
            tf.addfile(info, __import__('io').BytesIO(data))
        try:
            archive.import_profile(tpath)
            assert False, "expected ValueError for traversal tar"
        except ValueError:
            pass
        assert os.listdir(archive.CHROME_PROFILES_DIR) == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_export_import_round_trip():
    tmp = _setup()
    tmp2 = None
    try:
        # Create a profile dir on disk (not registered)
        prof = os.path.join(archive.CHROME_PROFILES_DIR, 'alice')
        os.makedirs(os.path.join(prof, 'Downloads'), exist_ok=True)
        with open(os.path.join(prof, 'Preferences'), 'w') as f:
            f.write('{"x": 1}')

        # Export to a zip
        dest = os.path.join(tmp, 'out.zip')
        out = archive.export_profile('alice', dest)
        assert out == dest
        assert os.path.isfile(dest)

        # Import into a fresh profiles dir / registry
        tmp2 = tempfile.mkdtemp(prefix='archive-test2-')
        archive.CHROME_PROFILES_DIR = os.path.join(tmp2, 'profiles')
        registry.REGISTRY_FILE = os.path.join(tmp2, 'profiles.json')
        registry.CHROME_PROFILES_DIR = archive.CHROME_PROFILES_DIR
        os.makedirs(archive.CHROME_PROFILES_DIR, exist_ok=True)

        entry = archive.import_profile(dest)
        assert entry['name'] == 'alice'
        assert os.path.isfile(os.path.join(entry['path'], 'Preferences'))
        assert os.path.isdir(os.path.join(entry['path'], 'Downloads'))
        # registered in the fresh registry
        assert registry.get_profile('alice') == entry
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if tmp2:
            shutil.rmtree(tmp2, ignore_errors=True)


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith('test_') and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()