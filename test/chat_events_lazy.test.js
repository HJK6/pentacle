const test = require('node:test');
const assert = require('node:assert/strict');

const {
  ensureChatEventsLoaded,
  refetchEventsForActiveChatSlots,
} = require('../renderer/chat_events_lazy');

function makeStreamState({ connected = true } = {}) {
  return {
    connected,
    events: [],
    drafts: {},
    sessions: [],
    eventsLoadedFor: new Set(),
  };
}

function makeCc(handler) {
  const calls = [];
  return {
    calls,
    requestStreamEvents(args) {
      calls.push(args);
      return Promise.resolve(typeof handler === 'function' ? handler(args, calls.length - 1) : { ok: true, count: 0 });
    },
  };
}

const silentLogger = { warn: () => {} };

test('ensureChatEventsLoaded fires RPC once per stream while in flight (idempotency)', () => {
  const streamState = makeStreamState();
  const cc = makeCc();
  assert.equal(ensureChatEventsLoaded(streamState, 'stream-a', cc, silentLogger), true);
  assert.equal(ensureChatEventsLoaded(streamState, 'stream-a', cc, silentLogger), false);
  assert.equal(ensureChatEventsLoaded(streamState, 'stream-a', cc, silentLogger), false);
  assert.equal(cc.calls.length, 1);
  assert.deepEqual(cc.calls[0], { streamId: 'stream-a' });
  assert.ok(streamState.eventsLoadedFor.has('stream-a'));
});

test('ensureChatEventsLoaded keeps the streamId in the set on success', async () => {
  const streamState = makeStreamState();
  const cc = makeCc(() => ({ ok: true, count: 3 }));
  ensureChatEventsLoaded(streamState, 'stream-a', cc, silentLogger);
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(streamState.eventsLoadedFor.has('stream-a'));
});

test('ensureChatEventsLoaded removes the streamId from the set on RPC error so future renders can retry', async () => {
  const streamState = makeStreamState();
  const cc = makeCc(() => ({ ok: false, error: 'stream not visible' }));
  ensureChatEventsLoaded(streamState, 'stream-a', cc, silentLogger);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(streamState.eventsLoadedFor.has('stream-a'), false);
});

test('ensureChatEventsLoaded removes the streamId on thrown promise so future renders can retry', async () => {
  const streamState = makeStreamState();
  const cc = {
    requestStreamEvents() { return Promise.reject(new Error('boom')); },
  };
  ensureChatEventsLoaded(streamState, 'stream-a', cc, silentLogger);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(streamState.eventsLoadedFor.has('stream-a'), false);
});

test('ensureChatEventsLoaded no-ops when disconnected — covers the cold-render-while-down case', () => {
  const streamState = makeStreamState({ connected: false });
  const cc = makeCc();
  assert.equal(ensureChatEventsLoaded(streamState, 'stream-a', cc, silentLogger), false);
  assert.equal(cc.calls.length, 0);
  assert.equal(streamState.eventsLoadedFor.has('stream-a'), false);
});

test('ensureChatEventsLoaded no-ops on empty streamId without touching the set or cc', () => {
  const streamState = makeStreamState();
  const cc = makeCc();
  assert.equal(ensureChatEventsLoaded(streamState, '', cc, silentLogger), false);
  assert.equal(ensureChatEventsLoaded(streamState, null, cc, silentLogger), false);
  assert.equal(ensureChatEventsLoaded(streamState, undefined, cc, silentLogger), false);
  assert.equal(cc.calls.length, 0);
  assert.equal(streamState.eventsLoadedFor.size, 0);
});

test('refetchEventsForActiveChatSlots fires ensureChatEventsLoaded only for chat-mode slots with a matched stream', () => {
  const streamState = makeStreamState();
  const cc = makeCc();
  // Slot 0: chat mode + matched stream → fire.
  // Slot 1: terminal mode → skip.
  // Slot 2: chat mode but botSlot → skip.
  // Slot 3: chat mode + no stream match → skip silently.
  const slots = [
    { name: 'a', hostId: 'local' },
    { name: 'b', hostId: 'local' },
    { name: 'c', hostId: 'local' },
    { name: 'd', hostId: 'local' },
  ];
  const slotViewModes = { 0: 'chat', 1: 'terminal', 2: 'chat', 3: 'chat' };
  const botSlots = { 2: true };
  const triggered = refetchEventsForActiveChatSlots({
    slots,
    slotViewModes,
    botSlots,
    streamState,
    cc,
    streamHostForHostId: () => 'hostb',
    findStreamSession: (_state, session) => {
      if (session.name === 'a') return { stream_id: 'stream-a' };
      return null;
    },
    logger: silentLogger,
  });
  assert.equal(triggered, 1);
  assert.equal(cc.calls.length, 1);
  assert.deepEqual(cc.calls[0], { streamId: 'stream-a' });
});

test('clearing eventsLoadedFor lets refetchEventsForActiveChatSlots re-backfill an already-loaded stream (the /cc-reconnect recovery)', () => {
  // Models restoreSlotsAfterReconnect: a summary resync snapshot wiped the open
  // transcript, but the stream is still in eventsLoadedFor (a socket-only drop
  // never fired the disconnect branch that clears it), so a plain refetch is a
  // no-op. Clearing the trackers first must let the backfill re-fire.
  const streamState = makeStreamState();
  const cc = makeCc();
  const args = {
    slots: [{ name: 'a', hostId: 'local' }],
    slotViewModes: { 0: 'chat' },
    botSlots: {},
    streamState,
    cc,
    streamHostForHostId: () => 'hostb',
    findStreamSession: () => ({ stream_id: 'stream-a' }),
    logger: silentLogger,
  };
  // First load marks the stream loaded.
  assert.equal(refetchEventsForActiveChatSlots(args), 1);
  assert.equal(cc.calls.length, 1);
  // Without clearing, a second refetch is a no-op (already loaded) — this is the
  // stuck state the wiped-but-connected slot would sit in.
  assert.equal(refetchEventsForActiveChatSlots(args), 0);
  assert.equal(cc.calls.length, 1);
  // The reconnect restore clears the trackers, so the backfill re-fires.
  streamState.eventsLoadedFor.clear();
  streamState.historyLoads = {};
  assert.equal(refetchEventsForActiveChatSlots(args), 1);
  assert.equal(cc.calls.length, 2);
  assert.deepEqual(cc.calls[1], { streamId: 'stream-a' });
});

test('refetchEventsForActiveChatSlots respects disconnect — no RPCs fire when streamState.connected is false', () => {
  const streamState = makeStreamState({ connected: false });
  const cc = makeCc();
  const triggered = refetchEventsForActiveChatSlots({
    slots: [{ name: 'a', hostId: 'local' }],
    slotViewModes: { 0: 'chat' },
    botSlots: {},
    streamState,
    cc,
    streamHostForHostId: () => 'hostb',
    findStreamSession: () => ({ stream_id: 'stream-a' }),
    logger: silentLogger,
  });
  assert.equal(triggered, 0);
  assert.equal(cc.calls.length, 0);
});

test('Disconnect-clear + reconnect-refire flow: events fetched twice across a reconnect cycle', async () => {
  // Simulates the applyChatStreamState lifecycle: streamState is shared
  // across connect → disconnect → reconnect transitions, and the caller is
  // responsible for clearing eventsLoadedFor on disconnect.
  const streamState = makeStreamState();
  const cc = makeCc(() => ({ ok: true, count: 0 }));
  const args = {
    slots: [{ name: 'a', hostId: 'local' }],
    slotViewModes: { 0: 'chat' },
    botSlots: {},
    streamState,
    cc,
    streamHostForHostId: () => 'hostb',
    findStreamSession: () => ({ stream_id: 'stream-a' }),
    logger: silentLogger,
  };

  // Connect → first lazy-load.
  refetchEventsForActiveChatSlots(args);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(cc.calls.length, 1);

  // Disconnect: caller clears the set (mirrors applyChatStreamState behavior).
  streamState.connected = false;
  streamState.eventsLoadedFor.clear();

  // Reconnect: refetch fires again for the still-active chat slot.
  streamState.connected = true;
  refetchEventsForActiveChatSlots(args);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(cc.calls.length, 2);
});

test('a late disconnected request cannot replace the new history result', async () => {
  const state = makeStreamState();
  const replies = [];
  const cc = { requestStreamEvents: () => new Promise(resolve => replies.push(resolve)) };
  const changes = [];
  ensureChatEventsLoaded(state, 'a', cc, silentLogger, { onChange: id => changes.push(id) });
  state.connected = false; state.eventsLoadedFor.clear(); state.historyLoads = {};
  state.connected = true;
  ensureChatEventsLoaded(state, 'a', cc, silentLogger, { onChange: id => changes.push(id) });
  replies[1]({ ok: true, count: 0 });
  await new Promise(resolve => setImmediate(resolve));
  replies[0]({ ok: false, error: 'old request' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(state.historyLoads.a.status, 'loaded');
  assert.deepEqual(changes, ['a']);
  assert.equal(state.eventsLoadedFor.has('a'), true);
});

test('failed history waits for explicit retry and zero-row success triggers a render', async () => {
  const state = makeStreamState(); const cc = makeCc((_args, n) => n ? { ok: true, count: 0 } : { ok: false, error: 'offline' });
  const changes = [];
  const options = { onChange: id => changes.push(id) };
  ensureChatEventsLoaded(state, 'a', cc, silentLogger, options);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ensureChatEventsLoaded(state, 'a', cc, silentLogger, options), false);
  assert.equal(cc.calls.length, 1);
  assert.equal(ensureChatEventsLoaded(state, 'a', cc, silentLogger, { ...options, retry: true }), true);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(state.historyLoads.a.status, 'loaded');
  assert.deepEqual(changes, ['a', 'a']);
});
