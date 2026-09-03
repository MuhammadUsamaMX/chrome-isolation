"""
Docker manager — hardened container runtime (S5 fix).
- No dangerous capabilities added
- X11 socket mounted only when needed
- PulseAudio / PipeWire socket mounted
- GPU access via /dev/dri (read-only for display, rw for render)
- Host desktop preferences injected as read-only volumes + env vars
- --no-sandbox used inside the container (Docker's default seccomp profile blocks unprivileged user namespaces; the container itself is the security boundary)
- profile registry used for all path lookups
"""
import grp
import hashlib
import os
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

# Stable hostname pools — deterministic per profile via MD5
_H_FIRST  = ['john','alex','sam','mike','lisa','chris','pat','lee','tom','jay','kai','max']
_H_DEVICE = ['laptop','desktop','pc','thinkpad','studio','home','book','box']


class DockerManager:
    _SIZE_CACHE_TTL = 30  # seconds

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
        dockerfile_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # Inside a PyInstaller onefile bundle __file__ points into the _MEIPASS
        # temp dir; the Dockerfile lives next to the packaged bridge binary.
        if not os.path.isfile(os.path.join(dockerfile_dir, 'Dockerfile')):
            dockerfile_dir = os.path.dirname(sys.executable)
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

        volumes = self._build_volumes(profile_dir, downloads_dir, host_mount)
        env = self._build_env(profile_name)
        device_gids = self._device_group_ids()
        hostname = self._profile_hostname(profile_name)

        run_kwargs = dict(
            image=DOCKER_IMAGE_NAME,
            name=cname,
            # Realistic host identity — hides Docker-generated hex hostname
            hostname=hostname,
            # 2 GB shared memory (Docker default 64 MB is a VM/container tell)
            shm_size='2g',
            # Realistic open-file limit (default 1024 is another container tell)
            ulimits=[Ulimit(name='nofile', soft=65536, hard=65536)],
            detach=True,
            volumes=volumes,
            environment=env,
            group_add=device_gids,
            # No privileged, no SYS_ADMIN, no ipc=host.
            # GPU backend flags are chosen per-profile in stealth-launch.sh.
            command=[
                f'--class=chrome-{profile_name}',
            ]
        )
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
    def _profile_host_mount(self, profile_name: str) -> str:
        """
        Per-profile host folder mounted read-only into the container.
        Falls back to the user's home directory when the profile has no
        custom host_mount configured.
        """
        entry = get_profile(profile_name)
        if entry and entry.get('host_mount'):
            return os.path.realpath(os.path.expanduser(entry['host_mount']))
        return os.path.expanduser('~')

    def _build_volumes(self, profile_dir: str, downloads_dir: str, host_mount: str = '') -> dict:
        uid = os.getuid()
        xdg_runtime = f'/run/user/{uid}'
        wayland_display = os.environ.get('WAYLAND_DISPLAY', 'wayland-0')
        wayland_sock = os.path.join(xdg_runtime, wayland_display)

        volumes = {
            # Profile data — isolated to this profile only
            profile_dir:   {'bind': '/home/chrome/.config/chromium', 'mode': 'rw'},
            downloads_dir: {'bind': '/home/chrome/Downloads',        'mode': 'rw'},
        }

        # Host folder (read-only) — the user's home by default, or a
        # per-profile custom directory. Mounted at /home/chrome/host so the
        # browser can read host files but never write to them.
        host_src = host_mount or os.path.expanduser('~')
        if os.path.isdir(host_src):
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

        # Mask /.dockerenv — Docker creates this zero-byte file at runtime;
        # fingerprinting scripts check its existence to detect containers.
        dockerenv_mask = os.path.join(APP_DATA_DIR, '.empty')
        if not os.path.exists(dockerenv_mask):
            open(dockerenv_mask, 'w').close()
        volumes[dockerenv_mask] = {'bind': '/.dockerenv', 'mode': 'ro'}

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
            # Audio
            'PULSE_SERVER': 'unix:/home/chrome/.runtime/pulse/native',
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


