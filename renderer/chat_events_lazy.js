'use strict';

// Per-stream lazy-load helpers for chat-stream events.
//
// The hello snapshot ships no events under events_mode:'summary'; chat-view
// slots fetch their stream's recent ring on demand via this module. The
// daemon's request_stream_events RPC is bridged into the renderer through
// `window.cc.requestStreamEvents`; on success the Electron-main client
// merges the ring into its own buffer and re-emits a snapshot payload that
// re-enters applyChatStreamPayload, repopulating `state.chatStream.events`.

function ensureChatEventsLoaded(streamState, streamId, cc, logger = console) {
  // Idempotent per stream until disconnect clears the set.
  const id = String(streamId || '');
  if (!id) return false;
  if (!streamState) return false;
  if (!streamState.eventsLoadedFor) return false;
  if (streamState.eventsLoadedFor.has(id)) return false;
  if (!streamState.connected) return false;
  if (!cc || typeof cc.requestStreamEvents !== 'function') return false;

  streamState.eventsLoadedFor.add(id);
  cc.requestStreamEvents({ streamId: id }).then((reply) => {
    if (!reply || reply.ok === false) {
      streamState.eventsLoadedFor.delete(id);
      logger.warn?.('[ChatStream] requestStreamEvents failed:', id, reply?.error);
    }
  }).catch((err) => {
    streamState.eventsLoadedFor.delete(id);
    logger.warn?.('[ChatStream] requestStreamEvents threw:', id, err?.message || err);
  });
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
    if (streamId && ensureChatEventsLoaded(streamState, streamId, cc, logger)) {
      triggered += 1;
    }
  }
  return triggered;
}

module.exports = {
  ensureChatEventsLoaded,
  refetchEventsForActiveChatSlots,
};
