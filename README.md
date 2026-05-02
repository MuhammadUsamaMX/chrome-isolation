<div align="center">

<img src="assets/icons/icon.png" width="96" height="96" alt="Chrome Isolation Icon"/>

# Chrome Isolation Manager

**Run fully isolated Chrome browser profiles in Docker containers — managed from a native GNOME desktop app.**

[![License: MIT](https://img.shields.io/badge/License-MIT-6366f1.svg)](LICENSE)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux%20%28GNOME%29-10b981.svg)](#requirements)
[![Electron](https://img.shields.io/badge/Electron-35-47848F.svg)](https://electronjs.org)
[![Docker](https://img.shields.io/badge/Docker-required-2496ED.svg)](https://docs.docker.com/get-docker/)

</div>

---

## What it does

Each browser profile lives in its own Docker container. Profiles are completely isolated from each other and from your host browser — no shared cookies, history, sessions, or fingerprints.

The app is a **native Electron desktop application** for GNOME/Wayland Linux. No web server, no Flask, no localhost port — the UI talks directly to the backend via a secure stdio IPC bridge.

Chrome that opens via this manager reads your host's fonts, icons, GTK theme, and dark/light mode preference (read-only) so it feels native — while remaining fully isolated at the data and identity level.

---

## Features

- **Full profile isolation** — Docker container per profile, separate data directories
- **Wayland-native** — runs on Wayland via `--ozone-platform=wayland`, no X11 needed
- **Stealth mode** — stable per-profile hardware fingerprint (CPU cores, RAM, screen resolution, timezone, user-agent)
- **Dark / Light mode** — follows host GNOME colour scheme; toggle in titlebar
- **Host preferences inherited** — fonts, icons, GTK theme, cursor theme all mounted read-only
- **No localhost attack surface** — all IPC is over stdio JSON, no HTTP server
- **Profile import / export** — safe ZIP/TAR archive with path-traversal protection
- **GNOME integration** — `.desktop` entry, app icon, `chrome-isolation --profile <name>` CLI
- **Hardened security** — no `--privileged`, no `SYS_ADMIN`, no `--ipc=host`; `/.dockerenv` masked

---

## Architecture

```
GNOME App (Electron)
  └── electron/main.js          — BrowserWindow, IPC handlers, window controls
  └── electron/preload.js       — contextBridge (narrow API only)
  └── electron/renderer/        — UI (HTML + CSS + JS, no Node access)
        └── index.html
        └── style.css           — dark/light design system
        └── app.js

  │  stdio JSON (newline-delimited)
  ▼

Python Backend (bridge.py)
  └── bridge.py                 — stdin/stdout JSON-RPC dispatcher
  └── profile_service.py        — high-level profile operations
  └── docker_manager.py         — Docker container lifecycle
  └── registry.py               — profile path registry (JSON)
  └── archive.py                — safe import/export
  └── validator.py              — input validation (path traversal prevention)
  └── desktop_manager.py        — .desktop entry generation
  └── config.py                 — paths & constants

  │  docker run / docker stop
  ▼

Docker Container (isolated-chrome image)
  └── Alpine Linux + Chromium
  └── scripts/docker/stealth-launch.sh   — entrypoint
  └── scripts/docker/hardware-spoof.sh   — stable fingerprint
  └── scripts/docker/user-agent-spoof.sh — stable UA
```

---

## Requirements

| Dependency | Version | Notes |
|---|---|---|
| Linux (GNOME + Wayland) | Any modern | Arch, Fedora, Ubuntu 22.04+ |
| Node.js | 18+ | [nodejs.org](https://nodejs.org) |
| npm | 8+ | Bundled with Node |
| Python | 3.10+ | Usually pre-installed |
| Docker | 24+ | [docs.docker.com](https://docs.docker.com/get-docker/) |
| `python-docker` | latest | `pip install docker` |

---

## Installation

```bash
# 1. Clone
git clone https://github.com/MuhammadUsamaMX/chrome-isolation.git
cd chrome-isolation

# 2. Install npm deps
npm install

# 3. Install Python dep
pip install docker

# 4. Run the installer (copies app, builds Docker image, creates .desktop entry)
bash install.sh
```

The installer:
- Copies the app to `~/.local/share/chrome-isolation-manager/`
- Builds the `isolated-chrome` Docker image
- Creates `~/.local/bin/chrome-isolation` launcher
- Installs `.desktop` entry for the GNOME app grid
- Stops and disables the old `chrome-manager.service` if present

After install, launch from the GNOME app grid or:

```bash
chrome-isolation
```

---

## Usage

### Create a profile
Click **New Profile** in the sidebar. Give it a name (letters, numbers, dash, underscore). Optionally set a custom storage path.

### Launch Chrome
Click **Launch** on any profile card. A Docker container starts and Chromium opens on your Wayland display. The container uses your host fonts, icons, and GTK theme (read-only).

### Stop Chrome
Click **Stop** on the card.

### Export / Import profiles
Use **Export** to save a profile as a ZIP archive. Use **Import** in the sidebar to restore from an archive. Archives are validated for path traversal and symlink attacks before extraction.

### CLI (per-profile desktop entry)
After creating a profile, a `.desktop` entry is added to GNOME so you can launch it directly:

```bash
chrome-isolation --profile Work
```

### Light / Dark mode
Click the moon/sun icon in the titlebar. Preference is saved across sessions.

---

## Security

| Issue | Fix |
|---|---|
| **S1 — Path traversal in profile names** | `validator.py` — regex allowlist + `os.path.realpath` prefix check |
| **S2 — Archive extraction traversal** | `archive.py` — member-by-member validation, temp-dir staging, `tarfile filter="data"` |
| **S3 — Localhost HTTP attack surface** | Eliminated — no Flask, all IPC via stdio JSON bridge |
| **S4 — Orphaned custom-path profiles** | `registry.py` — JSON registry tracks all profiles with actual paths |
| **S5 — Container over-privilege** | No `--privileged`, no `SYS_ADMIN`; `/.dockerenv` masked; realistic `shm_size`, `ulimits` |
| **S6 — Flask dev server in production** | Eliminated — Electron replaces Flask entirely |

---

## Project structure

```
chrome-isolation/
├── electron/                  # Electron frontend
│   ├── main.js                # Main process
│   ├── preload.js             # contextBridge API
│   └── renderer/              # UI
│       ├── index.html
│       ├── style.css
│       └── app.js
├── backend/                   # Python backend
│   ├── bridge.py              # stdio JSON-RPC
│   ├── profile_service.py
│   ├── docker_manager.py
│   ├── registry.py
│   ├── archive.py
│   ├── validator.py
│   ├── desktop_manager.py
│   └── config.py
├── scripts/
│   └── docker/                # Container entrypoint scripts
│       ├── stealth-launch.sh
│       ├── hardware-spoof.sh
│       └── user-agent-spoof.sh
├── assets/icons/icon.png
├── Dockerfile
├── install.sh
├── package.json
└── requirements.txt
```

---

## Development

```bash
# Run without installing
npm start

# Watch bridge stderr (Python tracebacks) in terminal
npm start 2>&1
```

The renderer has zero Node.js access — all calls go through `window.api` (contextBridge) → IPC → main process → Python bridge → Docker.

---

## Uninstall

```bash
rm -rf ~/.local/share/chrome-isolation-manager
rm ~/.local/bin/chrome-isolation
rm ~/.local/share/applications/chrome-isolation.desktop
rm ~/.local/share/icons/hicolor/256x256/apps/chrome-isolation.png
docker rmi isolated-chrome
```

---

## License

MIT — see [LICENSE](LICENSE).
