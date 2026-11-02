const { test } = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { headlessChromeArgs } = require('./lib/chrome_launch.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { connect } = require('./lib/cdp');
const { dispatchScenario } = require('./lib/scenario_dispatch');

test('failed mobile journey leaves following scenario at native width', async () => {
  const port = await new Promise(resolve => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); });
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'viewport-restore-'));
  const chrome = spawn(process.env.PENTACLE_CHROME || (process.platform === 'darwin' ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : 'google-chrome'), [...headlessChromeArgs(), '--disable-gpu', '--window-size=1600,1000', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank'], {stdio:'ignore'});
  let session;
  try {
    session = await connect(port, {match:/about:blank/, timeoutMs:15000});
    const baseline = await session.eval('innerWidth');
    assert.equal(baseline, 1600);
    let failed = 0, secondWidth;
    const notes = [];
    const ctx = { session, report: { note: value => notes.push(value) } };
    const scenarios = [async () => { await session.send('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:false}); assert.equal(await session.eval('innerWidth'),390); throw Error('forced mobile assertion'); }, async () => { secondWidth = await session.eval('innerWidth'); assert.equal(secondWidth, baseline); }];
    for (const [i, fn] of scenarios.entries()) if (await dispatchScenario(`fixture-${i}`, fn, ctx)) failed++;
    assert.match(notes[0], /forced mobile assertion/);
    assert.match(notes[1], /viewport cleanup: cleared/);
    assert.equal(failed, 1, `following scenario width ${secondWidth}`);
  } finally {
    if (session) session.close();
    if (chrome.exitCode === null) { const exited = new Promise(resolve => chrome.once('exit', resolve)); chrome.kill(); await exited; }
    fs.rmSync(profile, {recursive:true, force:true});
  }
});
