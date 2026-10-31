#!/usr/bin/env node
'use strict';

// Real layout and persistence against the built web entry and disposable daemon.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { startDaemon } = require('./scenarios/assistant_direct');
const { chromium } = require(process.env.PENTACLE_PLAYWRIGHT || 'playwright');
const out = path.resolve(process.argv[2] || fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-chat-width-')));
const hash = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');

async function run() {
  fs.mkdirSync(out, { recursive: true, mode: 0o700 });
  process.env.PENTACLE_RUNTIME_DIR = path.join(out, 'runtime');
  fs.mkdirSync(process.env.PENTACLE_RUNTIME_DIR, { recursive: true, mode: 0o700 });
  let daemon, web, browser;
  const samples = [];
  const measure = page => page.evaluate(() => {
    const cell = document.querySelector('#cell-0');
    const read = selector => {
      const node = cell.querySelector(selector);
      return { width: node.getBoundingClientRect().width, cap: getComputedStyle(node).maxWidth };
    };
    const input = cell.querySelector('.slot-chat-compose-input');
    const rect = input.getBoundingClientRect();
    return { list: read('.slot-chat-list'), composer: read('.slot-chat-compose'),
      earlier: read('.slot-chat-load-earlier'), pane: cell.getBoundingClientRect().width,
      scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
      text: cell.querySelector('.slot-chat-list').textContent,
      users: cell.querySelectorAll('.slot-chat-row.is-user').length,
      draft: input.value, inputUsable: rect.width > 0 && rect.height > 0 &&
        input.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)),
      preference: JSON.parse(localStorage.getItem('pentacle.settings.v1') || '{}').appearance?.chatWidth };
  });
  async function check(page, label, cap, reachesCap) {
    await page.waitForFunction(px => getComputedStyle(document.querySelector('#cell-0 .slot-chat-list')).maxWidth === `${px}px`, cap);
    const sample = await measure(page);
    samples.push({ label, ...sample });
    for (const item of [sample.list, sample.composer, sample.earlier]) {
      assert.equal(item.cap, `${cap}px`, `${label}: computed cap`);
      assert.ok(item.width <= Math.min(cap, sample.pane) + 1, `${label}: fits pane and cap`);
    }
    assert.ok(sample.scrollWidth <= sample.clientWidth + 1, `${label}: no document overflow`);
    assert.ok(sample.text.includes('Target history 24') || sample.text.includes('Width popout history'), `${label}: populated history`);
    assert.ok(sample.inputUsable, `${label}: usable composer`);
    if (reachesCap) {
      assert.ok(Math.abs(sample.list.width - cap) <= 1, `${label}: list reaches ${cap}, got ${sample.list.width}`);
      assert.ok(Math.abs(sample.composer.width - cap) <= 1, `${label}: composer reaches ${cap}, got ${sample.composer.width}`);
      assert.ok(Math.abs(sample.list.width - sample.composer.width) <= 1, `${label}: equal capped widths`);
    }
    return sample;
  }
  try {
    daemon = await startDaemon({ artifactsDir: out });
    daemon.seedHistoryPressure({ targetRows: 25, noiseRows: 0 });
    daemon.seedAssistantHistory('Width popout history');
    process.env.PENTACLE_CONFIG = daemon.configFile;
    web = await require('../../server').main(['--profile', daemon.configFile, '--bind', '127.0.0.1', '--port', '0']);
    browser = await chromium.launch({ headless: true,
      executablePath: process.env.PENTACLE_TEST_BROWSER || process.env.PENTACLE_CHROME, args: ['--no-sandbox'] });
    const context = await browser.newContext({ viewport: { width: 1800, height: 1000 } });
    const page = await context.newPage();
    await page.goto(web.url);
    // Terminal transport is the disposable counterpart; the chat UI is unmodified.
    await page.evaluate(() => { window.cc.createPty = async () => '%width-disposable'; window.cc.resizePty = () => {}; });
    await page.locator('.session-item[data-stream-id="mock-host:live"]').click();
    await page.locator('#header-0 .cell-view-toggle[data-mode="chat"]').click();
    await page.waitForFunction(() => document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Target history 24'));
    await page.locator('#header-0 .cell-maximize').click();
    await page.locator('#cell-0 .slot-chat-compose-input').fill('Unsent width draft');
    await page.locator('#settings-btn').click();
    assert.equal(await page.locator('[data-setting="chatWidth"]').count(), 1, 'Chat width setting exists');
    assert.equal(await page.locator('[data-setting="chatWidth"] .settings-row-desc').count(), 0);
    await page.locator('#settings-close').click();
    await check(page, 'initial default', 760, true);
    for (const [value, cap] of [['narrow', 600], ['default', 760], ['wide', 1000]]) {
      await page.locator('#settings-btn').click();
      await page.locator(`[data-setting="chatWidth"] [data-value="${value}"]`).click();
      await page.locator('#settings-close').click();
      const sample = await check(page, value, cap, true);
      assert.equal(sample.draft, 'Unsent width draft');
      assert.ok(sample.users > 0, 'user and assistant history are present');
      assert.equal(sample.preference, value);
    }
    await page.screenshot({ path: path.join(out, 'wide.png') });
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Target history 24'));
    // Maximization is transient; establish sufficient room again after workspace restoration.
    await page.locator('[data-pane-count-button="1"]').click();
    await check(page, 'reload wide', 1000, true);
    await page.locator('#settings-btn').click();
    assert.equal(await page.locator('[data-setting="chatWidth"] [data-value="wide"]').getAttribute('aria-pressed'), 'true');
    await page.locator('#settings-close').click();
    await page.locator('[data-pane-count-button="4"]').click();
    await check(page, 'split wide cap', 1000, false);
    await page.setViewportSize({ width: 390, height: 900 });
    await page.waitForSelector('body.web-sidebar-narrow');
    await check(page, 'narrow viewport', 1000, false);
    await page.screenshot({ path: path.join(out, 'narrow.png') });
    const popout = await context.newPage();
    const binding = { stream_id: 'mock-host:assistant', host: 'mock-host', desktop_host: 'mock-host',
      session_name: 'assistant', title: 'Disposable assistant' };
    await popout.goto(`${web.url}?pentacle-chat-popout=${encodeURIComponent(JSON.stringify(binding))}`);
    await popout.waitForFunction(() => document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Width popout history'));
    await check(popout, 'popout saved wide', 1000, true);
    fs.writeFileSync(path.join(out, 'verdict.json'), JSON.stringify({ status: 'PASS',
      source: execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
      inputs: Object.fromEntries(['renderer/app.js', 'renderer/chat_v3.css', 'test/e2e/web_chat_width_gate.cjs',
        'renderer/dist/web/bundle.js', 'renderer/dist/web/styles.css'].map(file => [file, hash(path.join(__dirname, '../..', file))])),
      samples }, null, 2));
    console.log(`PASS web chat width: ${out}`);
  } catch (error) {
    fs.writeFileSync(path.join(out, 'failure.json'), JSON.stringify({ error: error.stack, samples }, null, 2));
    throw error;
  } finally {
    try { await browser?.close(); } finally { try { await web?.close(); } finally { await daemon?.stop(); } }
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
