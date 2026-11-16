const test = require('node:test');
const assert = require('node:assert/strict');
const { createTypingProbe } = require('./e2e/lib/terminal_typing_probe');
const { runTerminalTypingProbe } = require('./e2e/lib/run_terminal_typing_probe');
const vm = require('node:vm');

test('delayed echoes never acknowledge multiple sends after the old eight-token boundary', () => {
  let now = 0;
  const probe = createTypingProbe({now: () => now, count: 9});
  const tokens = Array.from({length: 9}, () => {now += 250; return probe.send();});
  assert.equal(new Set(tokens).size, 9);
  now = 3000;
  assert.equal(probe.receive(tokens[0]).length, 1);
  assert.equal(probe.result().received, 1);
  assert.equal(probe.result().missing, 8);
  assert.equal(probe.result().complete, false);
  assert.equal(probe.receive(tokens[0]).length, 0);
  assert.equal(probe.result().duplicates, 1);
  probe.receive(tokens.slice(1).join(''));
  assert.equal(probe.result().received, 9);
  assert.equal(probe.result().complete, true);
});

test('UTF-8 split chunks correlate exactly once and parse/render use distinct endpoints', () => {
  let now = 10;
  const probe = createTypingProbe({now: () => now, count: 2});
  const a = probe.send();
  now = 20; const b = probe.send();
  const bytes = Buffer.from(a + b);
  now = 30; assert.equal(probe.receive(bytes.subarray(0, 1)).length, 0);
  now = 40; const matched = probe.receive(bytes.subarray(1));
  assert.equal(matched.length, 2);
  now = 50; probe.parsed(matched);
  now = 60; probe.rendered();
  const result = probe.result();
  assert.equal(result.complete, true);
  assert.equal(result.receive.p95Ms, 30);
  assert.equal(result.parseDelay.p95Ms, 10);
  assert.equal(result.renderDelay.p95Ms, 20);
  assert.equal(result.render.p95Ms, 50);
});

test('missing and unsent echoes fail completeness even when observed p95 is fast', () => {
  const probe = createTypingProbe({now: () => 1, count: 3});
  probe.receive(probe.send()); probe.send();
  const result = probe.result();
  assert.equal(result.receive.p95Ms, 0);
  assert.equal(result.missing, 1);
  assert.equal(result.unsent, 1);
  assert.equal(result.complete, false);
  assert.equal(result.render.count, 0);
});

for (const failInput of [false, true]) {
  test(`renderer hooks restore after ${failInput ? 'input failure' : 'echo completion'}`, async () => {
    let onRender, disposed = false, callbackCalled = false, clock = 0;
    const originalWrite = (data, callback) => { callback(); onRender(); };
    const originalApply = () => {};
    const term = { write: originalWrite,
      onRender(fn) { onRender = fn; return {dispose() {disposed = true;}}; },
      input(data) {
        if (failInput) throw new Error('input failed');
        this.write(data, () => {callbackCalled = true;});
      },
    };
    const store = {applyFrame: originalApply};
    const world = {state: {terminals: [{term}]}, window: {PentacleChatStore: store, cc: {}},
      performance: {now: () => ++clock}, TextDecoder, setTimeout: fn => queueMicrotask(fn)};
    const run = vm.runInNewContext(`(${runTerminalTypingProbe})(${createTypingProbe}, {slot:0,count:1,durationMs:0,drainMs:0})`, world);
    if (failInput) await assert.rejects(run, /input failed/);
    else {
      const result = await run;
      assert.equal(result.complete, true);
      assert.equal(result.render.count, 1);
      assert.equal(callbackCalled, true);
    }
    assert.equal(term.write, originalWrite);
    assert.equal(store.applyFrame, originalApply);
    assert.equal(disposed, true);
  });
}

