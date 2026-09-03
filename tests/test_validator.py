#!/usr/bin/env python3
"""Tests for backend/validator.py — run with: python3 tests/test_validator.py"""
import os
import shutil
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backend'))

from validator import validate_profile_name, safe_profile_path


def test_valid_names_pass():
    for name in ('alice', 'Alice-1', 'a_b', '0', 'x' * 64):
        assert validate_profile_name(name) == name, name


def test_empty_rejected():
    for bad in ('', '   ', None):
        try:
            validate_profile_name(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_traversal_rejected():
    for bad in ('../evil', '..', 'a/../b', 'a/b', 'a\\b'):
        try:
            validate_profile_name(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_too_long_rejected():
    try:
        validate_profile_name('x' * 65)
        assert False, "expected ValueError for 65-char name"
    except ValueError:
        pass


def test_bad_chars_rejected():
    for bad in ('a b', 'a.b', 'a@b', 'a:b', 'a$b', 'a(b)', 'a!b', 'a+b'):
        try:
            validate_profile_name(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_safe_profile_path_rejects_traversal():
    base = tempfile.mkdtemp(prefix='validator-test-')
    try:
        for name in ('../evil', '..', 'a/../b'):
            try:
                safe_profile_path(name, base)
                assert False, f"expected ValueError for {name!r}"
            except ValueError:
                pass
        # valid name resolves inside base
        p = safe_profile_path('good', base)
        assert p == os.path.join(base, 'good')
    finally:
        shutil.rmtree(base, ignore_errors=True)


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