#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# build.sh — Build Chrome Isolation Manager as a standalone AppImage + .deb
#
# What it does:
#   1. Checks / installs build deps (PyInstaller, electron-builder, fuse2)
#   2. Bundles the Python bridge into a single self-contained binary via PyInstaller
#   3. Builds the Electron app as:
#        dist/Chrome-Isolation-<version>.AppImage   (runs on any Linux)
#        dist/Chrome-Isolation-<version>.deb        (Debian/Ubuntu)
#
# Requirements (on the build machine):
#   - Node.js ≥ 18, npm
#   - Python ≥ 3.10, pip
#   - fuse2 / libfuse2 (for AppImage)
#
# Usage:
#   bash build.sh [--appimage-only] [--deb-only] [--skip-bridge]
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SRC"

# ── Colours ───────────────────────────────────────────────────────────────────
info()    { printf '\033[1;34m→ %s\033[0m\n' "$*"; }
success() { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn()    { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()     { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── Args ──────────────────────────────────────────────────────────────────────
BUILD_APPIMAGE=1
BUILD_DEB=1
BUILD_RPM=1
SKIP_BRIDGE=0

for arg in "$@"; do
  case "$arg" in
    --appimage-only) BUILD_DEB=0; BUILD_RPM=0 ;;
    --deb-only)      BUILD_APPIMAGE=0; BUILD_RPM=0 ;;
    --rpm-only)      BUILD_APPIMAGE=0; BUILD_DEB=0 ;;
    --skip-bridge)   SKIP_BRIDGE=1 ;;
  esac
done

# ── Step 1: Verify Node.js / npm ──────────────────────────────────────────────
info "Checking Node.js..."
command -v node >/dev/null 2>&1 || die "Node.js not found. Run: bash setup.sh"
command -v npm  >/dev/null 2>&1 || die "npm not found. Run: bash setup.sh"
NODE_VER=$(node --version)
info "Node.js $NODE_VER"

# ── Step 2: Install npm deps (including electron-builder) ─────────────────────
info "Installing npm dependencies..."
npm install --include=dev
success "npm deps installed."

# ── Step 3: Bundle Python bridge with PyInstaller ─────────────────────────────
if [[ "$SKIP_BRIDGE" -eq 0 ]]; then
  info "Checking Python / pip..."
  command -v python3 >/dev/null 2>&1 || die "python3 not found. Run: bash setup.sh"

  # Install PyInstaller + docker client if not present
  if ! python3 -c "import PyInstaller" 2>/dev/null; then
    info "Installing PyInstaller..."
    pip install --quiet pyinstaller
  fi
  if ! python3 -c "import docker" 2>/dev/null; then
    info "Installing python-docker..."
    pip install --quiet docker
  fi

  info "Bundling Python bridge (PyInstaller)..."
  python3 -m PyInstaller \
    --clean \
    --distpath dist/bridge-bin \
    --workpath /tmp/ci-pyinstaller \
    bridge.spec

  [[ -f dist/bridge-bin/bridge ]] || die "PyInstaller failed — dist/bridge-bin/bridge not found."
  chmod +x dist/bridge-bin/bridge
  success "Bridge binary: dist/bridge-bin/bridge ($(du -sh dist/bridge-bin/bridge | cut -f1))"
else
  warn "--skip-bridge: skipping PyInstaller step"
  if [[ ! -f dist/bridge-bin/bridge ]]; then
    die "dist/bridge-bin/bridge is missing and --skip-bridge was set. Cannot continue."
  fi
fi

# ── Step 4: Check fuse2 (required to mount AppImage at build time) ────────────
if [[ "$BUILD_APPIMAGE" -eq 1 ]]; then
  if ! ldconfig -p 2>/dev/null | grep -q libfuse || ! command -v fusermount >/dev/null 2>&1; then
    warn "fuse2/libfuse2 may not be installed."
    warn "If the AppImage build fails, install it:"
    warn "  Arch:   sudo pacman -S fuse2"
    warn "  Debian: sudo apt-get install fuse libfuse2"
    warn "  RHEL:   sudo dnf install fuse fuse-libs"
    warn "  Or set: APPIMAGE_EXTRACT_AND_RUN=1 (already set below)"
  fi
  # Suppress FUSE requirement for building in environments without it
  export APPIMAGE_EXTRACT_AND_RUN=1
fi

# ── Step 5: Build AppImage / .deb ─────────────────────────────────────────────
BUILD_TARGETS=""
[[ "$BUILD_APPIMAGE" -eq 1 ]] && BUILD_TARGETS="$BUILD_TARGETS --linux AppImage"
[[ "$BUILD_DEB"      -eq 1 ]] && BUILD_TARGETS="$BUILD_TARGETS --linux deb"
[[ "$BUILD_RPM"      -eq 1 ]] && BUILD_TARGETS="$BUILD_TARGETS --linux rpm"

info "Building Electron packages: $BUILD_TARGETS"
npx electron-builder $BUILD_TARGETS

# ── Step 6: Report ────────────────────────────────────────────────────────────
echo ""
success "Build complete! Output:"
find dist/ -maxdepth 1 \( -name "*.AppImage" -o -name "*.deb" -o -name "*.rpm" \) -print | while read -r f; do
  printf '  %-55s %s\n' "$f" "$(du -sh "$f" | cut -f1)"
done
echo ""
info "To run the AppImage:"
echo "  chmod +x dist/*.AppImage && ./dist/*.AppImage"
info "To install the .deb:"
echo "  sudo dpkg -i dist/*.deb && sudo apt-get install -f"
