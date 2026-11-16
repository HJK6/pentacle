const { test } = require('node:test');
const assert = require('node:assert/strict');
const { dispatchScenario } = require('./lib/scenario_dispatch');

test('failure cleanup polls until the starting viewport is restored', async () => {
  let width = 1600, readsAfterClear = 0, cleared = false;
  const session = {
    async send(method) { if (method === 'Emulation.clearDeviceMetricsOverride') cleared = true; },
    async eval(expression) {
      if (!expression.includes('innerWidth')) return null;
      if (cleared && ++readsAfterClear >= 2) width = 1600;
      return {width,height:1000};
    },
  };
  const notes = [];
  const failed = await dispatchScenario('mobile', async () => {width=390; throw Error('primary assertion');}, {session,report:{note:x=>notes.push(x)}});
  assert.equal(failed,true);
  assert.equal(width,1600);
  assert.ok(readsAfterClear >= 2);
  assert.match(notes[0], /primary assertion/);
});

test('cleanup timeout is recorded separately from the primary assertion', async () => {
  const { clearAndSettle } = require('./lib/viewport_settle');
  const session = {async send(){}, async eval(){return {width:390,height:844};}};
  await assert.rejects(clearAndSettle(session,{width:1600,height:1000},{timeoutMs:5,intervalMs:1}), /viewport.*timed out/i);
});

test('dispatch reports timeout separately and preserves the primary failure', async () => {
  let width = 1600;
  const notes = [];
  const session = {async send() {}, async eval() {return {width,height:1000};}};
  const failed = await dispatchScenario('mobile', async () => { width=390; throw Error('primary assertion'); }, {session,report:{note:x=>notes.push(x)}});
  assert.equal(failed, true);
  assert.match(notes[0], /primary assertion/);
  assert.match(notes[1], /viewport cleanup FAILED: viewport restore timed out/);
});
