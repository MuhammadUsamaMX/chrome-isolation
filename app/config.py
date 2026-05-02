"""
Chrome Isolation Manager - Configuration
"""
import os

# Base paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHROME_PROFILES_DIR = os.path.expanduser("~/Chrome")
DESKTOP_ENTRIES_DIR = os.path.expanduser("~/.local/share/applications")

# Docker configuration
DOCKER_IMAGE_NAME = "isolated-chrome"
CONTAINER_PREFIX = "chrome-"

# Web server configuration
HOST = "127.0.0.1"
PORT = 5000
DEBUG = False

# Ensure directories exist
os.makedirs(CHROME_PROFILES_DIR, exist_ok=True)
os.makedirs(DESKTOP_ENTRIES_DIR, exist_ok=True)
