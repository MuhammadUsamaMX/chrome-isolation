<div align="center">

<img src="assets/icons/icon.png" width="96" height="96" alt="Chrome Isolation Icon"/>

# Chrome Isolation Manager

**Run fully isolated Chrome browser profiles in Docker containers — managed from a native GNOME desktop app.**

[![License: Non-Commercial](https://img.shields.io/badge/License-Non--Commercial%20(All%20Rights%20Reserved)-dc2626.svg)](LICENSE)
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
- **Stealth mode** — each profile presents as its own machine: stable per-profile CPU core count, browser language, GPU backend (real GPU or software), timezone, user-agent, hostname, and window size
- **Host folder sharing is opt-in and narrow** — no host directory is mounted into a container unless *you* explicitly pick one. A folder you choose is mounted **read-only** at `/home/chrome/host`. Your home directory is **never** mounted implicitly; leaving **Host Folder** empty mounts an empty, per-profile `0700` directory instead (see [S7](#security) and finding F1)
- **Per-profile proxy** — route a profile's traffic through a SOCKS5/HTTP proxy for a different egress IP; manage a shared proxy list in the app (sidebar → **Proxies**), pick one per profile from a dropdown, or enter a custom URL; loopback addresses are rewritten to the host automatically, WebRTC is forced through the proxy, and authenticated proxies (`socks5://user:pass@host:port`) are handled by an in-container auth wrapper
- **Dark / Light mode** — follows host GNOME colour scheme; toggle in titlebar
- **Host preferences inherited** — a fixed, curated set of paths (system fonts/icons/themes and your `~/.local/share/fonts`, `~/.icons`, `~/.themes`, `~/.config/gtk-*`) is mounted read-only, so Chrome looks native without granting access to the rest of your home directory
- **No localhost attack surface** — all IPC is over stdio JSON, no HTTP server
- **Profile import / export** — safe ZIP/TAR archive with path-traversal protection
- **GNOME integration** — `.desktop` entry, app icon, `chrome-isolation --profile <name>` CLI
- **Hardened security** — no `--privileged`, no `--ipc=host`; containers run with `cap_drop=ALL`, `no-new-privileges`, a `pids_limit` and a read-only root filesystem; `.desktop` entry refresh no longer goes through a shell

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
  └── host_share.py             — resolves + validates what host dir (if any) is shared
  └── registry.py               — profile path registry (JSON)
  └── proxy_store.py            — shared proxy list
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

> The container base image and the bundled Chromium version are being refreshed
> (finding F3). Pin your expectations to the tag you actually run — check with
> `docker images isolated-chrome` and `docker exec chrome-<name> chromium --version`
> rather than assuming. **Provisional.**

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

> **Docker access is root-equivalent.** On first launch the app can offer to install
> Docker and add your user to the `docker` group. Membership in the `docker` group is
> equivalent to full root on this machine: anyone able to talk to the Docker socket can
> start a container with a bind mount of `/` and take over the machine. The app now
> warns about this before running the `usermod -aG docker` step — see finding
> [F7](#remediation-audit--2026-09-29) below. If you do not accept that trade-off,
> run the containers under a dedicated non-privileged user or a rootless Docker daemon.
>
> This warning text is **provisional** — the in-app prompt is still being finalised.

---

## Installation

### Option A — Standalone binary (recommended, any Linux)

Download the latest **`.AppImage`** from the [Releases page](https://github.com/MuhammadUsamaMX/chrome-isolation/releases). It runs on **any Linux distribution** — Node.js, Python, and npm are bundled inside; no compilation or package managers needed. The only system requirement is **Docker** (the app runs one container per profile — first launch offers to install it if missing).

```bash
# Download, make executable, run
wget https://github.com/MuhammadUsamaMX/chrome-isolation/releases/latest/download/chrome-isolation-1.1.0-linux-x64.AppImage
chmod +x chrome-isolation-1.1.0-linux-x64.AppImage
./chrome-isolation-1.1.0-linux-x64.AppImage
```

First launch: the app checks for Docker, and builds the `isolated-chrome` container image automatically (one-time, a few minutes).

### Option B — `.deb` / `.rpm` (Debian/Ubuntu, Fedora/RHEL)

```bash
# Debian / Ubuntu
sudo dpkg -i chrome-isolation-1.1.0-linux-x64.deb && sudo apt-get install -f

# Fedora / RHEL
sudo dnf install chrome-isolation-1.1.0-linux-x64.rpm
```

### Option C — From source (developers)

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
Click **New Profile** in the sidebar. Give it a name (letters, numbers, dash, underscore). Optionally set a custom storage path, a **Host Folder** — a directory from your machine mounted read-only into the container at `/home/chrome/host` — and a **Proxy** chosen from the saved list or entered as a custom URL such as `socks5://127.0.0.1:1080` or `socks5://user:pass@proxy.example.com:1080`.

**Leaving Host Folder empty mounts nothing from your home directory.** The container gets an empty per-profile directory (`~/.local/share/chrome-isolation-manager/host-shares/<profile>/`, mode `0700`) at `/home/chrome/host` instead. To share something, pick it with **Browse…**; to undo, use the **✕** button. The picker is available from the **Host Folder** field itself.

> Previously an empty **Host Folder** silently mounted your entire home directory
> read-only, which exposed SSH keys, `.gnupg`, keyring files and your other browser
> profiles to every container. That default has been removed (finding F1).
> This section is **provisional** until the host-share default is finalised and the
> in-app field copy is updated to match.

### Manage proxies
Click **Proxies** in the sidebar to add, view, and delete named proxies shared by all profiles (e.g. `tunnel-alan1` → `socks5://127.0.0.1:10080`). Profiles reference them by name, so updating a proxy in the list updates every profile using it.

### Edit a profile
Click **Edit** on any profile card to change the host folder, proxy, and machine timezone after creation. As with creation, an empty host folder means *no host directory is shared*. Changes apply the next time the profile is launched — the host mount is baked in at `docker run` time, so editing a running profile's host folder has no effect until it is stopped and relaunched.

### Launch Chrome
Click **Launch** on any profile card. A Docker container starts and Chromium opens on your Wayland display. The container uses your host fonts, icons, and GTK theme (read-only), and presents the profile's own machine fingerprint: CPU model and core count, DMI/board strings, total RAM, browser language, GPU backend (real GPU or software), timezone, user-agent, hostname, and window size. The spoofed values are mutually consistent — the advertised core count always matches the spoofed CPU string — and they are stable for the life of the profile.

> The fingerprint is generated inside the container at launch and held for the
> container's lifetime. Fields still being finalised: the exact RAM value and the
> DMI string pool. **Provisional.**

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

### Design-level mitigations

| Issue | Fix |
|---|---|
| **S1 — Path traversal in profile names** | `validator.py` — regex allowlist + `os.path.realpath` prefix check |
| **S2 — Archive extraction traversal** | `archive.py` — member-by-member validation, temp-dir staging, `tarfile filter="data"` |
| **S3 — Localhost HTTP attack surface** | Eliminated — no Flask, all IPC via stdio JSON bridge |
| **S4 — Orphaned custom-path profiles** | `registry.py` — JSON registry tracks all profiles with actual paths |
| **S5 — Container over-privilege** | No `--privileged`, no `--ipc=host`, no `SYS_ADMIN`. Containers run with `cap_drop=ALL`, `no-new-privileges:true`, a `pids_limit` and a read-only root filesystem, with only explicit `tmpfs` paths writable. The entrypoint still uses `--no-sandbox` because Docker's default seccomp profile blocks unprivileged user namespaces — the container remains the security boundary, so the flags above are what limit the blast radius of a renderer RCE |
| **S6 — Flask dev server in production** | Eliminated — Electron replaces Flask entirely |
| **S7 — Host files exposed to container** | **Nothing is mounted unless the user asks for it.** An explicit host folder is mounted read-only at `/home/chrome/host`; an empty setting mounts an empty per-profile `0700` directory, never `$HOME`. Read-only still permits exfiltration, so the control is "don't mount it" rather than "mount it read-only". Only a fixed list of curated appearance paths (system + user fonts/icons/themes/GTK config) is inherited |
| **S8 — Per-profile proxy** | Optional SOCKS5/HTTP proxy per profile; loopback rewritten to `host.docker.internal` (host-gateway alias), WebRTC forced through the proxy to prevent IP leaks, and `user:pass@` credentials handled by an in-container auth wrapper (Chromium ignores credentials in proxy URLs) |
| **S9 — Shell injection in desktop-entry refresh** | `desktop_manager.py` — `update-desktop-database` is invoked via `subprocess.run([...])` with `shell=False`; profile names are additionally constrained by the S1 validator |

### Remediation audit — 2026-09-29

An independent pass over the running installation on 2026-09-29 raised the findings
below. This table is the audit trail: what was wrong, what fixes it, and whether the
fix is in the tree yet.

| # | Finding | Remediation | Status |
|---|---|---|---|
| **F1** | `host_mount` fell back to `$HOME`, mounting the whole home directory read-only at `/home/chrome/host` for every profile — SSH keys, `.gnupg`, keyring files, other browser profiles all readable from a compromised page | Empty `host_mount` now resolves to an empty per-profile `0700` share dir; explicit mounts still honoured and validated | **Provisional** — landed in `config.py`/`docker_manager.py`; in-app copy and tests still pending |
| **F2** | DMI, `/proc/cpuinfo` and `/proc/meminfo` leaked the real machine (`MS-7E27`, `AMD Ryzen 7 9700X`, 32 GB), and the spoofed core count contradicted the CPU string | Spoof DMI/cpuinfo/meminfo inside the container and derive `hardwareConcurrency` from the spoofed model | **Provisional** — in progress |
| **F3** | Image was built 2026-09-13 on EOL `alpine:3.19` shipping Chromium 124 (Apr 2024) — a long, unsupported CVE tail | Rebuild on a supported Alpine base with a current Chromium | **Provisional** — rebuild in progress under a separate tag; `isolated-chrome:latest` is still the old image |
| **F4** | `--no-sandbox` with a weak container profile: no `cap_drop`, no `no-new-privileges`, no `pids_limit`, writable rootfs | See S5 — `cap_drop=ALL`, `no-new-privileges`, `pids_limit`, read-only rootfs | **Provisional** — in progress |
| **F5** | `/.dockerenv` was masked with a `0644` user-owned empty file instead of Docker's real `0755 root:root` — a fingerprint tell in itself | Match real Docker's ownership/mode, or drop the mask | **Provisional** — decision pending |
| **F6** | `chrome-Fiverr-Read` and `chrome-Fiverr-Demo` were left running on untagged image `b226dbacf344`, two builds behind `isolated-chrome:latest` | Remove the stale containers so every profile runs the current image | **Provisional** — container removal pending; see the cleanup runbook below |
| **F7** | The app installs Docker and runs `usermod -aG docker <user>` without explaining that Docker socket access is root-equivalent | Warn before the privilege-granting step | **Provisional** — in progress |
| **F8** | The Python bridge restarted 1 s after any exit, forever, dropping in-flight request payloads and re-running `_ensure_image()` each time | Exponential backoff with a cap, plus explicit handling of lost in-flight requests | **Provisional** — in progress |
| **F9** | Security fixes sat uncommitted in a dirty working tree, and the two stale containers still ran the pre-fix image layer | Commit the fix, clear the stale containers, rebuild | **Open** — see the cleanup runbook below |

> **Note on the audit tables.** Everything tagged *Provisional* describes the
> intended final behaviour. Parts of it are still landing; do not treat the
> *Status* column as a claim that the fix is already live in your installation.
> Verify against `docker inspect` before relying on it.

### Cleaning up stale containers and images

Two profile containers may be left over from an older image. Removing them does
**not** touch profile data: every profile's data lives in `~/Chrome/<profile>/` on the
host and is bind-mounted into the container, so the container's own writable layer
holds only disposable state (and is discarded on removal anyway).

Verify before removing anything:

```bash
# Which image does each container actually use?
docker ps -a --filter 'name=chrome-' --format '{{.Names}}\t{{.Image}}\t{{.Status}}'

# How many containers reference each image? "CONTAINERS" must be 0 before you prune.
docker system df -v | sed -n '/^Images/,/^Containers/p'

# Confirm the profile data is a host bind mount, not a Docker volume
docker inspect chrome-<name> --format '{{range .Mounts}}{{.Type}} {{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
```

You want to see `bind /home/x/Chrome/<name> -> /home/chrome/.config/chromium` and
`Local Volumes: 0`. Only then proceed — and re-run the second command *after* the
rebuild in F3 finishes, so you are not pruning the image a live container needs.


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
│   ├── host_share.py
│   ├── registry.py
│   ├── proxy_store.py
│   ├── archive.py
│   ├── validator.py
│   ├── desktop_manager.py
│   └── config.py
├── tests/                     # Backend tests
├── scripts/
│   ├── build/after-pack.js    # electron-builder post-pack hook
│   └── docker/                # Container entrypoint scripts
│       ├── stealth-launch.sh
│       ├── hardware-spoof.sh
│       └── user-agent-spoof.sh
├── assets/icons/icon.png
├── bridge.spec                # PyInstaller spec for the Python bridge
├── Dockerfile
├── build.sh                   # AppImage / .deb / .rpm build script
├── install.sh                 # Desktop app installer
├── uninstall.sh               # Full uninstaller (all install methods)
├── setup.sh                   # Dev environment setup
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

### Any install method — use the script

```bash
bash <(curl -s https://raw.githubusercontent.com/MuhammadUsamaMX/chrome-isolation/main/uninstall.sh)
# or, from a clone:
bash uninstall.sh
```

The script confirms first, then removes: profile containers + the `isolated-chrome` Docker image, all profile data (`~/Chrome`), the app data dir (registry, proxies, signatures), launcher, `.desktop` entry, icon, and the `.deb`/`.rpm` package if installed.

### Manual uninstall

```bash
rm -rf ~/.local/share/chrome-isolation-manager
rm ~/.local/bin/chrome-isolation
rm ~/.local/share/applications/chrome-isolation.desktop
rm ~/.local/share/icons/hicolor/256x256/apps/chrome-isolation.png
rm -rf ~/Chrome                      # profile data — back it up first!
docker ps -aq --filter 'name=chrome-' | xargs -r docker rm   # containers hold image refs
docker rmi isolated-chrome
# .deb:  sudo dpkg -r chrome-isolation
# .rpm:  sudo dnf remove chrome-isolation
```

---

## License

Non-Commercial — All rights reserved by MuhammadUsamaMX. See [LICENSE](LICENSE) for full terms.
