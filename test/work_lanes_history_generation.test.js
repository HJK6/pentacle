'use strict';

// chat_events_lazy: the lane generation is an explicit per-request option, never shared state.
const test = require('node:test');
const assert = require('node:assert/strict');
const lazy = require('../renderer/chat_events_lazy');

const quiet = { warn() {}, info() {} };
const flush = () => new Promise((resolve) => setImmediate(resolve));
function recorder() {
  const calls = [];
  return { calls, cc: { requestStreamEvents: async (args) => { calls.push(args); return { ok: true, received: 0, count: 0, nextBeforeDaemonSeq: null }; } } };
}
const streamState = () => ({ connected: true, eventsLoadedFor: new Set(), historyLoads: {}, historyPaging: {} });

test('initial read carries the generation only when asked', async () => {
  const { calls, cc } = recorder();
  const state = streamState();
  lazy.ensureChatEventsLoaded(state, 'hostc:a', cc, quiet, { generation: 'g1' });
  lazy.ensureChatEventsLoaded(state, 'hostc:b', cc, quiet, {});
  await flush();
  assert.deepEqual(calls, [{ streamId: 'hostc:a', generation: 'g1' }, { streamId: 'hostc:b' }]);
});

test('older-history pages carry the generation only when asked', async () => {
  const { calls, cc } = recorder();
  const state = streamState();
  state.historyPaging['hostc:a'] = { cursor: 50, exhausted: false, loading: false };
  state.historyPaging['hostc:b'] = { cursor: 50, exhausted: false, loading: false };
  await lazy.requestOlderHistory(state, 'hostc:a', cc, quiet, { generation: 'g1' });
  await lazy.requestOlderHistory(state, 'hostc:b', cc, quiet, {});
  assert.equal(calls[0].generation, 'g1');
  assert.equal('generation' in calls[1], false);
});

test('reconnect refetch passes each slot its own generation', async () => {
  const { calls, cc } = recorder();
  const state = streamState();
  const slots = [{ hostId: 'h' }, { hostId: 'h' }];
  lazy.refetchEventsForActiveChatSlots({
    slots, slotViewModes: ['chat', 'chat'], botSlots: [false, false], streamState: state, cc,
    streamHostForHostId: (id) => id,
    findStreamSession: (_s, _session, _host) => ({ stream_id: 'hostc:a' }),
    generationForSlot: (slot) => (slot === 0 ? 'g1' : undefined),
  });
  await flush();
  // The same stream id is loaded once; the first slot claims it with its generation.
  assert.deepEqual(calls, [{ streamId: 'hostc:a', generation: 'g1' }]);
});

test('retry keeps the generation of the failed read', async () => {
  const { calls, cc } = recorder();
  const state = streamState();
  state.historyLoads['hostc:a'] = { status: 'error', error: 'x', attempt: 0 };
  const timers = [];
  lazy.scheduleHistoryRetry(state, 'hostc:a', cc, quiet, { hasRows: false, generation: 'g1', setTimer: (fn) => timers.push(fn) });
  timers.forEach((fn) => fn());
  await flush();
  assert.equal(calls[0].generation, 'g1');
});

test('retry ownership is per slot kind and generation: an ordinary retry cannot suppress or cancel the lane-history retry', async () => {
  const calls = [];
  // Mirrors a closed stream: a read without the lane generation is refused, the lane read succeeds.
  const cc = { requestStreamEvents: async (args) => {
    calls.push(args);
    return args.generation ? { ok: true, received: 3, count: 3, nextBeforeDaemonSeq: null } : { ok: false, error: 'unknown_session' };
  } };
  const state = streamState();
  state.historyLoads['hostc:a'] = { status: 'error', error: 'x', attempt: 0 };
  const timers = [];
  const options = (generation) => ({ hasRows: false, generation, stillNeeded: () => true, setTimer: (fn) => timers.push(fn) });
  assert.equal(lazy.scheduleHistoryRetry(state, 'hostc:a', cc, quiet, options(undefined)), true);
  assert.equal(lazy.scheduleHistoryRetry(state, 'hostc:a', cc, quiet, options('g1')), true, 'lane retry is queued beside the ordinary one');
  assert.equal(timers.length, 2);
  assert.equal(lazy.scheduleHistoryRetry(state, 'hostc:a', cc, quiet, options(undefined)), false, 'same owner still deduplicates');
  assert.equal(lazy.scheduleHistoryRetry(state, 'hostc:a', cc, quiet, options('g1')), false, 'same owner still deduplicates');
  const [ordinary, lane] = timers.splice(0);
  ordinary();
  await flush();
  assert.deepEqual(calls, [{ streamId: 'hostc:a' }], 'the ordinary retry fires without a generation');
  lane();
  await flush();
  assert.deepEqual(calls, [{ streamId: 'hostc:a' }, { streamId: 'hostc:a', generation: 'g1' }],
    'the lane retry still fires, with its generation, after the ordinary retry replaced the shared load');
});

test('a reconnect reset still makes a pending retry moot', async () => {
  const { calls, cc } = recorder();
  const state = streamState();
  state.historyLoads['hostc:a'] = { status: 'error', error: 'x', attempt: 0 };
  const timers = [];
  lazy.scheduleHistoryRetry(state, 'hostc:a', cc, quiet, { hasRows: false, generation: 'g1', setTimer: (fn) => timers.push(fn) });
  state.historyLoads = {};
  state.eventsLoadedFor.clear();
  timers.forEach((fn) => fn());
  await flush();
  assert.deepEqual(calls, []);
});

test('retry owner token separates slot instances at the same generation', async () => {
  const { calls, cc } = recorder();
  const state = streamState();
  state.historyLoads['hostc:a'] = { status: 'error', error: 'x', attempt: 0 };
  const timers = [];
  const options = (owner) => ({ hasRows: false, generation: 'g1', owner, stillNeeded: () => true, setTimer: (fn) => timers.push(fn) });
  assert.equal(lazy.scheduleHistoryRetry(state, 'hostc:a', cc, quiet, options('g1#1')), true);
  assert.equal(lazy.scheduleHistoryRetry(state, 'hostc:a', cc, quiet, options('g1#1')), false, 'the same instance deduplicates');
  assert.equal(lazy.scheduleHistoryRetry(state, 'hostc:a', cc, quiet, options('g1#2')), true, 'a replacement instance is its own owner');
  assert.equal(timers.length, 2);
  void calls;
});
