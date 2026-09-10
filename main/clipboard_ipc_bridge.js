'use strict';

// Electron 44 exposes clipboard only in the main process and returns promises.
function registerClipboardIpc(ipcMain, clipboard) {
  ipcMain.handle('clipboard:read-text', () => clipboard.readText());
  ipcMain.handle('clipboard:write-text', (_event, text) => clipboard.writeText(String(text ?? '')));
}
module.exports = { registerClipboardIpc };
