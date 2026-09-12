const test = require('node:test');
const assert = require('node:assert/strict');

const { reattachTerminalSlotsAfterReconnect, preserveOpenSlotSessionsInSnapshot } = require('../renderer/slot_reconnect');

const silentLogger = { warn: () => {} };

function makeCc(handler) {
  const calls = [];
  return {
    calls,
    createPty(slot, name, hostId, cols, rows) {
      calls.push({ slot, name, hostId, cols, rows });
      return Promise.resolve(typeof handler === 'function' ? handler(calls.length - 1) : `%${slot + 1}`);
    },
  };
}

function term(cols = 100, rows = 30) { return { cols, rows }; }

test('reattachTerminalSlotsAfterReconnect re-issues createPty for every attached, non-bot slot with a live terminal', () => {
  const cc = makeCc();
  const slots = [
    { name: 'a', hostId: 'local' },   // slot 0: attached + term → re-attach
    null,                             // slot 1: empty → skip
    { name: 'c', hostId: 'merlin' },  // slot 2: bot → skip
    { name: 'd', hostId: 'local' },   // slot 3: attached but no term → skip
  ];
  const terminals = { 0: { term: term(120, 40) }, 2: { term: term() }, 3: null };
  const botSlots = { 2: true };
  const n = reattachTerminalSlotsAfterReconnect({ slots, botSlots, terminals, cc, logger: silentLogger });
  assert.equal(n, 1);
  assert.equal(cc.calls.length, 1);
  assert.deepEqual(cc.calls[0], { slot: 0, name: 'a', hostId: 'local', cols: 120, rows: 40 });
});

test('reattachTerminalSlotsAfterReconnect re-attaches multiple slots and defaults hostId/cols/rows', () => {
  const cc = makeCc();
  const slots = [{ name: 'a' }, { name: 'b', hostId: 'merlin' }];
  const terminals = { 0: { term: {} }, 1: { term: term(80, 24) } };
  const n = reattachTerminalSlotsAfterReconnect({ slots, botSlots: {}, terminals, cc, logger: silentLogger });
  assert.equal(n, 2);
  assert.deepEqual(cc.calls[0], { slot: 0, name: 'a', hostId: 'local', cols: 80, rows: 24 });
  assert.deepEqual(cc.calls[1], { slot: 1, name: 'b', hostId: 'merlin', cols: 80, rows: 24 });
});

test('reattachTerminalSlotsAfterReconnect records the returned pane id on the slot session', async () => {
  const cc = makeCc(() => '%42');
  const session = { name: 'a', hostId: 'local' };
  reattachTerminalSlotsAfterReconnect({ slots: [session], botSlots: {}, terminals: { 0: { term: term() } }, cc, logger: silentLogger });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(session.paneId, '%42');
});

test('reattachTerminalSlotsAfterReconnect swallows a rejected createPty (a failed slot never breaks the others)', async () => {
  const calls = [];
  const cc = {
    createPty(slot, name) {
      calls.push({ slot, name });
      return slot === 0 ? Promise.reject(new Error('attach failed')) : Promise.resolve('%2');
    },
  };
  const slots = [{ name: 'a', hostId: 'local' }, { name: 'b', hostId: 'local' }];
  const n = reattachTerminalSlotsAfterReconnect({ slots, botSlots: {}, terminals: { 0: { term: term() }, 1: { term: term() } }, cc, logger: silentLogger });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(n, 2);
  assert.equal(calls.length, 2);
});

test('reattachTerminalSlotsAfterReconnect is a no-op without a usable cc (desktop no-op parity / missing transport)', () => {
  assert.equal(reattachTerminalSlotsAfterReconnect({ slots: [{ name: 'a' }], terminals: { 0: { term: term() } }, cc: null }), 0);
  assert.equal(reattachTerminalSlotsAfterReconnect({ slots: [{ name: 'a' }], terminals: { 0: { term: term() } }, cc: {} }), 0);
  assert.equal(reattachTerminalSlotsAfterReconnect({}), 0);
});

// ── preserveOpenSlotSessionsInSnapshot ──────────────────────────────────────

test('preserveOpenSlotSessionsInSnapshot carries forward an open chat slot session the resync snapshot dropped', () => {
  const currentSessions = [{ stream_id: 'local:a', session_name: 'a' }, { stream_id: 'local:b', session_name: 'b' }];
  const snapshot = { events: [], sessions: [{ stream_id: 'local:b', session_name: 'b' }] }; // omits local:a
  const out = preserveOpenSlotSessionsInSnapshot(snapshot, {
    slots: [{ name: 'a', hostId: 'local' }],
    botSlots: {},
    slotViewModes: { 0: 'chat' },
    boundStreams: { 0: 'local:a' },
    currentSessions,
  });
  assert.deepEqual(out.sessions.map((s) => s.stream_id).sort(), ['local:a', 'local:b']);
  assert.deepEqual(out.events, []); // events untouched; the reducer preserves by surviving session
});

test('preserveOpenSlotSessionsInSnapshot returns the snapshot unchanged when the open stream is already present', () => {
  const snapshot = { events: [], sessions: [{ stream_id: 'local:a' }] };
  const out = preserveOpenSlotSessionsInSnapshot(snapshot, {
    slots: [{ name: 'a', hostId: 'local' }], botSlots: {}, slotViewModes: { 0: 'chat' },
    boundStreams: { 0: 'local:a' }, currentSessions: [{ stream_id: 'local:a' }],
  });
  assert.equal(out, snapshot); // same object, no copy
});

test('preserveOpenSlotSessionsInSnapshot ignores terminal-view, bot, and empty slots', () => {
  const currentSessions = [{ stream_id: 'local:t' }, { stream_id: 'local:bot' }];
  const snapshot = { events: [], sessions: [] };
  const out = preserveOpenSlotSessionsInSnapshot(snapshot, {
    slots: [{ name: 't', hostId: 'local' }, { name: 'bot', hostId: 'local' }, null],
    botSlots: { 1: true },
    slotViewModes: { 0: 'terminal', 1: 'chat', 2: 'chat' },
    boundStreams: { 0: 'local:t', 1: 'local:bot', 2: 'local:x' },
    currentSessions,
  });
  assert.equal(out, snapshot); // nothing added: slot0 terminal, slot1 bot, slot2 empty
});

test('preserveOpenSlotSessionsInSnapshot is a no-op without current sessions or a snapshot', () => {
  assert.deepEqual(preserveOpenSlotSessionsInSnapshot({ sessions: [] }, { currentSessions: [] }), { sessions: [] });
  assert.equal(preserveOpenSlotSessionsInSnapshot(null, {}), null);
});

test('preserveOpenSlotSessionsInSnapshot leaves a snapshot with a missing/non-array sessions untouched (no inventory eviction)', () => {
  const args = {
    slots: [{ name: 'a', hostId: 'local' }], botSlots: {}, slotViewModes: { 0: 'chat' },
    boundStreams: { 0: 'local:a' }, currentSessions: [{ stream_id: 'local:a' }],
  };
  const noSessions = { events: [] };                 // sessions omitted
  assert.equal(preserveOpenSlotSessionsInSnapshot(noSessions, args), noSessions);
  const objSessions = { events: [], sessions: { a: 1 } }; // malformed (object, not array)
  assert.equal(preserveOpenSlotSessionsInSnapshot(objSessions, args), objSessions);
});

test('preserveOpenSlotSessionsInSnapshot does NOT resurrect a genuinely-closed session', () => {
  const args = {
    slots: [{ name: 'a', hostId: 'local' }], botSlots: {}, slotViewModes: { 0: 'chat' },
    boundStreams: { 0: 'local:a' },
  };
  const snapshot = { events: [], sessions: [] };
  // status:closed, closed_at, and closed:true are each rejected.
  assert.equal(preserveOpenSlotSessionsInSnapshot(snapshot, { ...args, currentSessions: [{ stream_id: 'local:a', status: 'closed' }] }), snapshot);
  assert.equal(preserveOpenSlotSessionsInSnapshot(snapshot, { ...args, currentSessions: [{ stream_id: 'local:a', closed_at: 123 }] }), snapshot);
  assert.equal(preserveOpenSlotSessionsInSnapshot(snapshot, { ...args, currentSessions: [{ stream_id: 'local:a', closed: true }] }), snapshot);
  // a live row IS carried forward
  const out = preserveOpenSlotSessionsInSnapshot(snapshot, { ...args, currentSessions: [{ stream_id: 'local:a', status: 'open' }] });
  assert.deepEqual(out.sessions.map((s) => s.stream_id), ['local:a']);
});
