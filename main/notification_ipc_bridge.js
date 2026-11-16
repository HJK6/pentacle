'use strict';

function defaultNormalizeError(error) {
  if (error && typeof error === 'object') {
    return String(error.error || error.message || JSON.stringify(error));
  }
  return String(error || 'Chat stream command failed');
}

function registerNotificationIpcHandlers(ipcMain, chatStreamClient, normalizeError = defaultNormalizeError) {
  ipcMain.handle('chat-stream:prompt-list', async (_, args) => {
    try {
      const reply = await chatStreamClient.promptList(args || null);
      return { ok: true, ...reply };
    } catch (error) {
      return { ok: false, error: normalizeError(error) };
    }
  });
  ipcMain.handle('chat-stream:notification-list', async (_, args) => {
    try {
      const reply = await chatStreamClient.notificationList(args || null);
      return { ok: true, ...reply };
    } catch (error) {
      return { ok: false, error: normalizeError(error) };
    }
  });
  ipcMain.handle('chat-stream:notification-resolve', async (_, args) => {
    const a = args || {};
    try {
      const payload = {
        notification_id: a.notificationId,
        action_kind: a.actionKind,
        choice: a.choice,
        selections: a.selections,
        text: a.text,
        custom_text: a.custom_text !== undefined ? a.custom_text : a.customText,
        note: a.note,
        submit: a.submit,
        by: a.by || 'operator',
        spawn: a.spawn,
      };
      if (a.action_id !== undefined) payload.action_id = a.action_id;
      else if (a.actionId !== undefined) payload.action_id = a.actionId;
      const reply = await chatStreamClient.notificationResolve(payload);
      // reply carries `notification` (the updated record).
      return { ok: true, ...reply };
    } catch (error) {
      return { ok: false, error: normalizeError(error) };
    }
  });
  ipcMain.handle('chat-stream:notification-create', async (_, args) => {
    try {
      const reply = await chatStreamClient.notificationCreate(args || {});
      return { ok: true, ...reply };
    } catch (error) {
      return { ok: false, error: normalizeError(error) };
    }
  });
}

module.exports = {
  registerNotificationIpcHandlers,
};
