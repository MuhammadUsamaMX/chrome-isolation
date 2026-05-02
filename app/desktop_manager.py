"""
Desktop Manager - Handle .desktop file creation and management
"""
import os
from config import DESKTOP_ENTRIES_DIR

class DesktopManager:
    def __init__(self, launcher_script_path):
        self.launcher_script_path = os.path.abspath(launcher_script_path)
    
    def get_desktop_file_path(self, profile_name):
        """Get desktop entry file path"""
        return os.path.join(DESKTOP_ENTRIES_DIR, f"chrome-{profile_name}.desktop")
    
    def create_desktop_entry(self, profile_name):
        """Create a .desktop file for the profile"""
        desktop_file = self.get_desktop_file_path(profile_name)
        
        # Ensure icon exists
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        icon_path = os.path.join(base_dir, 'chrome-icon.png')
        
        if not os.path.exists(icon_path):
            try:
                import urllib.request
                print("⬇️  Downloading Chrome icon...")
                url = "https://upload.wikimedia.org/wikipedia/commons/e/e1/Google_Chrome_icon_%28February_2022%29.svg"
                # Use a PNG for better compatibility if SVG fails in some DEs, but SVG is generally fine. 
                # Let's actually use a reliable PNG source or the SVG. 
                # Wikimedia SVG is fine for modern Linux.
                urllib.request.urlretrieve(url, icon_path)
            except Exception as e:
                print(f"⚠️  Failed to download icon: {e}")
                icon_path = "google-chrome" # Fallback
        
        content = f"""[Desktop Entry]
Version=1.0
Type=Application
Name=Chrome ({profile_name})
Comment=Isolated Chrome Profile: {profile_name}
Exec={self.launcher_script_path} {profile_name}
Icon={icon_path}
Terminal=false
Categories=Network;WebBrowser;
StartupWMClass=chrome-{profile_name}
X-AppImage-Version={profile_name}
"""
        
        with open(desktop_file, 'w') as f:
            f.write(content)
        
        # Make executable
        os.chmod(desktop_file, 0o755)
        
        # Update desktop database
        os.system(f'update-desktop-database {DESKTOP_ENTRIES_DIR} > /dev/null 2>&1')
        
        return {"status": "created", "path": desktop_file}
    
    def remove_desktop_entry(self, profile_name):
        """Remove desktop entry file"""
        desktop_file = self.get_desktop_file_path(profile_name)
        
        if os.path.exists(desktop_file):
            os.remove(desktop_file)
            os.system(f'update-desktop-database {DESKTOP_ENTRIES_DIR} > /dev/null 2>&1')
            return {"status": "removed"}
        
        return {"status": "not_found"}
    
    def desktop_entry_exists(self, profile_name):
        """Check if desktop entry exists"""
        return os.path.exists(self.get_desktop_file_path(profile_name))
