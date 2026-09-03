#!/usr/bin/env python3
"""Tests for backend/registry.py — run with: python3 tests/test_registry.py"""
import os
import shutil
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backend'))

import registry


def _setup():
    """Point the registry at a temp file/dir so the real one is untouched."""
    tmp = tempfile.mkdtemp(prefix='registry-test-')
    registry.REGISTRY_FILE = os.path.join(tmp, 'profiles.json')
    registry.CHROME_PROFILES_DIR = os.path.join(tmp, 'profiles')
    os.makedirs(registry.CHROME_PROFILES_DIR, exist_ok=True)
    return tmp


def test_round_trip():
    tmp = _setup()
    try:
        assert registry.list_profiles() == []
        entry = registry.register_profile(
            'alice', os.path.join(registry.CHROME_PROFILES_DIR, 'alice'))
        assert entry['name'] == 'alice'
        assert entry['path'].endswith('alice')
        assert entry['host_mount'] == ''
        assert 'created_at' in entry
        assert registry.get_profile('alice') == entry
        assert registry.resolve_profile_path('alice') == entry['path']
        assert [p['name'] for p in registry.list_profiles()] == ['alice']

        # duplicate registration rejected
        try:
            registry.register_profile('alice', '/somewhere/else')
            assert False, "expected ValueError for duplicate registration"
        except ValueError:
            pass

        registry.unregister_profile('alice')
        assert registry.list_profiles() == []
        assert registry.get_profile('alice') is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_host_mount_round_trip():
    tmp = _setup()
    try:
        host = os.path.join(tmp, 'host-files')
        os.makedirs(host, exist_ok=True)
        entry = registry.register_profile(
            'dave', os.path.join(registry.CHROME_PROFILES_DIR, 'dave'), host_mount=host)
        assert entry['host_mount'] == host, entry
        assert registry.get_profile('dave')['host_mount'] == host

        # Persisted to disk
        data = registry._load()
        assert data['dave']['host_mount'] == host, data
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_update_profile():
    tmp = _setup()
    try:
        registry.register_profile('erin', os.path.join(registry.CHROME_PROFILES_DIR, 'erin'))
        host = os.path.join(tmp, 'host-files')
        os.makedirs(host, exist_ok=True)

        # Update both fields
        entry = registry.update_profile('erin', host_mount=host, proxy='socks5://127.0.0.1:1080')
        assert entry['host_mount'] == host, entry
        assert entry['proxy'] == 'socks5://127.0.0.1:1080', entry

        # None leaves a field unchanged
        entry = registry.update_profile('erin', proxy='')
        assert entry['host_mount'] == host, entry
        assert entry['proxy'] == '', entry

        # Unknown profile rejected
        try:
            registry.update_profile('nobody', host_mount=host)
            assert False, "expected ValueError for unknown profile"
        except ValueError:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_resolve_fallback():
    tmp = _setup()
    try:
        # Unregistered name falls back to the default managed path (not created)
        p = registry.resolve_profile_path('bob')
        assert p == os.path.join(registry.CHROME_PROFILES_DIR, 'bob')
        assert not os.path.exists(p)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_persistence():
    tmp = _setup()
    try:
        registry.register_profile('carol', os.path.join(registry.CHROME_PROFILES_DIR, 'carol'))
        # Reload from disk — simulates a fresh process
        data = registry._load()
        assert 'carol' in data
        assert data['carol']['name'] == 'carol'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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