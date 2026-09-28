"""
Docker manager — hardened container runtime (S5 fix).
- No dangerous capabilities added; *all* capabilities are dropped
- no-new-privileges set, so nothing inside can regain privilege
- Bounded pid/ulimit/memory footprint
- Read-only root filesystem with an explicit tmpfs set for the paths
  Chromium legitimately writes to
- X11 socket mounted only when needed
- PulseAudio / PipeWire socket mounted
- GPU access via /dev/dri (read-only for display, rw for render)
- Host desktop preferences injected as read-only volumes + env vars
- --no-sandbox used inside the container (Docker's default seccomp profile blocks unprivileged user namespaces; the container itself is the security boundary)
- profile registry used for all path lookups
- T1 (F1): /home/chrome/host defaults to a safe, empty, per-profile share
  directory (mode 0700) — NEVER to the user's home directory. See
  backend/host_share.py for the full rationale.
"""
import grp
import hashlib
import os
import re
import subprocess
import sys
import time
from typing import Optional

import docker
import docker.errors
from docker.types import Ulimit

from config import DOCKER_IMAGE_NAME, CONTAINER_PREFIX, APP_DATA_DIR
from validator import validate_profile_name
from registry import resolve_profile_path, register_profile, get_profile
from host_share import ensure_host_share_dir, resolve_host_mount
from proxy_store import resolve as _resolve_proxy
from image_update import dockerfile_dir as _dockerfile_dir

# Stable hostname pools — deterministic per profile via MD5
_H_FIRST  = ['john','alex','sam','mike','lisa','chris','pat','lee','tom','jay','kai','max']
_H_DEVICE = ['laptop','desktop','pc','thinkpad','studio','home','book','box']


class DockerManager:
    _SIZE_CACHE_TTL = 30  # seconds

    # ---- runtime hardening limits (F4) ------------------------------------
    # Bounded but generous: Chromium runs a browser process, zygote, GPU
    # process, one renderer per site instance plus utility processes, and
    # pipewire/pulse clients. 2048 leaves a wide margin while still preventing
    # a fork bomb from exhausting the host pid table.
    PIDS_LIMIT = 2048
    # ~2 GB RAM ceiling (mem_limit == memswap_limit, so no swap growth).
    # NOTE: tmpfs pages ARE charged to the cgroup memory limit, so this budget
    # covers /dev/shm (2g max), the small tmpfs set below, and Chromium's
    # anonymous heap together. Measured steady state for a single browsing
    # profile is ~200 MB anon + ~35 MB shmem, so 2 GB leaves ~10x headroom
    # while still bounding a runaway renderer. Raise this if heavy video
    # pages start hitting the cgroup OOM killer.
    MEM_LIMIT = '2g'

    def __init__(self):
        self.client = docker.from_env()
        self._size_cache = {}  # {profile_name: (timestamp, size_mb)}
        self._ensure_image()

    # ------------------------------------------------------------------ image
    def _ensure_image(self):
        try:
            self.client.images.get(DOCKER_IMAGE_NAME)
        except docker.errors.ImageNotFound:
            self._build_image()

    def _build_image(self):
        # Single source of truth for "where is the Dockerfile" — the background
        # updater (image_update.dockerfile_dir) resolves it the same way, and
        # the two must never drift.
        dockerfile_dir = _dockerfile_dir()
        env = os.environ.copy()
        env['DOCKER_BUILDKIT'] = '1'
        result = subprocess.run(
            ['docker', 'build', '-t', DOCKER_IMAGE_NAME, dockerfile_dir],
            env=env, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"Docker build failed:\n{result.stderr}")

    # ----------------------------------------------------------------- naming
    def container_name(self, profile_name: str) -> str:
        return f"{CONTAINER_PREFIX}{validate_profile_name(profile_name)}"

    def _profile_hostname(self, profile_name: str) -> str:
        """Return a stable, human-looking hostname derived from the profile name."""
        h = int(hashlib.md5(profile_name.encode()).hexdigest(), 16)
        first  = _H_FIRST[h % len(_H_FIRST)]
        device = _H_DEVICE[(h >> 8) % len(_H_DEVICE)]
        return f"{first}-{device}"

    # ----------------------------------------------------------------- status
    def container_status(self, profile_name: str) -> str:
        try:
            c = self.client.containers.get(self.container_name(profile_name))
            return c.status
        except docker.errors.NotFound:
            return "not_found"

    def all_container_statuses(self) -> dict:
        """Return a mapping of profile_name -> status for all profile containers in a single Docker API call.

        Optimizes get_all_profiles by reducing Docker daemon socket roundtrips from O(N) to O(1).
        """
        try:
            containers = self.client.containers.list(all=True)
            prefix = CONTAINER_PREFIX
            prefix_len = len(prefix)
            statuses = {}
            for c in containers:
                cname = (getattr(c, 'name', '') or '').lstrip('/')
                if cname.startswith(prefix):
                    prof_name = cname[prefix_len:]
                    statuses[prof_name] = c.status
            return statuses
        except Exception:
            return {}

    # ---------------------------------------------------------------- profile size
    def profile_size_mb(self, profile_name: str) -> float:
        profile_name = validate_profile_name(profile_name)
        now = time.time()
        cached = self._size_cache.get(profile_name)
        if cached and now - cached[0] < self._SIZE_CACHE_TTL:
            return cached[1]

        path = resolve_profile_path(profile_name)
        if not os.path.isdir(path):
            self._size_cache[profile_name] = (now, 0.0)
            return 0.0
        total = 0
        for dp, _, fnames in os.walk(path):
            for fn in fnames:
                fp = os.path.join(dp, fn)
                if os.path.exists(fp):
                    total += os.path.getsize(fp)
        size_mb = round(total / (1024 * 1024), 2)
        self._size_cache[profile_name] = (now, size_mb)
        return size_mb

    # --------------------------------------------------------------- start
    def start_container(self, profile_name: str) -> dict:
        profile_name = validate_profile_name(profile_name)
        cname = self.container_name(profile_name)
        profile_dir = resolve_profile_path(profile_name)
        downloads_dir = os.path.join(profile_dir, "Downloads")
        host_mount = self._profile_host_mount(profile_name)

        os.makedirs(profile_dir, exist_ok=True)
        os.makedirs(downloads_dir, exist_ok=True)

        # Remove any stopped container so config changes take effect
        try:
            c = self.client.containers.get(cname)
            if c.status == "running":
                return {"status": "already_running"}
            c.remove(force=True)
        except docker.errors.NotFound:
            pass

        volumes = self._build_volumes(profile_dir, downloads_dir, host_mount,
                                      profile_name=profile_name)
        env = self._build_env(profile_name)
        device_gids = self._device_group_ids()
        hostname = self._profile_hostname(profile_name)

        # Per-profile proxy (optional) — routes the profile's traffic through a
        # different egress IP. Loopback addresses are rewritten to
        # host.docker.internal so the container reaches a proxy running on the
        # host; WebRTC is forced through the proxy to avoid IP leaks.
        # Authenticated proxies (user:pass@) are handled by the in-container
        # proxy-wrapper.py, since Chromium ignores credentials in proxy URLs.
        entry = get_profile(profile_name)
        proxy = (entry or {}).get('proxy', '')
        if proxy:
            # Saved proxy names resolve to their URL from the global store
            proxy = _resolve_proxy(proxy)
        command = [f'--class=chrome-{profile_name}']
        extra_hosts = None
        if proxy:
            parsed = self._parse_proxy(proxy)
            if parsed and parsed['user']:
                env['CHROME_PROXY_SCHEME'] = parsed['scheme']
                env['CHROME_PROXY_USER'] = parsed['user']
                env['CHROME_PROXY_PASS'] = parsed['pass']
                env['CHROME_PROXY_HOST'] = parsed['host'].replace(
                    '127.0.0.1', 'host.docker.internal').replace(
                    'localhost', 'host.docker.internal')
                env['CHROME_PROXY_PORT'] = str(parsed['port'])
                command += [
                    '--proxy-server=socks5://127.0.0.1:1081',
                    '--webrtc-ip-handling-policy=disable_non_proxied_udp',
                ]
            else:
                proxy = proxy.replace('127.0.0.1', 'host.docker.internal') \
                               .replace('localhost', 'host.docker.internal')
                command += [
                    f'--proxy-server={proxy}',
                    '--webrtc-ip-handling-policy=disable_non_proxied_udp',
                ]
            extra_hosts = {'host.docker.internal': 'host-gateway'}

        # Networking is deliberately left at Docker's default bridge. The
        # container needs outbound internet (DNS, TLS, WebSocket) to browse at
        # all, and the default bridge gives outbound NAT without exposing any
        # host port and without host networking. NOTE: do NOT "harden" this to
        # network_mode='none' — that removes all connectivity and breaks
        # browsing entirely. It would also break the per-profile proxy path,
        # which reaches the host proxy via the host-gateway alias.
        run_kwargs = dict(
            image=DOCKER_IMAGE_NAME,
            name=cname,
            # Realistic host identity — hides Docker-generated hex hostname
            hostname=hostname,
            # 2 GB shared memory (Docker default 64 MB is a VM/container tell)
            shm_size='2g',
            # Realistic open-file limit (default 1024 is another container tell).
            # nproc bounds thread/process creation for a single uid inside the
            # container, complementing pids_limit at the cgroup level.
            ulimits=[
                Ulimit(name='nofile', soft=65536, hard=65536),
                Ulimit(name='nproc', soft=4096, hard=4096),
            ],
            detach=True,
            volumes=volumes,
            environment=env,
            group_add=device_gids,
            # ---- runtime hardening (F4) --------------------------------
            # No privileged, no SYS_ADMIN, no ipc=host.
            # Drop *every* capability: the container runs as uid 1000 (chrome),
            # which needs none of them. /dev/dri is accessed via the video/render
            # group in group_add, not via CAP_SYS_ADMIN / device mknod.
            cap_drop=['ALL'],
            # no-new-privileges: blocks setuid binaries and prevents any exec
            # from raising privilege, so a Chromium RCE cannot escalate even
            # if something inside the image were setuid-root.
            security_opt=['no-new-privileges'],
            # Generous for Chromium (browser + GPU + renderers + zygote + pipewire
            # clients) but bounded, so a fork bomb cannot exhaust the host.
            pids_limit=self.PIDS_LIMIT,
            # Read-only root filesystem. Every writable path the browser needs is
            # an explicit tmpfs (see _build_tmpfs) or an existing rw bind mount
            # (profile dir, Downloads, mounted sockets).
            read_only=True,
            tmpfs=self._build_tmpfs(),
            # ~2 GB RAM cap, documented on MEM_LIMIT. memswap == mem_limit
            # means the container cannot grow swap usage.
            mem_limit=self.MEM_LIMIT,
            memswap_limit=self.MEM_LIMIT,
            # GPU backend flags are chosen per-profile in stealth-launch.sh.
            command=command,
        )
        if extra_hosts:
            run_kwargs['extra_hosts'] = extra_hosts
        # GPU access only when the host actually exposes /dev/dri
        if os.path.exists('/dev/dri'):
            run_kwargs['devices'] = ['/dev/dri']
        # 'audio' group only if it exists on the host
        try:
            grp.getgrnam('audio')
            run_kwargs['group_add'] = ['audio'] + device_gids
        except KeyError:
            pass

        container = self.client.containers.run(**run_kwargs)
        return {"status": "started", "container_id": container.id}

    # ------------------------------------------------------------ tmpfs set
    # Paths Chromium (and its helpers) legitimately write to, given a read-only
    # root filesystem. Everything NOT listed here must come from a rw bind
    # mount — profile dir, Downloads, and the individual host sockets — which
    # the read-only rootfs does not affect because mounts are applied on top.
    #
    # Sized small on purpose: these are scratch space, not a place to stage
    # data. A renderer compromise is limited to a few hundred MB of tmpfs that
    # vanishes with the container.
    _TMPFS = {
        # Chromium's own scratch (Crashpad, mojo shared memory staging, temp
        # profile locks, Mesa/llvmpipe shader cache in some paths).
        '/tmp':                 'rw,nosuid,nodev,noexec,size=64m,mode=1777',
        # Alpine's /run holds runtime state: pipewire/pulse lookups, nscd-style
        # caches, dbus socket paths. Small and volatile. mode=1777 matches what
        # a normal container's /run looks like to anything that wants to drop a
        # lock or socket there.
        '/run':                 'rw,nosuid,nodev,noexec,size=8m,mode=1777',
        '/var/tmp':             'rw,nosuid,nodev,noexec,size=16m,mode=1777',
        # XDG_CACHE_HOME: fontconfig cache, mesa/gallium shader cache,
        # Chromium's disk_cache. Must be owned by uid 1000 (chrome).
        '/home/chrome/.cache':  'rw,nosuid,nodev,noexec,size=256m,uid=1000,gid=1000,mode=700',
        # XDG_DATA_HOME / XDG_STATE_HOME: fontconfig data, desktop entries,
        # Chromium's Local State-adjacent state that is not in the profile dir.
        '/home/chrome/.local':  'rw,nosuid,nodev,noexec,size=32m,uid=1000,gid=1000,mode=755',
        # XDG_RUNTIME_DIR. The Wayland/PipeWire/Pulse *sockets* are individual
        # rw bind mounts layered on top of this tmpfs (Docker mounts parents
        # first), so the directory itself stays writable for lock files while
        # only those specific host sockets are reachable.
        '/home/chrome/.runtime': 'rw,nosuid,nodev,noexec,size=8m,uid=1000,gid=1000,mode=700',
    }

    @classmethod
    def _build_tmpfs(cls) -> dict:
        return dict(cls._TMPFS)

    # ----------------------------------------------------------------- stop
    def stop_container(self, profile_name: str) -> dict:
        profile_name = validate_profile_name(profile_name)
        try:
            self.client.containers.get(self.container_name(profile_name)).stop(timeout=5)
            return {"status": "stopped"}
        except docker.errors.NotFound:
            return {"status": "not_found"}

    # ---------------------------------------------------------------- remove
    def remove_container(self, profile_name: str) -> dict:
        profile_name = validate_profile_name(profile_name)
        try:
            self.client.containers.get(self.container_name(profile_name)).remove(force=True)
            return {"status": "removed"}
        except docker.errors.NotFound:
            return {"status": "not_found"}

    # ============================================================= internals
    @staticmethod
    def _parse_proxy(proxy: str) -> Optional[dict]:
        """Split a proxy URL into scheme/user/pass/host/port. Returns None if
        the format is not recognised (profile_service validates it first)."""
        m = re.match(
            r'^(socks5|socks5h|http|https)://(?:([^:@/]+):([^:@/]+)@)?'
            r'([a-zA-Z0-9._-]+):(\d{1,5})$',
            proxy,
        )
        if not m:
            return None
        scheme, user, pwd, host, port = m.groups()
        return {
            'scheme': scheme,
            'user': user or '',
            'pass': pwd or '',
            'host': host,
            'port': int(port),
        }

    def _profile_host_mount(self, profile_name: str) -> str:
        """
        Per-profile host folder mounted READ-ONLY at /home/chrome/host.

        SECURITY (T1 / F1): an empty `host_mount` NO LONGER falls back to the
        user's home directory. That default handed every container the whole
        home dir (SSH keys, .gnupg, KWallet, other browser profiles);
        read-only stops writes but not exfiltration.

        `host_mount == ""` now means "use a safe, empty, per-profile share
        directory" at <APP_DATA_DIR>/host-shares/<profile_name>/ (mode 0700,
        created on demand). Existing profiles that already store "" become safe
        without any registry rewrite.

        An explicit `host_mount` is still honoured — that is a deliberate user
        choice — and is validated by host_share.validate_host_mount().
        """
        entry = get_profile(profile_name)
        configured = (entry or {}).get('host_mount', '')
        return resolve_host_mount(profile_name, configured)

    def _build_volumes(self, profile_dir: str, downloads_dir: str,
                       host_mount: str = '', profile_name: str = '') -> dict:
        uid = os.getuid()
        xdg_runtime = f'/run/user/{uid}'
        wayland_display = os.environ.get('WAYLAND_DISPLAY', 'wayland-0')
        wayland_sock = os.path.join(xdg_runtime, wayland_display)

        volumes = {
            # Profile data — isolated to this profile only
            profile_dir:   {'bind': '/home/chrome/.config/chromium', 'mode': 'rw'},
            downloads_dir: {'bind': '/home/chrome/Downloads',        'mode': 'rw'},
        }

        # Host folder (read-only) at /home/chrome/host.
        # `host_mount` is already resolved by _profile_host_mount(): either a
        # validated user-chosen directory or the safe empty per-profile share
        # dir. NEVER fall back to the home directory here.
        if host_mount:
            host_src = os.path.realpath(os.path.expanduser(host_mount))
        elif profile_name:
            host_src = ensure_host_share_dir(profile_name)
        else:
            # No profile context available — mount nothing rather than risk
            # exposing the host's home directory.
            host_src = ''
        if host_src and os.path.isdir(host_src):
            volumes[host_src] = {'bind': '/home/chrome/host', 'mode': 'ro'}

        # Wayland / PipeWire / PulseAudio — mount only the individual sockets
        # into the container's /home/chrome/.runtime (never the whole
        # XDG_RUNTIME_DIR, which would expose unrelated host sockets).
        for sock, bind_name in (
            (wayland_sock, wayland_display),
            (os.path.join(xdg_runtime, 'pipewire-0'), 'pipewire-0'),
            (os.path.join(xdg_runtime, 'pulse'), 'pulse'),
        ):
            if os.path.exists(sock):
                volumes[sock] = {'bind': f'/home/chrome/.runtime/{bind_name}', 'mode': 'rw'}

        # PulseAudio cookie (lives outside XDG_RUNTIME_DIR)
        for cookie in (
            os.path.expanduser('~/.config/pulse/cookie'),
            os.path.expanduser('~/.pulse-cookie'),
        ):
            if os.path.exists(cookie):
                volumes[cookie] = {'bind': '/home/chrome/.config/pulse/cookie', 'mode': 'ro'}
                break

        # ---- Host read-only desktop preferences (S5 / host pref brief) ----
        # Fonts
        for src in ('/usr/share/fonts', os.path.expanduser('~/.local/share/fonts')):
            if os.path.exists(src):
                volumes[src] = {'bind': src, 'mode': 'ro'}

        # Icons + themes
        for src in ('/usr/share/icons', '/usr/share/themes',
                    os.path.expanduser('~/.icons'), os.path.expanduser('~/.themes')):
            if os.path.exists(src):
                volumes[src] = {'bind': src, 'mode': 'ro'}

        # GTK config (read-only so isolated profile doesn't write back)
        for src in (os.path.expanduser('~/.config/gtk-3.0'),
                    os.path.expanduser('~/.config/gtk-4.0')):
            if os.path.exists(src):
                volumes[src] = {'bind': src, 'mode': 'ro'}

        # ---- /.dockerenv mask: REMOVED ON PURPOSE (F5 / T3) ----
        #
        # The container image runs as the non-root user `chrome` (UID 1000) and
        # this code runs as the host user, so a bind-mounted mask can never
        # reproduce Docker's real `/.dockerenv` (mode 0755, owner 0:0 --
        # verified against docker 29.8.1). A mask created here is 0644,
        # 1000:1000, and inside the container it stats as `644 1000:1000`
        # -- i.e. a *non-standard* `/.dockerenv`, which is a strictly worse
        # fingerprint tell than leaving the real file in place. The only way
        # to get 0:0 is to run the container as UID 0 or to chown a file as
        # root on the host; both undo the container hardening (T2) and are
        # not worth it.
        #
        # Decision: do not mask. `/.dockerenv` keeps its genuine 0755 root:root
        # metadata, so any check that merely stats the file sees exactly what
        # a real Docker container shows. This is an honest accept-the-tell:
        # a correct-looking file is worth more than a fake one, and the tell
        # is in any case one of many (cgroup v2 line, Alpine os-release,
        # uname release, /sys/class/dmi, /proc/cpuinfo) -- see T4, which
        # addresses those properly.
        #
        # The unused `.empty` marker file in APP_DATA_DIR is left in place
        # (harmless, and it is not a live container mount).
        return volumes

    def _build_env(self, profile_name: str) -> dict:
        wayland_display = os.environ.get('WAYLAND_DISPLAY', 'wayland-0')
        env = {
            # Wayland — no DISPLAY/X11
            'WAYLAND_DISPLAY':  wayland_display,
            # Container-side runtime dir holding the mounted sockets
            'XDG_RUNTIME_DIR':  '/home/chrome/.runtime',
            'XDG_SESSION_TYPE': 'wayland',
            'GDK_BACKEND':      'wayland',
            'QT_QPA_PLATFORM':  'wayland',
            # Audio — PipeWire (preferred) + PulseAudio fallback
            'PULSE_SERVER':   'unix:/home/chrome/.runtime/pulse/native',
            'PIPEWIRE_REMOTE': '/home/chrome/.runtime/pipewire-0',
            # Profile
            'CHROME_PROFILE': profile_name,
            # Locale
            'LANG':   os.environ.get('LANG',   'en_US.UTF-8'),
            'LC_ALL': os.environ.get('LC_ALL', os.environ.get('LANG', 'en_US.UTF-8')),
        }
        # Host GTK/cursor theme so Chrome feels native
        for key in ('GTK_THEME', 'ICON_THEME', 'XCURSOR_THEME', 'XCURSOR_SIZE'):
            if key in os.environ:
                env[key] = os.environ[key]

        # Detect host dark/light mode and forward so Chromium can match it.
        # Priority: gsettings (GNOME) → GTK_THEME suffix → fallback dark
        color_scheme = self._detect_host_color_scheme()
        env['HOST_COLOR_SCHEME'] = color_scheme
        # GTK_THEME dark variant hint (e.g. "Adwaita:dark")
        if color_scheme == 'dark':
            if 'GTK_THEME' not in env:
                env['GTK_THEME'] = 'Adwaita:dark'
            elif ':dark' not in env['GTK_THEME']:
                env['GTK_THEME'] = env['GTK_THEME'].rstrip(':light') + ':dark'
        return env

    @staticmethod
    def _detect_host_color_scheme() -> str:
        """Return 'dark' or 'light' based on the host GNOME/GTK colour preference."""
        # 1. gsettings (most reliable on GNOME)
        try:
            out = subprocess.run(
                ['gsettings', 'get', 'org.gnome.desktop.interface', 'color-scheme'],
                capture_output=True, text=True, timeout=2
            ).stdout.strip().strip("'")
            if 'dark' in out:
                return 'dark'
            if 'light' in out or out == 'default':
                return 'light'
        except Exception:
            pass
        # 2. GTK_THEME env var
        gtk = os.environ.get('GTK_THEME', '')
        if ':dark' in gtk.lower():
            return 'dark'
        if ':light' in gtk.lower():
            return 'light'
        # 3. ~/.config/gtk-4.0/settings.ini
        for ini in (os.path.expanduser('~/.config/gtk-4.0/settings.ini'),
                    os.path.expanduser('~/.config/gtk-3.0/settings.ini')):
            if os.path.exists(ini):
                try:
                    with open(ini) as f:
                        content = f.read()
                    if 'prefer-dark-theme=1' in content or 'prefer-dark-theme=true' in content.lower():
                        return 'dark'
                    if 'prefer-dark-theme=0' in content:
                        return 'light'
                except Exception:
                    pass
        return 'dark'  # sensible default

    def _device_group_ids(self) -> list:
        gids = []
        for gname in ('video', 'render'):
            try:
                gids.append(grp.getgrnam(gname).gr_gid)
            except KeyError:
                pass
        return gids


