// With nodeIntegration enabled, preload just sets up IPC convenience functions
// on window.cc for the renderer to use.

const { ipcRenderer } = require('electron');

window.cc = {
  // Config
  getConfig: () => ipcRenderer.invoke('get-config'),

  // Session listing (client mode — returns { local, remote })
  listSessions: () => ipcRenderer.invoke('list-sessions'),

  // Session management (client mode)
  renameSession: (name, newName, remote) => ipcRenderer.invoke('rename-session', name, newName, remote),
  killSession: (name, remote) => ipcRenderer.invoke('kill-session', name, remote),

  // PTY operations
  createPty: (slot, sessionName, remote) => ipcRenderer.invoke('pty:create', slot, sessionName, remote),
  writePty: (slot, data) => ipcRenderer.send('pty:write', slot, data),
  tmuxSend: (slot, ...keys) => ipcRenderer.send('pty:tmux-send', slot, ...keys),
  resizePty: (slot, cols, rows) => ipcRenderer.send('pty:resize', slot, cols, rows),
  scrollTmux: (slot, direction, lines) => ipcRenderer.send('pty:scroll', slot, direction, lines || 1),
  exitCopyMode: (slot) => ipcRenderer.send('pty:exit-copy-mode', slot),
  killPty: (slot) => ipcRenderer.invoke('pty:kill', slot),
  newSession: (agent, location) => ipcRenderer.invoke('pty:new-session', agent, location),

  // PTY events (remove old listeners first to prevent duplicates on reload)
  onPtyData: (callback) => {
    ipcRenderer.removeAllListeners('pty:data');
    ipcRenderer.on('pty:data', (_, slot, data) => callback(slot, data));
  },
  onPtyExit: (callback) => {
    ipcRenderer.removeAllListeners('pty:exit');
    ipcRenderer.on('pty:exit', (_, slot, exitCode) => callback(slot, exitCode));
  },

  // Meeting window
  openMeeting: () => ipcRenderer.send('meeting:open'),
  closeMeeting: () => ipcRenderer.send('meeting:close'),

  // Context menu
  showContextMenu: (sessionName, displayName) => {
    ipcRenderer.send('context-menu', sessionName, displayName);
  },

  // Actions from main process (remove old listeners first to prevent duplicates on reload)
  onAssignSlot: (callback) => {
    ipcRenderer.removeAllListeners('assign-slot');
    ipcRenderer.on('assign-slot', (_, slot, sessionName, remote) => callback(slot, sessionName, remote));
  },
  onAction: (callback) => {
    ipcRenderer.removeAllListeners('action');
    ipcRenderer.on('action', (_, action, sessionName, extra) => callback(action, sessionName, extra));
  },
};
