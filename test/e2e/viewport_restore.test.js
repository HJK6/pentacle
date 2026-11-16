const { test } = require('node:test');
const assert = require('node:assert/strict');
const { dispatchScenario } = require('./lib/scenario_dispatch');
const { runGridSplitWidths, geometry } = require('./lib/grid_split_scenario');

test('failed mobile scenario does not contaminate the next scenario', async () => {
  let width = 1600;
  const session = { async eval() { return {width,height:1000}; }, async send(method, params) {
    if (method === 'Emulation.setDeviceMetricsOverride') width = params.width;
    if (method === 'Emulation.clearDeviceMetricsOverride') width = 1600;
  } };
  let failures = 0;
  const notes = [];
  const ctx = { session, report: { note: text => notes.push(text) } };
  for (const [name, fn] of [['mobile', async () => { await session.send('Emulation.setDeviceMetricsOverride', {width:390, height:844}); throw Error('forced mobile assertion'); }], ['baseline', async () => assert.equal(width, 1600, `next width ${width}`)]]) {
    if (await dispatchScenario(name, fn, ctx)) failures++;
  }
  assert.match(notes[0], /forced mobile assertion/);
  assert.equal(failures, 1, `next scenario measured width ${width}`);
});

test('standalone grid widths restores after a failing assertion', async () => {
  let width = 1600;
  const g = { handles: [{x:0,y:0,width:10,height:10},{x:0,y:0,width:10,height:10}], grid:{x:0,width:1000}, gap:10, cells:[{width:250},{width:250},{width:250},{width:250}], saved:.5, savedBottom:.5 };
  const session = { async eval(expr) { return expr === geometry ? g : expr.includes('innerWidth') ? {width,height:1000} : undefined; }, async send(method, params) { if(method === 'Emulation.setDeviceMetricsOverride') width=params.width; if(method === 'Emulation.clearDeviceMetricsOverride') width=1600; } };
  await assert.rejects(runGridSplitWidths({session, report:{ok() {throw Error('forced grid assertion');}}}), /forced grid assertion/);
  assert.equal(width, 1600);
});

test('cleanup failure is recorded without replacing primary failure', async () => {
  const notes = [];
  const ctx = { session: { async eval() { return {width:1600,height:1000}; }, async send() { throw Error('clear failed'); } }, report: { note: text => notes.push(text) } };
  const failed = await dispatchScenario('mobile', async () => { throw Error('primary assertion'); }, ctx);
  assert.equal(failed, true);
  assert.match(notes[0], /primary assertion/);
  assert.match(notes[1], /viewport cleanup FAILED: clear failed/);
});
