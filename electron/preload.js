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

  // Chromium auto-update. `check()` starts a rebuild and resolves immediately
  // (the build itself runs on a backend background thread), and `status()`
  // is a cheap read the UI can poll to show progress.
  image: {
    check:  ()              => ipcRenderer.invoke('image:checkUpdate'),
    status: ()              => ipcRenderer.invoke('image:updateStatus'),
  },

  // Backend bridge lifecycle (F8 / T6). Fired when the Python bridge could not
  // be started and the app has given up restarting it — any request that was
  // in flight has been cancelled and must be retried by the user.
  bridge: {
    onFailed: (cb) => ipcRenderer.on('bridge:failed', (_e, info) => cb(info)),
  },
});
