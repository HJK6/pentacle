'use strict';

// Membership is authoritative only after an accepted, connected full inventory.
// Connection epochs are not inventory sequence numbers: equal-epoch frames are
// applied in websocket order by the caller.
function createClosedChatSlots({ readState, retire, setTimer = setTimeout, clearTimer = clearTimeout, graceMs = 1500 }) {
  const bindings = new Map();
  let rows = [];
  let authorityEpoch;
  let hydrated = false;

  function cancel(entry) {
    if (entry?.timer != null) clearTimer(entry.timer);
    if (entry) entry.timer = null;
  }
  function forget(slot) {
    cancel(bindings.get(slot));
    bindings.delete(slot);
  }
  function update(payload = {}) {
    const current = readState();
    if (!current.connected || (hydrated && authorityEpoch !== current.epoch)) {
      hydrated = false;
      for (const entry of bindings.values()) cancel(entry);
    }
    const authoritative = current.connected && Array.isArray(payload.sessions);
    if (authoritative) {
      rows = payload.sessions;
      authorityEpoch = current.epoch;
      hydrated = true;
    }
    current.slots.forEach((slot, index) => {
      let entry = bindings.get(index);
      if (!slot || slot.bot || (entry && (entry.slotGeneration !== slot.generation
        || entry.host !== slot.host || entry.name !== slot.name))) {
        forget(index);
        entry = null;
      }
      if (!slot || slot.bot || !current.connected || !hydrated) return;
      // Never infer a retirement identity from a title, display name or fallback.
      const matches = rows.filter(row => row && row.host === slot.host && row.session_name === slot.name);
      if (matches.length) {
        const row = matches[0];
        if (matches.length !== 1 || row.stream_id !== `${slot.host}:${slot.name}`
          || typeof row.session_generation !== 'string' || !row.session_generation) {
          forget(index); // Ambiguous/legacy present row is unknown, never closed.
          return;
        }
        if (!entry || entry.streamId !== row.stream_id || entry.sessionGeneration !== row.session_generation) {
          forget(index);
          entry = { host: row.host, name: row.session_name, streamId: row.stream_id,
            sessionGeneration: row.session_generation, slotGeneration: slot.generation, timer: null };
          bindings.set(index, entry);
        }
        cancel(entry);
        return;
      }
      if (!entry || !authoritative || entry.timer != null) return;
      const epoch = authorityEpoch;
      entry.timer = setTimer(() => {
        // Also defend against slot changes made without an intervening inventory.
        update();
        if (bindings.get(index) !== entry || entry.timer == null || !hydrated
          || !readState().connected || readState().epoch !== epoch || authorityEpoch !== epoch) return;
        forget(index); // Consume before invoking DOM/attachment cleanup; exactly once.
        retire(index, entry.streamId);
      }, graceMs);
    });
  }
  return { update, forget };
}
module.exports = { createClosedChatSlots };
