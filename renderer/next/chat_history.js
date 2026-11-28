'use strict';

const lazy = require('../chat_events_lazy');

const FIRST_ROWS = 120;
const PAGE_ROWS = 48;

// A preview-only fallback row or an answered question is not conversation history.
function hasRows(detail, selectAll) {
  const ordinary = candidate => (candidate?.transcriptItems || []).some(item =>
    !String(item?.id || '').startsWith('fallback:') && item?.eventCase !== 'agent-question-answer');
  if (ordinary(detail)) return true;
  return detail?.remainingCount > 0 && ordinary(selectAll());
}

// New rows below the previous newest one. While the reader is further up, the
// window grows by this much so the rows on screen do not slide away.
function newTailRows(items, previousTail) {
  if (!previousTail) return 0;
  const index = (items || []).findIndex(item => item?.id === previousTail);
  return index < 0 ? 0 : items.length - 1 - index;
}

// History for the chats on screen, under the current web's rules: the shared
// store keeps a focused stream's events, a load starts once connected, a failed
// or incomplete load retries on a bounded backoff, and older pages come on request.
function createChatHistory({ cc, store, onChange, logger = console, setTimer }) {
  const state = { connected: false, eventsLoadedFor: new Set(), historyLoads: {}, historyPaging: {} };
  let focused = [];
  const select = (id, visibleCount) => store.selectSessionDetail(id, { visibleCount, includeDraft: false });
  const held = id => hasRows(select(id, FIRST_ROWS), () => select(id, 'all'));
  const forget = id => {
    // Older rows may be gone too, so the way back to them starts from the fresh read.
    delete state.historyPaging[id];
    if (state.historyLoads[id]?.status === 'loading') return;
    state.eventsLoadedFor.delete(id);
    delete state.historyLoads[id];
  };

  function restart() {
    state.eventsLoadedFor.clear();
    state.historyLoads = {};
    for (const id of focused) ensure(id);
  }

  function ensure(id) {
    if (!id || !state.connected) return;
    lazy.ensureChatEventsLoaded(state, id, cc, logger, { onChange });
    const load = state.historyLoads[id];
    if (!load || load.status === 'loading' || load.settled) return;
    const rows = held(id);
    if (load.status === 'loaded' && (rows || load.completeEmpty)) { load.settled = true; return; }
    lazy.scheduleHistoryRetry(state, id, cc, logger, {
      hasRows: rows, onChange, setTimer,
      stillNeeded: stream => focused.includes(stream) && (state.historyLoads[stream]?.status === 'error' || !held(stream)),
    });
  }

  return {
    // The chats on screen. A stream that leaves may lose its events to other
    // traffic, so one that comes back is read again.
    focus(ids) {
      const next = [...new Set(ids.filter(Boolean))];
      const changed = next.length !== focused.length || next.some((id, index) => id !== focused[index]);
      if (changed) {
        for (const id of next) if (!focused.includes(id)) forget(id);
        focused = next;
        store.setFocusedChatStreams?.(next);
      }
      for (const id of next) ensure(id);
    },
    // A new connection may have missed events, so every load starts over.
    setConnected(connected) {
      if (state.connected === !!connected) return;
      state.connected = !!connected;
      restart();
    },
    // The daemon reconnected without the page ever seeing it go away (its
    // restart grace hides the disconnect), so the open chats are read again.
    reconnected() {
      if (state.connected) restart();
    },
    retry(id) {
      if (!id || !state.connected) return;
      lazy.ensureChatEventsLoaded(state, id, cc, logger, { retry: true, onChange });
    },
    status(id) {
      if (state.connected && (state.historyLoads[id]?.settled || state.historyLoads[id]?.completeEmpty)) return { message: '', retry: false };
      const rows = held(id);
      const status = lazy.chatHistoryStatus({ connected: state.connected, load: state.historyLoads[id], hasRows: rows, hasRendered: true });
      return rows && !status.retry ? { message: '', retry: false } : status;
    },
    hasOlder: id => lazy.historyHasOlder(state, id),
    loadingOlder: id => !!state.historyPaging[id]?.loading,
    loadOlder: (id, hasNewRows, stillWanted) => lazy.requestOlderHistory(state, id, cc, logger, {
      onChange, hasNewRows, expand: stream => store.setHistoryExpanded?.(stream),
      stillWanted: stream => focused.includes(stream) && (!stillWanted || stillWanted(stream)),
    }),
  };
}

module.exports = { FIRST_ROWS, PAGE_ROWS, hasRows, newTailRows, createChatHistory };
