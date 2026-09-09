function defaultNormalizeError(error) {
  return String(error?.message || error || 'Chat pop-out command failed');
}

function registerChatPopoutIpcHandlers(ipcMain, normalizeError = defaultNormalizeError, chatPopouts = null) {
  ipcMain.handle('chat-stream:chat-pop-out', async (_, args) => {
    try {
      if (!chatPopouts?.open) return { ok: false, error: 'chat_popout_unavailable' };
      return chatPopouts.open(args || {});
    } catch (error) {
      return { ok: false, error: normalizeError(error) };
    }
  });
  ipcMain.handle('chat-stream:chat-dock', async (_, args) => {
    try {
      if (!chatPopouts?.dock) return { ok: false, error: 'chat_popout_unavailable' };
      return chatPopouts.dock(args || {});
    } catch (error) {
      return { ok: false, error: normalizeError(error) };
    }
  });
}

module.exports = { registerChatPopoutIpcHandlers };
