#!/usr/bin/env python3
"""Security regression tests — run with: python3 -m pytest tests/test_security_regressions.py
or: python3 tests/test_security_regressions.py

Pure-Python, no Docker daemon required, so this file is CI-safe.

Every test here maps to a finding in ~/Todo-chrome-isolation-app.md. They are
written against the FIXED behaviour: if one of them fails, a vulnerability has
regressed. They are deliberately *not* weakened to match unfixed code — a red
test that documents F1/F7 is more useful than a fake green one.

Findings covered
----------------
F1  host_mount "" must not resolve to ~ (backend/host_share.py, docker_manager)
F1  host_mount "/" and symlink escapes must be rejected
F7  shell-injection surface: no os.system() in desktop_manager.py
S1  profile-name validation blocks ".." and absolute paths
S1  proxy URL validation blocks malformed URLs and non-allowed schemes
S2  archive member path validation blocks ".." and absolute paths
"""
import os
import shutil
import stat
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'backend'))

import archive
import proxy_store
import validator

try:
    import host_share
except ImportError:  # the T1 fix is not in place -> tests below must go red
    host_share = None

try:
    import docker_manager
except ImportError:  # `docker` SDK missing -> docker-level tests skip
    docker_manager = None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _require_host_share():
    """Fail loudly (with an explanation) when the T1 fix module is absent."""
    assert host_share is not None, (
        "backend/host_share.py is missing — the T1/F1 fix (empty host_mount must "
        "not fall back to the home directory) is not in place. This test "
        "documents finding F1 and is expected to fail until the fix lands."
    )


def _raises(fn, *args, **kwargs):
    """Call fn and return the ValueError message, or raise AssertionError."""
    try:
        fn(*args, **kwargs)
    except ValueError as e:
        return str(e)
    except Exception as e:
        raise AssertionError(
            f"expected ValueError from {getattr(fn, '__name__', fn)!r}, "
            f"got {type(e).__name__}: {e}"
        )
    raise AssertionError(
        f"expected ValueError from {getattr(fn, '__name__', fn)!r}, but it returned normally"
    )


def _tmp_shares_dir():
    """Redirect host_share.HOST_SHARES_DIR at a throwaway root; returns the root."""
    tmp = tempfile.mkdtemp(prefix='secreg-hostshare-')
    host_share.HOST_SHARES_DIR = os.path.join(tmp, 'host-shares')
    os.makedirs(host_share.HOST_SHARES_DIR, exist_ok=True)
    return tmp


def _home():
    return os.path.realpath(os.path.expanduser('~'))


# =========================================================================== #
# F1 — empty host_mount must not resolve to the home directory
# =========================================================================== #
def test_empty_host_mount_does_not_resolve_to_home():
    """An unset/empty host_mount must resolve to a DEDICATED safe directory,
    never to the user's home directory.

    This is the single highest-impact regression guard in the repo: the pre-fix
    code returned os.path.expanduser('~'), handing every container the whole
    home dir (SSH keys, .gnupg, keyrings, other browser profiles).
    """
    _require_host_share()
    tmp = _tmp_shares_dir()
    try:
        resolved = host_share.resolve_host_mount('Fiverr-Read', '')

        # 1. Not the home directory, in any spelling.
        assert resolved != _home(), \
            f"empty host_mount resolved to the home directory: {resolved}"
        assert os.path.realpath(resolved) != _home(), \
            f"empty host_mount realpath-es to the home directory: {resolved}"
        assert resolved != os.path.expanduser('~'), resolved
        assert not os.path.realpath(resolved).startswith(_home() + os.sep), \
            f"empty host_mount resolved inside the home directory: {resolved}"

        # 2. It IS a dedicated, purpose-built directory.
        assert os.path.realpath(resolved).startswith(
            os.path.realpath(host_share.HOST_SHARES_DIR) + os.sep
        ), f"expected a dedicated share dir, got: {resolved}"
        assert os.path.basename(resolved) == 'Fiverr-Read', resolved
        assert os.path.isdir(resolved), f"safe share dir was not created: {resolved}"

        # 3. It is empty and private — a zero-value mount, not a view of $HOME.
        assert os.listdir(resolved) == [], \
            "the default share dir must start empty"
        assert stat.S_IMODE(os.stat(resolved).st_mode) == 0o700, \
            f"default share dir must be 0700, got {oct(stat.S_IMODE(os.stat(resolved).st_mode))}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missing_host_mount_does_not_resolve_to_home():
    """None / whitespace host_mount take the same safe path as ''."""
    _require_host_share()
    tmp = _tmp_shares_dir()
    try:
        for value in (None, '', '   ', '\t\n'):
            resolved = host_share.resolve_host_mount('Zoom-1', value)
            assert os.path.realpath(resolved) != _home(), \
                f"host_mount={value!r} resolved to the home directory: {resolved}"
            assert os.path.realpath(resolved).startswith(
                os.path.realpath(host_share.HOST_SHARES_DIR) + os.sep
            ), (value, resolved)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_build_volumes_never_mounts_home():
    """Regression at the volume-construction layer.

    Even when _build_volumes() is called with no host_mount at all, it must not
    add the home directory as a bind source for /home/chrome/host.
    """
    if docker_manager is None:
        return  # docker SDK unavailable in this environment; nothing to assert
    import docker as _docker

    class _Img:  pass
    class _Ctr:  id = 'c'; status = 'running'
    class _Ctrs:
        def get(self, name): raise _docker.errors.NotFound('nope')
        def run(self, **kw):  return _Ctr()
        def list(self, **kw): return []
    class _Imgs:
        def get(self, name): return _Img()
    class _Cli:
        images = _Imgs()
        containers = _Ctrs()

    tmp = tempfile.mkdtemp(prefix='secreg-volumes-')
    saved = (_docker.from_env, host_share.HOST_SHARES_DIR if host_share else None)
    try:
        import registry
        saved_registry = (registry.REGISTRY_FILE, registry.CHROME_PROFILES_DIR)
        registry.REGISTRY_FILE = os.path.join(tmp, 'profiles.json')
        registry.CHROME_PROFILES_DIR = os.path.join(tmp, 'profiles')
        os.makedirs(registry.CHROME_PROFILES_DIR, exist_ok=True)
        if host_share is not None:
            host_share.HOST_SHARES_DIR = os.path.join(tmp, 'host-shares')
            os.makedirs(host_share.HOST_SHARES_DIR, exist_ok=True)

        _docker.from_env = lambda: _Cli()
        try:
            m = docker_manager.DockerManager()
        finally:
            _docker.from_env = saved[0]

        profile_dir = os.path.join(registry.CHROME_PROFILES_DIR, 'alice')
        downloads_dir = os.path.join(profile_dir, 'Downloads')
        os.makedirs(downloads_dir, exist_ok=True)

        # No host_mount argument at all — the worst case.
        try:
            vols = m._build_volumes(profile_dir, downloads_dir)
        except TypeError:
            # Older signature with no profile_name kwarg is fine too.
            vols = m._build_volumes(profile_dir, downloads_dir, '')

        assert _home() not in vols, f"home directory mounted from _build_volumes: {vols}"
        assert os.path.expanduser('~') not in vols, \
            f"home directory mounted from _build_volumes: {vols}"
        host_binds = [src for src, cfg in vols.items()
                      if cfg.get('bind') == '/home/chrome/host']
        for src in host_binds:
            assert os.path.realpath(src) != _home(), \
                f"/home/chrome/host sourced from the home directory: {src}"
            assert os.path.isdir(src), src
            assert os.listdir(src) == [], \
                f"/home/chrome/host exposes host files via {src}: {os.listdir(src)}"
    finally:
        registry.REGISTRY_FILE, registry.CHROME_PROFILES_DIR = saved_registry
        if host_share is not None and saved[1] is not None:
            host_share.HOST_SHARES_DIR = saved[1]
        shutil.rmtree(tmp, ignore_errors=True)


# =========================================================================== #
# F1 — "/" and symlink escapes are rejected
# =========================================================================== #
def test_host_mount_root_rejected():
    """Mounting / into a browser container is never acceptable."""
    _require_host_share()
    for spelling in ('/', '//', '/.', '/./', '/..'):
        msg = _raises(host_share.validate_host_mount, spelling)
        assert 'Refusing' in msg or 'system' in msg.lower(), (spelling, msg)


def test_host_mount_root_rejected_through_resolver():
    """The rejection must also apply on the resolve path used at launch."""
    _require_host_share()
    tmp = _tmp_shares_dir()
    try:
        _raises(host_share.resolve_host_mount, 'Rooted', '/')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_host_mount_system_roots_rejected():
    """A broad set of system locations must be refused, not just '/'."""
    _require_host_share()
    for bad in ('/etc', '/etc/ssh', '/home', '/root', '/root/.ssh', '/usr',
                '/usr/share', '/var', '/var/lib', '/proc', '/sys', '/boot',
                '/dev', '/bin', '/sbin', '/lib', '/lib64', '/opt', '/run',
                '/srv'):
        msg = _raises(host_share.validate_host_mount, bad)
        assert 'Refusing' in msg or 'not found' in msg.lower(), (bad, msg)


def test_symlink_pointing_outside_allowed_area_rejected():
    """A symlink whose target escapes the share area must be refused.

    realpath() must be applied BEFORE the allow/deny checks, otherwise a link
    named innocuously inside the share dir can expose the whole filesystem.
    """
    _require_host_share()
    tmp = tempfile.mkdtemp(prefix='secreg-symlink-')
    try:
        cases = {
            'looks-innocuous': '/',
            'shared-docs': '/etc',
            'root-alias': '/root',
            'keys': os.path.expanduser('~/.ssh'),
        }
        for name, target in cases.items():
            link = os.path.join(tmp, name)
            os.symlink(target, link)
            msg = _raises(host_share.validate_host_mount, link)
            assert ('Refusing' in msg or 'credential store' in msg), \
                f"symlink {name} -> {target} was not rejected: {msg}"

        # A symlink *inside* a share dir pointing out must also be refused.
        share = os.path.join(tmp, 'myshare')
        os.makedirs(share)
        escape = os.path.join(share, 'innocent')
        os.symlink('/etc', escape)
        msg = _raises(host_share.validate_host_mount, escape)
        assert 'Refusing' in msg, msg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_dotdot_traversal_into_forbidden_area_rejected():
    """`..` must be collapsed before validation, never trusted.

    A relative hop chain that lands on a real system directory must be refused
    for being a system location, not merely for "not existing".
    """
    _require_host_share()
    tmp = tempfile.mkdtemp(prefix='secreg-traversal-')
    try:
        share = os.path.join(tmp, 'a', 'b', 'share')
        os.makedirs(share)
        for target in ('/etc', '/proc', '/root', '/usr'):
            # Climb exactly as many levels as needed to reach the filesystem root.
            depth = len(os.path.abspath(share).split(os.sep))
            hop = os.path.join(share, *(['..'] * depth), target.lstrip(os.sep))
            assert os.path.realpath(hop) == target, (hop, os.path.realpath(hop))
            msg = _raises(host_share.validate_host_mount, hop)
            assert 'Refusing' in msg, (target, msg)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_credential_stores_rejected():
    """~/.ssh, ~/.gnupg and friends must never be shareable."""
    _require_host_share()
    home = _home()
    for sub in ('.ssh', '.gnupg', '.aws', '.azure', '.kube', '.password-store'):
        path = os.path.join(home, sub)
        if not os.path.isdir(path):
            continue
        msg = _raises(host_share.validate_host_mount, path)
        assert ('credential store' in msg or 'Refusing' in msg), (sub, msg)


def test_nonexistent_host_mount_rejected():
    _require_host_share()
    tmp = tempfile.mkdtemp(prefix='secreg-missing-')
    try:
        msg = _raises(host_share.validate_host_mount, os.path.join(tmp, 'nope'))
        assert 'not found' in msg.lower(), msg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =========================================================================== #
# S1 — profile-name validation
# =========================================================================== #
def test_profile_name_rejects_traversal_and_absolute_paths():
    """Profile names are used to build filesystem paths and container names."""
    bad_names = [
        '..', '../evil', '../../etc/passwd', 'a/../b', './evil',
        '/absolute', '/etc/passwd', 'a/b', 'a\\b', '..\\..\\evil',
        'a/..', 'foo/../../bar',
    ]
    for bad in bad_names:
        msg = _raises(validator.validate_profile_name, bad)
        assert 'Invalid profile name' in msg, (bad, msg)

    # And the path helper must independently refuse to escape its base.
    base = tempfile.mkdtemp(prefix='secreg-profile-path-')
    try:
        for bad in ('../evil', '..'):
            _raises(validator.safe_profile_path, bad, base)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_profile_name_still_accepts_ordinary_names():
    """Guard against an over-tightened regex breaking real profiles."""
    for good in ('alice', 'Fiverr-Read', 'Zoom-1', 'a_b', '0', 'x' * 64):
        assert validator.validate_profile_name(good) == good, good


# =========================================================================== #
# S1 / F7 — proxy URL validation
# =========================================================================== #
def test_proxy_url_rejects_malformed_and_disallowed_schemes():
    """Only socks5/socks5h/http/https with an explicit host:port are allowed.

    Anything else — file://, ftp://, javascript:, a missing port, an embedded
    newline, a bare hostname — must be refused before it reaches a container.
    """
    bad_urls = [
        '',                                  # empty
        'not-a-proxy',
        '127.0.0.1:1080',                    # no scheme
        'socks5://127.0.0.1',                # no port
        'socks5://',                         # no host
        'socks5://host:port',                # non-numeric port
        # NOTE: `socks5://host:99999` is NOT in this list. The grammar is
        # `\d{1,5}` with no numeric range check, so a 5-digit out-of-range port
        # is currently accepted. That is a pre-existing laxity, not a
        # regression, and it is out of scope for this suite.
        'file:///etc/passwd',                # dangerous scheme
        'ftp://host:21',
        'gopher://host:70',
        'javascript:alert(1)',
        'data:text/html,<script>1</script>',
        'chrome://settings',
        'about:blank',
        'http://host:8080/path',             # path is not part of the grammar
        'http://host:8080 extra',            # trailing junk
        'socks5://user:pass:extra@host:1080',
        'http://host:8080\r\nX-Injected: 1',  # header injection
        'socks5://[::1]:1080',               # bracketed IPv6 not in grammar
        'SOCKS5://host:1080',                # scheme is case-sensitive here
    ]
    for bad in bad_urls:
        _raises(proxy_store.validate_url, bad)


def test_proxy_url_accepts_valid_urls():
    for good in (
        'socks5://127.0.0.1:1080',
        'socks5h://proxy.example.com:1080',
        'http://127.0.0.1:8080',
        'https://proxy.example.com:3128',
        'http://user:pass@proxy.example.com:8080',
        ' socks5://127.0.0.1:1080 ',        # surrounding whitespace is trimmed
    ):
        assert proxy_store.validate_url(good) == good.strip(), good


def test_proxy_resolve_never_invents_a_scheme():
    """resolve() must pass values through untouched, not coerce them."""
    assert proxy_store.resolve('') == ''
    assert proxy_store.resolve(None) == ''
    assert proxy_store.resolve('socks5://127.0.0.1:1080') == 'socks5://127.0.0.1:1080'


# =========================================================================== #
# S2 — archive member path validation (pure function, no extraction)
# =========================================================================== #
def test_archive_member_path_rejects_traversal_and_absolute_paths():
    """archive._safe_member_path is the gate for every zip/tar member.

    Exercised as a pure function: nothing is written to disk, so this is safe
    to run in CI.
    """
    bad_members = [
        '..', '../evil', '../../etc/passwd', 'a/../../b',
        'profile/../../../../etc/shadow',
        '/etc/passwd', '/', '/tmp/abs', '//double/leading',
        '/home/x/.ssh/id_rsa',
    ]
    for bad in bad_members:
        _raises(archive._safe_member_path, bad)


def test_archive_member_path_accepts_ordinary_members():
    for good in ('profile1', 'profile1/Preferences', 'profile1/Default/History',
                 'a/b/c/d.txt', 'file with spaces.txt', 'dots.in.name.txt'):
        assert archive._safe_member_path(good) == good, good


# =========================================================================== #
# F7 — no shell-injection surface in desktop_manager.py
# =========================================================================== #
def test_desktop_manager_has_no_os_system():
    """F9/F7: the os.system -> subprocess.run fix must not be reverted."""
    src = open(os.path.join(ROOT, 'backend', 'desktop_manager.py')).read()
    offending = [ln for ln in src.splitlines()
                 if 'os.system(' in ln and not ln.strip().startswith('#')]
    assert not offending, \
        f"os.system() reintroduced in desktop_manager.py: {offending}"
    assert 'subprocess.run(' in src, \
        "desktop_manager.py should use subprocess.run() with an argument list"
    # shell=True would defeat the point of the fix.
    assert 'shell=True' not in src, \
        "desktop_manager.py must not use shell=True"


# --------------------------------------------------------------------------- #
# standalone runner (matches the style of the other tests/ files)
# --------------------------------------------------------------------------- #
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
