'use strict';

// Browser-local popout protocol. Only a same-origin window opened by this page
// can dock into it; no message can mutate a backing chat or asset lifecycle.
const PROTOCOL = 'pentacle.browser-popout.v1';
const CHAT_PARAM = 'pentacle-chat-popout';
const ASSET_PARAM = 'pentacle-asset-popout';

function chatContext(args = {}) {
  const stream_id = String(args.stream_id || '').trim();
  const host = String(args.host || '').trim();
  const session_name = String(args.session_name || '').trim();
  if (!stream_id || !host || !session_name) return null;
  return { stream_id, host, desktop_host: String(args.desktop_host || host),
    session_name, title: String(args.title || session_name).slice(0, 200),
    assistant_source_stream_id: String(args.assistant_source_stream_id || ''),
    assistant_generation: String(args.assistant_generation || '') };
}

function assetContext(args = {}) {
  const source = args.asset && typeof args.asset === 'object' ? args.asset : args;
  const asset_id = String(args.asset_id || args.assetId || source.asset_id || '').trim();
  const stream_id = String(args.stream_id || args.streamId || source.session_key?.stream_id || '').trim();
  const spec_id = String(args.spec_id || args.specId || source.spec_id || '').trim();
  const host = String(args.host || source.session_key?.host || '').trim();
  const session_name = String(args.session_name || source.session_key?.session_name || '').trim();
  if (!asset_id || (!stream_id && !spec_id && !(host && session_name))) return null;
  const asset = { asset_id, title: String(source.title || args.title || asset_id).slice(0, 200),
    content_type: String(source.content_type || source.contentType || 'unknown'),
    review_status: String(source.review_status || source.reviewStatus || 'pending_review'),
    spec_id: spec_id || null, updated_at: String(source.updated_at || ''),
    session_key: { host, session_name, stream_id } };
  return { asset_id, stream_id, spec_id: spec_id || null, host, session_name,
    title: asset.title, asset };
}

function keyFor(kind, context) {
  if (!context) return '';
  if (kind === 'chat') return `chat:${context.stream_id}`;
  if (context.spec_id) return `asset:spec:${context.spec_id}:${context.asset_id}`;
  if (context.stream_id) return `asset:stream:${context.stream_id}:${context.asset_id}`;
  return `asset:session:${context.host}:${context.session_name}:${context.asset_id}`;
}

function parseAssetContext(search) {
  const raw = new URLSearchParams(search || '').get(ASSET_PARAM);
  if (!raw || raw.length > 4096) return null;
  try { return assetContext(JSON.parse(raw)); } catch { return null; }
}

function sameIdentity(kind, a, b) {
  if (!a || !b || keyFor(kind, a) !== keyFor(kind, b)) return false;
  return kind === 'chat'
    ? a.host === b.host && a.session_name === b.session_name
      && a.assistant_source_stream_id === b.assistant_source_stream_id
      && a.assistant_generation === b.assistant_generation
    : a.asset_id === b.asset_id && a.stream_id === b.stream_id
      && a.spec_id === b.spec_id && a.host === b.host && a.session_name === b.session_name;
}

function createBrowserPopouts({ win, location, chatPopoutContext = null,
  assetPopoutContext = null, toast = () => {} } = {}) {
  if (!win || !location) throw new Error('browser window and location required');
  const registry = new Map();
  const childKind = chatPopoutContext ? 'chat' : assetPopoutContext ? 'asset' : null;
  const childContext = chatPopoutContext ? chatContext(chatPopoutContext)
    : assetPopoutContext ? assetContext(assetPopoutContext) : null;
  const childKey = childKind ? keyFor(childKind, childContext) : '';
  const dockHandlers = new Map();
  const pendingDocks = new Map();
  let hydrateHandler = null;
  let waitingHydrate = null;
  const origin = location.origin;
  const windowPrefix = `pentacle_${Math.random().toString(36).slice(2, 10)}`;
  let nextWindow = 0;

  function returnPath() {
    if (!win.document?.body || win.document.getElementById('pentacle-popout-return')) return;
    const link = win.document.createElement('a');
    link.id = 'pentacle-popout-return';
    link.href = '/';
    link.textContent = 'Open Pentacle main window';
    link.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;padding:9px 13px;border-radius:8px;background:#173725;color:#fff;font:14px system-ui,sans-serif';
    win.document.body.appendChild(link);
  }

  function open(kind, args) {
    const context = kind === 'chat' ? chatContext(args) : assetContext(args);
    if (!context) return Promise.resolve({ ok: false, error: 'popout_identity_required' });
    const key = keyFor(kind, context);
    const existing = registry.get(key);
    if (existing?.handle && !existing.handle.closed) {
      try {
        if (existing.handle.location.origin === origin) {
          existing.handle.focus?.();
          return Promise.resolve({ ok: true, reused: true, key });
        }
      } catch { /* navigated away; open the canonical served page again */ }
    }
    registry.delete(key);
    const url = new URL(kind === 'chat' ? '/' : '/asset-popout.html', location.href);
    url.searchParams.set(kind === 'chat' ? CHAT_PARAM : ASSET_PARAM, JSON.stringify(context));
    // This call must execute before any await: popup blockers require the
    // actual operator click gesture, not a later websocket callback.
    const handle = win.open(url.href, `${windowPrefix}_${kind}_${++nextWindow}`, 'width=1000,height=780');
    if (!handle) {
      toast('Popups are blocked. Your chat or asset remains open here.');
      return Promise.resolve({ ok: false, error: 'popup_blocked', fallback: 'original_view' });
    }
    registry.set(key, { kind, context, handle, transferState: args?.transfer_state || null });
    return Promise.resolve({ ok: true, reused: false, key });
  }

  function dock(kind, args) {
    const context = kind === 'chat' ? chatContext(args) : assetContext(args);
    if (!childKind || childKind !== kind || !sameIdentity(kind, childContext, context)) {
      return Promise.resolve({ ok: false, error: 'popout_identity_mismatch' });
    }
    if (!win.opener || win.opener.closed) {
      returnPath();
      return Promise.resolve({ ok: false, error: 'opener_unavailable' });
    }
    const requestId = win.crypto?.randomUUID?.() || `dock-${Date.now()}-${Math.random()}`;
    return new Promise(resolve => {
      const timer = setTimeout(() => {
        pendingDocks.delete(requestId);
        returnPath();
        resolve({ ok: false, error: 'opener_unavailable' });
      }, 1500);
      pendingDocks.set(requestId, { resolve, timer });
      try {
        win.opener.postMessage({ protocol: PROTOCOL, type: 'dock', kind, key: childKey,
          context, requestId, transferState: args?.transfer_state || null }, origin);
      } catch {
        clearTimeout(timer); pendingDocks.delete(requestId); returnPath();
        resolve({ ok: false, error: 'opener_unavailable' });
      }
    });
  }

  function announceReady() {
    if (!childKind) return false;
    if (!win.opener || win.opener.closed) { returnPath(); return false; }
    try {
      win.opener.postMessage({ protocol: PROTOCOL, type: 'ready', kind: childKind,
        key: childKey, context: childContext }, origin);
      return true;
    } catch { return false; }
  }

  function onMessage(event) {
    const data = event?.data;
    if (event.origin !== origin || data?.protocol !== PROTOCOL || !['chat', 'asset'].includes(data.kind)) return;
    if (childKind) {
      if (event.source !== win.opener || data.kind !== childKind || data.key !== childKey
        || !sameIdentity(childKind, childContext, data.context)) return;
      if (data.type === 'hydrate') {
        waitingHydrate = data.transferState || null;
        if (hydrateHandler) hydrateHandler(waitingHydrate);
      } else if (data.type === 'dock-ack') {
        const pending = pendingDocks.get(data.requestId);
        if (!pending) return;
        clearTimeout(pending.timer); pendingDocks.delete(data.requestId);
        if (data.ok) {
          pending.resolve({ ok: true, docked: true });
          win.close?.();
        } else {
          returnPath(); pending.resolve({ ok: false, error: 'dock_target_unavailable' });
        }
      }
      return;
    }
    const entry = registry.get(data.key);
    if (!entry || event.source !== entry.handle || data.kind !== entry.kind
      || !sameIdentity(entry.kind, entry.context, data.context)) return;
    if (data.type === 'ready') {
      entry.handle.postMessage({ protocol: PROTOCOL, type: 'hydrate', kind: entry.kind,
        key: data.key, context: entry.context, transferState: entry.transferState }, origin);
    } else if (data.type === 'dock') {
      Promise.resolve().then(() => dockHandlers.get(entry.kind)?.({
        ...data.context, transfer_state: data.transferState,
      })).then((result) => {
        const ok = result === true;
        entry.handle.postMessage({ protocol: PROTOCOL, type: 'dock-ack', kind: entry.kind,
          key: data.key, context: entry.context, requestId: data.requestId, ok }, origin);
        if (ok) registry.delete(data.key);
      }).catch(() => {
        entry.handle.postMessage({ protocol: PROTOCOL, type: 'dock-ack', kind: entry.kind,
          key: data.key, context: entry.context, requestId: data.requestId, ok: false }, origin);
      });
    }
  }

  win.addEventListener('message', onMessage);
  return {
    chatContext: chatPopoutContext, assetContext: assetPopoutContext,
    openChat: args => open('chat', args), openAsset: args => open('asset', args),
    dockChat: args => dock('chat', args), dockAsset: args => dock('asset', args),
    announceReady,
    onHydrate(handler) { hydrateHandler = handler; if (waitingHydrate) handler(waitingHydrate); },
    onChatDock(handler) { dockHandlers.set('chat', handler); },
    onAssetDock(handler) { dockHandlers.set('asset', handler); },
    size: () => registry.size,
    destroy() { win.removeEventListener('message', onMessage); for (const pending of pendingDocks.values()) clearTimeout(pending.timer); pendingDocks.clear(); registry.clear(); },
  };
}

module.exports = { createBrowserPopouts, chatContext, assetContext, keyFor, parseAssetContext, sameIdentity, PROTOCOL };
