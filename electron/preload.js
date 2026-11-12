/**
 * Electron preload — contextBridge exposes a narrow, typed API to the renderer.
 * The renderer has zero access to Node, IPC, or the filesystem directly.
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
  profiles: {
    list:    ()              => ipcRenderer.invoke('profiles:list'),
    create:  (name, path, hostMount, proxy) => ipcRenderer.invoke('profiles:create', name, path, hostMount, proxy),
    update:  (name, hostMount, proxy, timezone) => ipcRenderer.invoke('profiles:update', name, hostMount, proxy, timezone),
    pickFolder: ()           => ipcRenderer.invoke('profiles:pickFolder'),
    delete:  (name)         => ipcRenderer.invoke('profiles:delete', name),
    start:   (name)         => ipcRenderer.invoke('profiles:start',  name),
    stop:    (name)         => ipcRenderer.invoke('profiles:stop',   name),
    status:  (name)         => ipcRenderer.invoke('profiles:status', name),
    export:  (name)         => ipcRenderer.invoke('profiles:export', name),
    import:  ()             => ipcRenderer.invoke('profiles:import'),
  },

  // Global proxy store
  proxies: {
    list:   ()              => ipcRenderer.invoke('proxies:list'),
    add:    (name, url)     => ipcRenderer.invoke('proxies:add', name, url),
    delete: (name)          => ipcRenderer.invoke('proxies:delete', name),
  },

  // Custom window controls (frameless window)
  win: {
    minimize:        ()   => ipcRenderer.send('window:minimize'),
    maximize:        ()   => ipcRenderer.send('window:maximize'),
    close:           ()   => ipcRenderer.send('window:close'),
    isMaximized:     ()   => ipcRenderer.invoke('window:isMaximized'),
    onMaximizeChange: (cb) => ipcRenderer.on('window:maximized', (_e, state) => cb(state)),
  },

  // Chromium update — DETECT AND INFORM, REBUILD ONLY ON CONSENT.
  //
  // `check()` runs a version check and resolves immediately. It BUILDS
  // NOTHING: it records `update_available` and stops. `apply(version)` is the
  // explicit "Apply now" the user invokes, and `version` must be the
  // `consent_version` from the last `status()` — consent is tied to one
  // specific version, not a blanket opt-in to updates.
  //
  // `status()` returns the full contract documented on the `image:updateStatus`
  // handler in main.js. Note `update_available` is tri-state: true, false, or
  // null for "could not determine" — do not render null as "up to date".
  image: {
    check:   ()              => ipcRenderer.invoke('image:checkUpdate'),
    apply:   (version)       => ipcRenderer.invoke('image:applyUpdate', version),
    rebuild: ()              => ipcRenderer.invoke('image:rebuildImage'),
    status:  ()              => ipcRenderer.invoke('image:updateStatus'),
  },

  // Backend bridge lifecycle (F8 / T6). Fired when the Python bridge could not
  // be started and the app has given up restarting it — any request that was
  // in flight has been cancelled and must be retried by the user.
  bridge: {
    onFailed: (cb) => ipcRenderer.on('bridge:failed', (_e, info) => cb(info)),
  },
});
