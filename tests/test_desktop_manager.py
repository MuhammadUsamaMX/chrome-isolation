#!/usr/bin/env python3
"""Tests for backend/desktop_manager.py — run with: python3 tests/test_desktop_manager.py"""
import os
import shutil
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backend'))

import desktop_manager


def _setup():
    tmp = tempfile.mkdtemp(prefix='desktop-test-')
    desktop_manager.DESKTOP_ENTRIES_DIR = os.path.join(tmp, 'applications')
    os.makedirs(desktop_manager.DESKTOP_ENTRIES_DIR, exist_ok=True)
    return tmp


def test_create_and_remove_desktop_entry():
    tmp = _setup()
    try:
        profile_name = 'test_prof'
        assert not desktop_manager.desktop_entry_exists(profile_name)

        # Test creation
        res = desktop_manager.create_desktop_entry(profile_name)
        assert res['status'] == 'created'
        assert os.path.exists(res['path'])
        assert desktop_manager.desktop_entry_exists(profile_name)

        with open(res['path'], 'r') as f:
            content = f.read()
        assert f"Name=Chrome ({profile_name})" in content
        assert f"--profile {profile_name}" in content

        # Test removal
        rem_res = desktop_manager.remove_desktop_entry(profile_name)
        assert rem_res['status'] == 'removed'
        assert not os.path.exists(res['path'])
        assert not desktop_manager.desktop_entry_exists(profile_name)

        # Test removal when file doesn't exist
        rem_res_2 = desktop_manager.remove_desktop_entry(profile_name)
        assert rem_res_2['status'] == 'not_found'
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
