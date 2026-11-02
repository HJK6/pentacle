'use strict';
const assert = require('node:assert/strict');
const { dispatchScenario } = require('./scenario_dispatch');

async function viewportReliability(ctx) {
  if (!ctx.fixture) { ctx.report.note('viewport fixture: hermetic only'); return; }
  const {session, report} = ctx;
  const observed = { measurements: [], notes: [] };
  const measure = async label => {
    const width = await session.eval('innerWidth');
    observed.measurements.push({label, width, at: new Date().toISOString()});
    return width;
  };
  const baseline = await measure('baseline');
  const fixtureSession = {
    eval: session.eval.bind(session),
    async send(method, params) {
      const result = await session.send(method, params);
      if (method === 'Emulation.clearDeviceMetricsOverride') await measure('post-clear');
      return result;
    },
  };
  const fixtureCtx = { ...ctx, session: fixtureSession, report: { note: text => observed.notes.push(text) } };
  let nextWidth, failed, nextFailed;
  try {
    failed = await dispatchScenario('forced-mobile-fixture', async () => {
      await session.send('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:false});
      assert.equal(await measure('post-override'), 390);
      throw Error('forced mobile assertion');
    }, fixtureCtx);
    await measure('post-settle');
    const cleared = observed.measurements.find(item => item.label === 'post-clear');
    const settled = observed.measurements.find(item => item.label === 'post-settle');
    if (cleared && settled) observed.settleMs = Date.parse(settled.at) - Date.parse(cleared.at);
    nextFailed = await dispatchScenario('baseline-fixture', async () => {
      nextWidth = await measure('next-scenario');
      assert.equal(nextWidth, baseline);
    }, fixtureCtx);
  } finally {
    const diagnostic = {seq:910000,name:'viewport-diagnostic',...observed};
    await session.eval(`console.log(${JSON.stringify(JSON.stringify(diagnostic))}); console.log(${JSON.stringify('[HARNESS] '+JSON.stringify(diagnostic))})`);
  }
  assert.equal(failed, true, JSON.stringify(observed));
  assert.equal(nextFailed, false, JSON.stringify(observed));
  assert.match(observed.notes[0], /forced mobile assertion/);
  assert.match(observed.notes[1], /viewport cleanup: cleared/);
  report.ok('viewport fixture preserves failure and restores native width', true, {baseline, nextWidth, notes: observed.notes});
}

async function captureReliability(ctx) {
  if (!ctx.fixture) { ctx.report.note('capture fixture: hermetic only'); return; }
  const {session, report, cdp} = ctx;
  await session.drainConsole();
  const start = session.consoleLines.length, beaconStart = session.beacons.length;
  await session.eval(`(() => {
    console.log('fixture plain string');
    console.log({nested:{token:'synthetic-token',secret:'synthetic-secret',password:'synthetic-password',dsn:'synthetic-dsn',authorization:'synthetic-auth',cookie:'synthetic-cookie'}});
    console.log({large:'x'.repeat(3000)});
    console.log({a:{b:{c:{d:'deep'}}}});
    const cycle = {}; cycle.self = cycle; console.log(cycle);
    console.log(0,false,null);
    console.log('[HARNESS] {"seq":910001,"name":"reliability-fixture"}');
    console.log('[HARNESS] malformed');
    setTimeout(() => { throw Error('fixture uncaught exception'); }, 0);
  })()`);
  await cdp.sleep(100);
  const originalSend = session.send;
  let release;
  const held = new Promise(resolve => { release = resolve; });
  try {
    session.send = async function(method, params) {
      if (method === 'Runtime.callFunctionOn') await held;
      return originalSend.call(this, method, params);
    };
    await session.eval('console.log({pending:"fixture"})');
    let drained = false;
    const flush = session.drainConsole().then(() => { drained = true; });
    await cdp.sleep(20);
    assert.equal(drained, false);
    release(); await flush;
  } finally { release(); session.send = originalSend; }
  session._onConsole({type:'log',args:[{type:'object',objectId:'unavailable-fixture'}]});
  await session.drainConsole();
  const entries = session.consoleLines.slice(start);
  const jsonl = entries.map(entry => JSON.stringify(entry)).join('\n');
  const parsed = jsonl.split('\n').map(line => JSON.parse(line));
  assert.ok(parsed.some(entry => entry.text === 'fixture plain string'));
  const nested = JSON.parse(parsed.find(entry => entry.text.includes('synthetic') || entry.text.includes('[redacted]')).text).nested;
  for (const key of ['token','secret','password','dsn','authorization','cookie']) assert.equal(nested[key], '[redacted]');
  assert.doesNotMatch(jsonl, /synthetic-(token|secret|password|dsn|auth|cookie)/);
  for (const marker of ['[truncated]','[cyclic]','[unavailable]']) assert.ok(jsonl.includes(marker));
  assert.ok(parsed.some(entry => entry.text === '0 false null'));
  assert.ok(parsed.some(entry => entry.text === '[HARNESS] malformed'));
  assert.ok(parsed.some(entry => entry.type === 'exception' && entry.text.includes('fixture uncaught exception')));
  assert.ok(parsed.some(entry => entry.text.includes('pending')));
  assert.ok(parsed.every(entry => Buffer.byteLength(JSON.stringify(entry)) <= 8192));
  assert.deepEqual(session.beacons.slice(beaconStart).filter(b => b.name === 'reliability-fixture').map(b => b.seq), [910001]);
  report.ok('capture fixture preserves ordered beacons and bounded redacted entries', true, {entries:parsed.length});
}
module.exports = { viewportReliability, captureReliability };
