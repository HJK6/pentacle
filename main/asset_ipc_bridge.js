'use strict';

function defaultNormalizeError(error) {
  if (error && typeof error === 'object') {
    return String(error.error || error.message || JSON.stringify(error));
  }
  return String(error || 'Chat stream command failed');
}

function registerAssetIpcHandlers(ipcMain, chatStreamClient, normalizeError = defaultNormalizeError, assetPopouts = null) {
  ipcMain.handle('chat-stream:asset-list', async (_, args) => {
    try {
      const reply = await chatStreamClient.assetList(args || null);
      return { ok: true, ...reply };
    } catch (error) {
      return { ok: false, error: normalizeError(error) };
    }
  });
  ipcMain.handle('chat-stream:asset-get', async (_, args) => {
    try {
      const reply = await chatStreamClient.assetGet(args || {});
      return { ok: true, ...reply };
    } catch (error) {
      return { ok: false, error: normalizeError(error) };
    }
  });
  for (const [channel, method] of [
    ['chat-stream:asset-comments-list', 'assetCommentsList'],
    ['chat-stream:asset-comment-add', 'assetCommentAdd'],
    ['chat-stream:asset-comment-edit', 'assetCommentEdit'],
    ['chat-stream:asset-comment-delete', 'assetCommentDelete'],
    ['chat-stream:asset-comment-resolve', 'assetCommentResolve'],
    ['chat-stream:asset-review-set', 'assetReviewSet'],
    ['chat-stream:asset-comments-send-to-chat', 'assetCommentsSendToChat'],
    ['chat-stream:asset-delete', 'assetDelete'],
  ]) {
    ipcMain.handle(channel, async (_, args) => {
      try {
        const reply = await chatStreamClient[method](args || {});
        return { ok: true, ...reply };
      } catch (error) {
        return { ok: false, error: normalizeError(error) };
      }
    });
  }
  ipcMain.handle('chat-stream:asset-pop-out', async (_, args) => {
    try {
      if (!assetPopouts || typeof assetPopouts.open !== 'function') {
        return { ok: false, error: 'asset pop-out unavailable' };
      }
      return assetPopouts.open(args || {});
    } catch (error) {
      return { ok: false, error: normalizeError(error) };
    }
  });
  ipcMain.handle('chat-stream:asset-dock', async (_, args) => {
    try {
      if (!assetPopouts || typeof assetPopouts.dock !== 'function') {
        return { ok: false, error: 'asset dock unavailable' };
      }
      return assetPopouts.dock(args || {});
    } catch (error) {
      return { ok: false, error: normalizeError(error) };
    }
  });
}

module.exports = {
  registerAssetIpcHandlers,
};
