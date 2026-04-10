// With nodeIntegration enabled, preload just sets up IPC convenience functions
// on window.cc for the renderer to use.

const { ipcRenderer } = require('electron');

window.cc = {
  // PTY operations
  createPty: (slot, sessionName, cols, rows) => ipcRenderer.invoke('pty:create', slot, sessionName, cols, rows),
  writePty: (slot, data) => ipcRenderer.send('pty:write', slot, data),
  tmuxSend: (slot, ...keys) => ipcRenderer.send('pty:tmux-send', slot, ...keys),
  resizePty: (slot, cols, rows) => ipcRenderer.send('pty:resize', slot, cols, rows),
  scrollTmux: (paneId, direction, lines) => ipcRenderer.send('pty:scroll', paneId, direction, lines || 1),
  exitCopyMode: (slot) => ipcRenderer.send('pty:exit-copy-mode', slot),
  killPty: (slot) => ipcRenderer.invoke('pty:kill', slot),
  newSession: (agent) => ipcRenderer.invoke('pty:new-session', agent),
  checkSession: (sessionName) => ipcRenderer.invoke('pty:check-session', sessionName),

  // PTY events (remove old listeners first to prevent duplicates on reload)
  onPtyData: (callback) => {
    ipcRenderer.removeAllListeners('pty:data');
    ipcRenderer.on('pty:data', (_, slot, data) => callback(slot, data));
  },
  onPtyExit: (callback) => {
    ipcRenderer.removeAllListeners('pty:exit');
    ipcRenderer.on('pty:exit', (_, slot, exitCode) => callback(slot, exitCode));
  },

  // Mic server
  startMicServer: () => ipcRenderer.invoke('mic:start-server'),

  // Meeting window
  openMeeting: () => ipcRenderer.send('meeting:open'),
  closeMeeting: () => ipcRenderer.send('meeting:close'),

  // Activity detection + summaries
  detectActivity: () => ipcRenderer.invoke('pty:detect-activity'),
  captureAllPanes: () => ipcRenderer.invoke('pty:capture-all-panes'),

  // Image paste
  saveImage: (base64Data) => ipcRenderer.invoke('pty:save-image', base64Data),

  // Config
  getConfig: () => ipcRenderer.invoke('get-config'),

  // Dashboards
  getPipelineStats: () => ipcRenderer.invoke('dashboard:pipeline-stats'),
  // 0DTE: takes a trader_id (e.g. "bart", "sai") so multiple operators
  // can be viewed independently. Reads from DynamoDB directly.
  get0dteStats: (traderId) => ipcRenderer.invoke('dashboard:0dte-stats', traderId),
  list0dteTraders: () => ipcRenderer.invoke('dashboard:0dte-list-traders'),

  // Context menu
  showContextMenu: (sessionName, displayName) => {
    ipcRenderer.send('context-menu', sessionName, displayName);
  },

  // Actions from main process (remove old listeners first to prevent duplicates on reload)
  onAssignSlot: (callback) => {
    ipcRenderer.removeAllListeners('assign-slot');
    ipcRenderer.on('assign-slot', (_, slot, sessionName) => callback(slot, sessionName));
  },
  onAction: (callback) => {
    ipcRenderer.removeAllListeners('action');
    ipcRenderer.on('action', (_, action, sessionName, extra) => callback(action, sessionName, extra));
  },
};
