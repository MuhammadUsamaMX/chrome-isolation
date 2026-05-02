const { app, BrowserWindow, ipcMain, dialog, Menu, Tray, nativeImage } = require('electron');
const { spawn, execFileSync, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const readline = require('readline');

// Use Wayland natively when the compositor is available; fall back to X11 otherwise
app.commandLine.appendSwitch('ozone-platform-hint', 'auto');

// ─────────────────────────────────────────────
// Config
// ─────────────────────────────────────────────
// When packaged as AppImage/deb, use the bundled PyInstaller bridge binary
// (no Python required on the host). In dev, fall back to python3.
let bridgeCmd, bridgeArgs;
if (app.isPackaged) {
  // electron-builder puts extraResources at <appDir>/resources/
  const bundled = path.join(process.resourcesPath, 'bridge');
  bridgeCmd  = bundled;
  bridgeArgs = [];
} else {
  bridgeCmd  = process.env.PYTHON_BIN || 'python3';
  bridgeArgs = [path.join(__dirname, '..', 'backend', 'bridge.py')];
}

// ─────────────────────────────────────────────
// Requirement checker + auto-installer
// ─────────────────────────────────────────────

/** Detect which package manager / distro we're running on. */
function detectDistro() {
  try {
    const osRelease = fs.readFileSync('/etc/os-release', 'utf8');
    const id = (osRelease.match(/^ID=(.+)$/m) || [])[1]?.replace(/"/g, '') || '';
    const like = (osRelease.match(/^ID_LIKE=(.+)$/m) || [])[1]?.replace(/"/g, '') || '';
    const combined = `${id} ${like}`.toLowerCase();
    if (combined.includes('arch') || combined.includes('manjaro') || combined.includes('endeavour')) return 'pacman';
    if (combined.includes('fedora')) return 'dnf-fedora';
    if (combined.includes('rhel') || combined.includes('centos') || combined.includes('rocky') || combined.includes('alma')) return 'dnf-rhel';
    if (combined.includes('opensuse') || combined.includes('suse')) return 'zypper';
    if (combined.includes('debian') || combined.includes('ubuntu') || combined.includes('mint') || combined.includes('pop')) return 'apt';
  } catch (_) { /* /etc/os-release unreadable */ }

  // Fallback: detect by binary
  for (const [bin, pm] of [['pacman','pacman'],['apt-get','apt'],['dnf','dnf-fedora'],['yum','dnf-rhel'],['zypper','zypper']]) {
    if (spawnSync('which', [bin], { stdio: 'ignore' }).status === 0) return pm;
  }
  return null;
}

/** Install Docker using the detected package manager, via pkexec/sudo. */
function installDocker(pm) {
  // Each entry is [program, args[]] — run sequentially; any failure throws.
  const steps = {
    pacman: [
      ['pkexec', ['pacman', '-S', '--needed', '--noconfirm', 'docker']],
      ['pkexec', ['systemctl', 'enable', '--now', 'docker']],
      ['pkexec', ['usermod', '-aG', 'docker', process.env.USER || process.env.LOGNAME]],
    ],
    apt: [
      // Use the convenience script from get.docker.com (handles repo setup)
      ['pkexec', ['bash', '-c',
        'curl -fsSL https://get.docker.com | sh && ' +
        `usermod -aG docker ${process.env.USER || process.env.LOGNAME} && ` +
        'systemctl enable --now docker'
      ]],
    ],
    'dnf-fedora': [
      ['pkexec', ['bash', '-c',
        'dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo && ' +
        'dnf install -y docker-ce docker-ce-cli containerd.io && ' +
        'systemctl enable --now docker && ' +
        `usermod -aG docker ${process.env.USER || process.env.LOGNAME}`
      ]],
    ],
    'dnf-rhel': [
      ['pkexec', ['bash', '-c',
        'dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo && ' +
        'dnf install -y docker-ce docker-ce-cli containerd.io && ' +
        'systemctl enable --now docker && ' +
        `usermod -aG docker ${process.env.USER || process.env.LOGNAME}`
      ]],
    ],
    zypper: [
      ['pkexec', ['bash', '-c',
        'zypper install -y docker && ' +
        'systemctl enable --now docker && ' +
        `usermod -aG docker ${process.env.USER || process.env.LOGNAME}`
      ]],
    ],
  };

  const cmds = steps[pm];
  if (!cmds) throw new Error(`No install recipe for package manager: ${pm}`);

  for (const [prog, args] of cmds) {
    const result = spawnSync(prog, args, { stdio: 'inherit' });
    if (result.status !== 0) throw new Error(`Command failed: ${prog} ${args.join(' ')}`);
  }
}

/**
 * Check that Docker is present and the daemon is reachable.
 * If Docker is missing, offer to install it.
 * Returns true if ready, false if the user declined or install failed.
 */
async function checkRequirements() {
  // 1. Check docker CLI
  const dockerMissing = spawnSync('docker', ['--version'], { stdio: 'ignore' }).status !== 0;

  if (!dockerMissing) {
    // 2. Check daemon (non-fatal — group membership may need re-login)
    const daemonOk = spawnSync('docker', ['info'], { stdio: 'ignore' }).status === 0;
    if (!daemonOk) {
      await dialog.showMessageBox({
        type: 'warning',
        title: 'Docker daemon not running',
        message: 'Docker is installed but the daemon is not reachable.\n\n' +
                 'Possible causes:\n' +
                 '  • Your user is not yet in the docker group (re-login required)\n' +
                 '  • The Docker service is stopped (run: sudo systemctl start docker)\n\n' +
                 'The app will continue but containers may fail to start.',
        buttons: ['OK'],
      });
    }
    return true; // docker binary present — proceed
  }

  // 3. Docker missing — ask to install
  const pm = detectDistro();
  const pmLabel = pm
    ? { pacman: 'pacman (Arch)', apt: 'apt (Debian/Ubuntu)', 'dnf-fedora': 'dnf (Fedora)',
        'dnf-rhel': 'dnf (RHEL/Rocky/Alma)', zypper: 'zypper (openSUSE)' }[pm]
    : 'unknown';

  const { response } = await dialog.showMessageBox({
    type: 'question',
    title: 'Docker not found',
    message: 'Chrome Isolation requires Docker to run isolated browser profiles.',
    detail: pm
      ? `Docker will be installed via ${pmLabel}.\n` +
        'A system authentication prompt will appear.\n\n' +
        'After installation a re-login may be required for group membership.'
      : 'Could not detect your package manager.\n' +
        'Please install Docker manually: https://docs.docker.com/get-docker/',
    buttons: pm ? ['Install Docker', 'Cancel'] : ['OK'],
    defaultId: 0,
    cancelId: pm ? 1 : 0,
  });

  if (!pm || response !== 0) return false;

  // 4. Run install
  try {
    installDocker(pm);
  } catch (err) {
    await dialog.showMessageBox({
      type: 'error',
      title: 'Docker installation failed',
      message: 'Could not install Docker automatically.',
      detail: `${err.message}\n\nPlease install Docker manually:\nhttps://docs.docker.com/get-docker/`,
      buttons: ['OK'],
    });
    return false;
  }

  await dialog.showMessageBox({
    type: 'info',
    title: 'Docker installed',
    message: 'Docker was installed successfully.',
    detail: 'You may need to re-login for group membership to take effect.\n' +
            'The app will now continue.',
    buttons: ['Continue'],
  });
  return true;
}


let bridge = null;
const pendingRequests = new Map(); // id → { resolve, reject }
let reqCounter = 0;

function startBridge() {
  bridge = spawn(bridgeCmd, bridgeArgs, {
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  const rl = readline.createInterface({ input: bridge.stdout });
  rl.on('line', (line) => {
    try {
      const resp = JSON.parse(line);
      const pending = pendingRequests.get(resp.id);
      if (!pending) return;
      pendingRequests.delete(resp.id);
      if (resp.error) pending.reject(new Error(resp.error));
      else pending.resolve(resp.result);
    } catch (e) { /* ignore non-JSON lines */ }
  });

  bridge.stderr.on('data', (d) => {
    // Forward Python tracebacks to console in dev
    if (!app.isPackaged) process.stderr.write(d);
  });

  bridge.on('exit', (code) => {
    console.error(`Bridge exited (code=${code}). Restarting in 1s…`);
    setTimeout(startBridge, 1000);
  });
}

/** Send a request to the Python bridge and return a Promise. */
function callBackend(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = String(++reqCounter);
    pendingRequests.set(id, { resolve, reject });
    const payload = JSON.stringify({ id, method, params }) + '\n';
    bridge.stdin.write(payload);
  });
}

// ─────────────────────────────────────────────
// IPC handlers  (S3: no HTTP surface)
// ─────────────────────────────────────────────
function registerIpc() {
  ipcMain.handle('profiles:list', () => callBackend('list_profiles'));

  ipcMain.handle('profiles:create', (_, name, customPath) =>
    callBackend('create_profile', { name, custom_path: customPath || '' })
  );

  ipcMain.handle('profiles:delete', (_, name) =>
    callBackend('delete_profile', { name })
  );

  ipcMain.handle('profiles:start', (_, name) =>
    callBackend('start_profile', { name })
  );

  ipcMain.handle('profiles:stop', (_, name) =>
    callBackend('stop_profile', { name })
  );

  ipcMain.handle('profiles:status', (_, name) =>
    callBackend('profile_status', { name })
  );

  // Export — open native save dialog, then call backend
  ipcMain.handle('profiles:export', async (_, name) => {
    const { filePath, canceled } = await dialog.showSaveDialog(mainWindow, {
      title: `Export profile "${name}"`,
      defaultPath: `${name}.zip`,
      filters: [{ name: 'ZIP Archive', extensions: ['zip'] }],
    });
    if (canceled || !filePath) return { canceled: true };
    return callBackend('export_profile', { name, dest_path: filePath });
  });

  // Import — open native open dialog, then call backend
  ipcMain.handle('profiles:import', async () => {
    const { filePaths, canceled } = await dialog.showOpenDialog(mainWindow, {
      title: 'Import profile archive',
      filters: [
        { name: 'Archives', extensions: ['zip', 'tar', 'tgz', 'tar.gz'] },
      ],
      properties: ['openFile'],
    });
    if (canceled || !filePaths.length) return { canceled: true };
    return callBackend('import_profile', { archive_path: filePaths[0] });
  });

  // ── Custom window controls (frameless window) ──────────────────────────────
  ipcMain.on('window:minimize',  () => mainWindow?.minimize());
  ipcMain.on('window:maximize',  () => {
    if (!mainWindow) return;
    mainWindow.isMaximized() ? mainWindow.unmaximize() : mainWindow.maximize();
  });
  ipcMain.on('window:close',     () => mainWindow?.close());
  ipcMain.handle('window:isMaximized', () => mainWindow?.isMaximized() ?? false);
}

// ─────────────────────────────────────────────
// Window
// ─────────────────────────────────────────────
let mainWindow = null;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1080,
    height: 700,
    minWidth: 760,
    minHeight: 520,
    title: 'Chrome Isolation',
    icon: path.join(__dirname, '..', 'assets', 'icons', 'icon.png'),
    transparent: true,        // allow CSS border-radius to show through
    frame: false,             // custom titlebar
    hasShadow: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  mainWindow.setMenuBarVisibility(false);

  // Notify renderer when maximize state changes
  mainWindow.on('maximize',   () => mainWindow.webContents.send('window:maximized',   true));
  mainWindow.on('unmaximize', () => mainWindow.webContents.send('window:maximized', false));

  mainWindow.on('closed', () => { mainWindow = null; });
}

// ─────────────────────────────────────────────
// Handle --profile <name> launch argument
// ─────────────────────────────────────────────
function handleProfileArg() {
  const idx = process.argv.indexOf('--profile');
  if (idx !== -1 && process.argv[idx + 1]) {
    const name = process.argv[idx + 1];
    callBackend('start_profile', { name }).catch(console.error);
  }
}

// ─────────────────────────────────────────────
// App lifecycle
// ─────────────────────────────────────────────
app.whenReady().then(async () => {
  // Always create the window first so the user sees the UI while we check.
  createWindow();
  registerIpc();

  // Check Docker (and auto-install if missing). If the user cancels, quit.
  const ready = await checkRequirements();
  if (!ready) {
    app.quit();
    return;
  }

  startBridge();
  handleProfileArg();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('quit', () => {
  if (bridge) bridge.kill();
});
