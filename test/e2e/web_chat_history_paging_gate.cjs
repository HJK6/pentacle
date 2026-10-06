#!/usr/bin/env node
'use strict';
// Older persisted history is reachable: a tool-heavy chat larger than the daemon's
// newest-events window (and the store's per-stream cap) must page back to its first
// message through "Load earlier messages". Actual browser/store/web host/transport;
// only the loopback daemon counterpart is a fixture, and it models the real window
// (newest `limit` events below before_daemon_seq, clamped to 500).
//   PENTACLE_GATE_EXPECT_LEGACY=1 records the baseline RED instead of asserting the fix.
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
const out = path.resolve(process.argv[2] || fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-paging-')));
const legacy = process.env.PENTACLE_GATE_EXPECT_LEGACY === '1';
const browserBinary = process.env.PENTACLE_TEST_BROWSER || process.env.PENTACLE_CHROME || 'google-chrome';
const TURNS = 240; // 1440 events: three daemon windows, above the 1200-event store cap
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const hash = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');

async function run() {
  fs.mkdirSync(out, { recursive: true, mode: 0o700 });
  const daemon = await startDaemon({ artifactsDir: out });
  daemon.seedWindowedHistory({ turns: TURNS, window: 500 });
  let web, browser, page;
  const observations = {};
  try {
    web = await require(path.join(root, 'server')).main(['--profile', daemon.configFile, '--bind', '127.0.0.1', '--port', '0']);
    const port = await getFreePort();
    browser = spawn(browserBinary, ['--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-gpu',
      '--disable-renderer-backgrounding', '--disable-background-timer-throttling', '--disable-backgrounding-occluded-windows',
      '--window-size=1600,900', `--user-data-dir=${path.join(out, 'chrome')}`, `--remote-debugging-port=${port}`, 'about:blank'], { stdio: 'ignore' });
    page = await cdp.connect(port, { timeoutMs: 15000 });
    await page.send('Page.navigate', { url: web.url });
    await page.waitFor(`document.querySelector('.session-item[data-stream-id="${targetId}"]')`, { timeoutMs: 15000 });
    await page.click(`.session-item[data-stream-id="${targetId}"]`);
    await page.click('#cell-0 .cell-view-toggle[data-mode="chat"]');
    await page.waitFor(`document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Paged turn ${TURNS} assistant')`, { timeoutMs: 15000 });
    const pagedRequests = () => daemon.requests.filter(r => r.type === 'request_stream_events' && r.stream_id === targetId);
    const capture = async name => {
      const data = await page.eval(`(() => {
        const list = document.querySelector('#cell-0 .slot-chat-list');
        const text = list ? list.textContent : '';
        const button = document.querySelector('#cell-0 .slot-chat-load-earlier');
        const users = new Set((text.match(/Paged turn \\d+ user/g) || []));
        const assistants = new Set((text.match(/Paged turn \\d+ assistant/g) || []));
        const store = window.PentacleChatStore.getState();
        return { users: users.size, assistants: assistants.size, firstUser: text.includes('Paged turn 1 user'),
          firstAssistant: text.includes('Paged turn 1 assistant'),
          buttonVisible: !!button && getComputedStyle(button).display !== 'none', buttonDisabled: !!button && button.disabled,
          storeEvents: store.events.filter(e => e.stream_id === ${JSON.stringify(targetId)}).length,
          expanded: store.historyExpandedStreamIds || [], buildId: window.__PENTACLE_CONFIG__.buildId };
      })()`);
      observations[name] = { ...data, fetches: pagedRequests().length };
      fs.writeFileSync(path.join(out, `${name}.json`), JSON.stringify(observations[name], null, 2));
      await page.screenshot(path.join(out, `${name}.png`));
      return observations[name];
    };
    const initial = await capture('initial');
    assert.equal(initial.fetches, 1, 'one newest-window request on open');
    assert.equal(pagedRequests()[0].before_daemon_seq, undefined);
    assert.equal(initial.firstUser, false, 'the newest window does not contain the first message');
    if (legacy) {
      // Baseline RED: Load earlier only reveals rows the store already holds; once those are
      // exhausted the control hides and the older persisted messages are unreachable.
      for (let i = 0; i < 60 && (await capture('loop')).buttonVisible; i++) { await page.click('#cell-0 .slot-chat-load-earlier'); await wait(150); }
      const clicked = await capture('after-clicks');
      assert.equal(clicked.buttonVisible, false, 'baseline: control hides after the held window');
      assert.equal(clicked.firstUser, false, 'baseline: first message unreachable');
      assert.equal(clicked.fetches, 1, 'baseline never requests older history');
      assert.ok(clicked.users < TURNS, `baseline shows ${clicked.users}/${TURNS} user messages`);
    } else {
      assert.equal(initial.buttonVisible, true, 'control stays available while the daemon has older history');
      for (let i = 0; i < 120 && !(await capture('loop')).firstUser; i++) {
        const state = observations.loop;
        assert.equal(state.buttonVisible, true, `control stays until the first message is reached (click ${i})`);
        await page.click('#cell-0 .slot-chat-load-earlier');
        await page.waitFor("!document.querySelector('#cell-0 .slot-chat-load-earlier')?.disabled", { timeoutMs: 10000 });
        await wait(80);
      }
      const reached = await capture('reached-first');
      assert.equal(reached.firstUser, true); assert.equal(reached.firstAssistant, true);
      assert.equal(reached.users, TURNS, 'every persisted user message is reachable');
      assert.equal(reached.assistants, TURNS, 'every persisted assistant message is reachable');
      assert.ok(reached.storeEvents > 1200, `store lifted its per-stream cap for the paged chat (${reached.storeEvents})`);
      assert.deepEqual(reached.expanded, [targetId]);
      // Daemon contract: windows of at most 500, each strictly older than the last.
      const cursors = pagedRequests().slice(1).map(r => r.before_daemon_seq);
      assert.ok(cursors.length >= 2 && cursors.length <= 4, `bounded older-page requests: ${cursors.length}`);
      assert.ok(cursors.every(Number.isFinite) && cursors.every((c, i) => i === 0 || c < cursors[i - 1]), 'cursor moves strictly older');
      assert.ok(pagedRequests().slice(1).every(r => r.limit === 500));
      // The history start hides the control; live appends keep arriving.
      await page.waitFor("(() => { const b = document.querySelector('#cell-0 .slot-chat-load-earlier'); return !b || getComputedStyle(b).display === 'none'; })()", { timeoutMs: 8000 });
      daemon.appendTarget('Live append after paging');
      await page.waitFor("document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Live append after paging')", { timeoutMs: 8000 });
      // Detaching releases the expansion and the paging cursor.
      await page.click('#cell-0 .cell-close'); await wait(500);
      const detached = await page.eval('(window.PentacleChatStore.getState().historyExpandedStreamIds || []).length');
      assert.equal(detached, 0, 'detaching releases the history expansion');
    }
    assert.ok(daemon.requests.filter(r => r.stream_id).every(r => r.stream_id.startsWith('mock-host:')), 'only isolated fixture streams contacted');
    fs.writeFileSync(path.join(out, 'console.json'), JSON.stringify(page.consoleLines));
    fs.writeFileSync(path.join(out, 'verdict.json'), JSON.stringify({ status: legacy ? 'BASELINE_RED' : 'PASS', root,
      observations, fixtureConfigSha256: hash(daemon.configFile), harnessSha256: hash(__filename), nodeVersion: process.version,
      assets: ['bundle.js', 'chat_core.bundle.js'].map(file => ({ file, sha256: hash(path.join(root, 'renderer/dist/web', file)) })) }, null, 2));
    console.log(`${legacy ? 'BASELINE_RED' : 'PASS'} web history paging: ${out}`);
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
