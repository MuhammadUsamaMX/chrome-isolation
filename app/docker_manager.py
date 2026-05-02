"""
Docker Manager - Handle container lifecycle operations
"""
import docker
import os
import subprocess
from config import DOCKER_IMAGE_NAME, CONTAINER_PREFIX, CHROME_PROFILES_DIR
from desktop_manager import DesktopManager

class DockerManager:
    def __init__(self):
        self.client = docker.from_env()
        self.ensure_image_exists()
        # Initialize desktop manager for creating desktop entries
        launcher_script = os.path.join(os.path.dirname(os.path.dirname(__file__)), 
                                       'scripts', 'chrome-launcher.sh')
        self.desktop_mgr = DesktopManager(launcher_script)
    
    def ensure_image_exists(self):
        """Check if Docker image exists, build if missing"""
        try:
            self.client.images.get(DOCKER_IMAGE_NAME)
            print(f"✅ Docker image '{DOCKER_IMAGE_NAME}' found")
        except docker.errors.ImageNotFound:
            print(f"⚠️  Docker image '{DOCKER_IMAGE_NAME}' not found. Building...")
            self.build_image()
    
    def build_image(self):
        """Build the Docker image"""
        try:
            # Get the directory containing the Dockerfile
            dockerfile_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            
            print(f"🔨 Building Docker image from {dockerfile_dir}...")
            
            # Use subprocess to run docker build with BuildKit
            env = os.environ.copy()
            env['DOCKER_BUILDKIT'] = '1'
            
            result = subprocess.run(
                ['docker', 'build', '-t', DOCKER_IMAGE_NAME, dockerfile_dir],
                env=env,
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                print(f"✅ Successfully built Docker image '{DOCKER_IMAGE_NAME}'")
            else:
                print(f"❌ Failed to build Docker image:")
                print(result.stderr)
                raise Exception(f"Docker build failed: {result.stderr}")
                
        except Exception as e:
            print(f"❌ Error building Docker image: {e}")
            raise
    
    def get_container_name(self, profile_name):
        """Get container name for a profile"""
        return f"{CONTAINER_PREFIX}{profile_name}"
    
    def get_profile_dir(self, profile_name):
        """Get profile directory path"""
        return os.path.join(CHROME_PROFILES_DIR, profile_name)
    
    def container_exists(self, profile_name):
        """Check if container exists"""
        try:
            self.client.containers.get(self.get_container_name(profile_name))
            return True
        except docker.errors.NotFound:
            return False
    
    def container_status(self, profile_name):
        """Get container status"""
        try:
            container = self.client.containers.get(self.get_container_name(profile_name))
            return container.status
        except docker.errors.NotFound:
            return "not_found"
    
    def _get_device_group_ids(self):
        """Get GIDs for video and render groups to allow GPU access"""
        gids = []
        try:
            import grp
            for group in ['video', 'render']:
                try:
                    gid = grp.getgrnam(group).gr_gid
                    gids.append(gid)
                except KeyError:
                    pass
        except Exception as e:
            print(f"⚠️  Failed to get device group IDs: {e}")
        return gids

    def start_container(self, profile_name):
        """Start a Chrome container for the profile"""
        container_name = self.get_container_name(profile_name)
        profile_dir = self.get_profile_dir(profile_name)
        downloads_dir = os.path.join(profile_dir, "Downloads")
        
        # Create directories if they don't exist
        os.makedirs(profile_dir, exist_ok=True)
        os.makedirs(downloads_dir, exist_ok=True)
        
        # Check if container already exists
        try:
            container = self.client.containers.get(container_name)
            if container.status == "running":
                return {"status": "already_running"}
            else:
                # Remove the stopped container to force valid re-creation from the latest image
                # This ensures updates to the Dockerfile/Flags are applied immediately
                print(f"♻️  Removing stopped container {container_name} to ensure freshness")
                container.remove(force=True)
        except docker.errors.NotFound:
            pass
        
        # Get PulseAudio cookie
        pulse_cookie = None
        for path in [os.path.expanduser("~/.config/pulse/cookie"), 
                     os.path.expanduser("~/.pulse-cookie")]:
            if os.path.exists(path):
                pulse_cookie = path
                break
        
        if not pulse_cookie:
            pulse_cookie = "/tmp/pulse-cookie-generated"
            open(pulse_cookie, 'a').close()
        
        # Container configuration
        user_id = os.getuid()
        volumes = {
            '/tmp/.X11-unix': {'bind': '/tmp/.X11-unix', 'mode': 'rw'},
            f'/run/user/{user_id}/pulse': {'bind': '/run/user/1000/pulse', 'mode': 'rw'},
            pulse_cookie: {'bind': '/home/chrome/.config/pulse/cookie', 'mode': 'ro'},
            profile_dir: {'bind': '/home/chrome/.config/chromium', 'mode': 'rw'},
            downloads_dir: {'bind': '/home/chrome/Downloads', 'mode': 'rw'}
        }
        
        environment = {
            'DISPLAY': os.environ.get('DISPLAY', ':0'),
            'PULSE_SERVER': 'unix:/run/user/1000/pulse/native',
            'PULSE_COOKIE': '/home/chrome/.config/pulse/cookie',
            'CHROME_PROFILE': profile_name,  # Pass profile name for stealth fingerprinting
            'TZ': 'UTC',  # Will be overridden by stealth scripts
            'LANG': 'en_US.UTF-8',
            'LC_ALL': 'en_US.UTF-8'
        }
        
        devices = ['/dev/dri']
        
        # Get host DNS servers for proper network resolution
        dns_servers = self._get_host_dns_servers()
        
        # Automatically setup X11/Wayland access
        self._setup_display_access()
        
        # Get device group IDs for GPU access
        device_gids = self._get_device_group_ids()
        group_add = ['audio'] + device_gids
        
        # Create desktop entry if it doesn't exist
        if not self.desktop_mgr.desktop_entry_exists(profile_name):
            try:
                self.desktop_mgr.create_desktop_entry(profile_name)
                print(f"✅ Created desktop entry for profile: {profile_name}")
            except Exception as e:
                print(f"⚠️  Failed to create desktop entry: {e}")

        # Create and start container with security-hardened configuration
        # - No ipc=host (uses default IPC isolation)
        # - No SYS_ADMIN capability (removed dangerous capabilities)
        # - Uses Docker's default seccomp profile (compatible with Chrome)
        container = self.client.containers.run(
            DOCKER_IMAGE_NAME,
            name=container_name,
            detach=True,
            # Removed: ipc_mode='host' (security risk - shares IPC with host)
            # Removed: cap_add=['SYS_ADMIN', 'SYS_PTRACE', 'NET_ADMIN'] (dangerous capabilities)
            volumes=volumes,
            environment=environment,
            devices=devices,
            group_add=group_add,
            # Use Docker's default seccomp profile (no custom restrictions)
            dns=dns_servers,
            dns_opt=['ndots:0'],
            command=[
                f'--class=chrome-{profile_name}',
                '--enable-features=VulkanFromANGLE,DefaultANGLEVulkan',
                '--use-gl=angle',
                '--use-angle=vulkan'
            ]
        )
        
        return {"status": "created", "container_id": container.id}
    
    def stop_container(self, profile_name):
        """Stop a container"""
        try:
            container = self.client.containers.get(self.get_container_name(profile_name))
            container.stop()
            return {"status": "stopped"}
        except docker.errors.NotFound:
            return {"status": "not_found"}
    
    def remove_container(self, profile_name):
        """Remove a container"""
        try:
            container = self.client.containers.get(self.get_container_name(profile_name))
            container.remove(force=True)
            return {"status": "removed"}
        except docker.errors.NotFound:
            return {"status": "not_found"}
    
    def _get_host_dns_servers(self):
        """Get DNS servers - use Docker bridge gateway to access host DNS"""
        # Use Docker bridge gateway (172.17.0.1) to access host's DNS resolver
        # This allows containers to use the host's DNS configuration
        dns_servers = ['172.17.0.1']
        
        # Add fallback DNS servers for reliability
        dns_servers.extend(['8.8.8.8', '8.8.4.4'])
        
        print(f"✅ Using Docker bridge gateway DNS (172.17.0.1) with fallbacks: {', '.join(dns_servers[1:])}")
        
        return dns_servers
    
    def _setup_display_access(self):
        """Setup X11/Wayland display access for Docker containers"""
        import subprocess
        
        # Detect display server
        wayland_display = os.environ.get('WAYLAND_DISPLAY')
        xdg_session_type = os.environ.get('XDG_SESSION_TYPE', '').lower()
        
        # Setup X11 access (works for both X11 and XWayland)
        try:
            # We explicitly run this every time to ensure permissions are correct
            result = subprocess.run(['xhost', '+local:docker'], 
                          capture_output=True, 
                          text=True,
                          check=False)
            
            if result.returncode == 0:
                print("✅ X11 access configured for Docker")
            else:
                print(f"⚠️  Failed to configure X11 access: {result.stderr}")
                
        except FileNotFoundError:
            print("⚠️  xhost not found - X11 access may not work")
        
        # Additional Wayland setup if needed
        if wayland_display or xdg_session_type == 'wayland':
            print("ℹ️  Wayland detected - using XWayland compatibility")
    
    def get_profile_size(self, profile_name):
        """Get profile directory size in MB"""
        profile_dir = self.get_profile_dir(profile_name)
        if not os.path.exists(profile_dir):
            return 0
        
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(profile_dir):
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                if os.path.exists(filepath):
                    total_size += os.path.getsize(filepath)
        
        return round(total_size / (1024 * 1024), 2)  # Convert to MB
