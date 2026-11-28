'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { createChatHistory, hasRows, newTailRows } = require('../renderer/next/chat_history');

const flush = () => new Promise(resolve => setImmediate(resolve));

function open({ reply = () => ({ ok: true, received: 3, nextBeforeDaemonSeq: null }), rows = () => true } = {}) {
  const calls = [], pins = [], timers = [], changes = [];
  const expanded = [];
  const history = createChatHistory({
    cc: { requestStreamEvents: async args => { calls.push(args); return reply(args, calls.length); } },
    store: {
      setFocusedChatStreams: ids => pins.push([...ids]),
      setHistoryExpanded: id => expanded.push(id),
      selectSessionDetail: id => ({ transcriptItems: rows(id) ? [{ id: 'row' }] : [{ id: 'fallback:preview' }], remainingCount: 0 }),
    },
    onChange: id => changes.push(id),
    logger: { warn() {}, info() {} },
    setTimer: (fn, ms) => timers.push({ fn, ms }),
  });
  return { history, calls, pins, timers, changes, expanded, ids: () => calls.map(call => call.streamId) };
}

test('a chat on screen is pinned in the store at once and read when the connection is up', async () => {
  let held = false;
  const s = open({ rows: () => held });
  s.history.focus(['a:1', undefined, 'a:1']);
  assert.deepEqual(s.pins, [['a:1']]);
  assert.deepEqual(s.ids(), [], 'nothing is asked of a daemon that is not connected');
  assert.equal(s.history.status('a:1').message, 'Reconnecting…');
  s.history.setConnected(true);
  s.history.focus(['a:1']); s.history.focus(['a:1']);
  assert.deepEqual(s.ids(), ['a:1']);
  assert.deepEqual(s.pins, [['a:1']], 'the same chats are not pinned again');
  assert.equal(s.history.status('a:1').message, 'Loading messages…');
  await flush();
  held = true;
  assert.deepEqual(s.changes, ['a:1']);
  assert.deepEqual(s.history.status('a:1'), { message: '', retry: false });
});

test('a chat that left the screen is read again when it returns, and a reconnect reads every open chat', async () => {
  const s = open();
  s.history.setConnected(true);
  s.history.focus(['a:1', 'a:2']);
  await flush();
  s.history.focus(['a:1']);
  s.history.focus(['a:1', 'a:2']);
  assert.deepEqual(s.ids(), ['a:1', 'a:2', 'a:2']);
  await flush();
  s.history.setConnected(false);
  s.history.setConnected(true);
  assert.deepEqual(s.ids().slice(3), ['a:1', 'a:2']);
});

test('a read that fails or comes back empty retries on a bounded backoff, then offers Retry', async () => {
  let rows = false;
  const s = open({ reply: () => ({ ok: false, error: 'busy' }), rows: () => rows });
  s.history.setConnected(true);
  s.history.focus(['a:1']);
  await flush();
  assert.equal(s.history.status('a:1').message, 'Loading messages…');
  for (let attempt = 0; attempt < 5; attempt++) {
    s.history.focus(['a:1']);
    assert.equal(s.timers.length, attempt + 1, 'one retry is pending at a time');
    s.timers[attempt].fn();
    await flush();
  }
  assert.deepEqual(s.timers.map(timer => timer.ms), [1000, 2000, 4000, 8000, 16000]);
  s.history.focus(['a:1']);
  assert.equal(s.timers.length, 5, 'the budget is spent');
  assert.deepEqual(s.history.status('a:1'), { message: 'Messages could not be loaded.', retry: true });
  rows = true;
  assert.deepEqual(s.history.status('a:1'), { message: 'Messages could not be loaded.', retry: true }, 'held rows do not hide a failed read');
  const before = s.calls.length;
  s.history.retry('a:1');
  assert.equal(s.calls.length, before + 1, 'Retry starts a fresh read');
});

test('a retry is dropped once its chat has left the screen', async () => {
  const s = open({ rows: () => false });
  s.history.setConnected(true);
  s.history.focus(['a:1']);
  await flush();
  s.history.focus(['a:1']);
  assert.equal(s.timers.length, 1);
  s.history.focus([]);
  s.timers[0].fn();
  assert.deepEqual(s.ids(), ['a:1']);
});

test('older pages are asked for only while the daemon has more, with the store told to keep them', async () => {
  const s = open({ reply: (args, n) => n === 1 ? { ok: true, received: 500, nextBeforeDaemonSeq: 900 }
    : { ok: true, received: 20, nextBeforeDaemonSeq: null, exhausted: true } });
  s.history.setConnected(true);
  s.history.focus(['a:1']);
  await flush();
  assert.equal(s.history.hasOlder('a:1'), true);
  const loading = s.history.loadOlder('a:1', () => true);
  assert.equal(s.history.loadingOlder('a:1'), true);
  await loading;
  assert.deepEqual(s.calls[1], { streamId: 'a:1', beforeDaemonSeq: 900, limit: 500 });
  assert.deepEqual(s.expanded, ['a:1']);
  assert.equal(s.history.hasOlder('a:1'), false);
  assert.equal(s.history.loadingOlder('a:1'), false);
});

test('a chat read back to its start offers earlier messages again after it leaves and returns', async () => {
  const s = open({ reply: args => args.beforeDaemonSeq ? { ok: true, received: 20, nextBeforeDaemonSeq: null, exhausted: true }
    : { ok: true, received: 500, nextBeforeDaemonSeq: 900 } });
  s.history.setConnected(true);
  s.history.focus(['a:1']);
  await flush();
  await s.history.loadOlder('a:1', () => true);
  assert.equal(s.history.hasOlder('a:1'), false);
  s.history.focus([]);
  s.history.focus(['a:1']);
  await flush();
  assert.equal(s.history.hasOlder('a:1'), true, 'the store may have dropped the older rows, so they can be asked for again');
  await s.history.loadOlder('a:1', () => true);
  assert.deepEqual(s.calls.at(-1), { streamId: 'a:1', beforeDaemonSeq: 900, limit: 500 }, 'paging restarts from the fresh read');
});

test('a preview or an answered question is not history, and only rows below the last newest one extend the window', () => {
  assert.equal(hasRows({ transcriptItems: [{ id: 'fallback:x' }, { id: 'q', eventCase: 'agent-question-answer' }] }, () => null), false);
  assert.equal(hasRows({ transcriptItems: [{ id: 'm1' }] }, () => null), true);
  assert.equal(hasRows({ transcriptItems: [], remainingCount: 4 }, () => ({ transcriptItems: [{ id: 'm0' }] })), true);
  const items = ['m1', 'm2', 'm3', 'm4'].map(id => ({ id }));
  assert.equal(newTailRows(items, 'm2'), 2);
  assert.equal(newTailRows(items, 'm4'), 0);
  assert.equal(newTailRows(items, null), 0);
  assert.equal(newTailRows(items, 'gone'), 0, 'earlier rows arriving above do not count');
});

test('an empty first read does not hide older history once a retry reaches it', async () => {
  let rows = false;
  const s = open({ rows: () => rows, reply: (args, n) => args.beforeDaemonSeq ? { ok: true, received: 20, nextBeforeDaemonSeq: null, exhausted: true }
    : n === 1 ? { ok: true, received: 0, nextBeforeDaemonSeq: null } : { ok: true, received: 500, nextBeforeDaemonSeq: 1000 } });
  s.history.setConnected(true);
  s.history.focus(['a:1']);
  await flush();
  assert.equal(s.history.hasOlder('a:1'), false, 'nothing older is known after an empty read');
  s.history.focus(['a:1']);
  assert.equal(s.timers.length, 1, 'the empty read retries');
  s.timers[0].fn();
  rows = true;
  await flush();
  assert.deepEqual(s.history.status('a:1'), { message: '', retry: false });
  assert.equal(s.history.hasOlder('a:1'), true, 'the full reply sets the older boundary');
  await s.history.loadOlder('a:1', () => true);
  assert.deepEqual(s.calls.at(-1), { streamId: 'a:1', beforeDaemonSeq: 1000, limit: 500 });
});

test('a fresh read after paging keeps the deeper place it reached', async () => {
  const s = open({ reply: args => args.beforeDaemonSeq === 900 ? { ok: true, received: 500, nextBeforeDaemonSeq: 400 }
    : args.beforeDaemonSeq ? { ok: true, received: 20, nextBeforeDaemonSeq: null, exhausted: true }
    : { ok: true, received: 500, nextBeforeDaemonSeq: 900 } });
  s.history.setConnected(true);
  s.history.focus(['a:1']);
  await flush();
  await s.history.loadOlder('a:1', () => true);
  s.history.reconnected();
  await flush();
  await s.history.loadOlder('a:1', () => true);
  assert.deepEqual(s.calls.at(-1), { streamId: 'a:1', beforeDaemonSeq: 400, limit: 500 }, 'the store still holds the rows above 400');
});

test('older pages stop when the chat that asked for them is no longer wanted', async () => {
  let release;
  const s = open({ reply: (args, n) => n === 1 ? { ok: true, received: 500, nextBeforeDaemonSeq: 900 }
    : args.beforeDaemonSeq === 900 ? new Promise(resolve => { release = () => resolve({ ok: true, received: 500, nextBeforeDaemonSeq: 400 }); })
    : { ok: true, received: 20, nextBeforeDaemonSeq: null, exhausted: true } });
  s.history.setConnected(true);
  s.history.focus(['a:1']);
  await flush();
  let wanted = true;
  const loading = s.history.loadOlder('a:1', () => false, () => wanted);
  await flush();
  wanted = false;
  release();
  await loading;
  assert.deepEqual(s.calls.map(call => call.beforeDaemonSeq ?? null), [null, 900], 'no page is asked for after the reader left');
  assert.equal(s.history.loadingOlder('a:1'), false);
});

test('a daemon reconnect with no disconnect in between reads every open chat again', async () => {
  const s = open();
  s.history.setConnected(true);
  s.history.focus(['a:1', 'a:2']);
  await flush();
  s.history.reconnected();
  assert.deepEqual(s.ids(), ['a:1', 'a:2', 'a:1', 'a:2']);
  await flush();
  s.history.focus(['a:1', 'a:2']);
  assert.equal(s.calls.length, 4, 'each chat is read once per connection');
});
