#!/usr/bin/env python3
"""Tests for backend/docker_manager.py — run with: python3 tests/test_docker_manager.py

Uses a stub docker client (docker.from_env monkeypatched) so no real daemon
interaction is needed for the unit-level assertions.
"""
import grp
import os
import shutil
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backend'))

import docker_manager
import host_share
import registry


class _FakeImage:
    pass


class _FakeContainer:
    id = 'fake-container-id'
    status = 'running'


class _FakeContainers:
    def __init__(self):
        self.run_kwargs = None

    def get(self, name):
        raise docker_manager.docker.errors.NotFound('no such container')

    def run(self, **kwargs):
        self.run_kwargs = kwargs
        return _FakeContainer()


class _FakeImages:
    def get(self, name):
        return _FakeImage()


class _FakeClient:
    def __init__(self):
        self.images = _FakeImages()
        self.containers = _FakeContainers()


def _make_manager():
    """Return (manager, tmpdir) with registry redirected and docker stubbed."""
    tmp = tempfile.mkdtemp(prefix='docker-test-')
    registry.REGISTRY_FILE = os.path.join(tmp, 'profiles.json')
    registry.CHROME_PROFILES_DIR = os.path.join(tmp, 'profiles')
    os.makedirs(registry.CHROME_PROFILES_DIR, exist_ok=True)
    # Redirect the per-profile host-share root so tests never touch the real
    # ~/.local/share/chrome-isolation-manager/host-shares directory.
    host_share.HOST_SHARES_DIR = os.path.join(tmp, 'host-shares')
    os.makedirs(host_share.HOST_SHARES_DIR, exist_ok=True)

    real_from_env = docker_manager.docker.from_env
    docker_manager.docker.from_env = lambda: _FakeClient()
    try:
        m = docker_manager.DockerManager()
    finally:
        docker_manager.docker.from_env = real_from_env
    return m, tmp


def test_host_dns_removed():
    assert not hasattr(docker_manager.DockerManager, '_host_dns'), \
        '_host_dns must be deleted'


def test_build_volumes_socket_only():
    m, tmp = _make_manager()
    try:
        profile_dir = os.path.join(registry.CHROME_PROFILES_DIR, 'alice')
        downloads_dir = os.path.join(profile_dir, 'Downloads')
        os.makedirs(downloads_dir, exist_ok=True)

        uid = 424242
        xdg = f'/run/user/{uid}'
        real_exists = os.path.exists
        fake_sockets = {
            os.path.join(xdg, 'wayland-0'): True,
            os.path.join(xdg, 'pipewire-0'): True,
            os.path.join(xdg, 'pulse'): True,
        }

        def fake_exists(p):
            return fake_sockets.get(p, real_exists(p))

        real_getuid = docker_manager.os.getuid
        old_wd = os.environ.get('WAYLAND_DISPLAY')
        docker_manager.os.getuid = lambda: uid
        os.environ['WAYLAND_DISPLAY'] = 'wayland-0'
        os.path.exists = fake_exists
        try:
            vols = m._build_volumes(profile_dir, downloads_dir)
        finally:
            docker_manager.os.getuid = real_getuid
            os.path.exists = real_exists
            if old_wd is None:
                os.environ.pop('WAYLAND_DISPLAY', None)
            else:
                os.environ['WAYLAND_DISPLAY'] = old_wd

        # NO whole-XDG_RUNTIME_DIR mount
        assert xdg not in vols, f"whole XDG_RUNTIME_DIR must not be mounted: {vols}"

        # Per-socket mounts into /home/chrome/.runtime
        assert vols[os.path.join(xdg, 'wayland-0')] == \
            {'bind': '/home/chrome/.runtime/wayland-0', 'mode': 'rw'}, vols
        assert vols[os.path.join(xdg, 'pipewire-0')] == \
            {'bind': '/home/chrome/.runtime/pipewire-0', 'mode': 'rw'}, vols
        assert vols[os.path.join(xdg, 'pulse')] == \
            {'bind': '/home/chrome/.runtime/pulse', 'mode': 'rw'}, vols

        # Profile + Downloads mounts still present
        assert vols[profile_dir] == {'bind': '/home/chrome/.config/chromium', 'mode': 'rw'}
        assert vols[downloads_dir] == {'bind': '/home/chrome/Downloads', 'mode': 'rw'}

        # T1 (F1): the user's home directory must NEVER be mounted at
        # /home/chrome/host. With neither an explicit host_mount nor a profile
        # name, nothing is mounted there at all.
        home = os.path.realpath(os.path.expanduser('~'))
        assert home not in vols, f"home directory must never be mounted: {vols}"
        assert os.path.expanduser('~') not in vols, \
            f"home directory must never be mounted: {vols}"
        assert [k for k, v in vols.items() if v.get('bind') == '/home/chrome/host'] == [], vols

        # /etc/localtime mount removed
        assert '/etc/localtime' not in vols, vols
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_host_mount_custom_and_missing():
    m, tmp = _make_manager()
    try:
        profile_dir = os.path.join(registry.CHROME_PROFILES_DIR, 'alice')
        downloads_dir = os.path.join(profile_dir, 'Downloads')
        os.makedirs(downloads_dir, exist_ok=True)

        # Custom host dir is mounted read-only
        custom = os.path.join(tmp, 'host-files')
        os.makedirs(custom, exist_ok=True)
        vols = m._build_volumes(profile_dir, downloads_dir, custom)
        assert vols[custom] == {'bind': '/home/chrome/host', 'mode': 'ro'}, vols

        # Nonexistent host dir is skipped, not mounted
        missing = os.path.join(tmp, 'does-not-exist')
        vols = m._build_volumes(profile_dir, downloads_dir, missing)
        assert missing not in vols, vols

        # Empty host_mount + a profile name -> the safe per-profile share dir,
        # created 0700, and never the home directory.
        vols = m._build_volumes(profile_dir, downloads_dir, '', profile_name='alice')
        share = os.path.join(host_share.HOST_SHARES_DIR, 'alice')
        assert os.path.isdir(share), share
        assert vols[share] == {'bind': '/home/chrome/host', 'mode': 'ro'}, vols
        assert os.stat(share).st_mode & 0o777 == 0o700, oct(os.stat(share).st_mode)
        assert os.path.realpath(os.path.expanduser('~')) not in vols, vols
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_start_container_never_mounts_home():
    """Regression for F1: a registered profile with host_mount "" must not
    result in the user's home directory being bind-mounted."""
    m, tmp = _make_manager()
    try:
        registry.register_profile('nohome', os.path.join(tmp, 'profiles', 'nohome'),
                                  host_mount='')
        result = m.start_container('nohome')
        assert result['status'] == 'started', result
        vols = m.client.containers.run_kwargs['volumes']

        home = os.path.realpath(os.path.expanduser('~'))
        assert home not in vols, f"home mounted: {vols}"
        assert os.path.expanduser('~') not in vols, f"home mounted: {vols}"

        host_srcs = [k for k, v in vols.items() if v.get('bind') == '/home/chrome/host']
        assert host_srcs == [os.path.join(host_share.HOST_SHARES_DIR, 'nohome')], \
            f"unexpected host mount: {vols}"
        assert os.listdir(host_srcs[0]) == [], \
            "default share dir must be empty, not a view of the home dir"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_start_container_explicit_host_mount_honoured():
    m, tmp = _make_manager()
    try:
        custom = os.path.join(tmp, 'shared')
        os.makedirs(custom, exist_ok=True)
        with open(os.path.join(custom, 'note.txt'), 'w') as f:
            f.write('hello')
        registry.register_profile('shared', os.path.join(tmp, 'profiles', 'shared'),
                                  host_mount=custom)
        m.start_container('shared')
        vols = m.client.containers.run_kwargs['volumes']
        assert vols[custom] == {'bind': '/home/chrome/host', 'mode': 'ro'}, vols
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_build_env():
    m, tmp = _make_manager()
    try:
        old_wd = os.environ.get('WAYLAND_DISPLAY')
        os.environ['WAYLAND_DISPLAY'] = 'wayland-9'
        try:
            env = m._build_env('alice')
        finally:
            if old_wd is None:
                os.environ.pop('WAYLAND_DISPLAY', None)
            else:
                os.environ['WAYLAND_DISPLAY'] = old_wd

        assert env['XDG_RUNTIME_DIR'] == '/home/chrome/.runtime', env
        assert env['PULSE_SERVER'] == 'unix:/home/chrome/.runtime/pulse/native', env
        assert env['WAYLAND_DISPLAY'] == 'wayland-9', env
        assert env['CHROME_PROFILE'] == 'alice', env
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_kwargs_for(profile='alice'):
    m, tmp = _make_manager()
    try:
        m.start_container(profile)
        return m.client.containers.run_kwargs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_start_container_hardened():
    """F4: the container must run with no capabilities, no-new-privileges,
    a bounded pid/memory budget and a read-only rootfs."""
    kwargs = _run_kwargs_for()

    # No capabilities at all — the browser runs as uid 1000 and needs none.
    assert kwargs['cap_drop'] == ['ALL'], kwargs
    # nothing may raise privilege via setuid/exec
    assert 'no-new-privileges' in kwargs['security_opt'], kwargs
    # bounded fork bombs. Pinned to the literal, NOT to
    # `DockerManager.PIDS_LIMIT`: comparing the code to itself passes for any
    # value at all, and -1 (unlimited) would sail through. Same reasoning as
    # the `mem_limit == '2g'` assertion below.
    assert kwargs['pids_limit'] == 2048, kwargs
    # bounded memory, no swap growth
    assert kwargs['mem_limit'] == kwargs['memswap_limit'] == '2g', kwargs
    # read-only rootfs
    assert kwargs['read_only'] is True, kwargs
    # and we never regress to privileged / host namespaces
    assert 'privileged' not in kwargs, kwargs
    assert kwargs.get('network_mode') in (None, 'default', 'bridge'), kwargs
    assert 'ipc_mode' not in kwargs, kwargs

    # ---- H1: the tmpfs set must reach `docker run` (not just _build_tmpfs) --
    #
    # test_tmpfs_set_is_sufficient() exercises DockerManager._build_tmpfs()
    # in isolation, which is a good test of the DICT and completely blind to
    # whether the launch uses it. `tmpfs=` is passed to containers.run() at
    # exactly one place, and mutation of that line to `tmpfs=None` used to
    # leave this whole suite green. That is the most load-bearing claim in the
    # hardening: read_only=True with no writable scratch means Chromium has
    # nowhere to stage its profile lock, Crashpad handler, or shader cache, and
    # the browser simply fails to cold-start — a bricked product that no test
    # noticed. So assert on the value that actually reached the API, not on the
    # helper's return value.
    tmpfs = kwargs.get('tmpfs')
    assert tmpfs is not None, (
        'tmpfs must be passed to containers.run(); with read_only=True and no '
        'writable scratch Chromium cannot cold-start '
        f'(read_only={kwargs.get("read_only")!r}, tmpfs={tmpfs!r})'
    )
    assert isinstance(tmpfs, (dict, set, frozenset, list, tuple)), \
        f'tmpfs must be a mapping or a collection of paths, got {type(tmpfs)}'
    # docker-py accepts a dict {path: options} or a list of paths; normalise
    # both to a set of paths so the assertion is about the paths themselves.
    paths = set(tmpfs)
    # Pinned to literals, never to DockerManager._TMPFS / _build_tmpfs():
    # comparing the code to itself passes for any value at all, which is
    # exactly how the tmpfs=None mutation slipped through.
    required_tmpfs = {
        '/tmp', '/run', '/var/tmp',
        '/home/chrome/.cache', '/home/chrome/.local',
        '/home/chrome/.runtime',
    }
    missing = required_tmpfs - paths
    assert not missing, (
        f'these paths must be tmpfs-mounted for a read-only rootfs: '
        f'{sorted(missing)}; got {sorted(paths)}'
    )
    # The tmpfs must be a mapping carrying mount options, not a bare list of
    # paths: a list would mount them all with Docker's permissive defaults,
    # losing nosuid/nodev/noexec/size on every one of them.
    assert isinstance(tmpfs, dict), (
        'tmpfs must be the {path: options} mapping from _build_tmpfs(), not a '
        f'bare list of paths, or the nosuid/nodev/noexec/size flags are all '
        f'dropped: {tmpfs!r}'
    )
    # And the scratch the browser needs must be owned by the uid we pin below,
    # or a read-only-rootfs browser cannot write its own cache.
    assert 'uid=1000' in tmpfs['/home/chrome/.cache'], tmpfs['/home/chrome/.cache']

    # ---- H2: the non-root boundary must be pinned at the CALL SITE ---------
    #
    # Without this, the container runs as uid 1000 only because the Dockerfile
    # says `USER chrome` — a build-time knob nobody controls at runtime.
    # `cap_drop=ALL` and `no-new-privileges` do NOT stop a root-owned process
    # from writing the profile bind mount, the Downloads bind mount,
    # /dev/shm and /dev/dri, so a rebuilt or swapped image would silently
    # remove the non-root boundary with zero test signal. Adding user='root'
    # to run_kwargs used to leave this suite green. Pinned to the literal.
    assert kwargs.get('user') == '1000:1000', (
        'the container must be pinned to a non-root user at the call site, not '
        f"left to the image's USER directive: got {kwargs.get('user')!r}"
    )
    # Belt and braces: reject the two specific fail-open values, so a future
    # edit cannot reintroduce root by some spelling that is not literally
    # 'root' (e.g. '0', '0:0', 'root:root').
    usr = str(kwargs.get('user', ''))
    assert usr not in ('root', '0', '0:0', 'root:root', ''), \
        f'container must not run as root: {usr!r}'
    assert not usr.startswith('0'), f'container uid must not be 0: {usr!r}'
    # The pinned uid is the same one the tmpfs scratch is created with, so the
    # two hardening decisions cannot drift apart into a bricked browser.
    pinned_uid = usr.split(':')[0]
    for path in ('/home/chrome/.cache', '/home/chrome/.local',
                 '/home/chrome/.runtime'):
        assert f'uid={pinned_uid}' in tmpfs[path], (path, tmpfs[path], pinned_uid)


def test_start_container_ulimits_include_nproc():
    kwargs = _run_kwargs_for()
    names = {u.name: (u.soft, u.hard) for u in kwargs['ulimits']}
    assert names.get('nofile') == (65536, 65536), names
    assert 'nproc' in names, names
    soft, hard = names['nproc']
    assert soft == hard and soft > 0, names


def test_tmpfs_set_is_sufficient():
    """Every path Chromium writes to must be tmpfs or an rw bind mount."""
    tmpfs = docker_manager.DockerManager._build_tmpfs()

    for path in ('/tmp', '/run', '/var/tmp',
                 '/home/chrome/.cache', '/home/chrome/.local',
                 '/home/chrome/.runtime'):
        assert path in tmpfs, f'{path} must be tmpfs for a read-only rootfs: {tmpfs}'

    # .config is NOT tmpfs — it is the rw profile bind mount, and the pulse
    # cookie lands under it.
    assert '/home/chrome/.config' not in tmpfs, tmpfs

    # The profile dir and Downloads are rw bind mounts, so they are writable
    # without tmpfs.
    assert '/home/chrome/Downloads' not in tmpfs, tmpfs

    # No suid/exec anywhere; the runtime dir must stay owner-only.
    for path, opts in tmpfs.items():
        assert 'nosuid' in opts and 'nodev' in opts, (path, opts)
        assert 'noexec' in opts, (path, opts)
        assert opts.startswith('rw,'), (path, opts)
    assert 'mode=700' in tmpfs['/home/chrome/.runtime'], tmpfs
    assert 'uid=1000' in tmpfs['/home/chrome/.cache'], tmpfs
    # world-writable scratch, like a normal /tmp
    assert 'mode=1777' in tmpfs['/tmp'], tmpfs


def test_tmpfs_returns_copy():
    a = docker_manager.DockerManager._build_tmpfs()
    a['/tmp'] = 'rw'
    assert docker_manager.DockerManager._build_tmpfs()['/tmp'] != 'rw', \
        '_build_tmpfs must not hand out the class-level dict'


def test_start_container_command():
    m, tmp = _make_manager()
    try:
        result = m.start_container('alice')
        assert result['status'] == 'started', result
        kwargs = m.client.containers.run_kwargs
        assert kwargs is not None, 'containers.run was not called'

        cmd = kwargs['command']
        assert not any('--enable-features' in c for c in cmd), \
            f"command must not contain --enable-features: {cmd}"
        # The window class must be namespaced and must equal the
        # StartupWMClass in the profile's .desktop entry, so the WM can
        # associate the window with its launcher. It must NOT be
        # `chrome-alice` (never matched the entry) and must never be
        # `google-chrome` (that is real Chrome's class).
        assert '--class=chrome-isolation-alice' in cmd, cmd
        assert '--class=google-chrome' not in cmd, cmd
        # GPU backend flags moved to stealth-launch.sh (per-profile choice)
        assert not any('--use-gl' in c for c in cmd), cmd
        assert not any('--use-angle' in c for c in cmd), cmd

        # DNS args removed
        assert 'dns' not in kwargs, kwargs
        assert 'dns_opt' not in kwargs, kwargs

        # devices only when /dev/dri exists on the host. The value is now the
        # explicit three-field spec (see L2 note below); asserted to the
        # literal, exactly as strictly as the old bare-path assertion was.
        if os.path.exists('/dev/dri'):
            assert kwargs.get('devices') == ['/dev/dri:/dev/dri:rw'], kwargs
        else:
            assert 'devices' not in kwargs, kwargs

        # 'audio' group only when it exists on the host
        try:
            grp.getgrnam('audio')
            assert kwargs['group_add'][0] == 'audio', kwargs
        except KeyError:
            assert 'audio' not in kwargs['group_add'], kwargs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_start_container_gpu_device_permissions():
    """L2: the /dev/dri device mapping must grant read+write, and must NOT
    grant mknod.

    The module docstring used to claim "GPU access via /dev/dri (read-only for
    display, rw for render)". That was never true. A bare '/dev/dri' string is
    expanded by docker-py's parse_devices() into
    CgroupPermissions='rwm' over the whole tree, card*/renderD* — including
    the primary control node — so the code and the comment disagreed, and in
    the direction of granting more than described.

    Read+WRITE is deliberate and required: libdrm opens the render node
    O_RDWR and Mesa/GBM's drmPrimeHandleToFD() needs a writable fd, so
    tightening this to 'r' would not harden the GPU, it would silently fall
    back to llvmpipe and regress the acceleration T2 verified. What is dropped
    is only the 'm' (mknod) bit, which no GPU code path ever uses and which
    cap_drop=ALL already denies anyway. So the assertion pins BOTH halves: rw
    is granted, m is not.
    """
    from docker.utils.utils import parse_devices

    kwargs = _run_kwargs_for()

    if not os.path.exists('/dev/dri'):
        # No GPU on this host: there must be no device mapping at all.
        assert 'devices' not in kwargs, kwargs
        return

    devices = kwargs.get('devices')
    # Pinned to the literal, not to docker_manager.GPU_DEVICE_SPEC: comparing
    # the code to itself passes for any value at all.
    assert devices == ['/dev/dri:/dev/dri:rw'], devices

    # Assert the EFFECTIVE permission, not just the string, by running the
    # spec through docker-py's own parser. This is the assertion that catches
    # the two-footgun failure modes a plain string equality cannot see:
    #   * '/dev/dri:rw'  parses as host='/dev/dri', container_path='rw',
    #                    permissions defaulted back to 'rwm' — it would create
    #                    a device node at the literal path /rw and restrict
    #                    nothing at all;
    #   * a future docker-py that changes the default for a two-field string
    #                    would silently re-widen this to rwm.
    parsed = parse_devices(devices)
    assert len(parsed) == 1, parsed
    dev = parsed[0]
    assert dev['PathOnHost'] == '/dev/dri', dev
    # Not the 'rw' footgun: the container path must still be the real path.
    assert dev['PathInContainer'] == '/dev/dri', dev
    perms = dev['CgroupPermissions']
    assert perms == 'rw', (
        f'/dev/dri must be mapped read-write (libdrm opens the render node '
        f'O_RDWR; read-only would regress GPU accel to llvmpipe): {dev}'
    )
    # And the mknod bit must NOT be granted. cap_drop=ALL already strips
    # CAP_MKNOD so mknod(2) would fail regardless — this is defence in depth,
    # asserting that the boundary does not silently re-widen if the capability
    # drop is ever relaxed, and that nobody re-inherits the rwm default.
    assert 'm' not in perms, (
        f'/dev/dri must not be mknod-capable (docker-py defaults a bare device '
        f'string to rwm): {dev}'
    )
    assert 'r' in perms and 'w' in perms, dev


def test_gpu_device_spec_comment_matches_behaviour():
    """The module docstring must not claim less access than the code grants.

    The regression this guards is a documentation/layer disagreement, so the
    check is textual: if the code ever goes back to a bare '/dev/dri' (rwm)
    while the docstring describes something narrower, or vice versa, one of the
    two tests above has to notice. Here we only assert that the code and the
    shipped constant cannot drift apart — the constant IS what is passed to
    containers.run.
    """
    import re
    spec = docker_manager.GPU_DEVICE_SPEC
    assert spec == '/dev/dri:/dev/dri:rw', spec
    # Three colon-separated fields: host, container, permissions. Fewer than
    # three means the permission field is being silently ignored by
    # parse_devices(), which is the '/dev/dri:rw' footgun.
    assert len(spec.split(':')) == 3, spec
    assert re.fullmatch(r'/dev/dri:/dev/dri:rwm?', spec), spec
    # The docstring must point at the constant rather than restate a claim
    # that can rot.
    doc = docker_manager.__doc__ or ''
    assert 'GPU_DEVICE_SPEC' in doc, 'docstring must reference GPU_DEVICE_SPEC'
    assert 'read-only for display' in doc and 'never true' in doc, (
        'the docstring should record that the old "read-only for display" '
        'claim was incorrect, so the correction is auditable'
    )


def test_start_container_proxy():
    m, tmp = _make_manager()
    try:
        # Register a profile with a loopback proxy — must be rewritten to
        # host.docker.internal and add the host-gateway alias.
        registry.register_profile(
            'proxied', os.path.join(registry.CHROME_PROFILES_DIR, 'proxied'),
            proxy='socks5://127.0.0.1:1080')
        result = m.start_container('proxied')
        assert result['status'] == 'started', result
        kwargs = m.client.containers.run_kwargs

        cmd = kwargs['command']
        assert '--proxy-server=socks5://host.docker.internal:1080' in cmd, cmd
        assert '--webrtc-ip-handling-policy=disable_non_proxied_udp' in cmd, cmd
        assert kwargs.get('extra_hosts') == {'host.docker.internal': 'host-gateway'}, kwargs

        # A profile without a proxy gets no proxy flags and no extra_hosts
        registry.register_profile('plain', os.path.join(registry.CHROME_PROFILES_DIR, 'plain'))
        m.start_container('plain')
        kwargs2 = m.client.containers.run_kwargs
        assert not any('--proxy-server' in c for c in kwargs2['command']), kwargs2
        assert 'extra_hosts' not in kwargs2, kwargs2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_start_container_authenticated_proxy():
    m, tmp = _make_manager()
    try:
        # Authenticated proxy -> local wrapper (127.0.0.1:1081) + env creds
        registry.register_profile(
            'authproxy', os.path.join(registry.CHROME_PROFILES_DIR, 'authproxy'),
            proxy='socks5://user:pass@127.0.0.1:1080')
        result = m.start_container('authproxy')
        assert result['status'] == 'started', result
        kwargs = m.client.containers.run_kwargs

        cmd = kwargs['command']
        assert '--proxy-server=socks5://127.0.0.1:1081' in cmd, cmd
        assert '--webrtc-ip-handling-policy=disable_non_proxied_udp' in cmd, cmd
        assert kwargs.get('extra_hosts') == {'host.docker.internal': 'host-gateway'}, kwargs

        env = kwargs['environment']
        assert env['CHROME_PROXY_SCHEME'] == 'socks5', env
        assert env['CHROME_PROXY_USER'] == 'user', env
        assert env['CHROME_PROXY_PASS'] == 'pass', env
        assert env['CHROME_PROXY_HOST'] == 'host.docker.internal', env
        assert env['CHROME_PROXY_PORT'] == '1080', env
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_start_container_saved_proxy_name():
    m, tmp = _make_manager()
    try:
        # Point the proxy store at a temp file with one saved proxy
        import proxy_store
        store_tmp = tempfile.mkdtemp(prefix='proxy-store-')
        proxy_store.PROXIES_FILE = os.path.join(store_tmp, 'proxies.json')
        proxy_store.add_proxy('tunnel-a', 'socks5://127.0.0.1:10080')

        # Profile references the proxy by NAME — must resolve to its URL
        registry.register_profile(
            'named', os.path.join(registry.CHROME_PROFILES_DIR, 'named'),
            proxy='tunnel-a')
        result = m.start_container('named')
        assert result['status'] == 'started', result
        kwargs = m.client.containers.run_kwargs
        cmd = kwargs['command']
        assert '--proxy-server=socks5://host.docker.internal:10080' in cmd, cmd
        assert kwargs.get('extra_hosts') == {'host.docker.internal': 'host-gateway'}, kwargs
        shutil.rmtree(store_tmp, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_profile_size_cache():
    m, tmp = _make_manager()
    try:
        profile_dir = os.path.join(registry.CHROME_PROFILES_DIR, 'alice')
        os.makedirs(profile_dir, exist_ok=True)
        with open(os.path.join(profile_dir, 'f'), 'w') as f:
            f.write('x' * (1024 * 1024))  # 1 MB

        s1 = m.profile_size_mb('alice')
        assert s1 == 1.0, s1
        # second call within TTL hits the cache
        assert m._size_cache['alice'][1] == s1
        assert m.profile_size_mb('alice') == s1

        # expire the cache entry, grow the file, expect a fresh walk
        m._size_cache['alice'] = (time.time() - 31, s1)
        with open(os.path.join(profile_dir, 'f'), 'w') as f:
            f.write('x' * (2 * 1024 * 1024))  # 2 MB
        assert m.profile_size_mb('alice') == 2.0
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