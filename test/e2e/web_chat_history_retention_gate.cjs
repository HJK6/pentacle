#!/usr/bin/env node
'use strict';
// Actual browser/store/transport; only the loopback daemon counterpart is a fixture.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { startDaemon, targetId } = require('./scenarios/assistant_direct');
const { getFreePort } = require('./lib/disposable_web_daemon_config');
const cdp = require('./lib/cdp');
const root = path.resolve(process.env.PENTACLE_GATE_PRODUCT_ROOT || path.join(__dirname, '../..'));
const out = path.resolve(process.argv[2] || fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-retention-')));
const legacy = process.env.PENTACLE_GATE_EXPECT_LEGACY === '1';
const browserBinary = process.env.PENTACLE_TEST_BROWSER || process.env.PENTACLE_CHROME || 'google-chrome';
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const hash = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');

async function run() {
  fs.mkdirSync(out, { recursive: true, mode: 0o700 });
  const daemon = await startDaemon({ artifactsDir: out });
  const at = new Date().toISOString();
  const row = (kind, text, daemon_seq) => ({ host: 'mock-host', session_name: 'live', stream_id: targetId,
    provider: 'claude', kind, text, timestamp: at, daemon_seq });
  daemon.events.push(row('USER', 'Fixture user history', 2), row('ASSIST_TEXT', 'Fixture assistant history', 3),
    row('TOOL_RESULT', 'fixture tool'.repeat(100000), 10),
    { ...row('SYSTEM', 'Worked for 1s', 11), raw: { subtype: 'turn-summary' } });
  const answered = row('USER', JSON.stringify({ type: 'notification.answer',
    answer: { notification_id: 'fixture-answer', text: 'Fixture selected answer' } }), 4);
  daemon.events.push(answered);
  daemon.setDurableQuestion({ notification_id: 'fixture-answer', producer: 'agent_question.v1', state: 'answered',
    resolved_at: at, question: { question_id: 'fixture-q', producer_stream_id: targetId, state: 'answered',
      answer: { text: 'Fixture selected answer' }, answered_at: at } });
  let web, browser, page;
  const observations = {};
  try {
    web = await require(path.join(root, 'server')).main(['--profile', daemon.configFile, '--bind', '127.0.0.1', '--port', '0']);
    const port = await getFreePort();
    browser = spawn(browserBinary, ['--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-gpu',
      '--window-size=1600,900', `--user-data-dir=${path.join(out, 'chrome')}`, `--remote-debugging-port=${port}`, 'about:blank'], { stdio: 'ignore' });
    page = await cdp.connect(port, { timeoutMs: 15000 });
    // Accelerate only the existing ten-minute build poll. Baseline/candidate use
    // identical browser clock configuration; product recovery code is untouched.
    await page.send('Page.addScriptToEvaluateOnNewDocument', { source: `{
      const original = window.setInterval;
      window.setInterval = (fn, ms, ...args) => original(fn, ms === 600000 ? 100 : ms, ...args);
    }` });
    await page.send('Page.navigate', { url: web.url });
    const attach = async () => {
      await page.waitFor(`document.querySelector('.session-item[data-stream-id="${targetId}"]')`, { timeoutMs: 15000 });
      await page.click(`.session-item[data-stream-id="${targetId}"]`);
      await page.click('#cell-0 .cell-view-toggle[data-mode="chat"]');
    };
    const capture = async name => {
      const data = await page.eval(`(() => {
        const s = window.PentacleChatStore.getState();
        return {
          targetEvents:s.events.filter(e=>e.stream_id===${JSON.stringify(targetId)}).length,
          pins:s.eventBucketsByStream?.[${JSON.stringify(targetId)}]?.pins || {},
          rows:document.querySelectorAll('#cell-0 .slot-chat-row').length,
          answers:document.querySelectorAll('#cell-0 .slot-chat-v3-answers').length,
          assistant:document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Fixture assistant history'),
          user:document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Fixture user history'),
          history:document.querySelector('#cell-0 .slot-chat-history-state')?.textContent || null,
          draft:document.querySelector('#cell-0 .slot-chat-compose-input')?.value,
          attachments:document.querySelectorAll('#cell-0 .slot-chat-attachment-chip').length,
          buildId:window.__PENTACLE_CONFIG__.buildId
        };
      })()`);
      observations[name] = { ...data, fetches: daemon.requests.filter(r => r.type === 'request_stream_events' && r.stream_id === targetId).length };
      fs.writeFileSync(path.join(out, `${name}.json`), JSON.stringify(observations[name], null, 2));
      await page.screenshot(path.join(out, `${name}.png`));
      return observations[name];
    };
    await attach();
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Fixture assistant history')", { timeoutMs: 10000 });
    const before = await capture('before');
    assert.equal(before.answers, 1);
    daemon.disconnect(); await wait(1800);
    const reconnect = await capture('reconnect');
    assert.equal(reconnect.rows, before.rows);
    for (let i = 0; i < 30; i++) daemon.emitEvent({ ...row('TOOL_RESULT', 'noise'.repeat(50000), 100 + i),
      stream_id: 'mock-host:pressure', session_name: 'pressure' });
    daemon.emitEvent(answered); await wait(1800);
    const pressure = await capture('pressure');
    assert.equal(pressure.answers, 1);
    if (legacy) assert.equal(pressure.rows, 0, 'baseline RED is answer-only after unrelated stream pressure');
    else {
      assert.equal(pressure.rows, before.rows, 'attached history survives cache eviction pressure');
      assert.equal(pressure.pins.focused, true);
      assert.equal(pressure.assistant, true); assert.equal(pressure.user, true);
    }
    await page.click('#cell-0 .cell-close'); await attach(); await wait(1200);
    const reopened = await capture('reopened');
    if (legacy) assert.equal(reopened.rows, 0, 'baseline reopen stays answer-only');
    else assert.equal(reopened.rows, before.rows);

    {
      await page.eval(`(async () => {
        const input=document.querySelector('#cell-0 .slot-chat-compose-input');
        input.value='Unsent fixture draft';input.dispatchEvent(new Event('input',{bubbles:true}));
        const canvas=document.createElement('canvas');canvas.width=2;canvas.height=2;
        const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
        const transfer=new DataTransfer();transfer.items.add(new File([blob],'unsent.png',{type:'image/png'}));
        const file=document.querySelector('#cell-0 .slot-chat-attachment-input');
        file.files=transfer.files;file.dispatchEvent(new Event('change',{bubbles:true}));
      })()`);
      await page.waitFor("!!document.querySelector('#cell-0 .slot-chat-attachment-chip')", { timeoutMs: 5000 });
      const priorFetches = daemon.requests.filter(r => r.type === 'request_stream_events' && r.stream_id === targetId).length;
      // Mock only the host's advertised counterpart version; the browser runs
      // its real automatic poll and full rehydration through /cc and the daemon.
      web.handlers['get-build'].handler = () => ({ buildId: 'fixture-new-build' });
      for (let i = 0; i < 50 && daemon.requests.filter(r => r.type === 'request_stream_events' && r.stream_id === targetId).length === priorFetches; i++) await wait(100);
      const changed = await capture('changed-build');
      if (legacy) assert.equal(changed.fetches, priorFetches, 'baseline changed-build RED has no automatic history refetch');
      else assert.ok(changed.fetches > priorFetches, 'changed build automatically refetches bound history');
      assert.equal(changed.draft, 'Unsent fixture draft'); assert.equal(changed.attachments, 1);
      await wait(500);
      const repeated = await capture('same-build');
      assert.equal(repeated.fetches, changed.fetches, 'same build does not keep resetting history');
      const stateHandler = web.handlers['chat-stream:get-state'].handler;
      let stateReads = 0;
      web.handlers['chat-stream:get-state'].handler = (...args) => {
        stateReads++;
        if (stateReads === 1) throw new Error('fixture transient state read failure');
        return stateHandler(...args);
      };
      web.handlers['get-build'].handler = () => ({ buildId: 'fixture-retry-build' });
      for (let i = 0; !legacy && i < 50 && stateReads < 2; i++) await wait(100);
      await wait(200);
      const retried = await capture('retried-build');
      assert.equal(stateReads, legacy ? 0 : 2, 'failed build recovery retries on the next poll');
      assert.equal(retried.fetches, repeated.fetches + (legacy ? 0 : 1));
      assert.equal(retried.draft, 'Unsent fixture draft'); assert.equal(retried.attachments, 1);
      assert.equal(retried.assistant, !legacy); assert.equal(retried.user, !legacy);
      web.handlers['chat-stream:get-state'].handler = stateHandler;
    }
    await page.send('Page.reload'); await wait(800); await attach(); await wait(1000);
    const reloaded = await capture('reloaded');
    assert.equal(reloaded.rows, before.rows); assert.equal(reloaded.answers, 1);
    const requests = daemon.requests.filter(r => r.stream_id);
    assert.ok(requests.every(r => r.stream_id.startsWith('mock-host:')), 'only isolated fixture streams contacted');
    fs.writeFileSync(path.join(out, 'console.json'), JSON.stringify(page.consoleLines));
    fs.writeFileSync(path.join(out, 'verdict.json'), JSON.stringify({ status: legacy ? 'BASELINE_RED' : 'PASS', root,
      observations, fixtureConfigSha256: hash(daemon.configFile), harnessSha256: hash(__filename), nodeVersion: process.version, assets: ['bundle.js','chat_core.bundle.js'].map(file => ({ file,
        sha256: hash(path.join(root, 'renderer/dist/web', file)) })) }, null, 2));
    console.log(`${legacy ? 'BASELINE_RED' : 'PASS'} history retention/reconnect/reopen/reload/build recovery: ${out}`);
  } finally {
    page?.close();
    if (browser && browser.exitCode === null) {
      const exited = new Promise(resolve => browser.once('exit', resolve)); browser.kill('SIGTERM'); await exited;
    }
    if (web) await web.close(); await daemon.stop();
    fs.writeFileSync(path.join(out, 'cleanup.json'), JSON.stringify({ browserExited: !browser || browser.exitCode !== null || !!browser.signalCode,
      webClosed: true, fixtureDaemonClosed: true, liveSeatsTouched: 0 }));
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
