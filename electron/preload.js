/**
 * Electron preload — contextBridge exposes a narrow, typed API to the renderer.
 * The renderer has zero access to Node, IPC, or the filesystem directly.
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
  profiles: {
    list:    ()              => ipcRenderer.invoke('profiles:list'),
    create:  (name, path, hostMount, proxy) => ipcRenderer.invoke('profiles:create', name, path, hostMount, proxy),
    update:  (name, hostMount, proxy) => ipcRenderer.invoke('profiles:update', name, hostMount, proxy),
    pickFolder: ()           => ipcRenderer.invoke('profiles:pickFolder'),
    delete:  (name)         => ipcRenderer.invoke('profiles:delete', name),
    start:   (name)         => ipcRenderer.invoke('profiles:start',  name),
    stop:    (name)         => ipcRenderer.invoke('profiles:stop',   name),
    status:  (name)         => ipcRenderer.invoke('profiles:status', name),
    export:  (name)         => ipcRenderer.invoke('profiles:export', name),
    import:  ()             => ipcRenderer.invoke('profiles:import'),
  },

  // Custom window controls (frameless window)
  win: {
    minimize:        ()   => ipcRenderer.send('window:minimize'),
    maximize:        ()   => ipcRenderer.send('window:maximize'),
    close:           ()   => ipcRenderer.send('window:close'),
    isMaximized:     ()   => ipcRenderer.invoke('window:isMaximized'),
    onMaximizeChange: (cb) => ipcRenderer.on('window:maximized', (_e, state) => cb(state)),
  },
});
