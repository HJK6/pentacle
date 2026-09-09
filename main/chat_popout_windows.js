const path = require('path');

function contextFromArgs(args = {}) {
  const streamId = String(args.stream_id || '').trim();
  const host = String(args.host || '').trim();
  const sessionName = String(args.session_name || '').trim();
  if (!streamId || !host || !sessionName) return null;
  return { stream_id: streamId, host, desktop_host: String(args.desktop_host || host), session_name: sessionName, title: String(args.title || sessionName) };
}

function sessionMatches(context, session = {}) {
  return context?.host === String(session.host || session.host_id || '').trim()
    && context?.session_name === String(session.session_name || session.name || '').trim();
}

function createChatPopoutManager({ BrowserWindow, appRoot, preloadPath, getMainWindow, backgroundColor, width = 1000, height = 780 } = {}) {
  if (!BrowserWindow) throw new Error('BrowserWindow is required');
  const byStream = new Map();
  const root = appRoot || path.resolve(__dirname, '..');
  const preload = preloadPath || path.join(root, 'preload.js');

  function liveEntry(streamId) {
    const entry = byStream.get(streamId);
    if (entry?.window?.isDestroyed?.()) {
      byStream.delete(streamId);
      return null;
    }
    return entry || null;
  }

  function open(args = {}) {
    const context = contextFromArgs(args);
    if (!context) return { ok: false, error: 'stream_id_host_and_session_name_required' };
    const existing = liveEntry(context.stream_id);
    if (existing) {
      existing.window.show?.();
      existing.window.focus?.();
      return { ok: true, reused: true, windowId: existing.window.id };
    }
    const encodedContext = encodeURIComponent(JSON.stringify(context));
    const window = new BrowserWindow({
      width,
      height,
      minWidth: 640,
      minHeight: 520,
      title: context.title,
      backgroundColor,
      webPreferences: {
        preload,
        contextIsolation: false,
        nodeIntegration: true,
        additionalArguments: [`--pentacle-chat-popout=${encodedContext}`],
      },
    });
    const entry = { context, window };
    byStream.set(context.stream_id, entry);
    window.on?.('closed', () => {
      if (byStream.get(context.stream_id)?.window === window) byStream.delete(context.stream_id);
    });
    window.loadFile(path.join(root, 'renderer', 'index.html'));
    return { ok: true, reused: false, windowId: window.id };
  }

  function dock(args = {}) {
    const streamId = String(args.stream_id || '').trim();
    if (!streamId) return { ok: false, error: 'stream_id_required' };
    const entry = liveEntry(streamId);
    const context = { ...(entry?.context || {}), ...args, stream_id: streamId };
    const mainWindow = typeof getMainWindow === 'function' ? getMainWindow() : null;
    if (mainWindow && !mainWindow.isDestroyed?.()) mainWindow.webContents?.send?.('chat:popout-dock', context);
    if (entry?.window && !entry.window.isDestroyed?.()) entry.window.close?.();
    byStream.delete(streamId);
    return { ok: true, docked: true };
  }

  function broadcast(channel, payload) {
    const sessions = channel === 'chat-stream:frame'
      && payload?.type === 'session.inventory'
      && Array.isArray(payload.sessions) ? payload.sessions : null;
    for (const [streamId, entry] of byStream) {
      if (entry.window?.isDestroyed?.()) byStream.delete(streamId);
      else if (sessions && !sessions.some((session) => sessionMatches(entry.context, session))) {
        entry.window.close?.();
        byStream.delete(streamId);
      } else entry.window.webContents?.send?.(channel, payload);
    }
  }

  function closeForSession(session) {
    for (const [streamId, entry] of byStream) {
      if (!sessionMatches(entry.context, session)) continue;
      if (!entry.window?.isDestroyed?.()) entry.window.close?.();
      byStream.delete(streamId);
    }
  }

  function closeAll() {
    for (const entry of byStream.values()) if (!entry.window?.isDestroyed?.()) entry.window.close?.();
    byStream.clear();
  }

  return { open, dock, broadcast, closeForSession, closeAll, size: () => byStream.size };
}

module.exports = { createChatPopoutManager, contextFromArgs, sessionMatches };
