/*
 * Public Electron main-process adapter.
 *
 * The desktop shell owns window lifecycle and exposes small, provider-neutral
 * IPC contracts. Optional integrations belong behind separately reviewed
 * adapters; this file never contains credentials, fleet routing, or network
 * promotion logic.
 */
const { app, BrowserWindow, ipcMain, shell } = require('electron');
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs');
const crypto = require('node:crypto');
const { loadConfig } = require('./config-loader');

const FALLBACK_CONFIG = {
  appName: 'Pentacle',
  features: { mic: false },
  hosts: { local: { kind: 'local' } },
  agents: {},
};

let CONFIG = FALLBACK_CONFIG;
let configError = null;
try {
  const loaded = loadConfig(__dirname);
  CONFIG = (loaded && loaded.config) || loaded || FALLBACK_CONFIG;
} catch (error) {
  configError = error;
}

app.setName(CONFIG.appName || 'Pentacle');

const windows = new Set();
const sessions = new Map();
const events = new Map();
const blobs = new Map();
const telemetry = [];

function safeString(value, fallback = '') {
  const text = String(value == null ? '' : value).trim();
  return text || fallback;
}

function safeHostId(value) {
  const requested = safeString(value, 'local').toLowerCase();
  return /^(local|host-[a-d])$/.test(requested) ? requested : 'local';
}

function safeSessionName(value) {
  const requested = safeString(value, 'example-session');
  return requested.replace(/[^a-zA-Z0-9._-]/g, '-').slice(0, 80) || 'example-session';
}

function sessionId(hostId, sessionName) {
  return hostId + ':' + sessionName;
}

function nowIso() {
  return new Date().toISOString();
}

function publicSession(record) {
  return {
    stream_id: record.stream_id,
    host: record.host,
    session_name: record.session_name,
    title: record.title,
    last_event_at: record.last_event_at,
    last_text: record.last_text,
    last_kind: record.last_kind,
    working: record.working,
    online: true,
  };
}

function publicEvent(event) {
  return {
    daemon_seq: event.daemon_seq,
    host: event.host,
    provider: event.provider,
    session_id: event.session_id,
    session_name: event.session_name,
    stream_id: event.stream_id,
    timestamp: event.timestamp,
    kind: event.kind,
    text: event.text,
  };
}

function ensureSession(hostId, sessionName) {
  const key = sessionId(hostId, sessionName);
  let record = sessions.get(key);
  if (!record) {
    record = {
      stream_id: key,
      host: hostId,
      session_name: sessionName,
      title: sessionName,
      last_event_at: null,
      last_text: '',
      last_kind: null,
      working: false,
      nextSeq: 1,
    };
    sessions.set(key, record);
    events.set(key, []);
  }
  return record;
}

function addEvent(record, kind, text, provider = 'example-provider') {
  const event = {
    daemon_seq: record.nextSeq++,
    host: record.host,
    provider,
    session_id: record.stream_id,
    session_name: record.session_name,
    stream_id: record.stream_id,
    timestamp: nowIso(),
    kind,
    text: safeString(text),
  };
  record.last_event_at = event.timestamp;
  record.last_text = event.text;
  record.last_kind = event.kind;
  record.working = false;
  events.get(record.stream_id).push(event);
  const frame = { type: 'chat.event', event: publicEvent(event) };
  for (const window of windows) {
    if (!window.isDestroyed()) window.webContents.send('chat-stream:frame', frame);
  }
  return event;
}

function stateSnapshot() {
  return {
    sessions: Array.from(sessions.values()).map(publicSession),
    events: Array.from(events.values()).flat().map(publicEvent),
  };
}

function publicConfig() {
  const configuredHosts = CONFIG && CONFIG.hosts && typeof CONFIG.hosts === 'object' ? CONFIG.hosts : {};
  const hosts = {};
  for (const id of Object.keys(configuredHosts)) {
    const hostId = safeHostId(id);
    const entry = configuredHosts[id] || {};
    hosts[hostId] = { kind: safeString(entry.kind, 'local') };
  }
  if (!hosts.local) hosts.local = { kind: 'local' };
  return {
    appName: safeString(CONFIG.appName, 'Pentacle'),
    features: { mic: Boolean(CONFIG.features && CONFIG.features.mic) },
    hosts,
    configError: configError ? 'configuration unavailable' : null,
  };
}

function resultError(message) {
  return { ok: false, error: message };
}

async function openPublicUrl(value) {
  try {
    const url = new URL(safeString(value));
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return resultError('unsupported URL');
    await shell.openExternal(url.href);
    return { ok: true };
  } catch {
    return resultError('invalid URL');
  }
}

function registerIpc() {
  ipcMain.handle('open-external', (_event, url) => openPublicUrl(url));
  ipcMain.handle('get-config', () => publicConfig());

  ipcMain.handle('chat-stream:get-state', () => stateSnapshot());
  ipcMain.handle('chat-stream:spawn-catalog', () => ({
    agents: Object.keys(CONFIG.agents || {}).length ? Object.keys(CONFIG.agents) : ['example-agent'],
    hosts: ['local', 'hosta', 'hostb', 'hostc', 'hostd'],
  }));
  ipcMain.handle('chat-stream:spawn', async (_event, request, legacyHostId) => {
    const input = request && typeof request === 'object' ? request : { agent: request, host: legacyHostId };
    const host = safeHostId(input.host);
    const name = safeSessionName(input.sessionName || input.session_name || input.name);
    const record = ensureSession(host, name);
    record.working = true;
    return { ok: true, session: publicSession(record), stream_id: record.stream_id };
  });
  ipcMain.handle('chat-stream:send', async (_event, hostId, sessionName, text, requestId, optimisticId) => {
    const record = ensureSession(safeHostId(hostId), safeSessionName(sessionName));
    const event = addEvent(record, 'USER', text);
    return {
      ok: true,
      request_id: safeString(requestId, null),
      optimistic_id: safeString(optimisticId, null),
      event: publicEvent(event),
    };
  });
  ipcMain.handle('chat-stream:request-stream-events', (_event, args) => {
    const streamId = safeString(args && args.streamId);
    const rows = events.get(streamId) || [];
    const limit = Math.max(0, Math.min(Number(args && args.limit) || 200, 1000));
    return { ok: true, events: rows.slice(-limit).map(publicEvent), stream_id: streamId };
  });
  ipcMain.handle('chat-stream:interrupt', () => ({ ok: true, interrupted: false, confirm: 'not-working' }));
  ipcMain.handle('chat-stream:dismiss-question', () => ({ ok: true, dismissed: true, text_submitted: false }));
  ipcMain.handle('chat-stream:rename', (_event, hostId, sessionName, displayName) => {
    const record = ensureSession(safeHostId(hostId), safeSessionName(sessionName));
    record.title = safeString(displayName, record.title);
    return { ok: true, session: publicSession(record) };
  });
  ipcMain.handle('chat-stream:close', (_event, hostId, sessionName) => {
    const key = sessionId(safeHostId(hostId), safeSessionName(sessionName));
    sessions.delete(key);
    events.delete(key);
    return { ok: true };
  });
  ipcMain.handle('chat-stream:kill', () => ({ ok: true, killed: false }));
  ipcMain.handle('chat-stream:upload-blob', (_event, payload) => {
    const encoded = safeString(payload && (payload.data || payload.base64));
    if (!encoded) return resultError('missing blob data');
    try {
      const value = Buffer.from(encoded, 'base64');
      const sha256 = crypto.createHash('sha256').update(value).digest('hex');
      blobs.set(sha256, value);
      return { ok: true, blob_sha: sha256, bytes: value.length };
    } catch {
      return resultError('invalid blob data');
    }
  });
  ipcMain.handle('chat-stream:fetch-blob', (_event, blobSha) => {
    const value = blobs.get(safeString(blobSha));
    return value ? { ok: true, data: value.toString('base64'), bytes: value.length } : resultError('blob not found');
  });
  ipcMain.handle('harness:force-reconnect', () => ({ ok: true, state: stateSnapshot() }));

  ipcMain.handle('pty:create', () => resultError('local terminal adapter is not enabled in this example'));
  ipcMain.handle('pty:new-session', () => resultError('local terminal adapter is not enabled in this example'));
  ipcMain.handle('pty:check-session', () => ({ ok: false, exists: false }));
  ipcMain.handle('pty:kill', () => ({ ok: true }));
  ipcMain.on('pty:write', () => {});
  ipcMain.on('pty:resize', () => {});
  ipcMain.on('pty:scroll', () => {});
  ipcMain.on('pty:tmux-send', () => {});
  ipcMain.on('pty:exit-copy-mode', () => {});
  ipcMain.handle('pty:save-image', (_event, base64Data) => {
    try {
      const value = Buffer.from(safeString(base64Data), 'base64');
      const filePath = path.join(os.tmpdir(), 'desktop-paste-' + Date.now() + '.png');
      fs.writeFileSync(filePath, value);
      return { ok: true, path: filePath };
    } catch {
      return resultError('could not save image');
    }
  });

  ipcMain.handle('dashboard:list', () => ({ ok: true, boards: [] }));
  ipcMain.handle('ui-review:list-artifacts', () => []);
  ipcMain.handle('specs:list', () => ({ ok: true, specs: [], groups: [] }));
  ipcMain.handle('specs:get', () => resultError('no public spec adapter configured'));
  ipcMain.handle('specs:drive', () => resultError('no public spec adapter configured'));
  ipcMain.handle('specs:capabilities', () => ({ ok: true, capabilities: [] }));

  ipcMain.handle('mic:start-server', () => resultError('optional local microphone adapter is disabled'));
  ipcMain.on('meeting:open', () => {});
  ipcMain.on('meeting:close', () => {});

  ipcMain.on('perf-telemetry:record', (_event, value) => {
    if (telemetry.length >= 1000) telemetry.shift();
    telemetry.push({ at: nowIso(), event: safeString(value && value.event, 'unknown') });
  });
  ipcMain.handle('perf-telemetry:state', () => ({
    enabled: false,
    log_path: null,
    buffered_events: telemetry.length,
  }));
}

async function createMainWindow() {
  const window = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 720,
    minHeight: 480,
    show: false,
    webPreferences: {
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });
  windows.add(window);
  window.once('ready-to-show', () => window.show());
  window.on('closed', () => windows.delete(window));
  const indexPath = path.join(__dirname, 'renderer', 'index.html');
  try {
    await window.loadFile(indexPath);
  } catch {
    await window.loadURL('data:text/html,<h1>Pentacle</h1><p>Renderer not bundled.</p>');
  }
  return window;
}

registerIpc();

app.whenReady().then(() => {
  if (configError) console.warn('Using public fallback configuration:', configError.message || configError);
  return createMainWindow();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) void createMainWindow();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

