const test = require('node:test');
const assert = require('node:assert/strict');

const {
  ensureChatEventsLoaded,
  refetchEventsForActiveChatSlots,
  scheduleHistoryRetry,
  chatHistoryStatus,
  HISTORY_RETRY_DELAYS_MS,
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

// L6: a load that failed, or that left the store without rows for the stream,
// is not final. Timer-driven retries follow a bounded schedule; render-driven
// calls keep the explicit-retry contract above.
function fakeTimers() {
  const queue = [];
  return { queue, setTimer: (fn, ms) => { queue.push({ fn, ms }); return queue.length; }, runAll() { const due = queue.splice(0); due.forEach(t => t.fn()); return due.map(t => t.ms); } };
}

test('zero-row and failed loads retry on the bounded schedule, then stop', async () => {
  for (const reply of [{ ok: true, count: 0 }, { ok: false, error: 'unknown_session' }]) {
    const state = makeStreamState(); const cc = makeCc(() => reply); const timers = fakeTimers();
    const retry = { hasRows: false, stillNeeded: () => true, setTimer: timers.setTimer };
    ensureChatEventsLoaded(state, 'a', cc, silentLogger);
    await new Promise(resolve => setImmediate(resolve));
    const delays = [];
    for (let i = 0; i < 8; i += 1) {
      scheduleHistoryRetry(state, 'a', cc, silentLogger, retry);
      delays.push(...timers.runAll());
      await new Promise(resolve => setImmediate(resolve));
    }
    assert.deepEqual(delays, [...HISTORY_RETRY_DELAYS_MS]);
    assert.deepEqual(HISTORY_RETRY_DELAYS_MS, [1000, 2000, 4000, 8000, 16000]);
    assert.equal(cc.calls.length, 6, `${JSON.stringify(reply)}: initial plus five retries`);
    assert.equal(state.historyLoads.a.exhausted, true);
  }
});

test('a scheduled retry is skipped when rows arrived, the slot moved on, or the host disconnected', async () => {
  for (const [label, mutate] of [
    ['rows arrived', (ctx) => { ctx.needed = false; }],
    ['disconnected', (ctx) => { ctx.state.connected = false; }],
    ['load replaced', (ctx) => { ctx.state.historyLoads.a = { status: 'loading' }; }],
  ]) {
    const ctx = { state: makeStreamState(), needed: true };
    const cc = makeCc(() => ({ ok: true, count: 0 })); const timers = fakeTimers();
    ensureChatEventsLoaded(ctx.state, 'a', cc, silentLogger);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(scheduleHistoryRetry(ctx.state, 'a', cc, silentLogger, { hasRows: false, stillNeeded: () => ctx.needed, setTimer: timers.setTimer }), true);
    mutate(ctx);
    timers.runAll();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(cc.calls.length, 1, label);
  }
});

test('no retry is scheduled for a load that produced rows, is in flight, or is already pending', async () => {
  const state = makeStreamState(); const cc = makeCc(() => ({ ok: true, count: 3 })); const timers = fakeTimers();
  const opts = { hasRows: false, stillNeeded: () => true, setTimer: timers.setTimer };
  ensureChatEventsLoaded(state, 'a', cc, silentLogger);
  assert.equal(scheduleHistoryRetry(state, 'a', cc, silentLogger, opts), false, 'in flight');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(scheduleHistoryRetry(state, 'a', cc, silentLogger, { ...opts, hasRows: true }), false, 'rows present');
  assert.equal(scheduleHistoryRetry(state, 'a', cc, silentLogger, opts), true);
  assert.equal(scheduleHistoryRetry(state, 'a', cc, silentLogger, opts), false, 'already pending');
  assert.equal(timers.queue.length, 1);
});

test('manual retry resets the retry budget', async () => {
  const state = makeStreamState(); const cc = makeCc(() => ({ ok: true, count: 0 })); const timers = fakeTimers();
  const opts = { hasRows: false, stillNeeded: () => true, setTimer: timers.setTimer };
  ensureChatEventsLoaded(state, 'a', cc, silentLogger);
  await new Promise(resolve => setImmediate(resolve));
  for (let i = 0; i < 6; i += 1) { scheduleHistoryRetry(state, 'a', cc, silentLogger, opts); timers.runAll(); await new Promise(resolve => setImmediate(resolve)); }
  assert.equal(state.historyLoads.a.exhausted, true);
  assert.equal(ensureChatEventsLoaded(state, 'a', cc, silentLogger, { retry: true }), true);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(scheduleHistoryRetry(state, 'a', cc, silentLogger, opts), true);
  assert.deepEqual(timers.runAll(), [1000]);
});

test('chatHistoryStatus never leaves a row-less view without a status line while answers render', () => {
  const loaded = { status: 'loaded' };
  assert.deepEqual(chatHistoryStatus({ connected: false, load: loaded, hasRows: true, hasRendered: true }), { message: 'Reconnecting…', retry: false });
  assert.deepEqual(chatHistoryStatus({ connected: true, load: { status: 'error' }, hasRows: false, hasRendered: true }), { message: 'Messages could not be loaded.', retry: true });
  assert.deepEqual(chatHistoryStatus({ connected: true, load: { status: 'loading' }, hasRows: true, hasRendered: true }), { message: 'Syncing messages…', retry: false });
  assert.deepEqual(chatHistoryStatus({ connected: true, load: undefined, hasRows: false, hasRendered: false }), { message: 'Loading messages…', retry: false });
  assert.deepEqual(chatHistoryStatus({ connected: true, load: loaded, hasRows: true, hasRendered: true }), { message: '', retry: false });
  assert.deepEqual(chatHistoryStatus({ connected: true, load: loaded, hasRows: false, hasRendered: true }), { message: 'Loading messages…', retry: false });
  assert.deepEqual(chatHistoryStatus({ connected: true, load: { status: 'loaded', exhausted: true }, hasRows: false, hasRendered: true }), { message: 'Messages could not be loaded.', retry: true });
  // A genuinely empty stream keeps the ordinary empty state once retries are spent.
  assert.deepEqual(chatHistoryStatus({ connected: true, load: { status: 'loaded', exhausted: true }, hasRows: false, hasRendered: false }), { message: '', retry: false });
});
