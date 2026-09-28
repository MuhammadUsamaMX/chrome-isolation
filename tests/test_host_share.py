#!/usr/bin/env python3
"""Tests for backend/host_share.py — run with: python3 tests/test_host_share.py
or: python3 -m pytest tests/test_host_share.py

Covers the T1 / F1 fix: the container's /home/chrome/host must never default to
the user's home directory, and user-supplied mounts are validated.
"""
import os
import shutil
import stat
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backend'))

import host_share


def _with_tmp_shares():
    """Point host_share.HOST_SHARES_DIR at a throwaway root."""
    tmp = tempfile.mkdtemp(prefix='host-share-test-')
    host_share.HOST_SHARES_DIR = os.path.join(tmp, 'host-shares')
    os.makedirs(host_share.HOST_SHARES_DIR, exist_ok=True)
    return tmp


def _raises(fn, *args):
    try:
        fn(*args)
    except ValueError as e:
        return str(e)
    except Exception as e:  # wrong exception type is also a failure
        raise AssertionError(f"expected ValueError, got {type(e).__name__}: {e}")
    raise AssertionError("expected ValueError, but no exception was raised")


# ── default: empty host_mount -> safe, empty, 0700 per-profile dir ───────────
def test_empty_host_mount_yields_safe_dir():
    tmp = _with_tmp_shares()
    try:
        resolved = host_share.resolve_host_mount('Fiverr-Read', '')
        expected = os.path.join(host_share.HOST_SHARES_DIR, 'Fiverr-Read')
        assert resolved == expected, (resolved, expected)
        assert os.path.isdir(resolved), resolved
        assert os.listdir(resolved) == [], 'default share dir must start empty'
        mode = stat.S_IMODE(os.stat(resolved).st_mode)
        assert mode == 0o700, oct(mode)
        # It is NOT the home directory
        assert os.path.realpath(resolved) != os.path.realpath(os.path.expanduser('~'))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missing_and_none_host_mount_yield_safe_dir():
    tmp = _with_tmp_shares()
    try:
        for value in (None, '   ', '\t\n'):
            resolved = host_share.resolve_host_mount('Zoom-1', value)
            assert resolved == os.path.join(host_share.HOST_SHARES_DIR, 'Zoom-1'), resolved
            assert os.path.isdir(resolved)
        # default arg (no configured value at all)
        resolved = host_share.resolve_host_mount('Zoom-1')
        assert resolved == os.path.join(host_share.HOST_SHARES_DIR, 'Zoom-1'), resolved
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_safe_dir_is_created_on_demand_and_tightened():
    tmp = _with_tmp_shares()
    try:
        path = host_share.ensure_host_share_dir('Loose')
        assert os.path.isdir(path)
        # Simulate a pre-existing, world-readable dir; it must be tightened.
        os.chmod(path, 0o755)
        host_share.ensure_host_share_dir('Loose')
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o700, oct(mode)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_host_share_dir_path_format():
    tmp = _with_tmp_shares()
    try:
        path = host_share.host_share_dir('Fiverr-Demo')
        assert path.endswith(os.path.join('host-shares', 'Fiverr-Demo')), path
        # Non-creating accessor must not create anything
        assert not os.path.exists(path), path
        # Bad profile names are still rejected
        assert _raises(host_share.host_share_dir, '../evil')
        assert _raises(host_share.host_share_dir, '')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── explicit host_mount is honoured ─────────────────────────────────────────
def test_explicit_host_mount_honoured():
    tmp = _with_tmp_shares()
    try:
        share = os.path.join(tmp, 'my-share')
        os.makedirs(share)
        resolved = host_share.resolve_host_mount('Work', share)
        assert resolved == os.path.realpath(share), resolved
        assert resolved != host_share.ensure_host_share_dir('Work')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_explicit_host_mount_nonexistent_rejected():
    tmp = _with_tmp_shares()
    try:
        msg = _raises(host_share.validate_host_mount,
                      os.path.join(tmp, 'nope-not-here'))
        assert 'not found' in msg.lower(), msg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── `/` and other dangerous roots are rejected ──────────────────────────────
def test_root_rejected():
    for spelling in ('/', '//', '/.', '/./'):
        msg = _raises(host_share.validate_host_mount, spelling)
        assert 'Refusing' in msg, (spelling, msg)


def test_system_roots_rejected():
    for bad in ('/etc', '/home', '/root', '/usr', '/var', '/proc', '/sys',
                '/boot', '/dev', '/bin', '/sbin', '/lib', '/opt', '/run',
                '/srv'):
        msg = _raises(host_share.validate_host_mount, bad)
        assert 'Refusing' in msg or 'not found' in msg, (bad, msg)


def test_system_root_subtrees_rejected():
    for bad in ('/etc/ssh', '/usr/share', '/var/lib', '/root/.ssh'):
        msg = _raises(host_share.validate_host_mount, bad)
        assert 'Refusing' in msg, (bad, msg)


def test_resolve_rejects_root_explicit():
    tmp = _with_tmp_shares()
    try:
        _raises(host_share.resolve_host_mount, 'Rooted', '/')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── traversal / symlink escape ──────────────────────────────────────────────
def test_dotdot_traversal_rejected():
    tmp = _with_tmp_shares()
    try:
        base = os.path.join(tmp, 'a', 'b', 'share')
        os.makedirs(base)
        # Climb to the filesystem root, then descend into a forbidden subtree.
        climb = os.path.join(*(['..'] * len(os.path.abspath(base).split(os.sep))))
        assert os.path.realpath(climb) == os.sep, climb
        msg = _raises(host_share.validate_host_mount, os.path.join(climb, 'etc'))
        assert 'Refusing' in msg, msg
        # Climbing to the root itself is refused too
        msg = _raises(host_share.validate_host_mount, climb)
        assert 'Refusing' in msg, msg
        # A shorter hop that escapes /tmp into a forbidden subtree
        msg = _raises(host_share.validate_host_mount,
                      os.path.join(tmp, '..', '..', 'etc', 'ssh'))
        assert 'Refusing' in msg, msg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_symlink_escape_to_root_rejected():
    tmp = _with_tmp_shares()
    try:
        link = os.path.join(tmp, 'innocent-looking')
        os.symlink('/', link)
        msg = _raises(host_share.validate_host_mount, link)
        assert 'Refusing' in msg, msg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_symlink_escape_to_etc_rejected():
    tmp = _with_tmp_shares()
    try:
        link = os.path.join(tmp, 'shared-docs')
        os.symlink('/etc', link)
        msg = _raises(host_share.validate_host_mount, link)
        assert 'Refusing' in msg, msg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_symlink_escape_to_ssh_rejected():
    tmp = _with_tmp_shares()
    try:
        link = os.path.join(tmp, 'keys')
        os.symlink(os.path.expanduser('~/.ssh'), link)
        msg = _raises(host_share.validate_host_mount, link)
        assert 'credential store' in msg or 'Refusing' in msg, msg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_credential_stores_rejected_directly():
    home = os.path.expanduser('~')
    for sub in ('.ssh', '.gnupg', '.aws', '.azure', '.password-store'):
        path = os.path.join(home, sub)
        if not os.path.exists(path):
            continue
        msg = _raises(host_share.validate_host_mount, path)
        assert 'credential store' in msg, (sub, msg)


def test_non_string_rejected():
    assert _raises(host_share.validate_host_mount, 123) is not None
    assert _raises(host_share.validate_host_mount, ['/tmp']) is not None


# ── profile_service integration: create_profile / update_profile ────────────
def test_profile_service_rejects_root_host_mount():
    """create_profile and update_profile must refuse a '/' host mount."""
    import profile_service
    tmp = _with_tmp_shares()
    real_profiles_dir = profile_service.CHROME_PROFILES_DIR
    real_registry_file = __import__('registry').REGISTRY_FILE
    try:
        reg = __import__('registry')
        reg.REGISTRY_FILE = os.path.join(tmp, 'profiles.json')
        reg.CHROME_PROFILES_DIR = os.path.join(tmp, 'profiles')
        os.makedirs(reg.CHROME_PROFILES_DIR, exist_ok=True)
        profile_service.CHROME_PROFILES_DIR = os.path.join(tmp, 'profiles')

        msg = _raises(profile_service.create_profile, 'rootmount', '', '/')
        assert 'Refusing' in msg, msg

        # A safe explicit dir is accepted and stored verbatim
        share = os.path.join(tmp, 'ok-share')
        os.makedirs(share)
        entry = profile_service.create_profile('okshare', '', share, '')
        assert entry['host_mount'] == os.path.realpath(share), entry

        # update_profile also refuses '/'
        msg = _raises(profile_service.update_profile, 'okshare', '/')
        assert 'Refusing' in msg, msg

        # Clearing it back to '' is allowed and means "safe default"
        entry = profile_service.update_profile('okshare', '')
        assert entry['host_mount'] == '', entry
    finally:
        profile_service.CHROME_PROFILES_DIR = real_profiles_dir
        __import__('registry').REGISTRY_FILE = real_registry_file
        shutil.rmtree(tmp, ignore_errors=True)


# ── registry migration is non-destructive ───────────────────────────────────
def test_registry_does_not_rewrite_empty_host_mount():
    tmp = _with_tmp_shares()
    try:
        import json
        import registry
        real_file = registry.REGISTRY_FILE
        real_dir = registry.CHROME_PROFILES_DIR
        registry.REGISTRY_FILE = os.path.join(tmp, 'profiles.json')
        registry.CHROME_PROFILES_DIR = os.path.join(tmp, 'profiles')
        os.makedirs(registry.CHROME_PROFILES_DIR, exist_ok=True)

        with open(registry.REGISTRY_FILE, 'w') as f:
            json.dump({
                'Fiverr-Read': {'name': 'Fiverr-Read', 'path': '/tmp/x',
                                'host_mount': '', 'proxy': ''},
                'Custom': {'name': 'Custom', 'path': '/tmp/y',
                           'host_mount': '/tmp/custom', 'proxy': ''},
            }, f)
        before = open(registry.REGISTRY_FILE).read()
        mtime_before = os.stat(registry.REGISTRY_FILE).st_mtime_ns

        data = registry._load()
        after = open(registry.REGISTRY_FILE).read()
        mtime_after = os.stat(registry.REGISTRY_FILE).st_mtime_ns

        # Value untouched...
        assert data['Fiverr-Read']['host_mount'] == '', data
        # ...file untouched (not even rewritten byte-for-byte)...
        assert after == before, 'profiles.json must not be rewritten on load'
        assert mtime_after == mtime_before, 'profiles.json must not be rewritten on load'
        # ...but it IS tracked, so callers/UI can report the safe default.
        assert 'Fiverr-Read' in registry.default_host_mount_profiles(), \
            registry.default_host_mount_profiles()
        assert 'Custom' not in registry.default_host_mount_profiles()
    finally:
        registry.REGISTRY_FILE = real_file
        registry.CHROME_PROFILES_DIR = real_dir
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
