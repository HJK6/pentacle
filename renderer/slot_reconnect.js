'use strict';

// Slot restore after a browser<->host /cc websocket reconnect (web mode).
//
// When the browser's /cc socket drops (a backgrounded tab's suspended socket
// resuming, or a transient blip) the HOST stays up but tears down THIS
// connection's per-tab PTY attachments (server/ws_bridge.js removeSocket ->
// sender.destroy -> main/terminal_adapter.js). The reconnected socket is a
// fresh event.sender with no slot attachments, so the terminal feed freezes and
// pty:write goes nowhere until the operator closes/reopens the slot.
//
// The renderer keeps its xterm objects and its transport-level onPtyData/onData
// wiring across the reconnect (both live on the persistent window.cc transport),
// so simply re-issuing createPty for each attached slot re-binds the pane to the
// new socket: tmux redraws to the reattached client and input flows again — no
// xterm rebuild and no slot view-mode change. This mirrors what close/reopen
// does, minus the teardown.
//
// Desktop never reaches this: Electron's ipcRenderer transport does not drop, so
// preload.js exposes onReconnect as a no-op and app.js never calls this there.

function reattachTerminalSlotsAfterReconnect(args) {
  const { slots = [], botSlots = {}, terminals = {}, cc, logger = console } = args || {};
  if (!cc || typeof cc.createPty !== 'function') return 0;
  let reattached = 0;
  for (let slot = 0; slot < slots.length; slot += 1) {
    const session = slots[slot];
    const entry = terminals[slot];
    // Every attached, non-bot slot carries a live xterm (attachSession builds it
    // even under the chat/asset/status views), and its server-side attachment was
    // just destroyed with the old socket — so re-attach regardless of view mode.
    if (!session || botSlots[slot] || !entry || !entry.term) continue;
    const cols = entry.term.cols || 80;
    const rows = entry.term.rows || 24;
    try {
      Promise.resolve(cc.createPty(slot, session.name, session.hostId || 'local', cols, rows))
        .then(
          (paneId) => { if (paneId) session.paneId = paneId; },
          (error) => logger.warn?.('[reconnect] terminal re-attach failed:', slot, error?.message || error),
        );
      reattached += 1;
    } catch (error) {
      logger.warn?.('[reconnect] terminal re-attach threw:', slot, error?.message || error);
    }
  }
  return reattached;
}

// Guard an open chat slot's transcript against a reconnect resync wipe.
//
// The onReconnect re-pull applies get-state as a {type:'snapshot'}. Under
// events_mode:'summary' that snapshot carries no events, and the store reducer
// treats an empty events array as non-authoritative: it preserves existing
// events only for streams present in the snapshot's `sessions`. A summary
// snapshot's session list can momentarily OMIT an open stream (a filtered /
// nested / remote session, or an inventory gap right after a daemon reconnect),
// and then the reducer evicts that stream's transcript AND the composer loses
// its session detail — the blank "Loading chat…" + disabled send the operator
// saw, unrecoverable until close/reopen.
//
// So before pushing the resync snapshot to the store, carry forward the session
// summary (from the pre-reconnect store) for every OPEN chat slot whose bound
// stream the snapshot dropped. That keeps the stream in `survivingStreamIds` so
// its transcript is preserved and its composer stays live, without inventing any
// session the client did not already hold. Scoped to open slots only, so a
// genuinely-closed session is never resurrected.
// A daemon session row that is authoritatively closed must never be carried
// forward — doing so would resurrect a retiring session and suppress
// closed-slot retirement (the omission-vs-close race is otherwise invisible to
// this helper). Only genuinely-live rows are eligible to preserve.
function isClosedSessionRow(session) {
  return String(session?.status || '').toLowerCase() === 'closed'
    || !!session?.closed_at
    || session?.closed === true;
}

function preserveOpenSlotSessionsInSnapshot(snapshot, args) {
  if (!snapshot || typeof snapshot !== 'object') return snapshot;
  // Only augment a snapshot that already carries a real session inventory. If
  // `sessions` is missing / not an array, leave the snapshot untouched: turning
  // it into an array here would make the reducer treat it as an authoritative
  // (near-empty) inventory and evict unrelated sidebar/session/working state.
  if (!Array.isArray(snapshot.sessions)) return snapshot;
  const { slots = [], botSlots = {}, slotViewModes = {}, boundStreams = [], currentSessions = [] } = args || {};
  if (!Array.isArray(currentSessions) || currentSessions.length === 0) return snapshot;
  const snapshotSessions = snapshot.sessions;
  const present = new Set(snapshotSessions.map((s) => s && s.stream_id).filter(Boolean));
  const additions = [];
  for (let slot = 0; slot < slots.length; slot += 1) {
    if (!slots[slot] || botSlots[slot] || slotViewModes[slot] !== 'chat') continue;
    const streamId = boundStreams[slot];
    if (!streamId || present.has(streamId)) continue;
    const session = currentSessions.find((s) => s && s.stream_id === streamId && !isClosedSessionRow(s));
    if (session) { additions.push(session); present.add(streamId); }
  }
  if (additions.length === 0) return snapshot;
  return { ...snapshot, sessions: snapshotSessions.concat(additions) };
}

module.exports = { reattachTerminalSlotsAfterReconnect, preserveOpenSlotSessionsInSnapshot };
