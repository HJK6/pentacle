'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createClosedChatSlots } = require('../renderer/closed_chat_slots');
const { applyVersionedConnectionState } = require('../renderer/chat_stream_connection_state');

function fixture() {
  let now = 0, nextId = 0;
  const timers = new Map(), retired = [];
  const row = { host: 'local', session_name: 'chat', stream_id: 'local:chat', session_generation: 'one' };
  const state = { connected: true, epoch: 1, slots: [{ host: 'local', name: 'chat', generation: 1 }] };
  const guard = createClosedChatSlots({ readState: () => state,
    retire: (...args) => retired.push(args),
    setTimer: (fn, ms) => { const id = ++nextId; timers.set(id, { fn, due: now + ms }); return id; },
    clearTimer: id => timers.delete(id),
  });
  const inventory = sessions => guard.update({ sessions });
  const tick = ms => {
    now += ms;
    for (const [id, timer] of [...timers]) if (timer.due <= now) { timers.delete(id); timer.fn(); }
  };
  inventory([row]);
  return { row, state, guard, inventory, tick, retired, timers };
}

test('first missing deadline is 1500ms; repeated inventories do not postpone or duplicate retirement', () => {
  const f = fixture(); f.inventory([]); f.tick(800); f.inventory([]); f.tick(699);
  assert.deepEqual(f.retired, []); assert.equal(f.timers.size, 1);
  f.tick(1); assert.deepEqual(f.retired, [[0, 'local:chat']]);
  f.inventory([]); f.tick(2000); assert.equal(f.retired.length, 1);
});
test('reappearance cancels; a later absence receives a fresh deadline', () => {
  const f = fixture(); f.inventory([]); f.tick(1000); f.inventory([f.row]); f.tick(500);
  assert.deepEqual(f.retired, []); f.inventory([]); f.tick(1500); assert.equal(f.retired.length, 1);
});
test('disconnect and changed connection epoch require fresh authoritative membership', () => {
  for (const disconnect of [true, false]) {
    const f = fixture(); f.inventory([]); f.tick(1000);
    if (disconnect) f.state.connected = false;
    else f.state.epoch++;
    f.guard.update(); f.tick(1000); assert.deepEqual(f.retired, []);
    f.state.connected = true; f.guard.update(); f.tick(2000); assert.deepEqual(f.retired, []);
    f.inventory([]); f.tick(1499); assert.deepEqual(f.retired, []);
    f.tick(1); assert.equal(f.retired.length, 1);
  }
});
test('new daemon generation cancels an old absence and tracks the present replacement', () => {
  const f = fixture(); f.inventory([]); f.tick(1000);
  f.inventory([{ ...f.row, session_generation: 'two' }]); f.tick(1000); assert.deepEqual(f.retired, []);
  f.inventory([]); f.tick(1500); assert.deepEqual(f.retired, [[0, 'local:chat']]);
});
test('local rebind or detach invalidates callbacks even without a new inventory', () => {
  for (const replacement of [null, { host: 'local', name: 'chat', generation: 2 }]) {
    const f = fixture(); f.inventory([]); f.state.slots[0] = replacement; f.tick(1500);
    assert.deepEqual(f.retired, []);
  }
});
test('offline and hidden rows remain present; display labels cannot substitute for exact identity', () => {
  const f = fixture(); f.inventory([{ ...f.row, online: false, visibility: 'nested', title: 'different' }]);
  f.tick(2000); assert.deepEqual(f.retired, []);
  f.inventory([{ ...f.row, host: 'other', title: 'chat' }]); f.tick(1500);
  assert.deepEqual(f.retired, [[0, 'local:chat']]);
});
test('legacy, ambiguous and unmanaged identities are never automatically retired', () => {
  for (const rows of [[{ ...fixture().row, session_generation: undefined }],
    [{ ...fixture().row, stream_id: 'local:other' }], [fixture().row, fixture().row]]) {
    const f = fixture(); f.inventory(rows); f.inventory([]); f.tick(2000); assert.deepEqual(f.retired, []);
  }
  const f = fixture(); f.state.slots[0].bot = true; f.inventory([]); f.tick(2000); assert.deepEqual(f.retired, []);
});
test('status-only or malformed membership cannot start a timer; other slots are independent', () => {
  const f = fixture(); f.guard.update(); f.guard.update({ sessions: null }); f.tick(2000);
  assert.deepEqual(f.retired, []);
  const other = { ...f.row, session_name: 'other', stream_id: 'local:other' };
  f.state.slots.push({ host: 'local', name: 'other', generation: 1 });
  f.inventory([f.row, other]); f.inventory([other]); f.tick(1500);
  assert.deepEqual(f.retired, [[0, 'local:chat']]);
});
test('stale and unversioned inventory frames are rejected by the caller connection gate', () => {
  const f = fixture(); const connection = { connected: true, stateVersion: 2 };
  for (const version of [1, undefined]) {
    const payload = { connected: true, state_version: version, sessions: [] };
    if (applyVersionedConnectionState(connection, payload, () => {})) f.inventory(payload.sessions);
  }
  f.tick(2000); assert.deepEqual(f.retired, []);
});

test('initial unknown membership and attachment never observed in inventory are retained', () => {
  const f = fixture(); f.guard.forget(0);
  f.state.connected = false; f.guard.update(); f.state.connected = true;
  f.guard.update({ sessions: undefined }); f.tick(2000); assert.deepEqual(f.retired, []);
  f.inventory([]); f.tick(2000); assert.deepEqual(f.retired, []);
});
test('a canceled callback cannot consume a newly scheduled absence', () => {
  const f = fixture(); f.inventory([]);
  const stale = [...f.timers.values()][0].fn;
  f.state.slots[0] = { host: 'local', name: 'chat', generation: 2 };
  f.inventory([f.row]); f.inventory([]); stale();
  assert.deepEqual(f.retired, []); f.tick(1500); assert.deepEqual(f.retired, [[0, 'local:chat']]);
});
