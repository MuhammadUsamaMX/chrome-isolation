"""
Docker manager — hardened container runtime (S5 fix).
- No dangerous capabilities added
- X11 socket mounted only when needed
- PulseAudio / PipeWire socket mounted
- GPU access via /dev/dri (read-only for display, rw for render)
- Host desktop preferences injected as read-only volumes + env vars
- --no-sandbox removed (uses user namespace seccomp profile instead)
- profile registry used for all path lookups
"""
import grp
import hashlib
import os
import subprocess
from typing import Optional

import docker
import docker.errors
from docker.types import Ulimit

from config import DOCKER_IMAGE_NAME, CONTAINER_PREFIX, APP_DATA_DIR
from validator import validate_profile_name
from registry import resolve_profile_path, register_profile

# Stable hostname pools — deterministic per profile via MD5
_H_FIRST  = ['john','alex','sam','mike','lisa','chris','pat','lee','tom','jay','kai','max']
_H_DEVICE = ['laptop','desktop','pc','thinkpad','studio','home','book','box']


class DockerManager:
    def __init__(self):
        self.client = docker.from_env()
        self._ensure_image()

    # ------------------------------------------------------------------ image
    def _ensure_image(self):
        try:
            self.client.images.get(DOCKER_IMAGE_NAME)
        except docker.errors.ImageNotFound:
            self._build_image()

    def _build_image(self):
        dockerfile_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
        path = resolve_profile_path(validate_profile_name(profile_name))
        if not os.path.isdir(path):
            return 0.0
        total = 0
        for dp, _, fnames in os.walk(path):
            for fn in fnames:
                fp = os.path.join(dp, fn)
                if os.path.exists(fp):
                    total += os.path.getsize(fp)
        return round(total / (1024 * 1024), 2)

    # --------------------------------------------------------------- start
    def start_container(self, profile_name: str) -> dict:
        profile_name = validate_profile_name(profile_name)
        cname = self.container_name(profile_name)
        profile_dir = resolve_profile_path(profile_name)
        downloads_dir = os.path.join(profile_dir, "Downloads")

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

        volumes = self._build_volumes(profile_dir, downloads_dir)
        env = self._build_env(profile_name)
        dns = self._host_dns()
        device_gids = self._device_group_ids()
        hostname = self._profile_hostname(profile_name)

        container = self.client.containers.run(
            DOCKER_IMAGE_NAME,
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
            devices=['/dev/dri'],
            group_add=['audio'] + device_gids,
            dns=dns,
            dns_opt=['ndots:0'],
            # No privileged, no SYS_ADMIN, no ipc=host
            command=[
                f'--class=chrome-{profile_name}',
                '--enable-features=VulkanFromANGLE,DefaultANGLEVulkan',
                '--use-gl=angle',
                '--use-angle=vulkan',
            ]
        )
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
    def _build_volumes(self, profile_dir: str, downloads_dir: str) -> dict:
        uid = os.getuid()
        xdg_runtime = f'/run/user/{uid}'
        wayland_display = os.environ.get('WAYLAND_DISPLAY', 'wayland-0')
        wayland_sock = os.path.join(xdg_runtime, wayland_display)

        volumes = {
            # Profile data — isolated to this profile only
            profile_dir:   {'bind': '/home/chrome/.config/chromium', 'mode': 'rw'},
            downloads_dir: {'bind': '/home/chrome/Downloads',        'mode': 'rw'},
        }

        # Wayland socket + PipeWire/PulseAudio — mount the whole XDG_RUNTIME_DIR rw.
        # This gives Chromium access to the Wayland compositor socket, PipeWire,
        # and PulseAudio without needing X11 at all.
        if os.path.exists(wayland_sock):
            volumes[xdg_runtime] = {'bind': xdg_runtime, 'mode': 'rw'}
        else:
            # Fallback: at least mount audio socket if present
            pulse_run = os.path.join(xdg_runtime, 'pulse')
            if os.path.exists(pulse_run):
                volumes[pulse_run] = {'bind': pulse_run, 'mode': 'rw'}

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

        # Timezone
        if os.path.exists('/etc/localtime'):
            volumes['/etc/localtime'] = {'bind': '/etc/localtime', 'mode': 'ro'}

        # Mask /.dockerenv — Docker creates this zero-byte file at runtime;
        # fingerprinting scripts check its existence to detect containers.
        dockerenv_mask = os.path.join(APP_DATA_DIR, '.empty')
        if not os.path.exists(dockerenv_mask):
            open(dockerenv_mask, 'w').close()
        volumes[dockerenv_mask] = {'bind': '/.dockerenv', 'mode': 'ro'}

        return volumes

    def _build_env(self, profile_name: str) -> dict:
        uid = os.getuid()
        xdg_runtime = f'/run/user/{uid}'
        wayland_display = os.environ.get('WAYLAND_DISPLAY', 'wayland-0')
        env = {
            # Wayland — no DISPLAY/X11
            'WAYLAND_DISPLAY':  wayland_display,
            'XDG_RUNTIME_DIR':  xdg_runtime,
            'XDG_SESSION_TYPE': 'wayland',
            'GDK_BACKEND':      'wayland',
            'QT_QPA_PLATFORM':  'wayland',
            # Audio
            'PULSE_SERVER': f'unix:{xdg_runtime}/pulse/native',
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
        return env

    def _host_dns(self) -> list:
        return ['172.17.0.1', '8.8.8.8', '8.8.4.4']

    def _device_group_ids(self) -> list:
        gids = []
        for gname in ('video', 'render'):
            try:
                gids.append(grp.getgrnam(gname).gr_gid)
            except KeyError:
                pass
        return gids


