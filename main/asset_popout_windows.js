'use strict';

const path = require('path');

function assetIdFromArgs(args) {
  return String(args?.asset_id || args?.assetId || '').trim();
}

function streamKey(args) {
  return String(args?.stream_id || args?.streamId || args?.asset?.session_key?.stream_id || '').trim();
}

function assetKey(args) {
  const assetId = assetIdFromArgs(args);
  if (!assetId) return '';
  const specId = String(args?.spec_id || args?.specId || args?.asset?.spec_id || '').trim();
  if (specId) return `spec:${specId}:${assetId}`;
  const streamId = streamKey(args);
  if (streamId) return `stream:${streamId}:${assetId}`;
  const sessionKey = args?.asset?.session_key || args?.session_key || {};
  const host = String(sessionKey.host || args?.host || '').trim();
  const sessionName = String(sessionKey.session_name || args?.session_name || args?.sessionName || '').trim();
  if (host && sessionName) return `session:${host}:${sessionName}:${assetId}`;
  return `asset:${assetId}`;
}

function sessionMatches(args, session) {
  if (!args || !session) return false;
  const sessionKey = args.asset?.session_key || args.session_key || {};
  const streamId = streamKey(args);
  const candidateStream = String(session.stream_id || session.streamId || '').trim();
  if (streamId && candidateStream && streamId === candidateStream) return true;
  const host = String(session.host || '').trim();
  const sessionName = String(session.session_name || session.sessionName || '').trim();
  return !!host
    && !!sessionName
    && String(sessionKey.host || args.host || '').trim() === host
    && String(sessionKey.session_name || args.session_name || args.sessionName || '').trim() === sessionName;
}

function createAssetPopoutManager({
  BrowserWindow,
  appRoot,
  preloadPath,
  getMainWindow,
  backgroundColor = '#0c1310',
  width = 980,
  height = 760,
} = {}) {
  if (!BrowserWindow) throw new Error('BrowserWindow is required');
  const byKey = new Map();
  const root = appRoot || path.resolve(__dirname, '..');
  const preload = preloadPath || path.join(root, 'preload.js');

  function liveEntry(key) {
    const entry = byKey.get(key);
    if (!entry) return null;
    if (entry.window && typeof entry.window.isDestroyed === 'function' && entry.window.isDestroyed()) {
      byKey.delete(key);
      return null;
    }
    return entry;
  }

  function liveEntryForArgs(args) {
    const key = assetKey(args);
    if (!key) return null;
    const exact = liveEntry(key);
    if (exact) return exact;
    if (key.startsWith('asset:')) {
      const assetId = assetIdFromArgs(args);
      const matches = Array.from(byKey.values()).filter((entry) => entry.assetId === assetId);
      return matches.length === 1 ? matches[0] : null;
    }
    return null;
  }

  function sendInit(entry) {
    if (!entry?.window || entry.window.isDestroyed?.()) return;
    entry.window.webContents?.send?.('asset-popout:init', entry.args);
  }

  function open(args = {}) {
    const assetId = assetIdFromArgs(args);
    if (!assetId) return { ok: false, error: 'asset_id_required' };
    const key = assetKey(args);
    const existing = liveEntry(key);
    if (existing) {
      existing.window.show?.();
      existing.window.focus?.();
      sendInit(existing);
      return { ok: true, reused: true, windowId: existing.window.id };
    }
    const window = new BrowserWindow({
      width,
      height,
      minWidth: 640,
      minHeight: 520,
      title: args.title || args.asset?.title || assetId,
      backgroundColor,
      webPreferences: {
        preload,
        contextIsolation: false,
        nodeIntegration: true,
      },
    });
    const entry = { key, assetId, window, args: { ...args, asset_id: assetId } };
    byKey.set(key, entry);
    window.on?.('closed', () => {
      if (byKey.get(key)?.window === window) byKey.delete(key);
    });
    window.webContents?.on?.('did-finish-load', () => sendInit(entry));
    window.loadFile(path.join(root, 'renderer', 'asset_popout.html'));
    return { ok: true, reused: false, windowId: window.id };
  }

  function dock(args = {}) {
    const assetId = assetIdFromArgs(args);
    if (!assetId) return { ok: false, error: 'asset_id_required' };
    const entry = liveEntryForArgs(args);
    const dockArgs = entry?.args ? { ...entry.args, ...args } : { ...args, asset_id: assetId };
    const mainWindow = typeof getMainWindow === 'function' ? getMainWindow() : null;
    if (mainWindow && !mainWindow.isDestroyed?.()) {
      mainWindow.webContents?.send?.('asset:dock', dockArgs);
    }
    if (entry?.window && !entry.window.isDestroyed?.()) entry.window.close?.();
    if (entry?.key) byKey.delete(entry.key);
    return { ok: true, docked: true };
  }

  function broadcast(channel, payload) {
    const sessions = channel === 'chat-stream:frame'
      && payload?.type === 'session.inventory'
      && Array.isArray(payload.sessions) ? payload.sessions : null;
    for (const entry of Array.from(byKey.values())) {
      if (entry.window?.isDestroyed?.()) {
        byKey.delete(entry.key);
        continue;
      }
      if (sessions && !sessions.some((session) => sessionMatches(entry.args, session))) {
        entry.window.close?.();
        byKey.delete(entry.key);
        continue;
      }
      entry.window.webContents?.send?.(channel, payload);
    }
  }

  function closeForSession(session) {
    for (const entry of Array.from(byKey.values())) {
      if (!sessionMatches(entry.args, session)) continue;
      if (!entry.window?.isDestroyed?.()) entry.window.close?.();
      byKey.delete(entry.key);
    }
  }

  function closeAll() {
    for (const entry of Array.from(byKey.values())) {
      if (!entry.window?.isDestroyed?.()) entry.window.close?.();
    }
    byKey.clear();
  }

  return {
    open,
    dock,
    broadcast,
    closeForSession,
    closeAll,
    size: () => byKey.size,
  };
}

module.exports = {
  createAssetPopoutManager,
};
