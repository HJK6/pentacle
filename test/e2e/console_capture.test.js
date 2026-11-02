const { test } = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { pathToFileURL } = require('node:url');
const { connect, sleep } = require('./lib/cdp');
const { captureReliability, viewportReliability } = require('./lib/reliability_fixtures');

test('console fixture captures structured entries and exceptions', async () => {
  const port = await new Promise(resolve => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); });
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'console-capture-'));
  const chrome = spawn(process.env.PENTACLE_CHROME || (process.platform === 'darwin' ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : 'google-chrome'), ['--headless=new', '--disable-gpu', '--window-size=1600,1000', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, pathToFileURL(path.join(__dirname, 'fixtures/console_capture.html')).href], {stdio:'ignore'});
  let session;
  try {
    session = await connect(port, {match:/console_capture/, timeoutMs:15000});
    await session.eval('emitFixture()');
    await sleep(200);
    // Hold an extraction to prove flush waits for work already in flight.
    const originalSend = session.send.bind(session);
    let release;
    const held = new Promise(resolve => { release = resolve; });
    session.send = async (method, params) => {
      if (method === 'Runtime.callFunctionOn') await held;
      return originalSend(method, params);
    };
    await session.eval('console.log({pending:"fixture"})');
    let drained = false;
    const flushing = session.drainConsole().then(() => { drained = true; });
    await sleep(30);
    assert.equal(drained, false);
    release(); await flushing;
    const output = session.consoleLines.map(line => JSON.stringify(line)).join('\n');
    const lines = output.split('\n').map(line => JSON.parse(line));
    assert.equal(lines[0].text, 'plain string');
    assert.ok(lines.some(line => line.type === 'exception' && line.text.includes('fixture uncaught exception')));
    const nested = JSON.parse(lines[1].text).nested;
    for (const key of ['token','secret','password','dsn','authorization','cookie']) assert.equal(nested[key], '[redacted]');
    assert.doesNotMatch(output, /synthetic-(token|secret|password|dsn|auth|cookie)/);
    assert.match(lines[2].text, /\[truncated\]/);
    assert.match(lines[3].text, /\[truncated\]/);
    assert.ok(lines.some(line => line.text.includes('[cyclic]')));
    assert.ok(lines.some(line => line.text === '0 false null'));
    assert.ok(lines.some(line => line.text === '[HARNESS] malformed'));
    assert.deepEqual(session.beacons.map(b => b.seq), [1]);
    assert.ok(lines.some(line => line.text.includes('pending')));
    session._onConsole({type:'log', args:[{type:'object', objectId:'unavailable-object'}]});
    await session.drainConsole();
    assert.equal(session.consoleLines.at(-1).text, '[unavailable]');
    assert.ok(lines.every(line => Buffer.byteLength(JSON.stringify(line)) <= 8192));
    await viewportReliability({session, fixture:true, report:{ok(){}, note(){}}});
    await captureReliability({session, fixture:true, report:{ok(){}, note(){}}, cdp:{sleep}});

  } finally {
    if (session) session.close();
    if (chrome.exitCode === null) {
      const exited = new Promise(resolve => chrome.once('exit', resolve));
      chrome.kill();
      await exited;
    }
    fs.rmSync(profile, {recursive:true, force:true});
  }
});
