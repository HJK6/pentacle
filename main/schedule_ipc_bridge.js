'use strict';

function defaultNormalizeError(error) {
  if (error && typeof error === 'object') {
    return String(error.error || error.message || JSON.stringify(error));
  }
  return String(error || 'Chat stream command failed');
}

function registerScheduleIpcHandlers(ipcMain, chatStreamClient, normalizeError = defaultNormalizeError) {
  ipcMain.handle('chat-stream:schedule-get', async (_, scheduleId) => {
    try {
      const reply = await chatStreamClient.scheduleGet(scheduleId);
      return { ok: true, schedule: reply.schedule || reply };
    } catch (error) {
      return { ok: false, error: normalizeError(error) };
    }
  });
  ipcMain.handle('chat-stream:schedule-cancel', async (_, scheduleId) => {
    try {
      const reply = await chatStreamClient.scheduleCancel(scheduleId);
      return { ok: true, reply };
    } catch (error) {
      return { ok: false, error: normalizeError(error) };
    }
  });
  ipcMain.handle('chat-stream:schedule-run', async (_, scheduleId) => {
    try {
      const reply = await chatStreamClient.scheduleRun(scheduleId);
      return { ok: true, reply };
    } catch (error) {
      return { ok: false, error: normalizeError(error) };
    }
  });
  ipcMain.handle('chat-stream:schedule-reschedule', async (_, args) => {
    const { scheduleId, firesAtUtc } = args || {};
    try {
      const reply = await chatStreamClient.scheduleReschedule(scheduleId, firesAtUtc);
      return { ok: true, schedule: reply.schedule || null, reply };
    } catch (error) {
      return { ok: false, error: normalizeError(error) };
    }
  });
}

module.exports = {
  registerScheduleIpcHandlers,
};
