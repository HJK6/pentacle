// With nodeIntegration enabled, preload just sets up IPC convenience functions
// on window.cc for the renderer to use.

const { ipcRenderer } = require('electron');
const os = require('os');
const path = require('path');
const { loadConfig } = require('./config-loader');

function chatPopoutContextFromArgv(argv = process.argv) {
  const prefix = '--pentacle-chat-popout=';
  const raw = (argv || []).find((value) => String(value).startsWith(prefix));
  if (!raw) return null;
  try {
    const context = JSON.parse(decodeURIComponent(String(raw).slice(prefix.length)));
    if (!context?.stream_id || !context?.host || !context?.session_name) return null;
    return {
      stream_id: String(context.stream_id),
      host: String(context.host),
      desktop_host: String(context.desktop_host || context.host),
      session_name: String(context.session_name),
      title: String(context.title || context.session_name),
    };
  } catch (_) {
    return null;
  }
}

const chatPopoutContext = chatPopoutContextFromArgv();

// Synchronous host metadata — available at script-eval time so dashboards
// can self-register without waiting on an async getConfig() round-trip.
// `isClient` matches the logic in main.js: CONFIG.remote present → CLIENT.
let _isClient = false;
let _hasRemote = false;
let _hasDashboardHub = false;
let _dashboardHubConfig = null;
try {
  const _cfg = loadConfig(__dirname).config;
  _isClient = !!_cfg.remote;
  _hasRemote = !!_cfg.remote;
  _hasDashboardHub = !!(_cfg.dashboardHub && _cfg.dashboardHub.url);
  // Expose the full dashboardHub config to the renderer so dashboards
  // (e.g. pi-control.js) can read it via window.HOST without re-running
  // require('./config-loader') from a <script src> context where __dirname /
  // require resolution can vary by Electron version / asar packaging.
  _dashboardHubConfig = (_cfg.dashboardHub && _cfg.dashboardHub.url) ? _cfg.dashboardHub : null;
} catch { /* keep default false */ }

window.HOST = {
  hostname: os.hostname(),
  platform: process.platform,
  isClient: _isClient,
  hasRemote: _hasRemote,
  hasDashboardHub: _hasDashboardHub,
  dashboardHubConfig: _dashboardHubConfig,
};

window.cc = {
  // PTY operations — hostId threads through so each slot knows which tmux
  // server its session lives on. Defaults to 'local' for backcompat.
  createPty: (slot, sessionName, hostId, cols, rows) => ipcRenderer.invoke('pty:create', slot, sessionName, hostId || 'local', cols, rows),
  writePty: (slot, data) => ipcRenderer.send('pty:write', slot, data),
  pastePty: (slot, data) => ipcRenderer.invoke('pty:paste', slot, data),
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
  writeClipboard: (text) => ipcRenderer.invoke('clipboard:write-text', String(text ?? '')),
  readClipboard: () => ipcRenderer.invoke('clipboard:read-text'),

  // Meeting window
  openMeeting: () => ipcRenderer.send('meeting:open'),
  closeMeeting: () => ipcRenderer.send('meeting:close'),

  // Reload the window from the main process. A renderer location.reload() is
  // cancelled by the will-navigate guard in main.js, so route it over IPC.
  reloadApp: () => ipcRenderer.send('app:reload'),

  // Kill a tmux session directly on a specific host (bypasses the Python API).
  // Used by the trash flow so trashing on any machine actually removes the
  // session from tmux, not just from DynamoDB.
  killTmuxSession: (hostId, sessionName) => ipcRenderer.invoke('tmux:kill-session', hostId, sessionName),
  setWindowTitle: (hostId, sessionName, title, source) => ipcRenderer.invoke('tmux:set-window-title', hostId, sessionName, title, source || 'manual'),

  // Image paste
  saveImage: (base64Data) => ipcRenderer.invoke('pty:save-image', base64Data),

  // Config. Returned object includes isClient, platform, hostIds, hostname.
  // Session metadata used to come via apiUrl/apiPort tunnel; that's gone
  // since the server.py deprecation — use chat-stream:* IPC instead.
  getConfig: () => ipcRenderer.invoke('get-config'),
  // Open an external http(s) link in the OS browser (report/markdown links).
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  chatSpawn: (provider, hostId) => ipcRenderer.invoke('chat-stream:spawn', provider, hostId || 'local'),
  chatSpawnV2: (options) => ipcRenderer.invoke('chat-stream:spawn', options || {}),
  chatSpawnCatalog: () => ipcRenderer.invoke('chat-stream:spawn-catalog'),
  chatSend: (hostId, sessionName, text) => ipcRenderer.invoke('chat-stream:send', hostId || 'local', sessionName, text),
  // Phase 5 (desktop_chat_ui_mobile_parity): correlated send — the renderer
  // OWNS request_id (its optimistic_id correlation) and passes it through so the
  // daemon's send.result echoes the same id back on the frame channel. Additive;
  // legacy chatSend (above) is untouched and still the default composer path.
  chatSendCorrelated: (hostId, sessionName, text, requestId, optimisticId, attachments) => ipcRenderer.invoke('chat-stream:send', hostId || 'local', sessionName, text, requestId, optimisticId, attachments),
  chatUploadBlob: (payload) => ipcRenderer.invoke('chat-stream:upload-blob', payload || {}),
  chatFetchBlob: (blobSha) => ipcRenderer.invoke('chat-stream:fetch-blob', blobSha),
  // B3 (chat_send_turn_lifecycle_batch2): interrupt/cancel the latest in-flight
  // turn (daemon injects Escape into the agent pane).
  chatInterrupt: (hostId, sessionName) => ipcRenderer.invoke('chat-stream:interrupt', hostId || 'local', sessionName),
  // Settle an agent-asked question (Esc) and optionally submit formatted answer
  // text through the daemon's atomic question.dismiss RPC.
  chatDismissQuestion: (hostId, sessionName, payload) => ipcRenderer.invoke('chat-stream:dismiss-question', hostId || 'local', sessionName, payload || {}),
  chatRename: (hostId, sessionName, displayName) => ipcRenderer.invoke('chat-stream:rename', hostId || 'local', sessionName, displayName),
  // E2E walk harness only: force a ws reconnect (main registers the handler only
  // under PENTACLE_HARNESS=1, so this invoke rejects/no-ops in production).
  forceReconnect: () => ipcRenderer.invoke('harness:force-reconnect'),
  // Surface parity with renderer/web_cc.js. On the desktop the ipcRenderer
  // transport never drops (main and the renderer share a process), so there is
  // no websocket reconnect to signal: this is a deliberate no-op. In web mode
  // the same method notifies app.js so it can re-pull state after the /cc
  // websocket reconnects (see renderer/web_cc.js).
  onReconnect: () => {},
  chatClose: (hostId, sessionName, options) => ipcRenderer.invoke('chat-stream:close', hostId || 'local', sessionName, options || null),

  // Phase C parity bridges — replace renderer's old api() HTTP calls into
  // server.py with chat_streamd RPCs. window.cc.killSession was previously
  // a fetch wrapper; it's now an alias for chatKill so the trash drawer's
  // permanent-delete flow keeps working.
  chatKill: (args) => ipcRenderer.invoke('chat-stream:kill', args || {}),
  requestStreamEvents: (args) => ipcRenderer.invoke('chat-stream:request-stream-events', args || {}),
  scheduleGet: (scheduleId) => ipcRenderer.invoke('chat-stream:schedule-get', scheduleId),
  scheduleCancel: (scheduleId) => ipcRenderer.invoke('chat-stream:schedule-cancel', scheduleId),
  scheduleRun: (scheduleId) => ipcRenderer.invoke('chat-stream:schedule-run', scheduleId),
  scheduleReschedule: (scheduleId, options) => ipcRenderer.invoke('chat-stream:schedule-reschedule', {
    scheduleId,
    firesAtUtc: options && typeof options === 'object' ? options.at : options,
  }),
  // Notifications subsystem (Notifications dashboard talks to chat_streamd via these)
  promptList: (args) => ipcRenderer.invoke('chat-stream:prompt-list', args || null),
  notificationList: (args) => ipcRenderer.invoke('chat-stream:notification-list', args || null),
  notificationResolve: (notificationId, actionKind, options) => ipcRenderer.invoke('chat-stream:notification-resolve', {
    notificationId,
    actionKind,
    action_id: options?.action_id,
    choice: options?.choice,
    selections: options?.selections,
    text: options?.text,
    custom_text: options?.custom_text ?? options?.customText,
    note: options?.note,
    submit: options?.submit,
    by: options?.by || 'operator',
    spawn: options?.spawn,
  }),
  notificationCreate: (args) => ipcRenderer.invoke('chat-stream:notification-create', args || {}),
  assetList: (args) => ipcRenderer.invoke('chat-stream:asset-list', args || null),
  assetGet: (args) => ipcRenderer.invoke('chat-stream:asset-get', args || {}),
  assetCommentsList: (args) => ipcRenderer.invoke('chat-stream:asset-comments-list', args || {}),
  assetCommentAdd: (args) => ipcRenderer.invoke('chat-stream:asset-comment-add', args || {}),
  assetCommentEdit: (args) => ipcRenderer.invoke('chat-stream:asset-comment-edit', args || {}),
  assetCommentDelete: (args) => ipcRenderer.invoke('chat-stream:asset-comment-delete', args || {}),
  assetDelete: (args) => ipcRenderer.invoke('chat-stream:asset-delete', args || {}),
  assetCommentResolve: (args) => ipcRenderer.invoke('chat-stream:asset-comment-resolve', args || {}),
  assetReviewSet: (args) => ipcRenderer.invoke('chat-stream:asset-review-set', args || {}),
  assetCommentsSendToChat: (args) => ipcRenderer.invoke('chat-stream:asset-comments-send-to-chat', args || {}),
  assetPopOut: (args) => ipcRenderer.invoke('chat-stream:asset-pop-out', args || {}),
  assetDock: (args) => ipcRenderer.invoke('chat-stream:asset-dock', args || {}),
  chatPopoutContext: () => chatPopoutContext,
  chatPopOut: (args) => ipcRenderer.invoke('chat-stream:chat-pop-out', args || {}),
  chatDock: (args) => ipcRenderer.invoke('chat-stream:chat-dock', args || {}),

  perfRecord: (event, details) => { try { ipcRenderer.send('perf-telemetry:record', { event, details: details || null }); } catch (_) {} },
  perfState: () => ipcRenderer.invoke('perf-telemetry:state'),

  // Specs subsystem (Specs dashboard talks to chat_streamd via these)
  specsList: (filter) => ipcRenderer.invoke('specs:list', filter || null),
  specsGet: (specId) => ipcRenderer.invoke('specs:get', specId),
  specsDrive: (specId, options, callerStreamId) => ipcRenderer.invoke('specs:drive', { specId, options: options || {}, callerStreamId: callerStreamId || null }),
  specsCapabilities: () => ipcRenderer.invoke('specs:capabilities'),

  // Dashboards
  getPipelineStats: (batch) => ipcRenderer.invoke('dashboard:pipeline-stats', batch),
  getBusinessPipelineStats: () => ipcRenderer.invoke('dashboard:business-stats'),
  getPentacleMobileTestingStats: () => ipcRenderer.invoke('dashboard:pentacle-mobile-testing-stats'),
  setBatchGate: (batch, gate, setting) => ipcRenderer.invoke('dashboard:set-batch-gate', { batch, gate, setting }),
  get0dteStats: (traderId) => ipcRenderer.invoke('dashboard:0dte-stats', traderId),
  list0dteTraders: () => ipcRenderer.invoke('dashboard:0dte-list-traders'),
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
  onChatStreamFrame: (callback) => {
    ipcRenderer.removeAllListeners('chat-stream:frame');
    ipcRenderer.on('chat-stream:frame', (_, frame) => callback(frame));
  },
  onAssetPopoutInit: (callback) => {
    ipcRenderer.removeAllListeners('asset-popout:init');
    ipcRenderer.on('asset-popout:init', (_, payload) => callback(payload));
  },
  onAssetDock: (callback) => {
    ipcRenderer.removeAllListeners('asset:dock');
    ipcRenderer.on('asset:dock', (_, payload) => callback(payload));
  },
  onChatPopoutDock: (callback) => {
    ipcRenderer.removeAllListeners('chat:popout-dock');
    ipcRenderer.on('chat:popout-dock', (_, payload) => callback(payload));
  },
};
