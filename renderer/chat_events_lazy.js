'use strict';

// Per-stream lazy-load helpers for chat-stream events.
//
// The hello snapshot ships no events under events_mode:'summary'; chat-view
// slots fetch their stream's recent ring on demand via this module. The
// daemon's request_stream_events RPC is bridged into the renderer through
// `window.cc.requestStreamEvents`; on success the Electron-main client
// merges the ring into its own buffer and re-emits a snapshot payload that
// re-enters applyChatStreamPayload, repopulating `state.chatStream.events`.

function ensureChatEventsLoaded(streamState, streamId, cc, logger = console, options = {}) {
  const id = String(streamId || '');
  if (!id || !streamState?.eventsLoadedFor || !streamState.connected) return false;
  if (!cc || typeof cc.requestStreamEvents !== 'function') return false;
  const loads = streamState.historyLoads || (streamState.historyLoads = {});
  if (streamState.eventsLoadedFor.has(id)) return false;
  if (loads[id]?.status === 'error' && !options.retry) return false;
  const request = { status: 'loading', error: '' };
  loads[id] = request;
  streamState.eventsLoadedFor.add(id);
  const complete = (reply) => {
    // Disconnect/retry replaces this identity. An older result cannot finish a new request.
    if (!streamState.connected || streamState.historyLoads?.[id] !== request) return;
    if (!reply || reply.ok === false) {
      request.status = 'error';
      request.error = String(reply?.error || 'History request failed');
      streamState.eventsLoadedFor.delete(id);
      logger.warn?.('[ChatStream] requestStreamEvents failed:', id, request.error);
    } else {
      request.status = 'loaded';
    }
    options.onChange?.(id);
  };
  try {
    Promise.resolve(cc.requestStreamEvents({ streamId: id }))
      .then(complete, error => complete({ ok: false, error: error?.message || error }));
  } catch (error) {
    complete({ ok: false, error: error?.message || error });
  }
  return true;
}

function refetchEventsForActiveChatSlots(args) {
  // `args.findStreamSession` is injected so this module stays independent
  // of chat_ui_state's module shape; the caller supplies the same lookup
  // it uses elsewhere in the renderer.
  const {
    slots = [],
    slotViewModes = {},
    botSlots = {},
    streamState,
    cc,
    streamHostForHostId,
    findStreamSession,
    logger = console,
    onChange,
  } = args || {};
  if (!streamState) return 0;
  let triggered = 0;
  for (let slot = 0; slot < slots.length; slot++) {
    if (!slots[slot] || botSlots[slot]) continue;
    if (slotViewModes[slot] !== 'chat') continue;
    const session = slots[slot];
    const host = typeof streamHostForHostId === 'function' ? streamHostForHostId(session.hostId) : session.hostId;
    const streamSession = typeof findStreamSession === 'function'
      ? findStreamSession(streamState, session, host)
      : null;
    const streamId = streamSession?.stream_id;
    if (streamId && ensureChatEventsLoaded(streamState, streamId, cc, logger, { onChange })) {
      triggered += 1;
    }
  }
  return triggered;
}

module.exports = {
  ensureChatEventsLoaded,
  refetchEventsForActiveChatSlots,
};
