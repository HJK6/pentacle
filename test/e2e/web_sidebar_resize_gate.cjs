#!/usr/bin/env node
'use strict';

// Real Chromium layout oracle against a disposable web host and chat daemon.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { startDaemon } = require('./scenarios/assistant_direct');
const { chromium } = require(process.env.PENTACLE_PLAYWRIGHT || 'playwright');

const out = path.resolve(process.argv[2] || fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-sidebar-')));
const browserBinary = process.env.PENTACLE_TEST_BROWSER || process.env.PENTACLE_CHROME;
const hash = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function run() {
  fs.mkdirSync(out, { recursive: true, mode: 0o700 });
  const daemon = await startDaemon({ artifactsDir: out });
  const emptyHistoryNegative = process.env.PENTACLE_SIDEBAR_EMPTY_HISTORY_NEGATIVE === '1';
  if (!emptyHistoryNegative) daemon.seedAssistantHistory('Sidebar popout history probe');
  daemon.seedHistoryPressure({ targetRows: 205, noiseRows: 430 });
  process.env.PENTACLE_CONFIG = daemon.configFile;
  const web = await require('../../server').main(['--profile', daemon.configFile, '--bind', '127.0.0.1', '--port', '0']);
  const browser = await chromium.launch({ headless: true, executablePath: browserBinary,
    args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
  const samples = [];
  const extra = {};
  const read = () => page.evaluate(() => {
    const width = element => element.getBoundingClientRect().width;
    const row = name => [...document.querySelector(`.grid-row[data-row="${name}"]`).querySelectorAll('.grid-cell')].map(width);
    const handle = document.getElementById('sidebar-resizer');
    const settings = JSON.parse(localStorage.getItem('pentacle.settings.v1') || '{}');
    return { sidebar: width(document.querySelector('.sidebar')), top: row('top'), bottom: row('bottom'),
      visible: [...document.querySelectorAll('.grid-cell')].filter(e => getComputedStyle(e).display !== 'none').map(e => e.id),
      scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
      aria: { role: handle.getAttribute('role'), name: handle.getAttribute('aria-label'),
        orientation: handle.getAttribute('aria-orientation'), min: Number(handle.getAttribute('aria-valuemin')),
        max: Number(handle.getAttribute('aria-valuemax')), now: Number(handle.getAttribute('aria-valuenow')) },
      settings: settings.appearance || {}, narrow: document.body.classList.contains('web-sidebar-narrow') };
  });
  async function drag(delta) {
    const url = page.url();
    const box = await page.locator('#sidebar-resizer').boundingBox();
    assert.ok(box && box.width >= 10, 'focusable sidebar edge exists');
    const x = box.x + box.width / 2, y = box.y + Math.min(100, box.height / 2);
    await page.mouse.move(x, y);
    await page.mouse.down();
    assert.equal(await page.locator('#sidebar-resizer').evaluate(e => e.hasPointerCapture(window.__sidebarPointer)), true,
      'pointer captured during drag');
    await page.mouse.move(x + delta, y, { steps: 5 });
    await page.mouse.up();
    await sleep(80);
    assert.equal(await page.locator('#sidebar-resizer').evaluate(e => e.hasPointerCapture(window.__sidebarPointer)), false,
      'pointer capture released after drag');
    assert.equal(await page.evaluate(() => window.getSelection()?.toString() || ''), '', 'drag did not select text');
    assert.equal(page.url(), url, 'drag did not navigate');
  }
  function compare(before, after, delta, label) {
    const actual = after.sidebar - before.sidebar;
    assert.ok(Math.abs(actual - delta) <= 2, `${label}: sidebar delta ${actual} vs ${delta}`);
    for (const row of ['top', 'bottom']) {
      for (let i = 0; i < 2; i++) assert.ok(Math.abs((after[row][i] - before[row][i]) + actual / 2) <= 2,
        `${label}: ${row} slot ${i} half transfer`);
      assert.ok(Math.abs((after[row][0] - after[row][1]) - (before[row][0] - before[row][1])) <= 2,
        `${label}: ${row} difference retained`);
    }
    assert.ok(after.scrollWidth <= after.clientWidth + 1, `${label}: no horizontal overflow`);
    samples.push({ label, before, after, delta: actual });
  }
  try {
    await page.addInitScript(() => {
      document.addEventListener('pointerdown', event => {
        if (event.target?.id === 'sidebar-resizer') window.__sidebarPointer = event.pointerId;
      }, true);
    });
    await page.goto(web.url);
    await page.waitForSelector('body.web-sidebar-resizable #sidebar-resizer[aria-valuenow]');
    let before = await read();
    assert.ok(Math.abs(before.top[0] - before.top[1]) <= 2 && Math.abs(before.bottom[0] - before.bottom[1]) <= 2);
    await drag(100); let after = await read(); compare(before, after, 100, 'equal wider');
    before = after; await drag(-60); after = await read(); compare(before, after, -60, 'equal narrower');

    await page.evaluate(() => {
      const settings = JSON.parse(localStorage.getItem('pentacle.settings.v1') || '{}');
      settings.appearance = { ...settings.appearance, gridColSplitTop: .65, gridColSplitBottom: .35 };
      localStorage.setItem('pentacle.settings.v1', JSON.stringify(settings));
    });
    await page.reload(); await page.waitForSelector('body.web-sidebar-resizable #sidebar-resizer[aria-valuenow]');
    before = await read();
    assert.ok(before.top[0] - before.top[1] > 200 && before.bottom[0] - before.bottom[1] < -200);
    await drag(90); after = await read(); compare(before, after, 90, 'unequal wider');
    before = after; await drag(-70); after = await read(); compare(before, after, -70, 'unequal narrower');

    const handle = page.locator('#sidebar-resizer');
    await handle.focus();
    await page.keyboard.press('Shift+Tab'); await page.keyboard.press('Tab');
    assert.equal(await handle.evaluate(e => document.activeElement === e), true);
    assert.equal(after.aria.role, 'separator'); assert.equal(after.aria.orientation, 'vertical');
    assert.equal(after.aria.name, 'Sidebar width');
    assert.ok(Math.abs(after.aria.min - 260) <= 2, 'ARIA minimum matches measured sidebar floor');
    assert.ok(Math.abs(after.aria.now - after.sidebar) <= 2);
    const widestDifference = Math.max(...['top', 'bottom'].map(row => Math.abs(after[row][0] - after[row][1])));
    assert.ok(Math.abs(after.aria.max - (1600 - 441 - widestDifference)) <= 2,
      'ARIA maximum matches first row slot floor');
    before = after; await page.keyboard.press('ArrowRight'); after = await read(); compare(before, after, 10, 'keyboard right');
    before = after; await page.keyboard.press('ArrowLeft'); after = await read(); compare(before, after, -10, 'keyboard left');
    await page.keyboard.press('End'); after = await read();
    assert.ok(Math.abs(after.sidebar - after.aria.max) <= 2);
    assert.ok([...after.top, ...after.bottom].every(width => width >= 219));
    assert.ok(after.scrollWidth <= after.clientWidth + 1);
    await page.keyboard.press('Home'); after = await read(); assert.ok(Math.abs(after.sidebar - 260) <= 2);

    await drag(150);
    const saved = await read();
    await page.reload(); await page.waitForSelector('body.web-sidebar-resizable #sidebar-resizer[aria-valuenow]');
    const restored = await read();
    assert.ok(Math.abs(restored.sidebar - saved.sidebar) <= 2);
    assert.ok(Math.abs(restored.top[0] - saved.top[0]) <= 2);
    assert.ok(Math.abs(restored.bottom[0] - saved.bottom[0]) <= 2);
    const preferences = restored.settings;
    await page.setViewportSize({ width: 850, height: 900 }); await sleep(100);
    const medium = await read();
    assert.equal(medium.narrow, false); assert.ok([...medium.top, ...medium.bottom].every(width => width >= 219));
    assert.equal(JSON.stringify(medium.settings), JSON.stringify(preferences));
    await page.setViewportSize({ width: 620, height: 900 }); await sleep(100);
    const narrow = await read();
    assert.equal(narrow.narrow, true); assert.deepEqual(narrow.visible, ['cell-0']);
    assert.ok(narrow.top[0] > 0 && narrow.scrollWidth <= narrow.clientWidth + 1);
    assert.equal(JSON.stringify(narrow.settings), JSON.stringify(preferences));
    const narrowMoved = [];
    let narrowBefore = narrow;
    for (const movement of [-60, 40, 100, -500]) {
      await drag(movement);
      const narrowAfter = await read();
      const effective = narrowAfter.sidebar - narrowBefore.sidebar;
      assert.ok(Math.abs((narrowAfter.top[0] - narrowBefore.top[0]) + effective) <= 2,
        'narrow sole visible slot receives the full effective sidebar delta');
      assert.ok(narrowAfter.top[0] > 0 && narrowAfter.scrollWidth <= narrowAfter.clientWidth + 1);
      assert.deepEqual(narrowAfter.visible, ['cell-0']);
      narrowMoved.push({ requested: movement, effective, sidebar: narrowAfter.sidebar,
        soleSlot: narrowAfter.top[0], min: narrowAfter.aria.min, max: narrowAfter.aria.max });
      narrowBefore = narrowAfter;
    }
    assert.ok(Math.abs(narrowMoved[2].sidebar - narrowMoved[2].max) <= 2,
      'narrow widening stops at the one-slot maximum');
    assert.ok(Math.abs(narrowMoved[3].sidebar - narrowMoved[3].min) <= 2,
      'narrow narrowing stops at the sidebar minimum');
    extra.narrowDrag = narrowMoved;
    await page.evaluate(preference => {
      const settings = JSON.parse(localStorage.getItem('pentacle.settings.v1') || '{}');
      settings.appearance = { ...settings.appearance, ...preference };
      localStorage.setItem('pentacle.settings.v1', JSON.stringify(settings));
    }, preferences);
    await page.reload(); await page.waitForSelector('body.web-sidebar-resizable #sidebar-resizer[aria-valuenow]');
    assert.equal(JSON.stringify((await read()).settings), JSON.stringify(preferences));
    await page.screenshot({ path: path.join(out, 'sidebar-narrow-620.png') });
    for (let i = 0; i < 4; i++) {
      await page.locator(`[data-narrow-slot="${i}"]`).click();
      const visible = await read(); assert.deepEqual(visible.visible, [`cell-${i}`]);
    }
    await page.setViewportSize({ width: 1600, height: 900 }); await sleep(100);
    const wideAgain = await read();
    assert.equal(wideAgain.narrow, false);
    assert.ok(Math.abs(wideAgain.sidebar - restored.sidebar) <= 2);
    assert.ok(Math.abs(wideAgain.top[0] - restored.top[0]) <= 2);
    assert.ok(Math.abs(wideAgain.bottom[0] - restored.bottom[0]) <= 2);

    // Each legacy row divider remains independent after sidebar movement.
    const topDivider = page.locator('.grid-row[data-row="top"] .grid-col-resizer');
    const bottomDivider = page.locator('.grid-row[data-row="bottom"] .grid-col-resizer');
    const splitBefore = (await read()).settings;
    const topBox = await topDivider.boundingBox();
    await page.mouse.move(topBox.x + topBox.width / 2, topBox.y + topBox.height / 2);
    await page.mouse.down();
    await page.mouse.move(topBox.x + topBox.width / 2 + 36, topBox.y + topBox.height / 2, { steps: 5 });
    await page.mouse.up(); await sleep(80);
    const splitDragged = (await read()).settings;
    assert.notEqual(splitDragged.gridColSplitTop, splitBefore.gridColSplitTop);
    assert.equal(splitDragged.gridColSplitBottom, splitBefore.gridColSplitBottom);
    await topDivider.focus(); await page.keyboard.press('ArrowRight');
    const splitTop = (await read()).settings;
    assert.notEqual(splitTop.gridColSplitTop, splitDragged.gridColSplitTop);
    assert.equal(splitTop.gridColSplitBottom, splitDragged.gridColSplitBottom);
    await bottomDivider.focus(); await page.keyboard.press('ArrowLeft');
    const splitBottom = (await read()).settings;
    assert.equal(splitBottom.gridColSplitTop, splitTop.gridColSplitTop);
    assert.notEqual(splitBottom.gridColSplitBottom, splitTop.gridColSplitBottom);
    await topDivider.dblclick();
    const splitReset = (await read()).settings;
    assert.ok(Math.abs(splitReset.gridColSplitTop - .5) < .001, 'top divider reset');
    assert.equal(splitReset.gridColSplitBottom, splitBottom.gridColSplitBottom);
    const bottomBox = await bottomDivider.boundingBox();
    await page.mouse.move(bottomBox.x + bottomBox.width / 2, bottomBox.y + bottomBox.height / 2);
    await page.mouse.down();
    await page.mouse.move(bottomBox.x + bottomBox.width / 2 - 36, bottomBox.y + bottomBox.height / 2, { steps: 5 });
    await page.mouse.up(); await sleep(80);
    const bottomDragged = (await read()).settings;
    assert.equal(bottomDragged.gridColSplitTop, splitReset.gridColSplitTop);
    assert.notEqual(bottomDragged.gridColSplitBottom, splitReset.gridColSplitBottom);
    await bottomDivider.dblclick();
    const bottomReset = (await read()).settings;
    assert.equal(bottomReset.gridColSplitTop, bottomDragged.gridColSplitTop);
    assert.ok(Math.abs(bottomReset.gridColSplitBottom - .5) < .001, 'bottom divider reset');
    extra.dividers = { splitBefore, splitDragged, splitTop, splitBottom, splitReset, bottomDragged, bottomReset };

    // A live disposable transcript keeps its unsent draft and scroll anchor.
    await page.evaluate(() => {
      window.__fitCalls = [];
      window.cc.createPty = async (slot, _session, _host, cols, rows) => {
        window.__fitCalls.push({ slot, cols, rows, initial: true });
        return '%disposable-sidebar-pane';
      };
      window.cc.resizePty = (slot, cols, rows) => window.__fitCalls.push({ slot, cols, rows });
    });
    await page.locator('.session-item[data-stream-id="mock-host:live"]').click();
    await page.locator('#header-0 .cell-view-toggle[data-mode="chat"]').click();
    await page.waitForFunction(() => document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Target history 204'));
    await page.locator('#cell-0 .slot-chat-compose-input').fill('Unsent sidebar draft');
    const scroll = page.locator('#cell-0 .slot-chat-scroll');
    await scroll.evaluate(e => { e.scrollTop = Math.max(0, e.scrollHeight / 2); });
    const transcriptBefore = await page.evaluate(() => ({ draft: document.querySelector('#cell-0 .slot-chat-compose-input').value,
      scrollTop: document.querySelector('#cell-0 .slot-chat-scroll').scrollTop,
      rows: document.querySelectorAll('#cell-0 .slot-chat-row').length,
      fitCalls: window.__fitCalls.length }));
    await drag(50);
    const transcriptAfter = await page.evaluate(() => ({ draft: document.querySelector('#cell-0 .slot-chat-compose-input').value,
      scrollTop: document.querySelector('#cell-0 .slot-chat-scroll').scrollTop,
      rows: document.querySelectorAll('#cell-0 .slot-chat-row').length,
      fitCalls: window.__fitCalls.length }));
    assert.equal(transcriptAfter.draft, transcriptBefore.draft);
    assert.equal(transcriptAfter.rows, transcriptBefore.rows);
    assert.ok(Math.abs(transcriptAfter.scrollTop - transcriptBefore.scrollTop) <= 2);
    extra.transcript = { transcriptBefore, transcriptAfter };

    await page.locator('#header-0 .cell-view-toggle[data-mode="terminal"]').click();
    await sleep(100);
    const terminalGeometry = () => page.evaluate(() => {
      const terminal = document.querySelector('#cell-0 .cell-terminal');
      const screen = terminal?.querySelector('.xterm-screen');
      const latest = window.__fitCalls.filter(call => call.slot === 0).at(-1);
      const box = terminal?.getBoundingClientRect(), screenBox = screen?.getBoundingClientRect();
      return { calls: window.__fitCalls.length, cols: latest?.cols, rows: latest?.rows,
        width: box?.width, right: box?.right, screenWidth: screenBox?.width, screenRight: screenBox?.right };
    });
    const terminalBefore = await terminalGeometry();
    await drag(50);
    const terminalAfter = await terminalGeometry();
    assert.ok(terminalAfter.calls > terminalBefore.calls, 'active disposable terminal was refit after sidebar resize');
    const glyphWidth = terminalBefore.screenWidth / terminalBefore.cols;
    const initialPadding = terminalBefore.width - terminalBefore.screenWidth;
    assert.ok(glyphWidth > 0 && initialPadding >= 0, 'terminal glyph/padding baseline is measurable');
    assert.ok(terminalAfter.cols < terminalBefore.cols, 'resized PTY has fewer columns after sidebar widens');
    const expectedColumns = Math.floor((terminalAfter.width - initialPadding) / glyphWidth);
    assert.ok(Math.abs(terminalAfter.cols - expectedColumns) <= 1,
      `PTY columns ${terminalAfter.cols} match resized cell width ${terminalAfter.width}, expected ${expectedColumns}`);
    assert.ok(Math.abs(terminalAfter.screenWidth - terminalAfter.cols * glyphWidth) <= glyphWidth,
      'rendered terminal screen matches requested columns');
    assert.ok(terminalAfter.screenRight <= terminalAfter.right + 2,
      'terminal screen does not clip beyond its container');
    extra.terminalFit = { terminalBefore, terminalAfter, glyphWidth, initialPadding, expectedColumns };
    await page.setViewportSize({ width: 620, height: 900 }); await sleep(100);
    await page.locator('[data-narrow-slot="1"]').click();
    await page.locator('[data-narrow-slot="0"]').click();
    await page.locator('#header-0 .cell-view-toggle[data-mode="chat"]').click();
    const narrowChat = await page.evaluate(() => {
      const input = document.querySelector('#cell-0 .slot-chat-compose-input');
      const box = input.getBoundingClientRect();
      return { draft: input.value, box: { x: box.x, y: box.y, width: box.width, height: box.height },
        hit: input.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)),
        scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth };
    });
    assert.equal(narrowChat.draft, 'Unsent sidebar draft');
    assert.ok(narrowChat.box.width > 0 && narrowChat.box.height > 0 && narrowChat.hit,
      'narrow chat composer remains usable');
    assert.ok(narrowChat.scrollWidth <= narrowChat.clientWidth + 1);
    extra.narrowChat = narrowChat;
    await page.setViewportSize({ width: 1600, height: 900 }); await sleep(100);

    // One-slot maximized geometry transfers the full effective delta.
    await page.locator('#header-0 .cell-maximize').click();
    const maxBefore = await read();
    await drag(40); const maxAfter = await read();
    assert.ok(Math.abs((maxAfter.top[0] - maxBefore.top[0]) + (maxAfter.sidebar - maxBefore.sidebar)) <= 2);
    await page.locator('#header-0 .cell-maximize').click();
    extra.maximized = { before: maxBefore, after: maxAfter };

    // The chat popout uses the real browser entry but has no sidebar resize UI.
    const popout = await browser.newPage({ viewport: { width: 850, height: 900 } });
    try {
      const context = { stream_id: 'mock-host:assistant', host: 'mock-host',
        desktop_host: 'mock-host', session_name: 'assistant', title: 'Disposable assistant' };
      await popout.goto(`${web.url}?pentacle-chat-popout=${encodeURIComponent(JSON.stringify(context))}`);
      await popout.waitForSelector('body.chat-popout #cell-0 .slot-chat-list');
      await popout.locator('#cell-0 .slot-chat-list').getByText('Sidebar popout history probe', { exact: true })
        .waitFor({ state: 'visible', timeout: emptyHistoryNegative ? 3000 : 10000 });
      const state = await popout.evaluate(() => ({
        popout: document.body.classList.contains('chat-popout'),
        resizable: document.body.classList.contains('web-sidebar-resizable'),
        handleVisible: getComputedStyle(document.getElementById('sidebar-resizer')).display !== 'none',
        chatVisible: !![...document.querySelectorAll('#cell-0 .slot-chat-list *')]
          .find(node => node.textContent?.trim() === 'Sidebar popout history probe' && getComputedStyle(node).display !== 'none'),
      }));
      assert.deepEqual(state, { popout: true, resizable: false, handleVisible: false, chatVisible: true });
      extra.popout = state;
    } finally { await popout.close(); }

    // Pointer cancellation restores the committed width and releases capture.
    const cancelBefore = await read();
    const cancelBox = await handle.boundingBox();
    const cancelX = cancelBox.x + 5, cancelY = cancelBox.y + 100;
    await page.mouse.move(cancelX, cancelY); await page.mouse.down();
    await page.mouse.move(cancelX + 50, cancelY, { steps: 5 });
    await page.locator('#sidebar-resizer').evaluate(e => e.dispatchEvent(new PointerEvent('pointercancel', {
      bubbles: true, pointerId: window.__sidebarPointer, isPrimary: true })));
    await page.mouse.up(); await sleep(80);
    const cancelAfter = await read();
    assert.ok(Math.abs(cancelAfter.sidebar - cancelBefore.sidebar) <= 2);
    assert.equal(await handle.evaluate(e => e.hasPointerCapture(window.__sidebarPointer)), false);
    extra.cancel = { before: cancelBefore.sidebar, after: cancelAfter.sidebar };

    // Dark and light keyboard/contrast paths at desktop and narrow widths.
    extra.theme = [];
    for (const theme of ['dark', 'light']) for (const width of [1600, 850]) {
      await page.setViewportSize({ width, height: 900 });
      await page.evaluate(theme => {
        const record = JSON.parse(localStorage.getItem('pentacle.settings.v1') || '{}');
        record.appearance = { ...record.appearance, sidebarWidth: 260,
          gridColSplitTop: .5, gridColSplitBottom: .5, theme };
        localStorage.setItem('pentacle.settings.v1', JSON.stringify(record));
      }, theme);
      await page.reload(); await page.waitForSelector('body.web-sidebar-resizable #sidebar-resizer[aria-valuenow]');
      await handle.focus();
      const focus = await handle.evaluate(e => ({ outline: getComputedStyle(e).outlineStyle,
        line: getComputedStyle(e, '::after').backgroundColor,
        background: getComputedStyle(document.querySelector('.sidebar')).backgroundColor,
        theme: document.documentElement.dataset.theme }));
      assert.equal(focus.theme, theme);
      await page.screenshot({ path: path.join(out, `sidebar-${theme}-${width}.png`) });
      assert.notEqual(focus.outline, 'none');
      const color = input => input.match(/[\d.]+/g).slice(0, 3).map(Number).map(value => {
        const normalized = value / 255; return normalized <= .04045 ? normalized / 12.92 : ((normalized + .055) / 1.055) ** 2.4;
      });
      const lum = input => color(input).reduce((sum, channel, i) => sum + channel * [.2126, .7152, .0722][i], 0);
      const a = lum(focus.line), b = lum(focus.background);
      const contrast = (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
      assert.ok(contrast >= 3, `${theme}/${width} handle contrast ${contrast}`);
      const keyBefore = await read(); await page.keyboard.press('ArrowRight'); const keyRight = await read();
      assert.ok(Math.abs(keyRight.sidebar - keyBefore.sidebar - 10) <= 2);
      await page.keyboard.press('ArrowLeft'); const keyLeft = await read();
      assert.ok(Math.abs(keyLeft.sidebar - keyBefore.sidebar) <= 2);
      extra.theme.push({ theme, width, focus, contrast, keyBefore: keyBefore.sidebar,
        keyRight: keyRight.sidebar, keyLeft: keyLeft.sidebar });
    }

    const result = { status: 'PASS', source: require('node:child_process').execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
      bundleSha256: hash(path.join(__dirname, '../../renderer/dist/web/bundle.js')),
      stylesSha256: hash(path.join(__dirname, '../../renderer/dist/web/styles.css')),
      samples, saved, restored, medium, narrow, wideAgain, extra };
    fs.writeFileSync(path.join(out, 'verdict.json'), JSON.stringify(result, null, 2));
    console.log(`PASS web sidebar resize: ${out}`);
  } finally {
    await browser.close(); await web.close(); await daemon.stop();
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
