// With nodeIntegration enabled, preload just sets up IPC convenience functions
// on window.cc for the renderer to use.

const { ipcRenderer } = require('electron');
const os = require('os');
const path = require('path');

// Synchronous host metadata — available at script-eval time so dashboards
// can self-register without waiting on an async getConfig() round-trip.
// `isClient` matches the logic in main.js: CONFIG.remote present → CLIENT.
let _isClient = false;
let _hasRemote = false;
let _hasDashboardHub = false;
try {
  const _cfg = require(path.join(__dirname, 'pentacle.config.js'));
  _isClient = !!_cfg.remote;
  _hasRemote = !!_cfg.remote;
  _hasDashboardHub = !!(_cfg.dashboardHub && _cfg.dashboardHub.url);
} catch { /* keep default false */ }

window.HOST = {
  hostname: os.hostname(),
  platform: process.platform,
  isClient: _isClient,
  hasRemote: _hasRemote,
  hasDashboardHub: _hasDashboardHub,
};

window.cc = {
  // PTY operations — hostId threads through so each slot knows which tmux
  // server its session lives on. Defaults to 'local' for backcompat.
  createPty: (slot, sessionName, hostId, cols, rows) => ipcRenderer.invoke('pty:create', slot, sessionName, hostId || 'local', cols, rows),
  writePty: (slot, data) => ipcRenderer.send('pty:write', slot, data),
  tmuxSend: (slot, ...keys) => ipcRenderer.send('pty:tmux-send', slot, ...keys),
  resizePty: (slot, cols, rows) => ipcRenderer.send('pty:resize', slot, cols, rows),
  // Scroll now takes slot (the main process looks up host+paneId from the slot).
  // This keeps pane-id routing race-safe — a stale paneId can't land on a
  // replacement attach because we check slot identity on every callback.
  scrollTmux: (slot, direction, lines) => ipcRenderer.send('pty:scroll', slot, direction, lines || 1),
  exitCopyMode: (slot) => ipcRenderer.send('pty:exit-copy-mode', slot),
  killPty: (slot) => ipcRenderer.invoke('pty:kill', slot),
  newSession: (agent, location) => ipcRenderer.invoke('pty:new-session', agent, location || 'local'),
  checkSession: (sessionName, hostId) => ipcRenderer.invoke('pty:check-session', sessionName, hostId || 'local'),

  // PTY events
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

  // Activity detection + summaries. Keys are prefixed with `hostId:` in client
  // mode (e.g. `remote:session:0`, `local:session:0`). Host-mode hosts can
  // still strip the prefix — renderer code should treat `hostId:` as opaque.
  detectActivity: () => ipcRenderer.invoke('pty:detect-activity'),
  captureAllPanes: () => ipcRenderer.invoke('pty:capture-all-panes'),

  // Per-host tmux session list — used to surface sessions from hosts that
  // don't run the Python API (e.g. WSL local in client mode).
  listSessionsByHost: () => ipcRenderer.invoke('tmux:list-sessions-by-host'),

  // Kill a tmux session directly on a specific host (bypasses the Python API).
  // Used by the trash flow so trashing on any machine actually removes the
  // session from tmux, not just from DynamoDB.
  killTmuxSession: (hostId, sessionName) => ipcRenderer.invoke('tmux:kill-session', hostId, sessionName),

  // Image paste
  saveImage: (base64Data) => ipcRenderer.invoke('pty:save-image', base64Data),

  // Config. Returned object includes isClient, apiUrl, apiPort, platform, hostIds.
  getConfig: () => ipcRenderer.invoke('get-config'),

  // Dashboards
  getPipelineStats: (batch) => ipcRenderer.invoke('dashboard:pipeline-stats', batch),
  get0dteStats: (traderId) => ipcRenderer.invoke('dashboard:0dte-stats', traderId),
  list0dteTraders: () => ipcRenderer.invoke('dashboard:0dte-list-traders'),
  getAmaterasuOcrStats: () => ipcRenderer.invoke('dashboard:amaterasu-ocr-stats'),
  getChatStreamState: () => ipcRenderer.invoke('chat-stream:get-state'),
  listUiReviewArtifacts: () => ipcRenderer.invoke('ui-review:list-artifacts'),

  // Context menu
  showContextMenu: (sessionName, displayName, hostId) => {
    ipcRenderer.send('context-menu', sessionName, displayName, hostId || 'local');
  },

  // Actions from main process. `assign-slot` now carries hostId.
  onAssignSlot: (callback) => {
    ipcRenderer.removeAllListeners('assign-slot');
    ipcRenderer.on('assign-slot', (_, slot, sessionName, hostId) => callback(slot, sessionName, hostId || 'local'));
  },
  onAction: (callback) => {
    ipcRenderer.removeAllListeners('action');
    ipcRenderer.on('action', (_, action, sessionName, extra) => callback(action, sessionName, extra));
  },
  onChatStreamEvent: (callback) => {
    ipcRenderer.removeAllListeners('chat-stream:event');
    ipcRenderer.on('chat-stream:event', (_, payload) => callback(payload));
  },
};
