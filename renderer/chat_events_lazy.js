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
  // An explicit retry re-requests any settled load, including a zero-row one.
  if (options.retry && loads[id] && loads[id].status !== 'loading') streamState.eventsLoadedFor.delete(id);
  if (streamState.eventsLoadedFor.has(id)) return false;
  if (loads[id]?.status === 'error' && !options.retry) return false;
  const request = { status: 'loading', error: '', attempt: options.attempt || 0 };
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

// A load that failed, or that completed while the store still holds no rows
// for the stream, is not final: the view would otherwise show only durable
// answered-question rows as if they were the conversation. Render-driven calls
// above keep the explicit-retry contract; this timer-driven retry is bounded.
const HISTORY_RETRY_DELAYS_MS = Object.freeze([1000, 2000, 4000, 8000, 16000]);

function historyLoadIncomplete(load, hasRows) {
  return !!load && (load.status === 'error' || (load.status === 'loaded' && !hasRows));
}

function scheduleHistoryRetry(streamState, streamId, cc, logger = console, options = {}) {
  const id = String(streamId || '');
  const load = streamState?.historyLoads?.[id];
  if (!id || !streamState.connected || load?.retryPending || !historyLoadIncomplete(load, options.hasRows)) return false;
  const attempt = load.attempt || 0;
  if (attempt >= HISTORY_RETRY_DELAYS_MS.length) {
    if (!load.exhausted) logger.info?.('[chat.history]', { subsystem: 'chat_history', bug_ref: 'spec_pentacle__web_chat_transcript_collapse_2026_09', streamId: id, event: 'retry_exhausted', attempt });
    load.exhausted = true;
    return false;
  }
  load.retryPending = true;
  logger.info?.('[chat.history]', { subsystem: 'chat_history', bug_ref: 'spec_pentacle__web_chat_transcript_collapse_2026_09', streamId: id, event: 'retry_scheduled', attempt: attempt + 1, delayMs: HISTORY_RETRY_DELAYS_MS[attempt] });
  const setTimer = options.setTimer || setTimeout;
  setTimer(() => {
    load.retryPending = false;
    // The slot may have moved on, rows may have arrived, or a reconnect may
    // have replaced this load; each makes the retry moot.
    if (streamState.historyLoads?.[id] !== load || !streamState.connected) return;
    if (typeof options.stillNeeded === 'function' && !options.stillNeeded(id)) return;
    ensureChatEventsLoaded(streamState, id, cc, logger, { retry: true, attempt: attempt + 1, onChange: options.onChange });
  }, HISTORY_RETRY_DELAYS_MS[attempt]);
  return true;
}

// The history status stays visible above durable answer groups until rows
// arrive or the bounded retry budget ends. An empty success is distinct from
// a failed request; both exhausted states offer an explicit fresh budget.
function chatHistoryStatus({ connected, load, hasRows, hasRendered }) {
  if (!connected) return { message: 'Reconnecting…', retry: false };
  if (load?.status === 'error') return load.exhausted
    ? { message: 'Messages could not be loaded.', retry: true }
    : { message: 'Loading messages…', retry: false };
  if (load?.status !== 'loaded') return { message: hasRows && hasRendered ? 'Syncing messages…' : 'Loading messages…', retry: false };
  if (hasRows) return { message: '', retry: false };
  return load.exhausted
    ? { message: 'No messages yet.', retry: true }
    : { message: 'Loading messages…', retry: false };
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
  scheduleHistoryRetry,
  chatHistoryStatus,
  HISTORY_RETRY_DELAYS_MS,
};
