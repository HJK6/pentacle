/*
 * Public Electron main-process adapter.
 *
 * The desktop shell owns window lifecycle and exposes small, provider-neutral
 * IPC contracts. Optional integrations belong behind separately reviewed
 * adapters; this file never contains credentials, fleet routing, or network
 * promotion logic.
 */
const { app, BrowserWindow, ipcMain, shell, clipboard } = require('electron');
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs');
const { loadConfig } = require('./config-loader');

const FALLBACK_CONFIG = {
  appName: 'Pentacle',
  features: { mic: false },
  hosts: { local: { kind: 'local' } },
  agents: {},
};

let CONFIG = FALLBACK_CONFIG;
let configError = null;
let configWarnings = [];
try {
  const loaded = loadConfig(__dirname);
  CONFIG = (loaded && loaded.config) || loaded || FALLBACK_CONFIG;
  configWarnings = loaded.warnings || [];
} catch (error) {
  configError = error;
}

app.setName(CONFIG.appName || 'Pentacle');

const windows = new Set();
const telemetry = [];
const chatStreamClient = require('./main/chat_stream_client');
const { registerAssetIpcHandlers } = require('./main/asset_ipc_bridge');
const { registerScheduleIpcHandlers } = require('./main/schedule_ipc_bridge');
const { probeMicServer } = require('./main/mic-url');
const { registerNotificationIpcHandlers } = require('./main/notification_ipc_bridge');
const { createAssetPopoutManager } = require('./main/asset_popout_windows');
const assetPopouts = createAssetPopoutManager({ BrowserWindow, appRoot: __dirname, getMainWindow: () => [...windows][0] });
function safeString(value, fallback = '') { return String(value ?? '').trim() || fallback; }
function nowIso() { return new Date().toISOString(); }
function normalizeChatStreamError(error) { return String(error?.error || error?.message || error || 'Daemon unavailable'); }
function resultError(message) { return { ok: false, error: normalizeChatStreamError(message) }; }
function publicConfig() {
  const { token, tokenPath, ...chatStream } = CONFIG.chatStream || {};
  return { ...CONFIG, chatStream, hostIds: CONFIG.chatStream?.hosts || ['local'], platform: process.platform,
    hostname: os.hostname(), isClient: Boolean(CONFIG.remote), configError: configError?.message || null, configWarnings };
}
async function command(action) {
  try { return { ok: true, ...await action() }; }
  catch (error) { return { ...resultError(error), code: error?.code, remediation: error?.remediation }; }
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
  require('./main/clipboard_ipc_bridge').registerClipboardIpc(ipcMain, clipboard);
  ipcMain.handle('open-external', (_event, url) => openPublicUrl(url));
  ipcMain.handle('get-config', () => publicConfig());

  ipcMain.handle('chat-stream:get-state', () => chatStreamClient.snapshot());
  ipcMain.handle('chat-stream:spawn-catalog', () => command(async () => ({ catalog: await chatStreamClient.getSpawnCatalog() })));
  ipcMain.handle('chat-stream:spawn', async (_event, request, legacyHostId) => {
    const input = request && typeof request === 'object' ? request : { provider: request, host: legacyHostId };
    const spawnProfile = input.spawnProfile || input.spawn_profile;
    if (spawnProfile === 'desktop_manual' && (!input.model || !input.effort)) return resultError('A model and effort are required');
    return command(async () => {
      const response = await chatStreamClient.spawnSession({ ...input, host: input.hostId || input.host || 'local', spawnProfile });
      if (response?.state === 'queued') return { ...response, streamId: response.stream_id };
      const session = response.session || response;
      return { ...response, session, streamId: response.stream_id || session?.stream_id,
        requested: session?.requested_launch_tuple, resolved: session?.resolved_launch_tuple,
        actualLaunch: session?.actual_launch_tuple };
    });
  });
  ipcMain.handle('chat-stream:send', (_event, host, sessionName, text, requestId, optimisticId, attachments) =>
    command(async () => {
      const receipt = await chatStreamClient.sendMessage({ host, sessionName, text, requestId, optimisticId, attachments });
      return { ...receipt, ok: receipt.delivery === 'landed' || receipt.action_committed === true,
        ...(receipt.delivery === 'not_landed' && !receipt.action_committed ? { error: receipt.reason || 'Message was not delivered' } : {}) };
    }));
  ipcMain.handle('chat-stream:request-stream-events', (_event, args) => command(() => chatStreamClient.requestStreamEvents(args)));
  ipcMain.handle('chat-stream:interrupt', (_event, host, sessionName) => command(() => chatStreamClient.interruptMessage({ host, sessionName })));
  ipcMain.handle('chat-stream:dismiss-question', (_event, host, sessionName, payload = {}) =>
    command(() => chatStreamClient.dismissQuestion({ host, sessionName, questionKey: payload.questionKey || payload.question_key, text: payload.text })));
  ipcMain.handle('chat-stream:rename', (_event, host, sessionName, displayName) =>
    command(() => chatStreamClient.renameSession({ host, sessionName, displayName, source: 'manual' })));
  ipcMain.handle('chat-stream:close', (_event, host, sessionName, options) => command(() => chatStreamClient.closeSession({ ...options, host, sessionName })));
  ipcMain.handle('chat-stream:kill', (_event, args) => command(() => chatStreamClient.killSessionRpc(args)));
  ipcMain.handle('chat-stream:upload-blob', (_event, payload) => command(() => chatStreamClient.uploadBlob({ ...payload,
    data: typeof payload?.data === 'string' ? Buffer.from(payload.data, 'base64') : payload?.data })));
  ipcMain.handle('chat-stream:fetch-blob', (_event, blobSha) => command(() => chatStreamClient.fetchBlob({ blobSha })));
  registerAssetIpcHandlers(ipcMain, chatStreamClient, normalizeChatStreamError, assetPopouts);
  registerScheduleIpcHandlers(ipcMain, chatStreamClient, normalizeChatStreamError);
  registerNotificationIpcHandlers(ipcMain, chatStreamClient, normalizeChatStreamError);
  ipcMain.handle('tmux:kill-session', (_event, host, sessionName) => command(() => chatStreamClient.killSessionRpc({ host, sessionName })));
  ipcMain.handle('tmux:set-window-title', (_event, host, sessionName, displayName) => command(() => chatStreamClient.renameSession({ host, sessionName, displayName, source: 'manual' })));
  if (process.env.PENTACLE_HARNESS === '1') ipcMain.handle('harness:force-reconnect', () => { chatStreamClient.forceReconnect('harness'); return { ok: true }; });

  const stopTerminals = require('./main/terminal_adapter').registerTerminalIpc(ipcMain, CONFIG, chatStreamClient);
  app.on('before-quit', stopTerminals);
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
  ipcMain.handle('specs:list', (_event, filter) => command(() => chatStreamClient.specsList(filter)));
  ipcMain.handle('specs:get', (_event, id) => command(() => chatStreamClient.specsGet(id)));
  ipcMain.handle('specs:drive', (_event, id, options, caller) => command(() => chatStreamClient.specsDrive(id, options, caller)));
  ipcMain.handle('specs:capabilities', () => command(() => chatStreamClient.specsCapabilities()));

  // Microphone service ownership stays external to the public desktop.
  ipcMain.handle('mic:start-server', () => probeMicServer(CONFIG));
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
      nodeIntegration: true,
      contextIsolation: false,
      sandbox: false,
      preload: path.join(__dirname, 'preload.js'),
    },
  });
  windows.add(window);
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event) => event.preventDefault());
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
if (CONFIG.chatStream?.url) chatStreamClient.init(CONFIG, (frame) => {
  for (const window of BrowserWindow.getAllWindows()) {
    if (!window.isDestroyed()) window.webContents.send('chat-stream:frame', frame);
  }
});
app.on('before-quit', () => chatStreamClient.destroy());

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
