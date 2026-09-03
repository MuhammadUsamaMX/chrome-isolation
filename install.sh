#!/usr/bin/env bash
# install.sh — installs Chrome Isolation Manager as a proper GNOME desktop app.
# Run as the target user (no sudo needed; installs to ~/.local/).
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
APP_NAME="chrome-isolation"
APP_DISPLAY_NAME="Chrome Isolation Manager"
INSTALL_DIR="$HOME/.local/share/chrome-isolation-manager"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
AUTOSTART_DIR="$HOME/.config/autostart"
OLD_SERVICE="chrome-manager.service"

# Source directory (where this script lives)
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Helpers ───────────────────────────────────────────────────────────────────
info()    { printf '\033[1;34m→ %s\033[0m\n' "$*"; }
success() { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn()    { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()     { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── Preflight ─────────────────────────────────────────────────────────────────
[[ -f "$SRC_DIR/package.json" ]] || die "Run this script from the project root."
command -v node   >/dev/null 2>&1 || die "node not found. Install Node.js first."
command -v npm    >/dev/null 2>&1 || die "npm not found."
command -v docker >/dev/null 2>&1 || die "Docker not found. Install Docker first."
[[ -d "$SRC_DIR/node_modules" ]]  || { info "Installing npm deps..."; npm --prefix "$SRC_DIR" install; }

# ── Stop old Flask service (S3: remove localhost attack surface) ───────────────
if systemctl --user is-active --quiet "$OLD_SERVICE" 2>/dev/null; then
    info "Stopping old chrome-manager.service..."
    systemctl --user disable --now "$OLD_SERVICE" 2>/dev/null || true
    success "Old service stopped and disabled."
elif systemctl is-active --quiet "$OLD_SERVICE" 2>/dev/null; then
    warn "System-wide $OLD_SERVICE found. Stopping it requires sudo — skipping."
fi

# ── Create directories ────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR"

# ── Copy application files ─────────────────────────────────────────────────────
info "Copying application to $INSTALL_DIR..."
rsync -a --delete \
    --exclude '.git' \
    --exclude 'node_modules/.cache' \
    --exclude '*.pyc' \
    --exclude '__pycache__' \
    "$SRC_DIR/electron/"    "$INSTALL_DIR/electron/"
rsync -a --delete \
    "$SRC_DIR/backend/"     "$INSTALL_DIR/backend/"
rsync -a --delete \
    "$SRC_DIR/scripts/"     "$INSTALL_DIR/scripts/"
rsync -a --delete \
    "$SRC_DIR/assets/"      "$INSTALL_DIR/assets/"
cp "$SRC_DIR/Dockerfile"    "$INSTALL_DIR/Dockerfile"
cp "$SRC_DIR/.dockerignore" "$INSTALL_DIR/.dockerignore"
cp "$SRC_DIR/package.json"  "$INSTALL_DIR/package.json"

# Copy node_modules (symlink or rsync depending on available space)
if [[ ! -d "$INSTALL_DIR/node_modules" ]]; then
    info "Copying node_modules..."
    rsync -a "$SRC_DIR/node_modules/" "$INSTALL_DIR/node_modules/"
fi

success "Files copied."

# ── Build Docker image ────────────────────────────────────────────────────────
info "Building isolated-chrome Docker image (this may take a few minutes)..."
docker build \
    --build-arg "USER_ID=$(id -u)" \
    --build-arg "GROUP_ID=$(id -g)" \
    -t isolated-chrome \
    "$INSTALL_DIR"
success "Docker image built."

# ── Launcher script ────────────────────────────────────────────────────────────
info "Creating launcher at $BIN_DIR/$APP_NAME..."
cat > "$BIN_DIR/$APP_NAME" <<LAUNCHER
#!/usr/bin/env bash
# Chrome Isolation Manager launcher
exec node "$INSTALL_DIR/node_modules/.bin/electron" "$INSTALL_DIR" "\$@"
LAUNCHER
chmod +x "$BIN_DIR/$APP_NAME"
success "Launcher created."

# ── Icon ──────────────────────────────────────────────────────────────────────
cp "$SRC_DIR/assets/icons/icon.png" "$ICON_DIR/$APP_NAME.png"
# backend/desktop_manager.py expects the icon at APP_DATA_DIR/icon.png
cp "$SRC_DIR/assets/icons/icon.png" "$INSTALL_DIR/icon.png"
success "Icon installed."

# ── .desktop entry ────────────────────────────────────────────────────────────
info "Creating .desktop entry..."
cat > "$DESKTOP_DIR/$APP_NAME.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=$APP_DISPLAY_NAME
GenericName=Browser Profile Manager
Comment=Manage isolated Chrome browser profiles in Docker containers
Exec=$BIN_DIR/$APP_NAME %u
Icon=$APP_NAME
Terminal=false
Categories=Network;WebBrowser;Utility;
Keywords=chrome;browser;privacy;isolation;docker;
StartupNotify=true
StartupWMClass=chrome-isolation
DESKTOP
chmod 644 "$DESKTOP_DIR/$APP_NAME.desktop"
success ".desktop entry created."

# ── Update icon / desktop caches ──────────────────────────────────────────────
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi
if command -v xdg-desktop-menu >/dev/null 2>&1; then
    xdg-desktop-menu forceupdate 2>/dev/null || true
fi
success "Desktop caches updated."

# ── Make sure ~/.local/bin is on PATH ─────────────────────────────────────────
if ! echo "$PATH" | grep -q "$BIN_DIR"; then
    warn "'$BIN_DIR' is not in your PATH."
    warn "Add this to your ~/.bashrc or ~/.profile:"
    warn "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
success "Installation complete!"
printf '  Run with: %s\n' "$APP_NAME"
printf '  Or launch from GNOME application grid.\n\n'
