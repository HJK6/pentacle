'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createRemoteClipboardPoller } = require('../renderer/remote-clipboard-poll');
const flush = () => new Promise(resolve => setImmediate(resolve));

test('remote clipboard awaits writes in order and stop drains pending writes', async () => {
  const writes = [], pending = [], indices = [];
  const poller = createRemoteClipboardPoller({
    fetchSince: async idx => { indices.push(idx); return idx === 0 ? { lines: ['one', 'two'], total: 2 } : { lines: [], total: 2 }; },
    writeClipboard: text => { writes.push(text); return new Promise(r => pending.push(r)); },
    setIntervalFn: () => 1, clearIntervalFn() {},
  });
  poller.start(); await flush(); assert.deepEqual(writes, ['one']);
  let stopped = false; const stop = poller.stop().then(() => { stopped = true; });
  await flush(); assert.equal(stopped, false);
  pending.shift()(); await flush(); assert.deepEqual(writes, ['one', 'two']); assert.equal(stopped, false);
  pending.shift()(); await stop; assert.deepEqual(indices, [0, 2]); assert.equal(stopped, true);
});

test('async clipboard rejection is reported and does not advance the cursor', async () => {
  const indices = [], warnings = []; let tick, attempts = 0;
  const poller = createRemoteClipboardPoller({
    fetchSince: async idx => { indices.push(idx); return { lines: ['one'], total: 1 }; },
    writeClipboard: async () => { if (attempts++ === 0) throw Error('clipboard unavailable'); },
    setIntervalFn: fn => { tick = fn; return 1; }, clearIntervalFn() {}, consoleRef: { warn: (...args) => warnings.push(args) },
  });
  poller.start(); await flush(); assert.equal(warnings.length, 1);
  await tick(); assert.deepEqual(indices, [0, 0]); await poller.stop(); assert.equal(indices.at(-1), 1);
});
