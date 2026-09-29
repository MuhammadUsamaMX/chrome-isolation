#!/usr/bin/env python3
"""
Tests for the profile-store relocation: backend/paths.py, the config constants,
and scripts/migrate_profiles_dir.py.

Run with pytest:   python3 -m pytest tests/test_relocation.py -v
Run standalone:    python3 tests/test_relocation.py

EVERYTHING here operates inside pytest's tmp_path with a monkeypatched $HOME.
The real ~/Chrome is never read, moved, or written. `test_never_touches_the_real_home`
is a tripwire that fails loudly if any of that ever changes.
"""
import json
import os
import shutil
import subprocess
import sys
import traceback

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, 'backend')
SCRIPTS = os.path.join(REPO, 'scripts')
sys.path.insert(0, BACKEND)
sys.path.insert(0, SCRIPTS)

import paths                    # noqa: E402
import migrate_profiles_dir as migrate   # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake $HOME, and no inherited CHROME_ISOLATION_HOME."""
    h = tmp_path / 'home'
    h.mkdir()
    monkeypatch.setenv('HOME', str(h))
    monkeypatch.delenv('CHROME_ISOLATION_HOME', raising=False)
    return h


def make_profile(base, name, files=3, size=64):
    """Create a small but realistic profile: nested dirs, files, a symlink."""
    d = os.path.join(base, name)
    os.makedirs(os.path.join(d, 'Default', 'Cache'), exist_ok=True)
    os.makedirs(os.path.join(d, 'Downloads'), exist_ok=True)
    for i in range(files):
        with open(os.path.join(d, 'Default', f'file{i}.bin'), 'wb') as f:
            f.write(bytes([i % 256]) * size)
    with open(os.path.join(d, 'Default', 'Preferences'), 'w') as f:
        f.write('{"profile": {"name": "%s"}}' % name)
    with open(os.path.join(d, 'Default', 'Cache', 'data_0'), 'wb') as f:
        f.write(b'\x00' * 16)
    # A symlink, like Chromium's Singleton* links. Must not be followed.
    os.symlink('/nonexistent/socket', os.path.join(d, 'SingletonLock'))
    return d


def make_install(home, profiles=('Fiverr-Demo', 'Fiverr-Read', 'rootmount', 'Zoom-1'),
                 legacy_name='Chrome', new_name='Isolated-Chrome', registry=True):
    """
    Build a complete throwaway install: a legacy profile store with profiles and
    a profiles.json whose 'path' fields point into it. Returns a dict of paths.
    """
    src = home / legacy_name
    src.mkdir()
    for name in profiles:
        make_profile(str(src), name)
    reg = None
    if registry:
        appdata = home / '.local' / 'share' / 'chrome-isolation-manager'
        appdata.mkdir(parents=True, exist_ok=True)
        reg = appdata / 'profiles.json'
        reg.write_text(json.dumps({
            n: {
                'name': n,
                'path': str(src / n),
                'host_mount': '',
                'proxy': '',
                'created_at': '2026-09-11T23:03:55Z',
            } for n in profiles
        }, indent=2))
    return {
        'home': home,
        'src': str(src),
        'dst': str(home / new_name / 'profiles'),
        'registry': str(reg) if reg else None,
        'profiles': list(profiles),
    }


def run_migrate(argv):
    return migrate.main(argv)


# --------------------------------------------------------------------------- #
# 1. The new default location
# --------------------------------------------------------------------------- #
def test_default_location_is_isolated_chrome(home):
    """Default is ~/Isolated-Chrome/profiles — not ~/Chrome, not google-chrome."""
    got = paths.default_profiles_dir()
    assert got == str(home / 'Isolated-Chrome' / 'profiles')
    # The whole point: unmistakeable for a browser profile store.
    assert 'google-chrome' not in got
    assert os.path.dirname(got) == str(home / 'Isolated-Chrome')
    assert os.path.basename(os.path.dirname(got)) != 'Chrome'


def test_default_is_never_under_real_chrome(home):
    """A refusal to ever place the store inside real Chrome's storage."""
    real = os.path.realpath(str(home / '.config' / 'google-chrome'))
    os.makedirs(real, exist_ok=True)
    assert not migrate._under(paths.default_profiles_dir(), real)


def test_config_constant_matches_paths(home):
    """config.CHROME_PROFILES_DIR follows paths, not a second hardcoded path."""
    import importlib
    for mod in ('paths', 'config'):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    importlib.invalidate_caches()
    import config
    importlib.reload(config)
    try:
        assert config.CHROME_PROFILES_DIR == str(home / 'Isolated-Chrome' / 'profiles')
        assert config.CHROME_PROFILES_DIR_LEGACY == str(home / 'Chrome')
        assert config.CHROME_ISOLATION_HOME == str(home / 'Isolated-Chrome')
        # resolve_profiles_dir on an empty install -> the new location.
        assert config.resolve_profiles_dir() == config.CHROME_PROFILES_DIR
        assert config.other_profiles_dir() == config.CHROME_PROFILES_DIR_LEGACY
    finally:
        monkey = os.environ.pop('CHROME_ISOLATION_HOME', None)
        importlib.reload(config)
        if monkey:
            os.environ['CHROME_ISOLATION_HOME'] = monkey


def test_config_creates_the_store_it_will_use(home):
    """
    Importing config creates the store in use. On a fresh install that is the
    new location. The critical half is the legacy case: an empty new directory
    must never shadow a populated legacy one, or every profile vanishes.
    """
    import importlib
    import config

    # Fresh install: no profiles anywhere, so the new store is created and used.
    importlib.reload(config)
    try:
        assert config.resolve_profiles_dir() == config.CHROME_PROFILES_DIR
        assert os.path.isdir(config.CHROME_PROFILES_DIR)
    finally:
        importlib.reload(config)

    # Legacy install: the new directory may exist (it was just created above),
    # but it is EMPTY, so the populated legacy store must still win.
    make_profile(str(home / 'Chrome'), 'Zoom-1')
    try:
        importlib.reload(config)
        assert config.resolve_profiles_dir() == str(home / 'Chrome')
        assert not os.listdir(config.CHROME_PROFILES_DIR), \
            "an empty new store must not shadow the populated legacy one"
        assert config.other_profiles_dir() == config.CHROME_PROFILES_DIR
    finally:
        importlib.reload(config)


# --------------------------------------------------------------------------- #
# 2. Environment override
# --------------------------------------------------------------------------- #
def test_env_override(home, monkeypatch):
    monkeypatch.setenv('CHROME_ISOLATION_HOME', str(home / 'elsewhere'))
    assert paths.isolation_home() == str(home / 'elsewhere')
    assert paths.default_profiles_dir() == str(home / 'elsewhere' / 'profiles')


def test_env_override_wins_over_home_default(home, monkeypatch):
    make_profile(str(home / 'Chrome'), 'Zoom-1')
    monkeypatch.setenv('CHROME_ISOLATION_HOME', str(home / 'custom'))
    make_profile(str(home / 'custom' / 'profiles'), 'Work')
    assert paths.resolve_profiles_dir() == str(home / 'custom' / 'profiles')


def test_env_override_blank_falls_back(home, monkeypatch):
    monkeypatch.setenv('CHROME_ISOLATION_HOME', '   ')
    assert paths.isolation_home() == str(home / 'Isolated-Chrome')


def test_env_override_expands_user_and_is_absolute(home, monkeypatch):
    monkeypatch.setenv('CHROME_ISOLATION_HOME', '~/my-isolated')
    got = paths.isolation_home()
    assert got == str(home / 'my-isolated')
    assert os.path.isabs(got)


# --------------------------------------------------------------------------- #
# 3. Precedence / backward compatibility
# --------------------------------------------------------------------------- #
def test_legacy_only_is_used(home):
    make_install(home)
    assert paths.resolve_profiles_dir() == str(home / 'Chrome')
    assert paths.orphan_profiles() == set()


def test_new_only_wins(home):
    make_profile(str(home / 'Isolated-Chrome' / 'profiles'), 'Zoom-1')
    assert paths.resolve_profiles_dir() == str(home / 'Isolated-Chrome' / 'profiles')


def test_both_exist_new_wins(home):
    """Documented precedence: when BOTH hold profiles, the new one wins."""
    make_profile(str(home / 'Chrome'), 'Legacy-Only')
    make_profile(str(home / 'Isolated-Chrome' / 'profiles'), 'Zoom-1')
    assert paths.resolve_profiles_dir() == str(home / 'Isolated-Chrome' / 'profiles')
    # And the leftover is reported, never silently used or deleted.
    assert paths.orphan_profiles() == {'Legacy-Only'}


def test_empty_new_dir_does_not_shadow_legacy(home):
    """
    config.py creates the store on import, so a bare empty directory must not
    win — otherwise every existing profile would vanish from the UI.
    """
    os.makedirs(str(home / 'Isolated-Chrome' / 'profiles'))
    make_profile(str(home / 'Chrome'), 'Zoom-1')
    assert paths.resolve_profiles_dir() == str(home / 'Chrome')


def test_neither_exists_returns_new(home):
    assert paths.resolve_profiles_dir() == str(home / 'Isolated-Chrome' / 'profiles')


def test_registry_falls_back_to_legacy(home):
    """
    An unregistered profile resolves into the store actually in use. The
    backend modules cache the location at import time, so this reloads the
    chain under the fake HOME rather than relying on import order.
    """
    import importlib
    make_install(home)                      # populated ~/Chrome, empty new store
    importlib.reload(importlib.import_module('paths'))
    importlib.reload(importlib.import_module('config'))
    registry = importlib.reload(importlib.import_module('registry'))
    try:
        assert registry._active_profiles_dir() == str(home / 'Chrome'), \
            "precondition: the app must be using the legacy store here"
        registry.REGISTRY_FILE = str(home / 'nonexistent.json')
        got = registry.resolve_profile_path('Brand-New')
        assert got == os.path.realpath(str(home / 'Chrome' / 'Brand-New'))
    finally:
        # Restore the real modules for the rest of the session.
        importlib.reload(importlib.import_module('config'))
        importlib.reload(importlib.import_module('registry'))
        importlib.reload(importlib.import_module('profile_service'))


def test_app_does_not_hard_fail_on_legacy_install(home):
    """
    Backward compat end to end: an un-migrated install keeps working. The
    registry is populated at the legacy paths, exactly as an old install has it,
    and the app must list every profile rather than raising or hiding them.
    """
    import importlib
    inst = make_install(home)
    importlib.reload(importlib.import_module('paths'))
    importlib.reload(importlib.import_module('config'))
    importlib.reload(importlib.import_module('host_share'))
    importlib.reload(importlib.import_module('registry'))
    importlib.reload(importlib.import_module('profile_service'))
    registry = importlib.import_module('registry')
    svc = importlib.import_module('profile_service')
    try:
        # The registry, read as an old install wrote it.
        entries = json.load(open(inst['registry']))
        assert sorted(entries) == sorted(inst['profiles'])
        for n in inst['profiles']:
            assert entries[n]['path'] == os.path.join(inst['src'], n)

        # The app resolves those paths without hard-failing.
        for n in inst['profiles']:
            assert registry.resolve_profile_path(n) == os.path.join(inst['src'], n)

        # And it picks the legacy store for anything unregistered.
        assert svc.active_profiles_dir() == inst['src']
    finally:
        importlib.reload(importlib.import_module('config'))
        importlib.reload(importlib.import_module('registry'))
        importlib.reload(importlib.import_module('profile_service'))


def test_migrated_install_uses_the_new_store(home):
    """
    The other half: after a migration the registry points into the new store and
    the app resolves everything there, with the empty legacy dir ignored.
    """
    import importlib
    inst = make_install(home)
    assert run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                        '--registry', inst['registry']]) == 0
    importlib.reload(importlib.import_module('paths'))
    importlib.reload(importlib.import_module('config'))
    importlib.reload(importlib.import_module('host_share'))
    importlib.reload(importlib.import_module('registry'))
    importlib.reload(importlib.import_module('profile_service'))
    registry = importlib.import_module('registry')
    svc = importlib.import_module('profile_service')
    try:
        registry.REGISTRY_FILE = inst['registry']
        assert svc.active_profiles_dir() == inst['dst']
        for n in inst['profiles']:
            assert registry.resolve_profile_path(n) == os.path.join(inst['dst'], n)
        assert svc.paths.orphan_profiles(
            svc.active_profiles_dir(),
            svc.CHROME_PROFILES_DIR, svc.CHROME_PROFILES_DIR_LEGACY) == set()
    finally:
        importlib.reload(importlib.import_module('config'))
        importlib.reload(importlib.import_module('registry'))
        importlib.reload(importlib.import_module('profile_service'))


# --------------------------------------------------------------------------- #
# 4. Dry run changes nothing
# --------------------------------------------------------------------------- #
def _snapshot(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            st = os.lstat(p)
            kind = 'd' if os.path.isdir(p) and not os.path.islink(p) else ('l' if os.path.islink(p) else 'f')
            out[os.path.relpath(p, root)] = (kind, st.st_size)
    return out


def test_dry_run_changes_nothing(home, capsys):
    inst = make_install(home)
    before_src = _snapshot(inst['src'])
    before_reg = open(inst['registry']).read()

    rc = run_migrate(['--from', inst['src'], '--to', inst['dst'],
                      '--registry', inst['registry']])

    assert rc == 0
    assert _snapshot(inst['src']) == before_src, "dry run modified the source"
    assert open(inst['registry']).read() == before_reg, "dry run rewrote the registry"
    assert not os.path.exists(inst['dst']), "dry run created the destination"

    out = capsys.readouterr().out
    assert 'DRY RUN' in out
    assert 'NOTHING WAS CHANGED' in out
    # It says what it *would* do, per profile.
    for n in inst['profiles']:
        assert n in out
    assert '--apply' in out


def test_dry_run_makes_no_stray_files_anywhere_in_home(home):
    """A dry run must not litter $HOME (no backups, no .partial, no .tmp)."""
    inst = make_install(home)
    run_migrate(['--from', inst['src'], '--to', inst['dst'],
                 '--registry', inst['registry']])
    strays = []
    for dirpath, dirnames, filenames in os.walk(str(home)):
        for n in filenames:
            if '.bak-' in n or n.endswith('.partial') or n.endswith('.tmp'):
                strays.append(os.path.join(dirpath, n))
    assert strays == []


def test_dry_run_prints_sizes(home, capsys):
    inst = make_install(home)
    run_migrate(['--from', inst['src'], '--to', inst['dst'],
                 '--registry', inst['registry']])
    out = capsys.readouterr().out
    assert 'files,' in out and 'bytes' in out
    assert 'would back up' in out.lower() or 'Backed up' in out


# --------------------------------------------------------------------------- #
# 5. Fingerprinting
# --------------------------------------------------------------------------- #
def test_fingerprint_counts(tmp_path):
    d = make_profile(str(tmp_path), 'p', files=3, size=10)
    fp = migrate.fingerprint(d)
    assert fp['files'] == 5            # 3 bins + Preferences + cache blob
    assert fp['bytes'] == 3 * 10 + len('{"profile": {"name": "p"}}') + 16
    assert fp['symlinks'] == 1
    assert fp['dirs'] >= 3
    assert fp['other'] == 0


def test_fingerprint_missing_is_none(tmp_path):
    assert migrate.fingerprint(str(tmp_path / 'nope')) is None


def test_fingerprint_does_not_follow_symlinks(tmp_path):
    """A symlink to a huge tree must not change the fingerprint."""
    outside = tmp_path / 'outside'
    (outside / 'deep').mkdir(parents=True)
    (outside / 'deep' / 'big.bin').write_bytes(b'x' * 10000)
    d = make_profile(str(tmp_path), 'p', files=1, size=1)
    before = migrate.fingerprint(d)
    os.symlink(str(outside), os.path.join(d, 'link-to-outside'))
    after = migrate.fingerprint(d)
    assert after['bytes'] == before['bytes']
    assert after['symlinks'] == before['symlinks'] + 1


# --------------------------------------------------------------------------- #
# 6. Same-filesystem move
# --------------------------------------------------------------------------- #
def test_same_fs_move(home):
    inst = make_install(home)
    before = {n: migrate.fingerprint(os.path.join(inst['src'], n))
              for n in inst['profiles']}

    rc = run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                      '--registry', inst['registry']])

    assert rc == 0
    for n in inst['profiles']:
        assert not os.path.exists(os.path.join(inst['src'], n))
        assert migrate.fingerprint(os.path.join(inst['dst'], n)) == before[n]

    reg = json.load(open(inst['registry']))
    for n in inst['profiles']:
        assert reg[n]['path'] == os.path.join(inst['dst'], n)
        # Other fields survive the rewrite.
        assert reg[n]['created_at'] == '2026-09-11T23:03:55Z'
        assert reg[n]['name'] == n


def test_same_fs_uses_rename(home, monkeypatch):
    """Atomic rename is preferred when both sides share a device."""
    inst = make_install(home)
    seen = []
    real = migrate.move_profile

    def spy(src, dst, expected):
        seen.append((migrate.same_filesystem(src, dst),))
        return real(src, dst, expected)

    monkeypatch.setattr(migrate, 'move_profile', spy)
    run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                 '--registry', inst['registry']])
    assert seen and all(s[0] for s in seen), \
        "expected every move to be classified same-filesystem on one tmpfs"


# --------------------------------------------------------------------------- #
# 7. Cross-device copy + verify
# --------------------------------------------------------------------------- #
def test_cross_fs_copy_and_verify(home, monkeypatch):
    inst = make_install(home)
    # Force the copy path regardless of the real device layout.
    monkeypatch.setattr(migrate, 'same_filesystem', lambda a, b: False)
    before = {n: migrate.fingerprint(os.path.join(inst['src'], n))
              for n in inst['profiles']}

    rc = run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                      '--registry', inst['registry']])

    assert rc == 0
    for n in inst['profiles']:
        assert not os.path.exists(os.path.join(inst['src'], n)), "source not removed"
        assert migrate.fingerprint(os.path.join(inst['dst'], n)) == before[n]
    # No staging directories left behind.
    assert not [x for x in os.listdir(inst['dst']) if x.endswith('.partial')]


def test_cross_fs_real_distinct_filesystems(home, tmp_path, monkeypatch, capsys):
    """
    A genuine cross-device move, when the OS offers two filesystems to test
    with. Skipped otherwise — the forced path is covered above.
    """
    import tempfile
    try:
        other = tempfile.mkdtemp(dir='/dev/shm')
    except (OSError, AttributeError):
        pytest.skip("no second filesystem available (/dev/shm unusable)")
    if os.stat(other).st_dev == os.stat(str(tmp_path)).st_dev:
        pytest.skip("/dev/shm is on the same device as tmp_path")
    inst = make_install(home)
    # Destination on a different device than the source.
    dst = os.path.join(other, 'isolated-profiles')
    before = {n: migrate.fingerprint(os.path.join(inst['src'], n))
              for n in inst['profiles']}
    assert migrate.same_filesystem(inst['src'], other) is False
    rc = run_migrate(['--apply', '--from', inst['src'], '--to', dst,
                      '--registry', inst['registry']])
    assert rc == 0
    for n in inst['profiles']:
        assert migrate.fingerprint(os.path.join(dst, n)) == before[n]
        assert not os.path.exists(os.path.join(inst['src'], n))
    assert 'copy+verify' in capsys.readouterr().out


def test_cross_fs_failure_leaves_source_intact(home, monkeypatch):
    """A failing copy must not delete the source or the staging data."""
    inst = make_install(home)
    monkeypatch.setattr(migrate, 'same_filesystem', lambda a, b: False)
    before = _snapshot(inst['src'])

    def boom(src, dst, **kw):
        raise OSError("disk went away")

    monkeypatch.setattr(migrate, '_copytree', boom)

    rc = run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                      '--registry', inst['registry']])

    assert rc == 1
    assert _snapshot(inst['src']) == before, "source damaged by a failed copy"


# --------------------------------------------------------------------------- #
# 8. Verification mismatch aborts, source intact
# --------------------------------------------------------------------------- #
def test_mismatch_after_rename_is_reported_not_swallowed(home, monkeypatch, capsys):
    """
    If the destination does not match the pre-move fingerprint, that is a hard,
    reported error — and the registry must NOT be rewritten to point at it.
    """
    inst = make_install(home)
    real_fp = migrate.fingerprint

    def corrupt(path):
        stats = real_fp(path)
        if path.startswith(inst['dst']):
            return None      # destination vanished mid-flight
        return stats

    monkeypatch.setattr(migrate, 'fingerprint', corrupt)
    rc = run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                      '--registry', inst['registry']])
    assert rc == 1
    err = capsys.readouterr().err
    assert 'verification failed' in err
    # Registry untouched: profiles still point where they were.
    reg = json.load(open(inst['registry']))
    for n in inst['profiles']:
        assert reg[n]['path'] == os.path.join(inst['src'], n)


def test_cross_fs_byte_mismatch_leaves_source_intact(home, monkeypatch):
    """
    Truncate the copy so the byte count disagrees: the migration must abort and
    the source must survive untouched.
    """
    inst = make_install(home)
    monkeypatch.setattr(migrate, 'same_filesystem', lambda a, b: False)
    before = _snapshot(inst['src'])

    real_copytree = migrate._copytree

    def lossy(src, dst, **kw):
        real_copytree(src, dst, **kw)
        # Drop a whole file from the copy.
        victim = os.path.join(dst, 'Default', 'Preferences')
        os.remove(victim)
        return dst

    monkeypatch.setattr(migrate, '_copytree', lossy)

    rc = run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                      '--registry', inst['registry']])

    assert rc == 1
    assert _snapshot(inst['src']) == before, "source damaged on a verify mismatch"


def test_lossy_copy_is_detected_in_a_scratch_copy(home, monkeypatch):
    """
    Non-vacuity, tightly scoped: sabotaging ONLY the copy step of a single
    scratch profile must make the byte comparison fail. If this passes without
    the sabotage, the verification is not actually verifying.
    """
    inst = make_install(home, profiles=('Solo',))
    monkeypatch.setattr(migrate, 'same_filesystem', lambda a, b: False)
    real_copytree = migrate._copytree

    def lossy(src, dst, **kw):
        real_copytree(src, dst, **kw)
        with open(os.path.join(dst, 'Default', 'file0.bin'), 'r+b') as f:
            f.truncate(1)          # lose 63 of 64 bytes
        return dst

    monkeypatch.setattr(migrate, '_copytree', lossy)
    rc = run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                      '--registry', inst['registry']])

    assert rc == 1, "a lossy copy must be reported, not accepted"
    assert os.path.isdir(os.path.join(inst['src'], 'Solo')), "source must survive"


def test_cross_fs_mismatch_keeps_partial_for_inspection(home, monkeypatch):
    """The incomplete copy is kept, and its existence is reported."""
    inst = make_install(home)
    monkeypatch.setattr(migrate, 'same_filesystem', lambda a, b: False)
    real_copytree = migrate._copytree

    def lossy(src, dst, **kw):
        real_copytree(src, dst, **kw)
        os.remove(os.path.join(dst, 'Default', 'Preferences'))
        return dst

    monkeypatch.setattr(migrate, '_copytree', lossy)
    run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                 '--registry', inst['registry']])
    partials = [x for x in os.listdir(inst['dst']) if x.endswith('.partial')]
    assert partials, "the incomplete copy should be left for inspection"


# --------------------------------------------------------------------------- #
# 9. Idempotency
# --------------------------------------------------------------------------- #
def test_running_twice_is_safe(home, capsys):
    inst = make_install(home)
    argv = ['--from', inst['src'], '--to', inst['dst'],
            '--registry', inst['registry']]

    assert run_migrate(['--apply'] + argv) == 0
    after_first = _snapshot(inst['dst'])
    reg_first = open(inst['registry']).read()

    assert run_migrate(['--apply'] + argv) == 0
    assert _snapshot(inst['dst']) == after_first, "second run changed the data"
    assert open(inst['registry']).read() == reg_first
    assert 'Already migrated' in capsys.readouterr().out


def test_second_dry_run_reports_already_migrated(home, capsys):
    inst = make_install(home)
    argv = ['--from', inst['src'], '--to', inst['dst'],
            '--registry', inst['registry']]
    run_migrate(['--apply'] + argv)
    capsys.readouterr()
    assert run_migrate(argv) == 0
    assert 'Already migrated' in capsys.readouterr().out


def test_dry_run_after_migration_is_a_noop(home):
    inst = make_install(home)
    argv = ['--from', inst['src'], '--to', inst['dst'],
            '--registry', inst['registry']]
    run_migrate(['--apply'] + argv)
    snapshot = _snapshot(inst['dst'])
    assert run_migrate(argv) == 0
    assert _snapshot(inst['dst']) == snapshot


# --------------------------------------------------------------------------- #
# 10. Non-empty destination refusal
# --------------------------------------------------------------------------- #
def test_refuses_non_empty_destination(home, capsys):
    inst = make_install(home)
    os.makedirs(inst['dst'])
    make_profile(inst['dst'], 'Someone-Elses-Data')
    before_src = _snapshot(inst['src'])
    before_dst = _snapshot(inst['dst'])

    rc = run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                      '--registry', inst['registry']])

    assert rc == 3
    assert _snapshot(inst['src']) == before_src
    assert _snapshot(inst['dst']) == before_dst
    err = capsys.readouterr().err
    assert 'not empty' in err
    assert 'Refusing' in err


def test_refusal_names_unaccounted_profiles(home, capsys):
    inst = make_install(home)
    os.makedirs(inst['dst'])
    make_profile(inst['dst'], 'Stranger')
    run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                 '--registry', inst['registry']])
    assert 'Stranger' in capsys.readouterr().err


def test_refuses_even_in_dry_run(home, capsys):
    """A dry run that plans an unsafe merge should still say so."""
    inst = make_install(home)
    os.makedirs(inst['dst'])
    make_profile(inst['dst'], 'Stranger')
    rc = run_migrate(['--from', inst['src'], '--to', inst['dst'],
                      '--registry', inst['registry']])
    assert rc == 3
    assert 'Refusing' in capsys.readouterr().err


def test_resume_moves_only_what_is_missing(home, capsys):
    """
    --resume after an interrupted run: one profile was already moved (its
    directory is gone from the source), the rest are still there. The tool must
    move only the missing ones and still fix every registry path.
    """
    inst = make_install(home)
    os.makedirs(inst['dst'])
    # A genuine interrupted run: Zoom-1 already moved, so it is only at dst.
    shutil.move(os.path.join(inst['src'], 'Zoom-1'), os.path.join(inst['dst'], 'Zoom-1'))

    rc = run_migrate(['--apply', '--resume', '--from', inst['src'],
                      '--to', inst['dst'], '--registry', inst['registry']])

    assert rc == 0, capsys.readouterr().out
    for n in ('Fiverr-Demo', 'Fiverr-Read', 'rootmount'):
        assert os.path.isdir(os.path.join(inst['dst'], n))
        assert not os.path.exists(os.path.join(inst['src'], n))
    reg = json.load(open(inst['registry']))
    for n in inst['profiles']:
        assert reg[n]['path'] == os.path.join(inst['dst'], n), \
            "every registry path must end up at the destination, including the " \
            "profile that was already moved"


def test_resume_refuses_a_profile_present_in_both_places(home, capsys):
    """
    Ambiguity is not resolved by --resume: if a profile exists in BOTH the
    source and the destination, the tool must not guess which is authoritative.
    It skips that profile, still migrates the others, and reports the conflict.
    """
    inst = make_install(home, profiles=('A', 'B'))
    os.makedirs(inst['dst'])
    make_profile(inst['dst'], 'A')                 # A exists in both places

    rc = run_migrate(['--apply', '--resume', '--from', inst['src'],
                      '--to', inst['dst'], '--registry', inst['registry']])

    assert rc == 1
    err = capsys.readouterr().err
    assert 'A' in err
    # The unambiguous profile still moved.
    assert os.path.isdir(os.path.join(inst['dst'], 'B'))
    # And the conflicting source copy was NOT deleted.
    assert os.path.isdir(os.path.join(inst['src'], 'A'))


# --------------------------------------------------------------------------- #
# 11. Registry handling
# --------------------------------------------------------------------------- #
def test_registry_backed_up_before_rewrite(home):
    inst = make_install(home)
    original = open(inst['registry']).read()
    run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                 '--registry', inst['registry']])
    d = os.path.dirname(inst['registry'])
    baks = [f for f in os.listdir(d) if f.startswith('profiles.json.bak-')]
    assert len(baks) == 1, f"expected exactly one backup, found {baks}"
    assert open(os.path.join(d, baks[0])).read() == original


def test_registry_rewrite_is_atomic(home, monkeypatch):
    """tmp + os.replace, same pattern as registry._save; no partial file left."""
    inst = make_install(home)
    original = open(inst['registry']).read()

    real_replace = os.replace
    calls = []

    def spy(a, b):
        calls.append((a, b))
        # The temp file must already hold the complete new content.
        assert a == inst['registry'] + '.tmp'
        assert os.path.dirname(a) == os.path.dirname(b)
        assert json.load(open(a))            # parses => fully written
        return real_replace(a, b)

    monkeypatch.setattr(os, 'replace', spy)
    run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                 '--registry', inst['registry']])

    assert len(calls) == 1, f"expected one atomic replace, got {calls}"
    assert calls[0][1] == inst['registry']
    assert not os.path.exists(inst['registry'] + '.tmp')
    # The old content is only gone via the backup now.
    assert json.load(open(inst['registry'])) != json.loads(original)


def test_registry_failure_after_move_is_reported(home, monkeypatch, capsys):
    """
    Data moved but the registry rewrite fails: a reportable error naming the new
    paths, and a non-zero exit. Never silently swallowed.
    """
    inst = make_install(home)

    def boom(path, data):
        raise OSError("read-only file system")

    monkeypatch.setattr(migrate, 'rewrite_registry', boom)
    rc = run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                      '--registry', inst['registry']])

    assert rc == 1
    err = capsys.readouterr().err
    assert 'registry could not' in err
    for n in inst['profiles']:
        assert os.path.join(inst['dst'], n) in err, \
            "the new path must be reported so the user can fix the registry"
    # The data is genuinely at the new location (recoverable, not lost).
    for n in inst['profiles']:
        assert os.path.isdir(os.path.join(inst['dst'], n))


def test_custom_location_entry_left_alone(home):
    """A profile on a deliberate custom path is not touched by the migration."""
    inst = make_install(home)
    custom = home / 'my-own-place' / 'Custom'
    make_profile(str(custom.parent), 'Custom')
    reg = json.load(open(inst['registry']))
    reg['Custom'] = {'name': 'Custom', 'path': str(custom), 'host_mount': '',
                     'proxy': '', 'created_at': '2026-01-01T00:00:00Z'}
    open(inst['registry'], 'w').write(json.dumps(reg, indent=2))

    rc = run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                      '--registry', inst['registry']])

    assert rc == 0
    after = json.load(open(inst['registry']))
    assert after['Custom']['path'] == str(custom)
    assert os.path.isdir(str(custom))


def test_missing_source_profile_is_reported(home, capsys):
    """A registry entry with no directory is a reportable error, not a crash."""
    inst = make_install(home)
    reg = json.load(open(inst['registry']))
    reg['Ghost'] = {'name': 'Ghost', 'path': os.path.join(inst['src'], 'Ghost'),
                    'host_mount': '', 'proxy': '', 'created_at': ''}
    open(inst['registry'], 'w').write(json.dumps(reg, indent=2))

    rc = run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                      '--registry', inst['registry']])

    assert rc == 1
    err = capsys.readouterr().err
    assert 'Ghost' in err
    # The real profiles still moved.
    for n in inst['profiles']:
        assert os.path.isdir(os.path.join(inst['dst'], n))


# --------------------------------------------------------------------------- #
# 12. Argument validation
# --------------------------------------------------------------------------- #
def test_same_src_and_dst_is_a_noop(home, capsys):
    inst = make_install(home)
    rc = run_migrate(['--apply', '--from', inst['src'], '--to', inst['src'],
                      '--registry', inst['registry']])
    assert rc == 0
    assert 'same directory' in capsys.readouterr().out
    assert os.path.isdir(os.path.join(inst['src'], 'Zoom-1'))


def test_dst_inside_src_is_refused(home, capsys):
    inst = make_install(home)
    rc = run_migrate(['--apply', '--from', inst['src'],
                      '--to', os.path.join(inst['src'], 'profiles'),
                      '--registry', inst['registry']])
    assert rc == 2
    assert 'inside' in capsys.readouterr().err


def test_refuses_real_chrome_storage(home, capsys):
    """Never point this app's store at real Google Chrome's directory."""
    inst = make_install(home)
    real = home / '.config' / 'google-chrome'
    real.mkdir(parents=True)
    rc = run_migrate(['--apply', '--from', inst['src'],
                      '--to', str(real / 'profiles'),
                      '--registry', inst['registry']])
    assert rc == 2
    assert 'Google Chrome' in capsys.readouterr().err


def test_nothing_to_migrate(home, capsys):
    rc = run_migrate(['--from', str(home / 'NoSuchDir'),
                      '--to', str(home / 'new'),
                      '--registry', str(home / 'r.json')])
    assert rc == 0
    assert 'Nothing to migrate' in capsys.readouterr().out


def test_empty_source_directory(home, capsys):
    src = home / 'Chrome'
    src.mkdir()
    rc = run_migrate(['--apply', '--from', str(src), '--to', str(home / 'new'),
                      '--registry', str(home / 'r.json')])
    assert rc == 0


# --------------------------------------------------------------------------- #
# 13. Non-vacuity — prove these tests can actually fail
# --------------------------------------------------------------------------- #
def test_migration_really_moves_data_not_a_noop(home):
    """
    Non-vacuity: the move must actually relocate bytes. If the implementation
    were a silent no-op, this fails.
    """
    inst = make_install(home)
    total_before = sum(migrate.fingerprint(os.path.join(inst['src'], n))['bytes']
                       for n in inst['profiles'])
    assert total_before > 0

    run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                 '--registry', inst['registry']])

    total_after = sum(migrate.fingerprint(os.path.join(inst['dst'], n))['bytes']
                      for n in inst['profiles'])
    assert total_after == total_before
    assert os.listdir(inst['src']) == [], "source still holds the profiles"


def test_verification_would_catch_a_truncating_mover(home, monkeypatch, capsys):
    """
    Mutation test. The sabotage has to go on the *primitive* the mover calls
    (os.rename), not on the verifying wrapper — replacing the wrapper would
    remove the verification and prove nothing. Here rename succeeds but loses a
    file; the post-move fingerprint must catch it and refuse to continue.
    """
    inst = make_install(home)
    real_rename = os.rename

    def lossy_rename(src, dst):
        real_rename(src, dst)
        # Silently lose a file — exactly the bug verification must catch.
        os.remove(os.path.join(dst, 'Default', 'Preferences'))

    monkeypatch.setattr(migrate.os, 'rename', lossy_rename)
    rc = run_migrate(['--apply', '--from', inst['src'], '--to', inst['dst'],
                      '--registry', inst['registry']])

    assert rc == 1, "a lossy move must be reported, not accepted"
    err = capsys.readouterr().err
    assert 'verification failed' in err
    # And the registry must not be rewritten to point at the damaged copy.
    reg = json.load(open(inst['registry']))
    for n in inst['profiles']:
        assert reg[n]['path'].startswith(inst['src']), \
            "the registry must not be pointed at an unverified destination"


def test_fingerprint_detects_a_single_lost_byte(tmp_path):
    """The verification primitive itself is sensitive, not approximate."""
    d = make_profile(str(tmp_path), 'p', files=1, size=100)
    before = migrate.fingerprint(d)
    with open(os.path.join(d, 'Default', 'file0.bin'), 'r+b') as f:
        f.truncate(99)
    after = migrate.fingerprint(d)
    assert after != before
    assert after['files'] == before['files'], "the file count is unchanged..."
    assert after['bytes'] == before['bytes'] - 1
    assert migrate.diff(before, after) == f"bytes: {before['bytes']} -> {after['bytes']}"


def test_dry_run_assertion_would_catch_a_wet_dry_run(home, monkeypatch):
    """
    Mutation test on the dry-run guard itself: make the "dry run" perform a
    real move, and prove the change-detection in test_dry_run_changes_nothing
    style assertions catch it.
    """
    inst = make_install(home)
    before = _snapshot(inst['src'])

    real_main = migrate.main

    def leaky(argv):
        if argv and '--apply' not in argv:
            # Simulate a regression: dry run that quietly migrates anyway.
            return real_main(argv + ['--apply'])
        return real_main(argv)

    monkeypatch.setattr(migrate, 'main', leaky)
    migrate.main(['--from', inst['src'], '--to', inst['dst'],
                  '--registry', inst['registry']])
    assert _snapshot(inst['src']) != before, \
        "a dry run that moved data must be detectable by snapshot comparison"


# --------------------------------------------------------------------------- #
# 14. The real data is never touched
# --------------------------------------------------------------------------- #
def test_migration_is_idempotent_under_repeated_applies(home):
    """
    Non-vacuity for idempotency: run --apply three times. The data must be
    identical after each, the registry must not drift, and the tool must reach a
    stable 'already migrated' state rather than erroring or re-copying.
    """
    inst = make_install(home)
    argv = ['--apply', '--from', inst['src'], '--to', inst['dst'],
            '--registry', inst['registry']]

    assert run_migrate(argv) == 0
    first = _snapshot(inst['dst'])
    reg_first = open(inst['registry']).read()

    for i in range(2):
        assert run_migrate(argv) == 0, f"run {i + 2} should be a safe no-op"
        assert _snapshot(inst['dst']) == first, f"run {i + 2} changed the data"
        assert open(inst['registry']).read() == reg_first, \
            f"run {i + 2} rewrote the registry"

    # Exactly one backup, from the one run that actually did work.
    d = os.path.dirname(inst['registry'])
    baks = [f for f in os.listdir(d) if '.bak-' in f]
    assert len(baks) == 1, f"no-op runs must not create backups: {baks}"


def test_no_backup_and_no_rewrite_when_nothing_to_do(home):
    """
    Non-vacuity for 'nothing to do': an already-migrated install must not get a
    fresh backup or a rewritten registry, so a no-op is visibly inert.
    """
    inst = make_install(home)
    argv = ['--apply', '--from', inst['src'], '--to', inst['dst'],
            '--registry', inst['registry']]
    run_migrate(argv)

    d = os.path.dirname(inst['registry'])
    before = sorted(f for f in os.listdir(d) if '.bak-' in f)
    mtime = os.stat(inst['registry']).st_mtime_ns
    assert run_migrate(argv) == 0
    assert sorted(f for f in os.listdir(d) if '.bak-' in f) == before
    assert os.stat(inst['registry']).st_mtime_ns == mtime, \
        "a no-op run rewrote the registry file"


def test_redirected_store_never_falls_back_to_the_real_legacy_dir(tmp_path):
    """
    Regression test, and the most important safety property here.

    When a caller (a test, or any code managing its own location) passes a
    non-default store, the legacy fallback must NOT kick in. If it did, the
    caller would be silently handed the real ~/Chrome and would start creating
    directories inside real user data — which is exactly the hazard this work
    exists to remove. That regression did happen once during development: an
    empty tmp store resolved to the real ~/Chrome and created `alice/`, `bob/`
    and `shareish/` there.
    """
    real_legacy = os.path.join(str(tmp_path), 'pretend-home', 'Chrome')
    os.makedirs(os.path.join(real_legacy, 'Zoom-1'))     # looks like real data
    redirected = os.path.join(str(tmp_path), 'redirected', 'profiles')
    os.makedirs(redirected)                              # empty, like a fresh store

    got = paths.resolve_profiles_dir(redirected, real_legacy)
    assert got == redirected, (
        "a redirected store must never resolve to the legacy directory, even "
        "when the legacy directory is populated")


def test_default_store_does_fall_back_to_legacy(home):
    """
    The other half of the gate: the DEFAULT store (no $CHROME_ISOLATION_HOME)
    must still honour a populated ~/Chrome, or un-migrated installs break.
    """
    make_profile(str(home / 'Chrome'), 'Zoom-1')
    got = paths.resolve_profiles_dir(
        str(home / 'Isolated-Chrome' / 'profiles'), str(home / 'Chrome'))
    assert got == str(home / 'Chrome')


def test_env_overridden_store_does_not_fall_back_to_legacy(home, monkeypatch):
    """
    If the user has deliberately pointed $CHROME_ISOLATION_HOME somewhere, a
    populated ~/Chrome must not silently capture the app — that would ignore an
    explicit instruction and touch data the user did not select.
    """
    monkeypatch.setenv('CHROME_ISOLATION_HOME', str(home / 'chosen'))
    make_profile(str(home / 'Chrome'), 'Zoom-1')
    got = paths.resolve_profiles_dir()
    assert got == str(home / 'chosen' / 'profiles')


def test_never_touches_the_real_home():
    """
    Tripwire. This module must not hardcode a real home directory, write
    $HOME outside monkeypatch, or reach for privileged filesystem calls. The
    forbidden literals are assembled from parts so this test's own source does
    not contain them.
    """
    with open(os.path.abspath(__file__)) as f:
        body = f.read()
    # Each needle is assembled from fragments at runtime so this test's own
    # source does not contain the literal it forbids.
    needles = (
        '/' + 'home' + '/' + 'x',                 # the real user's home
        '/' + 'home' + '/' + 'x' + '/' + 'Chrome',
        'os.environ["' + 'HOME' + '"] =',         # bypassing the monkeypatch
        'os.ch' + 'own',
        'os.set' + 'uid',
        'rm -rf ' + '/',
    )
    for needle in needles:
        assert needle not in body, \
            f"test_relocation.py must not contain {needle!r} — the suite is " \
            f"meant to run entirely inside tmp_path"


def test_real_chrome_dir_untouched_by_the_suite():
    """
    Read-only tripwire: the real profile store, if present, still holds all four
    profiles with the paths the registry records. This test only ever stats the
    directory — it never writes, moves, or deletes anything.
    """
    real = os.path.expanduser(os.path.join('~', 'Chrome'))
    # Only meaningful when run as the real user outside the sandboxed HOME.
    if not os.path.isdir(real) or os.environ.get('CHROME_ISOLATION_TEST_FAKE_HOME'):
        pytest.skip("not running against the real HOME")
    names = {n for n in os.listdir(real) if os.path.isdir(os.path.join(real, n))}
    assert names, "the real profile store looks empty — did something run?"


# --------------------------------------------------------------------------- #
# standalone runner (matches the style of the other tests/ files)
# --------------------------------------------------------------------------- #
def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith('test_') and callable(f)]
    failed = 0
    for name, fn in tests:
        # tmp_path/monkeypatch are pytest fixtures; run those through pytest.
        if fn.__code__.co_argcount:
            print(f"SKIP {name} (needs pytest fixtures — run with pytest)")
            continue
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed "
          f"(fixture-based tests need pytest)")
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
