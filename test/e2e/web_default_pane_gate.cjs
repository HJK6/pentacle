#!/usr/bin/env node
'use strict';
// First-visit workspace default in real Chrome against a disposable daemon:
// with an assistant composite (here bound to a direct-assistant target) a
// fresh profile shows one pane holding the assistant chat, bound exactly as a
// sidebar click binds it; the pane and its unsent draft survive a reload; a
// reload while the target is on another generation retires the pane (never a
// stale target) and it does not reopen later.
// Without a composite the first visit shows one empty pane. Only the loopback
// daemon counterpart is a fixture.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { startDaemon } = require('./scenarios/assistant_direct');
const { getFreePort } = require('./lib/disposable_web_daemon_config');
const cdp = require('./lib/cdp');
const root = path.resolve(path.join(__dirname, '../..'));
const out = path.resolve(process.argv[2] || fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-default-pane-')));
const browserBinary = process.env.PENTACLE_TEST_BROWSER || process.env.PENTACLE_CHROME || 'google-chrome';
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const DRAFT = 'Unsent words for the assistant';

function readDirectPane(page, daemon) {
  return page.eval(`(() => ({
    copyId: document.querySelector('#header-0 .cell-copyid')?.dataset.streamId || null,
    lamp: !!document.querySelector('#header-0 .cell-assistant-icon'),
    label: document.querySelector('#header-0 .cell-label')?.textContent || null,
    draft: document.querySelector('#cell-0 .slot-chat-compose-input')?.value ?? null,
    binding: (JSON.parse(localStorage.getItem('pentacle.workspace.v1') || 'null')?.bindings || [])[0] || null,
    generation: window.PentacleChatStore?.state?.sessions?.find(s => s.stream_id === ${JSON.stringify(daemon.targetId)})?.session_generation || null,
  }))()`);
}

// Reload and wait for the next document: the old one carries a marker, so a
// condition it already satisfies cannot pass for the reloaded page.
async function reloadPage(page) {
  await page.eval('window.__defaultPaneOldDocument = true');
  await page.send('Page.reload', {});
  await page.waitFor("window.__defaultPaneOldDocument !== true && document.readyState !== 'loading'", { timeoutMs: 15000 });
}

// Returning profile: reload keeps the direct pane and draft; a changed target
// generation never opens a stale target and retires the saved pane.
async function directJourney(page, daemon) {
  const target = JSON.stringify(daemon.targetId);
  const steps = {};
  await page.waitFor(`document.querySelector('#header-0 .cell-copyid')?.dataset.streamId === ${target}`, { timeoutMs: 15000 });
  steps.firstVisit = await readDirectPane(page, daemon);
  await page.eval(`(() => { const input = document.querySelector('#cell-0 .slot-chat-compose-input');
    input.value = ${JSON.stringify(DRAFT)}; input.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
  await reloadPage(page);
  await page.waitFor(`document.querySelector('#header-0 .cell-copyid')?.dataset.streamId === ${target}
    && document.querySelector('#cell-0 .slot-chat-compose-input')?.value === ${JSON.stringify(DRAFT)}`, { timeoutMs: 15000 });
  steps.reloaded = await readDirectPane(page, daemon);
  daemon.setGeneration('rotated-generation');
  await page.waitFor(`window.PentacleChatStore?.state?.sessions?.find(s => s.stream_id === ${target})?.session_generation === 'rotated-generation'`, { timeoutMs: 15000 });
  await reloadPage(page);
  await page.waitFor(`window.PentacleChatStore?.state?.sessions?.find(s => s.stream_id === ${target})?.session_generation === 'rotated-generation'
    && document.querySelectorAll('#session-list .session-item').length > 0`, { timeoutMs: 15000 });
  await wait(1500);
  steps.staleGeneration = await readDirectPane(page, daemon);
  daemon.setGeneration(daemon.generation);
  await page.waitFor(`window.PentacleChatStore?.state?.sessions?.find(s => s.stream_id === ${target})?.session_generation === ${JSON.stringify(daemon.generation)}`, { timeoutMs: 15000 });
  await wait(1500);
  steps.afterValid = await readDirectPane(page, daemon);
  return steps;
}

async function firstVisit(name, { omitAssistant }) {
  const dir = path.join(out, name);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const daemon = await startDaemon({ artifactsDir: dir, omitAssistant });
  let web, browser, page;
  try {
    web = await require(path.join(root, 'server')).main(['--profile', daemon.configFile, '--bind', '127.0.0.1', '--port', '0']);
    const port = await getFreePort();
    browser = spawn(browserBinary, ['--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-gpu',
      '--window-size=1600,900', `--user-data-dir=${path.join(dir, 'chrome')}`, `--remote-debugging-port=${port}`, 'about:blank'], { stdio: 'ignore' });
    page = await cdp.connect(port, { timeoutMs: 15000 });
    await page.send('Page.navigate', { url: web.url });
    await page.waitFor("document.querySelectorAll('#session-list .session-item').length > 0", { timeoutMs: 15000 });
    await wait(1500);
    const observed = await page.eval(`(() => ({
      paneCount: document.querySelector('.grid').dataset.paneCount,
      pressed: document.querySelector('[data-pane-count-button][aria-pressed="true"]')?.dataset.paneCountButton,
      shown: [0, 1, 2, 3].filter(slot => getComputedStyle(document.getElementById('cell-' + slot)).display !== 'none'),
      occupied: [0, 1, 2, 3].filter(slot => document.getElementById('cell-' + slot).classList.contains('occupied')),
      label: document.querySelector('#cell-0 .cell-label')?.textContent,
      chat: getComputedStyle(document.querySelector('#cell-0 .slot-chat-layer') || document.body).display !== 'none'
        && !!document.querySelector('#cell-0 .slot-chat-layer'),
      rows: document.querySelectorAll('#session-list .session-item').length,
    }))()`);
    const { data } = await page.send('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(dir, 'first-visit.png'), Buffer.from(data, 'base64'));
    const direct = omitAssistant ? null : await directJourney(page, daemon);
    fs.writeFileSync(path.join(dir, 'observed.json'), JSON.stringify({ observed, direct }, null, 2));
    return { observed, direct, spawned: daemon.requests.filter(r => r.type === 'spawn').length,
      sourceId: daemon.sourceId, targetId: daemon.targetId, generation: daemon.generation };
  } finally {
    page?.close();
    if (browser && browser.exitCode === null) {
      const exited = new Promise(resolve => browser.once('exit', resolve)); browser.kill('SIGTERM'); await exited;
    }
    if (web) await web.close();
    await daemon.stop();
  }
}

(async () => {
  fs.mkdirSync(out, { recursive: true, mode: 0o700 });
  const withAssistant = await firstVisit('with-assistant', { omitAssistant: false });
  assert.equal(withAssistant.observed.paneCount, '1', JSON.stringify(withAssistant));
  assert.equal(withAssistant.observed.pressed, '1');
  assert.deepEqual(withAssistant.observed.shown, [0]);
  assert.deepEqual(withAssistant.observed.occupied, [0]);
  assert.equal(withAssistant.observed.chat, true);
  assert.equal(withAssistant.spawned, 0);
  const { direct, sourceId, targetId, generation } = withAssistant;
  for (const step of ['firstVisit', 'reloaded']) {
    assert.equal(direct[step].copyId, targetId, step);
    assert.equal(direct[step].lamp, true, step);
    assert.equal(direct[step].label, 'Assistant', step);
    assert.equal(direct[step].generation, generation, step);
  }
  assert.equal(direct.reloaded.draft, DRAFT);
  assert.equal(direct.reloaded.binding.assistantSourceId, sourceId);
  assert.equal(direct.staleGeneration.copyId, null, 'a changed generation must not open a stale target');
  assert.equal(direct.staleGeneration.generation, 'rotated-generation');
  assert.equal(direct.staleGeneration.binding, null, 'the stale saved pane is retired');
  assert.equal(direct.afterValid.copyId, null, 'restore runs once; a retired pane does not reopen');
  const without = await firstVisit('without-assistant', { omitAssistant: true });
  assert.equal(without.observed.paneCount, '1', JSON.stringify(without));
  assert.equal(without.observed.pressed, '1');
  assert.deepEqual(without.observed.shown, [0]);
  assert.deepEqual(without.observed.occupied, []);
  assert.equal(without.spawned, 0);
  fs.writeFileSync(path.join(out, 'verdict.json'), JSON.stringify({ status: 'PASS', withAssistant, without }, null, 2));
  console.log(`PASS first-visit workspace default (assistant / no assistant): ${out}`);
  process.exit(0);
})().catch(error => { console.error('FAIL first-visit workspace default:', error.stack || error.message); process.exit(1); });
