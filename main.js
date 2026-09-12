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
const chatStreamClient = require('./main/chat_stream_client');
const { createAssetPopoutManager } = require('./main/asset_popout_windows');
const { createCcHandlers, safeString, resultError } = require('./main/cc_handlers');
const assetPopouts = createAssetPopoutManager({ BrowserWindow, appRoot: __dirname, getMainWindow: () => [...windows][0] });

// The portable half of the window.cc surface lives in main/cc_handlers.js so the
// headless web host (server/) serves exactly the same handlers. Only the
// Electron-native channels are registered here, and they are listed in that
// module's WEB_LOCAL / WEB_UNSUPPORTED so the websocket dispatcher can refuse
// them with a reason.
const ccHandlers = createCcHandlers({ CONFIG, chatStreamClient, assetPopouts, configError, configWarnings });

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
  ipcMain.on('meeting:open', () => {});
  ipcMain.on('meeting:close', () => {});

  // Reload the requesting window from the main process. A renderer-initiated
  // location.reload() emits will-navigate, which the guard in createMainWindow
  // cancels; a main-process reload does not, so this is how the Settings
  // "Reload now" button applies changes.
  ipcMain.on('app:reload', (event) => event.sender.reload());

  const stopTerminals = ccHandlers.register(ipcMain);
  app.on('before-quit', stopTerminals);
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
