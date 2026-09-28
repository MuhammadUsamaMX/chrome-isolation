const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const { spawn, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const readline = require('readline');

// Use Wayland natively when the compositor is available; fall back to X11 otherwise
app.commandLine.appendSwitch('ozone-platform-hint', 'auto');
// AMD iGPU on Wayland: keep GPU in main process to avoid zygote GPU crash (error_code=1002)
app.commandLine.appendSwitch('in-process-gpu');

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

/**
 * True if the current user is already a member of the `docker` group.
 * Membership is equivalent to root on the host, so this is a security-relevant
 * fact, not a cosmetic one.
 */
function isInDockerGroup() {
  const user = process.env.USER || process.env.LOGNAME;
  if (!user) return false;
  // Prefer getent (works for LDAP/SSSD users) and fall back to `id`.
  const getent = spawnSync('getent', ['group', 'docker'], { encoding: 'utf8' });
  if (getent.status === 0 && getent.stdout) {
    const members = (getent.stdout.split(':')[3] || '').split(',').filter(Boolean);
    if (members.includes(user)) return true;
  }
  const idOut = spawnSync('id', ['-nG', user], { encoding: 'utf8' });
  if (idOut.status === 0 && idOut.stdout) {
    return idOut.stdout.trim().split(/\s+/).includes('docker');
  }
  return false;
}

/**
 * The consent dialog shown immediately before any `usermod -aG docker`.
 * Returns true only on an explicit, informed confirmation.
 */
async function confirmDockerGroupConsent(user) {
  const { response } = await dialog.showMessageBox({
    type: 'warning',
    title: 'This is equivalent to giving up root',
    message: `Add "${user}" to the "docker" group?`,
    detail:
      'Read this before you agree — the docker group is NOT a limited permission.\n\n' +
      'The Docker daemon runs as root and has no per-command authorization, ' +
      'no audit log, and no policy of any kind. Anyone in the docker group can:\n' +
      '  • start a container that mounts the entire host filesystem as root\n' +
      '  • read and write any file on this machine, including SSH keys\n' +
      '  • load kernel modules, so it is a full root compromise, not a partial one\n' +
      '  • escape any container, including the isolated browser profiles this app creates\n\n' +
      'Any program running as you — including anything in this app — inherits that\n' +
      'power the moment you agree. Container isolation cannot protect the host from\n' +
      'the account that owns the Docker socket.\n\n' +
      `To undo this later:  sudo gpasswd -d ${user} docker   (then log out and back in)\n` +
      'To install Docker without the group (every command needs sudo): choose Cancel ' +
      'and follow https://docs.docker.com/get-docker/',
    buttons: ['Cancel', 'I understand, add me'],
    defaultId: 0,   // Enter / Escape = safe option
    cancelId: 0,    // Escape must never grant root
    noLink: true,
  });
  return response === 1;
}

/** Install Docker using the detected package manager, via pkexec/sudo. */
async function installDocker(pm) {
  const user = process.env.USER || process.env.LOGNAME;
  if (!user) throw new Error('Cannot determine the current user name.');

  // Consent gate — no `usermod -aG docker` may run without it.
  if (!await confirmDockerGroupConsent(user)) {
    const err = new Error('Docker group membership was declined.');
    err.userCancelled = true;
    throw err;
  }

  // Each entry is [program, args[]] — run sequentially; any failure throws.
  const steps = {
    pacman: [
      ['pkexec', ['pacman', '-S', '--needed', '--noconfirm', 'docker']],
      ['pkexec', ['systemctl', 'enable', '--now', 'docker']],
      ['pkexec', ['usermod', '-aG', 'docker', user]],
    ],
    apt: [
      // Use the convenience script from get.docker.com (handles repo setup)
      ['pkexec', ['bash', '-c',
        'curl -fsSL https://get.docker.com | sh && ' +
        `usermod -aG docker ${user} && ` +
        'systemctl enable --now docker'
      ]],
    ],
    'dnf-fedora': [
      ['pkexec', ['bash', '-c',
        'dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo && ' +
        'dnf install -y docker-ce docker-ce-cli containerd.io && ' +
        'systemctl enable --now docker && ' +
        `usermod -aG docker ${user}`
      ]],
    ],
    'dnf-rhel': [
      ['pkexec', ['bash', '-c',
        'dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo && ' +
        'dnf install -y docker-ce docker-ce-cli containerd.io && ' +
        'systemctl enable --now docker && ' +
        `usermod -aG docker ${user}`
      ]],
    ],
    zypper: [
      ['pkexec', ['bash', '-c',
        'zypper install -y docker && ' +
        'systemctl enable --now docker && ' +
        `usermod -aG docker ${user}`
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
 * Warn once per app launch if the user is already in the `docker` group.
 *
 * This is the state the app is normally in *after* a successful install, so
 * the install-time consent is not enough on its own: without this the user is
 * root-equivalent with no prompt at all on every subsequent launch. It is a
 * single informational dialog per launch (not per profile / per action), so it
 * is not noisy enough to justify dropping it in favour of docs only.
 */
async function warnIfDockerGroupMember() {
  let member = false;
  try { member = isInDockerGroup(); } catch (_) { /* detection failed — stay quiet */ }
  if (!member) return;
  const user = process.env.USER || process.env.LOGNAME || 'your account';
  await dialog.showMessageBox({
    type: 'warning',
    title: 'Security notice: docker group membership',
    message: `Your account (${user}) is a member of the "docker" group.`,
    detail:
      'The Docker daemon has no authorization layer, so this membership is\n' +
      'equivalent to full root access on this host. Any process running as you —\n' +
      'a browser add-on, a script, an npm postinstall, this app — can start a\n' +
      'root container that mounts and rewrites your whole filesystem.\n\n' +
      'The isolated browser profiles this app creates are NOT a boundary against\n' +
      'that: the account that owns the Docker socket is already the host.\n\n' +
      'If you did not intend this, remove it with:\n' +
      `  sudo gpasswd -d ${user} docker\n` +
      'then log out and back in (or reboot). Docker will still work for the app\n' +
      'via a root-owned socket only if you reconfigure it — otherwise run the app\n' +
      'as a user you are willing to treat as root.',
    buttons: ['I understand'],
    defaultId: 0,
    cancelId: 0,
    noLink: true,
  });
}

/**
 * Check that Docker is present and the daemon is reachable.
 * If Docker is missing, offer to install it.
 * Returns true if ready, false if the user declined or install failed.
 */
async function checkRequirements() {
  // Docker present and usable
  const dockerMissing = spawnSync('docker', ['--version'], { stdio: 'ignore' }).status !== 0;

  if (!dockerMissing) {
    // The daemon socket is the privilege boundary — say so on every launch.
    await warnIfDockerGroupMember();
    // Check daemon (non-fatal — group membership may need re-login)
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
        'Adding your user to the docker group is part of this install and it is\n' +
        'equivalent to full root access — you will be asked to confirm that\n' +
        'explicitly on the next screen.\n\n' +
        'After installation a re-login may be required for group membership.'
      : 'Could not detect your package manager.\n' +
        'Please install Docker manually: https://docs.docker.com/get-docker/',
    buttons: pm ? ['Install Docker', 'Cancel'] : ['OK'],
    defaultId: 0,
    cancelId: pm ? 1 : 0,
  });

  if (!pm || response !== 0) return false;

  // 4. Run install — installDocker() shows the docker-group consent dialog and
  //    throws err.userCancelled if the user declines.
  try {
    await installDocker(pm);
  } catch (err) {
    if (err.userCancelled) {
      await dialog.showMessageBox({
        type: 'info',
        title: 'Installation cancelled',
        message: 'Docker was not modified.',
        detail: 'The app needs the Docker daemon to start a profile container, so it\n' +
                'cannot continue. You can install Docker yourself at any time:\n' +
                'https://docs.docker.com/get-docker/',
        buttons: ['OK'],
      });
      return false;
    }
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
    type: 'warning',
    title: 'Docker installed — you now have root-equivalent access',
    message: 'Docker was installed successfully.',
    detail: `Your user (${process.env.USER || process.env.LOGNAME || 'this account'}) was added to the\n` +
            'docker group, which is equivalent to full root access on this host.\n' +
            'This was necessary to run the app, but it applies to every program\n' +
            'running as you, not just this app.\n\n' +
            'You may need to re-login for group membership to take effect.\n' +
            'To revoke it later: sudo gpasswd -d <user> docker',
    buttons: ['Continue'],
    defaultId: 0,
    cancelId: 0,
  });
  return true;
}


// ─────────────────────────────────────────────
// Bridge lifecycle (F8 / T6)
//
// Restart policy: exponential backoff, capped, with a hard limit on
// consecutive failures after which the app stops looping and tells both the
// renderer and the user that the backend is gone.
//
//   attempt 1 fails -> retry in   1s
//   attempt 2 fails -> retry in   2s
//   attempt 3 fails -> retry in   4s
//   attempt 4 fails -> retry in   8s
//   attempt 5 fails -> give up, show an error dialog
//
// "Consecutive" means the bridge did not survive BRIDGE_STABLE_MS. A bridge
// that has been up that long counts as a fresh start, so a crash an hour into
// a session retries immediately instead of inheriting an old backoff. This is
// what keeps the first-launch path (T6 regression guard) working: the very
// first startBridge() is not a restart and is never counted as a failure.
const BRIDGE_BACKOFF_BASE_MS = 1000;
const BRIDGE_BACKOFF_MAX_MS  = 30000;
const BRIDGE_MAX_FAILURES    = 5;
const BRIDGE_STABLE_MS       = 10000;

let bridge = null;
const pendingRequests = new Map(); // id → { resolve, reject, sent }
let pendingQueue = []; // { id, payload } queued while the bridge is not up yet
let reqCounter = 0;
let bridgeFailures = 0;   // consecutive restarts that never reached 'stable'
let bridgeStopped = null; // set to an Error once we have given up
let restartTimer = null;
let stableTimer = null;

/**
 * Reject only the requests that were actually written to a bridge that is now
 * dead. Queued-but-unsent requests are retried, not lost: a payload that never
 * reached a process cannot have been acted on, so there is nothing to lose.
 */
function failInFlightRequests(message) {
  for (const [id, p] of pendingRequests) {
    if (p.sent) p.reject(new Error(message));
    pendingRequests.delete(id);
  }
  // Anything left was queued for a bridge that is not coming back.
  for (const { id } of pendingQueue) {
    const p = pendingRequests.get(id);
    if (p) p.reject(new Error(message));
  }
  pendingQueue = [];
}

/** Give up restarting: stop the loop and surface the failure to the user. */
async function bridgeGiveUp(reason) {
  const message = `The Chrome Isolation backend (${bridgeCmd}) could not be started: ${reason}`;
  bridgeStopped = new Error(message);
  for (const [, p] of pendingRequests) p.reject(bridgeStopped);
  pendingRequests.clear();
  pendingQueue = [];
  if (stableTimer) { clearTimeout(stableTimer); stableTimer = null; }
  if (restartTimer) { clearTimeout(restartTimer); restartTimer = null; }
  bridge = null;
  console.error(`Bridge gave up after ${BRIDGE_MAX_FAILURES} consecutive failures: ${message}`);

  // Tell the renderer (if a window exists) so the UI can stop pretending the
  // backend is reachable; it is not enough to write to the console.
  try {
    mainWindow?.webContents.send('bridge:failed', { reason, message });
  } catch (_) { /* window torn down */ }

  const { response } = await dialog.showMessageBox({
    type: 'error',
    title: 'Backend unavailable',
    message: 'Chrome Isolation could not start its background service.',
    detail: `${message}\n\n` +
            `It failed to start ${BRIDGE_MAX_FAILURES} times in a row. ` +
            'Any action that was in flight has been cancelled — please retry it.\n\n' +
            'Common causes: the bundled bridge is missing or not executable, ' +
            'Python is not installed, or the app directory is read-only.',
    buttons: ['Retry', 'Quit'],
    defaultId: 0,
    cancelId: 1,
  });
  if (response === 0) {
    bridgeStopped = null;
    bridgeFailures = 0;
    startBridge();
  } else {
    app.quit();
  }
}

/** Common path for 'error' and 'exit': back off, or give up. */
function handleBridgeLoss(reason) {
  failInFlightRequests(`Bridge unavailable: ${reason}`);
  bridge = null;
  if (stableTimer) { clearTimeout(stableTimer); stableTimer = null; }

  bridgeFailures += 1;
  if (bridgeFailures >= BRIDGE_MAX_FAILURES) {
    bridgeGiveUp(reason);
    return;
  }
  const delay = Math.min(
    BRIDGE_BACKOFF_BASE_MS * Math.pow(2, bridgeFailures - 1),
    BRIDGE_BACKOFF_MAX_MS,
  );
  console.error(`Bridge lost (${reason}). Restart attempt ${bridgeFailures}/${BRIDGE_MAX_FAILURES - 1} in ${delay}ms…`);
  if (restartTimer) clearTimeout(restartTimer);
  restartTimer = setTimeout(() => { restartTimer = null; startBridge(); }, delay);
}

function startBridge() {
  if (bridgeStopped) return;
  bridge = spawn(bridgeCmd, bridgeArgs, {
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  const child = bridge;

  const rl = readline.createInterface({ input: child.stdout });
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

  child.stderr.on('data', (d) => {
    // Forward Python tracebacks to console in dev
    if (!app.isPackaged) process.stderr.write(d);
  });

  // A broken pipe must not become an unhandled 'error' event (which would take
  // the whole app down); the 'exit' handler below does the real recovery.
  child.stdin.on('error', (err) => {
    console.error('Bridge stdin error:', err.message);
  });

  // Both 'error' (spawn failure) and 'exit' land here. 'lost' + the identity
  // check make sure a single failure is counted once, and that a late event
  // from a superseded child cannot null out a healthy bridge.
  let lost = false;
  const onLoss = (reason) => {
    if (lost) return;
    lost = true;
    if (bridge !== child) return;
    handleBridgeLoss(reason);
  };

  child.on('error', (err) => onLoss(`failed to start (${err.message})`));
  child.on('exit', (code, signal) =>
    onLoss(signal ? `killed by ${signal}` : `exited with code ${code}`));

  // Surviving the stability window resets the backoff.
  if (bridgeFailures > 0) {
    stableTimer = setTimeout(() => {
      stableTimer = null;
      bridgeFailures = 0;
    }, BRIDGE_STABLE_MS);
  }

  // Flush requests queued while the bridge was starting.
  if (pendingQueue.length && child.stdin.writable) {
    const queued = pendingQueue;
    pendingQueue = [];
    for (const { id, payload } of queued) {
      const p = pendingRequests.get(id);
      if (p) p.sent = true;
      child.stdin.write(payload);
    }
  }
}

/** Send a request to the Python bridge and return a Promise. */
function callBackend(method, params = {}) {
  return new Promise((resolve, reject) => {
    // After the give-up path, reject immediately instead of queueing payloads
    // that will never be written.
    if (bridgeStopped) { reject(bridgeStopped); return; }
    const id = String(++reqCounter);
    const payload = JSON.stringify({ id, method, params }) + '\n';
    if (bridge && bridge.stdin.writable) {
      pendingRequests.set(id, { resolve, reject, sent: true });
      bridge.stdin.write(payload);
    } else {
      pendingRequests.set(id, { resolve, reject, sent: false });
      pendingQueue.push({ id, payload });
    }
  });
}

// ─────────────────────────────────────────────
// IPC handlers  (S3: no HTTP surface)
// ─────────────────────────────────────────────
function registerIpc() {
  ipcMain.handle('profiles:list', () => callBackend('list_profiles'));

  ipcMain.handle('profiles:create', (_, name, customPath, hostMount, proxy) =>
    callBackend('create_profile', {
      name,
      custom_path: customPath || '',
      host_mount: hostMount || '',
      proxy: proxy || '',
    })
  );

  ipcMain.handle('profiles:update', (_, name, hostMount, proxy, timezone) =>
    callBackend('update_profile', {
      name,
      host_mount: hostMount || '',
      proxy: proxy || '',
      timezone: timezone || '',
    })
  );

  // Native folder picker for the read-only host mount field
  ipcMain.handle('profiles:pickFolder', async () => {
    const { filePaths, canceled } = await dialog.showOpenDialog(mainWindow, {
      title: 'Select host folder (mounted read-only)',
      properties: ['openDirectory'],
    });
    if (canceled || !filePaths.length) return { canceled: true };
    return { canceled: false, path: filePaths[0] };
  });

  // ── Global proxy store ──────────────────────────────────────────────────────
  ipcMain.handle('proxies:list', () => callBackend('list_proxies'));
  ipcMain.handle('proxies:add', (_, name, url) =>
    callBackend('add_proxy', { name, url })
  );
  ipcMain.handle('proxies:delete', (_, name) =>
    callBackend('delete_proxy', { name })
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
  // Start the bridge before the window so the renderer's first IPC call can
  // never hit a null bridge (callBackend queues until the bridge is up).
  startBridge();

  // Always create the window first so the user sees the UI while we check.
  createWindow();
  registerIpc();

  // Check Docker (and auto-install if missing). If the user cancels, quit.
  const ready = await checkRequirements();
  if (!ready) {
    app.quit();
    return;
  }

  handleProfileArg();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('quit', () => {
  if (restartTimer) clearTimeout(restartTimer);
  if (stableTimer) clearTimeout(stableTimer);
  if (bridge) bridge.kill();
});
