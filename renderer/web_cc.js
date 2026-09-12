'use strict';

// ── Browser implementation of window.cc / window.HOST ────────────────────────
// Method-for-method with preload.js, over the websocket the headless host
// serves at /cc. renderer/app.js is not forked: it sees the same names, the
// same argument shapes, and the same "invoke returns a promise, send returns
// nothing" split.
//
// Installed SYNCHRONOUSLY, before any renderer script runs, because app.js
// reads window.cc and window.HOST at module scope. Calls made before the socket
// opens are queued and flushed on connect; when the socket drops, every
// in-flight request is rejected so no caller waits forever, and the queue is
// replayed after the reconnect.
//
// A few methods never reach the host — see WEB_LOCAL in main/cc_handlers.js.
// The clipboard is the one that matters: routing it over the socket would read
// the SERVER's clipboard, not the viewer's.

const RECONNECT_MIN_MS = 250;
const RECONNECT_MAX_MS = 5000;

function chatPopoutContextFromSearch(search) {
  // URLSearchParams already percent-decodes the value. Decoding a second time
  // (as preload.js must, because its argv value is still raw) would eat a
  // literal % in a title and can turn a valid context into null.
  const raw = new URLSearchParams(search || '').get('pentacle-chat-popout');
  if (!raw) return null;
  try {
    const context = JSON.parse(raw);
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

function createTransport({ url, logger = console } = {}) {
  const pending = new Map();
  const queued = [];
  const listeners = new Map();
  let socket = null;
  let nextId = 1;
  let attempt = 0;
  let closed = false;

  function flush() {
    while (queued.length && socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(queued.shift()));
    }
  }

  function rejectAllPending(reason) {
    for (const [, entry] of pending) entry.reject(new Error(reason));
    pending.clear();
  }

  function connect() {
    if (closed) return;
    socket = new WebSocket(url);
    socket.addEventListener('open', () => { attempt = 0; flush(); });
    socket.addEventListener('message', (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (message && message.event) {
        const handler = listeners.get(message.event);
        if (handler) {
          try { handler(...(message.args || [])); } catch (e) { logger.warn(`[web] ${message.event} handler threw:`, e); }
        }
        return;
      }
      const entry = message && pending.get(message.id);
      if (!entry) return;
      pending.delete(message.id);
      if (message.ok) entry.resolve(message.result);
      else entry.reject(Object.assign(new Error(message.error?.message || 'request failed'), { code: message.error?.code }));
    });
    socket.addEventListener('close', () => {
      // A dropped socket can never answer; failing fast beats a hung promise.
      rejectAllPending('pentacle web host connection lost');
      if (closed) return;
      const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_MIN_MS * 2 ** attempt++);
      setTimeout(connect, delay);
    });
    socket.addEventListener('error', () => { try { socket.close(); } catch {} });
  }

  connect();

  return {
    get socket() { return socket; },
    /** invoke-mode: resolves with the handler's return value. */
    call(method, ...args) {
      const id = nextId++;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        const frame = { id, method, args };
        if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(frame));
        else queued.push(frame);
      });
    },
    /** send-mode: fire-and-forget, returns undefined exactly like ipcRenderer.send. */
    fire(method, ...args) {
      const frame = { method, args };
      if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(frame));
      else queued.push(frame);
      return undefined;
    },
    /** Replaces any previous handler, mirroring preload's removeAllListeners. */
    on(event, handler) { listeners.set(event, handler); },
    close() { closed = true; try { socket && socket.close(); } catch {} },
  };
}

// The viewer's clipboard, never the host's. readClipboard is async here, which
// matches the public preload (it invokes clipboard:read-text).
function browserClipboard() {
  return {
    async writeText(text) {
      const value = String(text ?? '');
      try {
        if (navigator.clipboard?.writeText) return await navigator.clipboard.writeText(value);
      } catch { /* fall through to the legacy path */ }
      try {
        const el = document.createElement('textarea');
        el.value = value;
        el.setAttribute('readonly', '');
        el.style.cssText = 'position:fixed;opacity:0';
        document.body.appendChild(el);
        el.select();
        document.execCommand('copy');
        document.body.removeChild(el);
      } catch { /* nothing else to try */ }
    },
    async readText() {
      try {
        if (navigator.clipboard?.readText) return await navigator.clipboard.readText();
      } catch { /* permission or focus denied */ }
      return '';
    },
  };
}

function buildCc(transport, { clipboard, chatPopoutContext, reload = () => window.location.reload() }) {
  const call = transport.call.bind(transport);
  const fire = transport.fire.bind(transport);
  const on = transport.on.bind(transport);

  return {
    // PTY operations — hostId threads through so each slot knows which tmux
    // server its session lives on. Defaults to 'local' for backcompat.
    createPty: (slot, sessionName, hostId, cols, rows) => call('pty:create', slot, sessionName, hostId || 'local', cols, rows),
    writePty: (slot, data) => fire('pty:write', slot, data),
    pastePty: (slot, data) => call('pty:paste', slot, data),
    tmuxSend: (slot, ...keys) => fire('pty:tmux-send', slot, ...keys),
    resizePty: (slot, cols, rows) => fire('pty:resize', slot, cols, rows),
    scrollTmux: (slot, direction, lines) => fire('pty:scroll', slot, direction, lines || 1),
    exitCopyMode: (slot) => fire('pty:exit-copy-mode', slot),
    killPty: (slot) => call('pty:kill', slot),
    newSession: (agent, location) => call('pty:new-session', agent, location || 'local'),
    checkSession: (sessionName, hostId) => call('pty:check-session', sessionName, hostId || 'local'),

    // PTY events
    onPtyData: (callback) => on('pty:data', (slot, data) => callback(slot, data)),
    onPtyExit: (callback) => on('pty:exit', (slot, exitCode) => callback(slot, exitCode)),

    startMicServer: () => call('mic:start-server'),
    // The viewer's clipboard, not the host's — see WEB_LOCAL.
    writeClipboard: (text) => clipboard.writeText(String(text ?? '')),
    readClipboard: () => clipboard.readText(),

    // No native meeting window in a browser; the host refuses these.
    openMeeting: () => fire('meeting:open'),
    closeMeeting: () => fire('meeting:close'),
    reloadApp: () => { reload(); return undefined; },

    killTmuxSession: (hostId, sessionName) => call('tmux:kill-session', hostId, sessionName),
    setWindowTitle: (hostId, sessionName, title, source) => call('tmux:set-window-title', hostId, sessionName, title, source || 'manual'),
    saveImage: (base64Data) => call('pty:save-image', base64Data),

    getConfig: () => call('get-config'),
    // In a browser the OS browser IS the browser; opening a tab beats a round
    // trip to a channel the host refuses.
    openExternal: (url) => {
      try {
        const u = new URL(String(url || ''));
        if (u.protocol === 'http:' || u.protocol === 'https:') {
          window.open(u.href, '_blank', 'noopener,noreferrer');
          return Promise.resolve({ ok: true });
        }
      } catch (_) { /* malformed / disallowed scheme */ }
      return Promise.resolve({ ok: false, error: 'unsupported URL' });
    },

    chatSpawn: (provider, hostId) => call('chat-stream:spawn', provider, hostId || 'local'),
    chatSpawnV2: (options) => call('chat-stream:spawn', options || {}),
    chatSpawnCatalog: () => call('chat-stream:spawn-catalog'),
    chatSend: (hostId, sessionName, text) => call('chat-stream:send', hostId || 'local', sessionName, text),
    chatSendCorrelated: (hostId, sessionName, text, requestId, optimisticId, attachments) => call('chat-stream:send', hostId || 'local', sessionName, text, requestId, optimisticId, attachments),
    chatUploadBlob: (payload) => call('chat-stream:upload-blob', payload || {}),
    chatFetchBlob: (blobSha) => call('chat-stream:fetch-blob', blobSha),
    chatInterrupt: (hostId, sessionName) => call('chat-stream:interrupt', hostId || 'local', sessionName),
    chatDismissQuestion: (hostId, sessionName, payload) => call('chat-stream:dismiss-question', hostId || 'local', sessionName, payload || {}),
    chatRename: (hostId, sessionName, displayName) => call('chat-stream:rename', hostId || 'local', sessionName, displayName),
    forceReconnect: () => call('harness:force-reconnect'),
    chatClose: (hostId, sessionName, options) => call('chat-stream:close', hostId || 'local', sessionName, options || null),

    chatKill: (args) => call('chat-stream:kill', args || {}),
    requestStreamEvents: (args) => call('chat-stream:request-stream-events', args || {}),
    scheduleGet: (scheduleId) => call('chat-stream:schedule-get', scheduleId),
    scheduleCancel: (scheduleId) => call('chat-stream:schedule-cancel', scheduleId),
    scheduleRun: (scheduleId) => call('chat-stream:schedule-run', scheduleId),
    scheduleReschedule: (scheduleId, options) => call('chat-stream:schedule-reschedule', {
      scheduleId,
      firesAtUtc: options && typeof options === 'object' ? options.at : options,
    }),

    promptList: (args) => call('chat-stream:prompt-list', args || null),
    notificationList: (args) => call('chat-stream:notification-list', args || null),
    notificationResolve: (notificationId, actionKind, options) => call('chat-stream:notification-resolve', {
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
    notificationCreate: (args) => call('chat-stream:notification-create', args || {}),
    assetList: (args) => call('chat-stream:asset-list', args || null),
    assetGet: (args) => call('chat-stream:asset-get', args || {}),
    assetCommentsList: (args) => call('chat-stream:asset-comments-list', args || {}),
    assetCommentAdd: (args) => call('chat-stream:asset-comment-add', args || {}),
    assetCommentEdit: (args) => call('chat-stream:asset-comment-edit', args || {}),
    assetCommentDelete: (args) => call('chat-stream:asset-comment-delete', args || {}),
    assetDelete: (args) => call('chat-stream:asset-delete', args || {}),
    assetCommentResolve: (args) => call('chat-stream:asset-comment-resolve', args || {}),
    assetReviewSet: (args) => call('chat-stream:asset-review-set', args || {}),
    assetCommentsSendToChat: (args) => call('chat-stream:asset-comments-send-to-chat', args || {}),
    assetPopOut: (args) => call('chat-stream:asset-pop-out', args || {}),
    assetDock: (args) => call('chat-stream:asset-dock', args || {}),
    chatPopoutContext: () => chatPopoutContext,
    chatPopOut: (args) => call('chat-stream:chat-pop-out', args || {}),
    chatDock: (args) => call('chat-stream:chat-dock', args || {}),

    perfRecord: (event, details) => { try { fire('perf-telemetry:record', { event, details: details || null }); } catch (_) {} },
    perfState: () => call('perf-telemetry:state'),

    specsList: (filter) => call('specs:list', filter || null),
    specsGet: (specId) => call('specs:get', specId),
    specsDrive: (specId, options, callerStreamId) => call('specs:drive', specId, options || {}, callerStreamId || null),
    specsCapabilities: () => call('specs:capabilities'),

    getPipelineStats: (batch) => call('dashboard:pipeline-stats', batch),
    getBusinessPipelineStats: () => call('dashboard:business-stats'),
    getPentacleMobileTestingStats: () => call('dashboard:pentacle-mobile-testing-stats'),
    setBatchGate: (batch, gate, setting) => call('dashboard:set-batch-gate', { batch, gate, setting }),
    get0dteStats: (traderId) => call('dashboard:0dte-stats', traderId),
    list0dteTraders: () => call('dashboard:0dte-list-traders'),
    getChatStreamState: () => call('chat-stream:get-state'),
    listUiReviewArtifacts: () => call('ui-review:list-artifacts'),

    // Native context menu — refused by the host; the browser keeps its own.
    showContextMenu: (sessionName, displayName, hostId) => fire('context-menu', sessionName, displayName, hostId || 'local'),

    onAssignSlot: (callback) => on('assign-slot', (slot, sessionName, hostId) => callback(slot, sessionName, hostId || 'local')),
    onAction: (callback) => on('action', (action, sessionName, extra) => callback(action, sessionName, extra)),
    onChatStreamFrame: (callback) => on('chat-stream:frame', (frame) => callback(frame)),
    onAssetPopoutInit: (callback) => on('asset-popout:init', (payload) => callback(payload)),
    onAssetDock: (callback) => on('asset:dock', (payload) => callback(payload)),
    onChatPopoutDock: (callback) => on('chat:popout-dock', (payload) => callback(payload)),
  };
}

function buildHost(config) {
  return {
    hostname: config.hostname || '',
    platform: config.platform || 'linux',
    isClient: config.isClient !== undefined ? !!config.isClient : !!config.remote,
    hasRemote: !!config.remote,
    hasDashboardHub: !!(config.dashboardHub && config.dashboardHub.url),
    dashboardHubConfig: (config.dashboardHub && config.dashboardHub.url) ? config.dashboardHub : null,
  };
}

/**
 * Install window.cc and window.HOST. Synchronous by contract — app.js reads
 * both at module scope, so this must run before any renderer script.
 */
function installWebCc({
  clipboard = browserClipboard(),
  location = window.location,
  logger = console,
} = {}) {
  const config = window.__PENTACLE_CONFIG__;
  if (!config) throw new Error('window.__PENTACLE_CONFIG__ is missing — the page was not served by the Pentacle web host');
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const transport = createTransport({ url: `${scheme}//${location.host}/cc`, logger });
  window.HOST = buildHost(config);
  window.cc = buildCc(transport, {
    clipboard,
    chatPopoutContext: chatPopoutContextFromSearch(location.search),
  });
  return transport;
}

module.exports = {
  installWebCc,
  createTransport,
  buildCc,
  buildHost,
  browserClipboard,
  chatPopoutContextFromSearch,
};
