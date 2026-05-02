#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# setup.sh — One-shot setup for Chrome Isolation Manager
#
# Detects your Linux distro and auto-installs ALL required dependencies:
#   • Node.js + npm
#   • Python 3 + pip
#   • Docker (+ adds current user to docker group)
#   • python-docker module
#   • fuse2 / libfuse2 (for AppImage)
#   • Chromium / Wayland helper libs in the container (via Dockerfile)
#
# Supports:
#   Arch Linux / Manjaro / EndeavourOS  (pacman)
#   Debian / Ubuntu / Linux Mint / Pop!_OS  (apt)
#   Fedora 36+  (dnf)
#   RHEL / AlmaLinux / Rocky Linux 8-9  (dnf / yum)
#   openSUSE Leap / Tumbleweed  (zypper)
#
# After deps are installed the script:
#   1. Builds the Docker image
#   2. Runs install.sh to wire up the GNOME desktop entry
#   3. Optionally builds the standalone AppImage (pass --build-appimage)
#
# Usage (as the target user, NOT root):
#   bash setup.sh               — install deps + install app from source
#   bash setup.sh --build       — also build the AppImage binary
#   bash setup.sh --appimage ./Chrome-Isolation.AppImage
#                               — install from a pre-built AppImage
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colours ───────────────────────────────────────────────────────────────────
info()    { printf '\033[1;34m→ %s\033[0m\n' "$*"; }
success() { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn()    { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()     { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
step()    { printf '\n\033[1;37m━━ %s\033[0m\n' "$*"; }

# ── Guard: don't run as root ──────────────────────────────────────────────────
[[ "$EUID" -ne 0 ]] || die "Run as your regular user, not root. (sudo will be called automatically when needed)"

# ── Args ──────────────────────────────────────────────────────────────────────
DO_BUILD=0
APPIMAGE_PATH=""

for arg in "$@"; do
  case "$arg" in
    --build)      DO_BUILD=1 ;;
    --appimage)   shift; APPIMAGE_PATH="${1:-}" ;;
    --appimage=*) APPIMAGE_PATH="${arg#--appimage=}" ;;
  esac
done

# ──────────────────────────────────────────────────────────────────────────────
# 1. Detect package manager / distro
# ──────────────────────────────────────────────────────────────────────────────
step "Detecting Linux distribution"

DISTRO_ID=""
PKG_MGR=""

if [[ -f /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
  DISTRO_ID="${ID:-unknown}"
  DISTRO_LIKE="${ID_LIKE:-}"
fi

detect_pm() {
  if   command -v pacman  &>/dev/null; then PKG_MGR="pacman"
  elif command -v apt-get &>/dev/null; then PKG_MGR="apt"
  elif command -v dnf     &>/dev/null; then PKG_MGR="dnf"
  elif command -v yum     &>/dev/null; then PKG_MGR="yum"
  elif command -v zypper  &>/dev/null; then PKG_MGR="zypper"
  else die "Unsupported package manager. Install Node.js, Python3, Docker manually then re-run."
  fi
}
detect_pm
info "Distro: ${DISTRO_ID} — Package manager: ${PKG_MGR}"

# ──────────────────────────────────────────────────────────────────────────────
# 2. Helper: install packages via detected PM
# ──────────────────────────────────────────────────────────────────────────────
pkg_install() {
  case "$PKG_MGR" in
    pacman) sudo pacman -S --needed --noconfirm "$@" ;;
    apt)    sudo apt-get install -y "$@" ;;
    dnf)    sudo dnf install -y "$@" ;;
    yum)    sudo yum install -y "$@" ;;
    zypper) sudo zypper install -y "$@" ;;
  esac
}

# ──────────────────────────────────────────────────────────────────────────────
# 3. Node.js
# ──────────────────────────────────────────────────────────────────────────────
step "Node.js"

if command -v node &>/dev/null && [[ "$(node --version | sed 's/v//' | cut -d. -f1)" -ge 18 ]]; then
  success "Node.js $(node --version) already installed."
else
  info "Installing Node.js..."
  case "$PKG_MGR" in
    pacman) pkg_install nodejs npm ;;
    apt)
      # Use NodeSource for a recent LTS if the distro ships an old one
      NODE_MAJOR=20
      if ! apt-cache show "nodejs" 2>/dev/null | grep -q "Version: ${NODE_MAJOR}"; then
        info "Adding NodeSource repository for Node.js ${NODE_MAJOR}..."
        curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | sudo -E bash -
      fi
      pkg_install nodejs
      ;;
    dnf|yum)
      # Fedora/RHEL: use Node.js module stream
      sudo dnf module enable nodejs:20 -y 2>/dev/null || true
      pkg_install nodejs npm
      ;;
    zypper) pkg_install nodejs20 npm20 ;;
  esac
  command -v node &>/dev/null || die "Node.js installation failed."
  success "Node.js $(node --version) installed."
fi

# ──────────────────────────────────────────────────────────────────────────────
# 4. Python 3 + pip
# ──────────────────────────────────────────────────────────────────────────────
step "Python 3"

if command -v python3 &>/dev/null; then
  PY_VER=$(python3 --version | awk '{print $2}')
  success "Python ${PY_VER} already installed."
else
  info "Installing Python 3..."
  case "$PKG_MGR" in
    pacman) pkg_install python python-pip ;;
    apt)    pkg_install python3 python3-pip python3-venv ;;
    dnf|yum) pkg_install python3 python3-pip ;;
    zypper) pkg_install python3 python3-pip ;;
  esac
  success "Python $(python3 --version) installed."
fi

# Ensure pip is available
if ! python3 -m pip --version &>/dev/null; then
  case "$PKG_MGR" in
    pacman) pkg_install python-pip ;;
    apt)    pkg_install python3-pip ;;
    dnf|yum) pkg_install python3-pip ;;
    zypper) pkg_install python3-pip ;;
  esac
fi

# python-docker
if ! python3 -c "import docker" 2>/dev/null; then
  info "Installing python-docker..."
  # Try system package first (avoids PEP 668 externally-managed-environment error)
  case "$PKG_MGR" in
    pacman) pkg_install python-docker ;;
    apt)    pkg_install python3-docker || pip install --break-system-packages docker ;;
    dnf|yum) pkg_install python3-docker || pip install docker ;;
    zypper) pip install docker ;;
  esac
fi
python3 -c "import docker" || die "python-docker installation failed."
success "python-docker ready."

# ──────────────────────────────────────────────────────────────────────────────
# 5. Docker
# ──────────────────────────────────────────────────────────────────────────────
step "Docker"

if command -v docker &>/dev/null; then
  success "Docker $(docker --version | awk '{print $3}' | tr -d ,) already installed."
else
  info "Installing Docker..."
  case "$PKG_MGR" in
    pacman)
      pkg_install docker
      sudo systemctl enable --now docker
      ;;
    apt)
      # Official Docker repo
      sudo apt-get install -y ca-certificates curl gnupg lsb-release
      sudo install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/$(
        . /etc/os-release && echo "$ID"
      )/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
      echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") \
        $(lsb_release -cs) stable" | \
        sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
      sudo apt-get update -q
      pkg_install docker-ce docker-ce-cli containerd.io
      sudo systemctl enable --now docker
      ;;
    dnf)
      sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo 2>/dev/null || \
        sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
      pkg_install docker-ce docker-ce-cli containerd.io
      sudo systemctl enable --now docker
      ;;
    yum)
      sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
      pkg_install docker-ce docker-ce-cli containerd.io
      sudo systemctl enable --now docker
      ;;
    zypper)
      sudo zypper addrepo https://download.docker.com/linux/sles/docker-ce.repo || true
      pkg_install docker-ce docker-ce-cli containerd.io
      sudo systemctl enable --now docker
      ;;
  esac
  command -v docker &>/dev/null || die "Docker installation failed."
  success "Docker installed."
fi

# Add user to docker group if not already a member
if ! groups "$USER" | grep -qw docker; then
  info "Adding $USER to the docker group..."
  sudo usermod -aG docker "$USER"
  warn "Docker group membership requires re-login to take effect."
  warn "For this session, prefixing docker commands with newgrp or re-login."
fi

# ──────────────────────────────────────────────────────────────────────────────
# 6. fuse2 (for AppImage mounting / building)
# ──────────────────────────────────────────────────────────────────────────────
step "FUSE (AppImage support)"

_fuse_ok() { ldconfig -p 2>/dev/null | grep -q libfuse2 || ldconfig -p 2>/dev/null | grep -q libfuse.so.2; }

if _fuse_ok; then
  success "libfuse2 already present."
else
  info "Installing fuse2/libfuse2..."
  case "$PKG_MGR" in
    pacman) pkg_install fuse2 ;;
    apt)    pkg_install fuse libfuse2 ;;
    dnf|yum) pkg_install fuse fuse-libs ;;
    zypper) pkg_install fuse libfuse2 ;;
  esac
fi

# ──────────────────────────────────────────────────────────────────────────────
# 7. npm install (get electron + electron-builder)
# ──────────────────────────────────────────────────────────────────────────────
step "npm dependencies"
cd "$SRC"
info "Running npm install..."
npm install --include=dev
success "npm deps installed."

# ──────────────────────────────────────────────────────────────────────────────
# 8a. Install from pre-built AppImage (--appimage flag)
# ──────────────────────────────────────────────────────────────────────────────
if [[ -n "$APPIMAGE_PATH" ]]; then
  step "Installing from AppImage: $APPIMAGE_PATH"
  [[ -f "$APPIMAGE_PATH" ]] || die "AppImage not found: $APPIMAGE_PATH"

  APPIMAGE_DEST="$HOME/.local/share/chrome-isolation-manager/chrome-isolation.AppImage"
  mkdir -p "$(dirname "$APPIMAGE_DEST")"
  cp "$APPIMAGE_PATH" "$APPIMAGE_DEST"
  chmod +x "$APPIMAGE_DEST"

  # Build Docker image (always needed)
  info "Building isolated-chrome Docker image..."
  docker build \
    --build-arg "USER_ID=$(id -u)" \
    --build-arg "GROUP_ID=$(id -g)" \
    -t isolated-chrome "$SRC"
  success "Docker image built."

  # Desktop entry pointing to AppImage
  BIN_DIR="$HOME/.local/bin"
  DESKTOP_DIR="$HOME/.local/share/applications"
  ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
  mkdir -p "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR"

  cat > "$BIN_DIR/chrome-isolation" <<LAUNCHER
#!/usr/bin/env bash
exec "$APPIMAGE_DEST" "\$@"
LAUNCHER
  chmod +x "$BIN_DIR/chrome-isolation"

  cp "$SRC/assets/icons/icon.png" "$ICON_DIR/chrome-isolation.png"

  cat > "$DESKTOP_DIR/chrome-isolation.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Chrome Isolation Manager
GenericName=Browser Profile Manager
Comment=Manage isolated Chrome browser profiles in Docker containers
Exec=$BIN_DIR/chrome-isolation %u
Icon=chrome-isolation
Terminal=false
Categories=Network;WebBrowser;Utility;
StartupNotify=true
StartupWMClass=chrome-isolation
DESKTOP

  gtk-update-icon-cache -q -t -f "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
  update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
  xdg-desktop-menu forceupdate 2>/dev/null || true
  success "AppImage installed. Launch: chrome-isolation"
  exit 0
fi

# ──────────────────────────────────────────────────────────────────────────────
# 8b. Optionally build the AppImage
# ──────────────────────────────────────────────────────────────────────────────
if [[ "$DO_BUILD" -eq 1 ]]; then
  step "Building standalone AppImage"
  bash "$SRC/build.sh"
  APPIMAGE_FILE=$(find "$SRC/dist" -maxdepth 1 -name "*.AppImage" | head -1)
  if [[ -n "$APPIMAGE_FILE" ]]; then
    success "AppImage built: $APPIMAGE_FILE"
    echo ""
    info "To share with others on any Linux distro:"
    echo "  $APPIMAGE_FILE"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────────
# 9. Install from source (default path)
# ──────────────────────────────────────────────────────────────────────────────
step "Installing Chrome Isolation Manager from source"
bash "$SRC/install.sh"

# ──────────────────────────────────────────────────────────────────────────────
# Done
# ──────────────────────────────────────────────────────────────────────────────
echo ""
success "All done!"
echo ""
echo "  Launch:  chrome-isolation"
echo "  Or open Chrome Isolation Manager from your app grid."
echo ""
if groups "$USER" | grep -qw docker; then
  : # already in group
else
  warn "Re-login required for Docker access without sudo."
  warn "Or run: newgrp docker"
fi
