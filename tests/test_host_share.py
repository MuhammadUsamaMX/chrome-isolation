#!/usr/bin/env python3
"""Tests for backend/host_share.py — run with: python3 tests/test_host_share.py
or: python3 -m pytest tests/test_host_share.py

Covers the T1 / F1 fix: the container's /home/chrome/host must never default to
the user's home directory, and user-supplied mounts are validated.
"""
import contextlib
import os
import shutil
import stat
import sys
import tempfile
import traceback

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backend'))

import host_share
import paths


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


# ── the home directory is NOT shareable, in any spelling (F1) ───────────────
def test_home_directory_itself_rejected():
    """`~`, `~/`, `$HOME` and the plain expanded path all resolve to the home
    directory, which is exactly what F1 was about. `/home` is in
    _FORBIDDEN_ROOTS only as an exact match, so without a dedicated check the
    original finding could be re-armed by typing one character."""
    for spelling in ('~', '~/', '~/.', ' ~/ '):
        msg = _raises(host_share.validate_host_mount, spelling)
        assert 'home directory' in msg, (spelling, msg)
        assert 'Refusing' in msg, (spelling, msg)
    # The fully expanded path ($HOME expanded) is the same directory and must
    # be refused too.
    msg = _raises(host_share.validate_host_mount,
                  os.path.realpath(os.path.expanduser('~')))
    assert 'home directory' in msg, msg
    # And via the resolve_* entry point, which is what the app actually calls.
    tmp = _with_tmp_shares()
    try:
        msg = _raises(host_share.resolve_host_mount, 'HomeTest', '~')
        assert 'home directory' in msg, msg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_home_escape_by_dotdot_rejected():
    """`~/.gnupg/..` realpaths back to the home directory. The credential-store
    check only looks *downward*, so this needs the home check to catch it."""
    home_sub = os.path.join(os.path.expanduser('~'), '.gnupg')
    if not os.path.exists(home_sub):
        pytest_home = tempfile.mkdtemp(prefix='fake-home-')
        try:
            os.makedirs(os.path.join(pytest_home, '.gnupg'))
            escape = os.path.join(pytest_home, '.gnupg', '..')
            # Sanity: this really does resolve to the temp home.
            assert os.path.realpath(escape) == os.path.realpath(pytest_home)
            old_env = os.environ.get('HOME')
            os.environ['HOME'] = pytest_home
            try:
                msg = _raises(host_share.validate_host_mount, escape)
                assert 'home directory' in msg, msg
            finally:
                if old_env is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = old_env
        finally:
            shutil.rmtree(pytest_home, ignore_errors=True)
    else:
        escape = os.path.join(home_sub, '..')
        msg = _raises(host_share.validate_host_mount, escape)
        assert 'home directory' in msg, msg


def test_symlink_to_home_rejected():
    tmp = _with_tmp_shares()
    try:
        link = os.path.join(tmp, 'my-share')
        os.symlink(os.path.realpath(os.path.expanduser('~')), link)
        msg = _raises(host_share.validate_host_mount, link)
        assert 'home directory' in msg, msg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_home_subfolder_still_allowed():
    """The guarantee is "not the whole home directory", not "nothing in home"."""
    home = os.path.realpath(os.path.expanduser('~'))
    share = os.path.join(home, 'share')
    created = not os.path.isdir(share)
    if created:
        os.makedirs(share, exist_ok=True)
    try:
        resolved = host_share.validate_host_mount(share)
        assert resolved == share, resolved
        assert resolved != home
    finally:
        if created:
            shutil.rmtree(share, ignore_errors=True)


def test_resolve_home_subfolder_allowed():
    tmp = _with_tmp_shares()
    share = os.path.join(os.path.realpath(os.path.expanduser('~')), 'share')
    created = not os.path.isdir(share)
    if created:
        os.makedirs(share, exist_ok=True)
    try:
        resolved = host_share.resolve_host_mount('SubHome', share)
        assert resolved == share, resolved
    finally:
        if created:
            shutil.rmtree(share, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


def test_profile_service_rejects_home_mount():
    """create_profile / update_profile must refuse '~', not just '/'."""
    import profile_service
    tmp = _with_tmp_shares()
    real_profiles_dir = profile_service.CHROME_PROFILES_DIR
    reg = __import__('registry')
    real_registry_file, real_reg_dir = reg.REGISTRY_FILE, reg.CHROME_PROFILES_DIR
    try:
        reg.REGISTRY_FILE = os.path.join(tmp, 'profiles.json')
        reg.CHROME_PROFILES_DIR = os.path.join(tmp, 'profiles')
        os.makedirs(reg.CHROME_PROFILES_DIR, exist_ok=True)
        profile_service.CHROME_PROFILES_DIR = os.path.join(tmp, 'profiles')

        msg = _raises(profile_service.create_profile, 'homeish', '', '~')
        assert 'home directory' in msg, msg

        share = os.path.realpath(os.path.join(os.path.expanduser('~'), 'share'))
        created = not os.path.isdir(share)
        if created:
            os.makedirs(share, exist_ok=True)
        try:
            entry = profile_service.create_profile('shareish', '', share, '')
            assert entry['host_mount'] == share, entry
            msg = _raises(profile_service.update_profile, 'shareish', '~')
            assert 'home directory' in msg, msg
        finally:
            if created:
                shutil.rmtree(share, ignore_errors=True)
    finally:
        profile_service.CHROME_PROFILES_DIR = real_profiles_dir
        reg.REGISTRY_FILE, reg.CHROME_PROFILES_DIR = real_registry_file, real_reg_dir
        shutil.rmtree(tmp, ignore_errors=True)


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
# ── M2: this app's own storage and real browsers' storage are refused ────────
# F1 said an explicit user choice could not reach the most sensitive targets.
# It could: the profile store (every OTHER profile's cookies/logins/history),
# the app data dir (profiles.json — all profile names plus proxy URLs that may
# carry user:pass@ credentials — proxies.json, the other host-shares/), and
# real Chrome's own tree. All one field away in the Edit modal.
def _deny_reason(msg):
    """The specific reason text from a refusal, or '' if it is not a refusal."""
    return msg.split(':', 1)[0] if msg.startswith('Refusing') else ''


@contextlib.contextmanager
def _isolated_env():
    """
    Run a block against a throwaway $HOME so the deny-list can be tested
    without depending on what happens to exist on the machine running the
    tests, and without writing anything into the real home.

    Redirects $HOME, $CHROME_ISOLATION_HOME and host_share's two app-dir
    globals. The globals matter: host_share captured APP_DATA_DIR and
    HOST_SHARES_DIR from config at import time, and the deny-list reads those
    module globals (so tests can redirect them the way _with_tmp_shares does).
    """
    tmp = tempfile.mkdtemp(prefix='host-share-home-')
    home = os.path.join(tmp, 'home')
    os.makedirs(home)
    old_env = {'HOME': os.environ.get('HOME'),
               paths.ISOLATION_HOME_ENV: os.environ.get(paths.ISOLATION_HOME_ENV)}
    old_app_data = host_share.APP_DATA_DIR
    old_shares = host_share.HOST_SHARES_DIR
    os.environ['HOME'] = home
    os.environ[paths.ISOLATION_HOME_ENV] = os.path.join(home, 'Isolated-Chrome')
    host_share.APP_DATA_DIR = paths.app_data_dir()
    host_share.HOST_SHARES_DIR = os.path.join(host_share.APP_DATA_DIR, 'host-shares')
    os.makedirs(host_share.HOST_SHARES_DIR, exist_ok=True)
    try:
        yield home
    finally:
        host_share.APP_DATA_DIR = old_app_data
        host_share.HOST_SHARES_DIR = old_shares
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def _is_below(parent, child):
    return child == parent or child.startswith(parent + os.sep)


def _mkdirs(root, *rel):
    out = []
    for r in rel:
        p = os.path.join(root, r)
        os.makedirs(p, exist_ok=True)
        out.append(p)
    return out


def test_profile_store_rejected_including_other_profiles():
    """The active profile store is the exact harm F1 was opened for."""
    with _isolated_env() as home:
        store, other, deep = _mkdirs(
            home, 'Isolated-Chrome/profiles',
            'Isolated-Chrome/profiles/Zoom-1',
            'Isolated-Chrome/profiles/Zoom-1/Default')
        for path in (store, other, deep):
            msg = _raises(host_share.validate_host_mount, path)
            assert 'profile store' in msg, (path, msg)
        # and it must not be accepted through the resolve_* entry point either
        tmp = _with_tmp_shares()
        try:
            msg = _raises(host_share.resolve_host_mount, 'Zoom-1', deep)
            assert 'Refusing' in msg, msg
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def test_app_data_dir_and_its_contents_rejected():
    """APP_DATA_DIR holds profiles.json/proxies.json and the other profiles'
    host-shares/ — the whole registry, in one bind mount."""
    with _isolated_env() as home:
        app_data, shares, other_share = _mkdirs(
            home, '.local/share/chrome-isolation-manager',
            '.local/share/chrome-isolation-manager/host-shares',
            '.local/share/chrome-isolation-manager/host-shares/Fiverr-Read')
        registry = os.path.join(app_data, 'profiles.json')
        for path in (app_data, shares, other_share):
            msg = _raises(host_share.validate_host_mount, path)
            assert 'Refusing' in msg, (path, msg)
        # A FILE in there must be refused by the policy rule, not by "not found".
        open(registry, 'w').close()
        msg = _raises(host_share.validate_host_mount, registry)
        assert 'Refusing' in msg and 'not found' not in msg, msg


def test_real_browser_stores_rejected():
    """Real Chrome/Firefox trees are the same harm reached the other way."""
    with _isolated_env() as home:
        targets = _mkdirs(
            home, '.config/google-chrome', '.config/google-chrome/Default',
            '.mozilla', '.mozilla/firefox/abc.default',
            '.config/BraveSoftware', '.config/microsoft-edge',
            '.config/vivaldi', '.config/opera')
        for path in targets:
            msg = _raises(host_share.validate_host_mount, path)
            assert 'Refusing' in msg, (path, msg)


def test_dotdot_escape_into_denied_root_rejected():
    """A `..` hop out of an allowed subfolder must not launder the target.
    realpath() collapses it before the comparison, so the same rule fires."""
    with _isolated_env() as home:
        share, store, _zoom, other_profile = _mkdirs(
            home, 'share', 'Isolated-Chrome/profiles',
            'Isolated-Chrome/profiles/Zoom-1/Default',
            'Isolated-Chrome/profiles/Fiverr-Read')
        browser = os.path.join(home, '.config', 'google-chrome')
        escapes = [
            # out of a *legitimately shareable* subfolder, into the store
            os.path.join(share, '..', 'Isolated-Chrome', 'profiles'),
            os.path.join(share, '..', 'Isolated-Chrome', 'profiles', 'Zoom-1',
                         'Default'),
            # a sibling profile reached by hopping up and back down
            os.path.join(store, 'Zoom-1', '..', 'Fiverr-Read'),
            # and the same trick aimed at a real browser's tree
            os.path.join(share, '..', '.config', 'google-chrome'),
            # redundant '.' mixed in, so it is not merely a literal '..' match
            os.path.join(share, '.', '..', 'Isolated-Chrome', 'profiles',
                         'Zoom-1', '..', 'Fiverr-Read'),
        ]
        for path in escapes:
            # Precondition: the escape really does land on a denied root, so a
            # pass below cannot be explained by the path going somewhere else.
            landed = os.path.realpath(path)
            assert landed != share and not _is_below(share, landed), (path, landed)
            # The escape landed on a denied root (or on the isolation home
            # that contains it) — not somewhere unrelated.
            denied = [os.path.realpath(store), os.path.realpath(other_profile),
                      os.path.realpath(browser),
                      os.path.realpath(os.path.dirname(store))]
            assert any(landed == d or _is_below(d, landed) for d in denied), \
                (path, landed, denied)
            msg = _raises(host_share.validate_host_mount, path)
            assert 'Refusing' in msg, (path, msg)


def test_symlink_to_denied_root_rejected():
    """A symlink with an innocent name resolves to the target and is refused."""
    with _isolated_env() as home:
        store, browser = _mkdirs(home, 'Isolated-Chrome/profiles',
                                 '.config/google-chrome')
        links_dir = os.path.join(home, 'shared-docs')
        os.makedirs(links_dir, exist_ok=True)
        for name, target in (('my-share', store),
                             ('public-html', browser),
                             ('pics', os.path.join(store, 'Zoom-1'))):
            link = os.path.join(links_dir, name)
            os.symlink(target, link)
            msg = _raises(host_share.validate_host_mount, link)
            assert 'Refusing' in msg, (link, msg)


def test_denied_roots_follow_the_relocated_store():
    """The deny-list must read the store location from paths.py, not hardcode
    `~/Isolated-Chrome`. A $CHROME_ISOLATION_HOME override must be honoured."""
    with _isolated_env() as home:
        elsewhere = os.path.join(home, 'somewhere-else')
        os.makedirs(elsewhere, exist_ok=True)
        os.environ[paths.ISOLATION_HOME_ENV] = elsewhere
        store = os.path.join(elsewhere, 'profiles')
        _mkdirs(elsewhere, 'profiles/Zoom-1/Default')
        try:
            msg = _raises(host_share.validate_host_mount, store)
            assert 'Refusing' in msg, msg
        finally:
            os.environ[paths.ISOLATION_HOME_ENV] = os.path.join(
                home, 'Isolated-Chrome')
        # The old location is no longer the store, so it is no longer denied
        # *as a store* — but it still exists as a path, and mounting it is not
        # something the deny-list silently allows just because the env moved.
        old = os.path.join(home, 'Isolated-Chrome', 'profiles')
        if os.path.isdir(old):
            # paths.legacy_profiles_dir/default_profiles_dir still name the
            # default location, so this remains refused. Assert the behaviour
            # rather than a comment: the default location is always denied.
            msg = _raises(host_share.validate_host_mount, old)
            assert 'Refusing' in msg, msg


def test_denied_paths_rejected_even_when_absent():
    """The policy check runs before the existence check, so a denied path is
    refused as a policy violation rather than reported as 'not found'."""
    with _isolated_env() as home:
        for rel in ('Isolated-Chrome/profiles/does-not-exist',
                    '.config/google-chrome/does-not-exist',
                    '.mozilla/does-not-exist',
                    '.local/share/chrome-isolation-manager/does-not-exist'):
            path = os.path.join(home, rel)
            assert not os.path.exists(path), path
            msg = _raises(host_share.validate_host_mount, path)
            assert 'Refusing' in msg and 'not found' not in msg, (path, msg)


def test_hostile_isolation_home_does_not_refuse_all_of_home():
    """$CHROME_ISOLATION_HOME is user-controlled. If it is set to $HOME itself,
    adding the *unresolved* home directory to the deny-list would refuse every
    subfolder of home and silently break the documented behaviour. Only the
    locations named inside it are refused."""
    with _isolated_env() as home:
        os.environ[paths.ISOLATION_HOME_ENV] = home
        try:
            denied = [r for r, _ in host_share._app_owned_roots()]
            assert os.path.realpath(home) not in denied, denied
            assert os.sep not in denied, denied
            # ~/profiles is the store in this configuration, so it IS refused
            profiles = _mkdirs(home, 'profiles/Fiverr-Read')[0]
            assert os.path.join(home, 'profiles') in \
                [os.path.realpath(d) for d in denied], denied
            msg = _raises(host_share.validate_host_mount, profiles)
            assert 'Refusing' in msg, msg
            # ... but an ordinary subfolder of home is still shareable
            share = _mkdirs(home, 'share')[0]
            assert host_share.validate_host_mount(share) == share
        finally:
            os.environ[paths.ISOLATION_HOME_ENV] = os.path.join(
                home, 'Isolated-Chrome')


def test_plain_home_subfolders_still_allowed():
    """The deny-list must be a list, not a blanket ban on the home directory:
    a dedicated subfolder of home is still shareable (the documented
    behaviour), while the entries above are not."""
    with _isolated_env() as home:
        for rel in ('share', 'Downloads', 'Documents',
                    '.local/share/other-app-data'):
            path = _mkdirs(home, rel)[0]
            resolved = host_share.validate_host_mount(path)
            assert resolved == path, (resolved, path)


def test_real_host_denied_paths():
    """Against the live tree: the paths the review measured are refused, and a
    real subfolder of the real home is still allowed."""
    store = os.path.realpath(paths.resolve_profiles_dir())
    app_data = os.path.realpath(host_share.APP_DATA_DIR)
    home = os.path.realpath(os.path.expanduser('~'))
    for path in (store,
                 os.path.join(store, 'some-profile', 'Default'),
                 app_data,
                 os.path.join(app_data, 'host-shares', 'some-profile'),
                 os.path.join(home, '.config', 'google-chrome'),
                 os.path.join(home, '.mozilla')):
        msg = _raises(host_share.validate_host_mount, path)
        assert 'Refusing' in msg, (path, msg)


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


# --------------------------------------------------------------------------- #
# Parent-directory bypass: a deny-list entry is worthless if its PARENT is
# shareable. `~/.config/google-chrome/..` realpaths to `~/.config`, so allowing
# ~/.config hands over every browser store the list refuses, with one `..`.
# Found by hand on 2026-09-29 after the browser-store deny-list landed.
# --------------------------------------------------------------------------- #

def test_config_dir_itself_is_refused():
    with pytest.raises(ValueError):
        host_share.validate_host_mount('~/.config')


def test_dotdot_cannot_escape_the_browser_store_denial():
    """`~/.config/google-chrome/..` resolves to ~/.config, not to a browser
    store -- which is precisely why ~/.config has to be refused too."""
    import os
    assert os.path.realpath(os.path.expanduser('~/.config/google-chrome/..')) == \
        os.path.realpath(os.path.expanduser('~/.config'))
    with pytest.raises(ValueError):
        host_share.validate_host_mount('~/.config/google-chrome/..')


@pytest.mark.parametrize('victim', [
    '~/.config/google-chrome', '~/.config/BraveSoftware', '~/.mozilla',
])
def test_no_refused_browser_store_is_reachable_via_a_parent(victim):
    """For every refused browser store, the paths that resolve to it from an
    allowed spelling must also be refused."""
    import os
    real = os.path.realpath(os.path.expanduser(victim))
    for spelling in (real, real + '/.', real + '/../' + os.path.basename(real)):
        with pytest.raises(ValueError):
            host_share.validate_host_mount(spelling)


def test_a_real_subfolder_of_home_is_still_shareable():
    """The ~/.config subtree refusal must not creep into all of $HOME."""
    for ok in ('~/Books', '~/Downloads', '~/Videos', '~/Pictures'):
        try:
            host_share.validate_host_mount(ok)
        except ValueError as exc:  # pragma: no cover - only on a broken guard
            pytest.fail(f'{ok} was refused: {exc}')
