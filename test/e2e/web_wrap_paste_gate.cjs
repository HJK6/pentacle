#!/usr/bin/env node
'use strict';
// Real web composer/renderer; only the isolated loopback daemon is a fixture.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const root = process.env.PENTACLE_WRAP_PASTE_ROOT || path.resolve(__dirname, '../..');
const { startDaemon, targetId } = require(root + '/test/e2e/scenarios/assistant_direct');
const { getFreePort } = require(root + '/test/e2e/lib/disposable_web_daemon_config');
const { headlessChromeArgs } = require('./lib/chrome_launch.cjs');
const cdp = require(root + '/test/e2e/lib/cdp');
const out = path.resolve(process.argv[2] || fs.mkdtempSync(path.join(os.tmpdir(), 'web-wrap-paste-')));
const expectRed = process.argv.includes('--expect-red');
const browserBinary = process.env.PENTACLE_TEST_BROWSER || process.env.PENTACLE_CHROME || 'google-chrome';
const input = '#cell-0 .slot-chat-compose-input';
const chips = '#cell-0 .slot-chat-attachment-chip';
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const longUrl = 'https://example.com/' + 'abcdefghij'.repeat(35);
const longWord = 'LongWord' + 'abcdefghij'.repeat(35);
const label = 'LinkedLabel' + 'abcdefghij'.repeat(35);
const markdown = [longUrl, longWord, `[${label}](${longUrl})`, '`' + longWord + '`', '```\n' + longWord + '\n```'].join('\n\n');
const result = { subsystem: 'chat', bug_ref: 'spec_pentacle__web_wrap_image_paste_2026_10', geometry: [], paste: [] };

// This body executes in the browser and uses actual File/DataTransfer data.
function pasteFixture(options) {
  const e = document.querySelector('#cell-0 .slot-chat-compose-input');
  e.focus();
  if (options.draft !== undefined) {
    e.value = options.draft; e.dispatchEvent(new Event('input', { bubbles: true }));
    e.setSelectionRange(...(options.selection || [e.value.length, e.value.length]));
  }
  const canvas = document.createElement('canvas'); canvas.width = 3; canvas.height = 2;
  canvas.getContext('2d').fillRect(0, 0, 3, 2);
  const files = (options.mimes || ['image/png']).map((mime, i) => {
    let bytes = Uint8Array.from(atob(canvas.toDataURL(mime === 'image/jpeg' ? mime : 'image/png').split(',')[1]), c => c.charCodeAt(0));
    if (options.invalid === 'empty') bytes = new Uint8Array();
    if (options.invalid === 'corrupt') bytes = new Uint8Array([1, 2, 3]);
    if (options.size) { const padded = new Uint8Array(options.size); padded.set(bytes); bytes = padded; }
    return new File([bytes], `clipboard-${i}.${mime === 'image/jpeg' ? 'jpg' : 'png'}`, { type: mime });
  });
  const dt = new DataTransfer(); files.forEach(f => dt.items.add(f));
  if (options.text) dt.setData('text/plain', options.text);
  dt.setData('text/html', '<b>never insert HTML</b>');
  let clipboardData = dt;
  if (options.shape) clipboardData = {
    items: options.shape === 'files' ? [] : files.map(f => ({ kind: 'file', type: f.type,
      getAsFile: () => options.shape === 'null' ? null : f })),
    files: options.shape === 'items' || options.shape === 'null' ? [] : files,
    getData: type => dt.getData(type),
  };
  const event = new ClipboardEvent('paste', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'clipboardData', { value: clipboardData });
  let inputs = 0; const count = () => { inputs++; }; e.addEventListener('input', count);
  e.dispatchEvent(event); e.removeEventListener('input', count);
  return { prevented: event.defaultPrevented, inputs, caret: e.selectionStart,
    textCorrect: options.expected === undefined || e.value === options.expected,
    files: files.map(f => ({ mime: f.type, bytes: f.size })) };
}

async function main() {
  fs.mkdirSync(out, { recursive: true, mode: 0o700 });
  const daemon = await startDaemon({ artifactsDir: out });
  // Structured provenance exercises the real fenced-code path, without changing
  // the interpreter's policy for plain terminal transcripts.
  daemon.events.push({ ...daemon.events[0], event_id: 'wrap-structured', message_id: 'wrap-structured',
    daemon_seq: 2, text: markdown, raw: { source: 'claude-jsonl' } });
  let web, browser, page;
  const clear = async () => {
    await page.eval(`(() => { let e; while ((e = document.querySelector('${chips} .slot-chat-attachment-remove'))) e.click(); })()`);
    await page.waitFor(`document.querySelectorAll('${chips}').length === 0`);
  };
  const paste = async options => {
    const observed = await page.eval(`(${pasteFixture.toString()})(${JSON.stringify(options)})`);
    result.paste.push({ name: options.name, ...observed });
    return observed;
  };
  try {
    web = await require(root + '/server').main(['--profile', daemon.configFile, '--bind', '127.0.0.1', '--port', '0']);
    const port = await getFreePort();
    browser = spawn(browserBinary, [...headlessChromeArgs(), '--disable-gpu', `--user-data-dir=${out}/chrome`,
      `--remote-debugging-port=${port}`, 'about:blank'], { stdio: ['ignore', 'ignore', fs.openSync(out + '/chrome.log', 'w')] });
    page = await cdp.connect(port, { timeoutMs: 15000 });
    result.browser = await page.send('Browser.getVersion');
    await page.send('Page.navigate', { url: web.url });
    await page.waitFor(`document.querySelector('.session-item[data-stream-id="${targetId}"]')`, { timeoutMs: 20000 });
    await page.click(`.session-item[data-stream-id="${targetId}"]`);
    await page.click('#cell-0 .cell-view-toggle[data-mode="chat"]');
    await page.waitFor(`document.querySelector('${input}')`);
    await page.eval(`(() => { const e = document.querySelector('${input}'); e.value = ${JSON.stringify(longUrl + '\n\n' + longWord)};
      e.dispatchEvent(new Event('input', { bubbles: true })); e.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })); })()`);
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Echo:')");
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-md-code code')");
    for (const theme of ['default', 'chat-first']) for (const [width, height] of [[1440, 900], [390, 844]]) {
      await page.eval(`document.documentElement.classList.toggle('chat-first-style', ${theme === 'chat-first'})`);
      await page.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
      await wait(150);
      const observed = await page.eval(`(() => {
        const list = document.querySelector('#cell-0 .slot-chat-list');
        const all = [...list.querySelectorAll('.slot-chat-user-bubble,.slot-chat-assistant-card')];
        const nodes = [];
        for (const container of all) {
          const box = container.getBoundingClientRect(); const cs = getComputedStyle(container);
          const left = box.left + parseFloat(cs.paddingLeft) + parseFloat(cs.borderLeftWidth);
          const right = box.right - parseFloat(cs.paddingRight) - parseFloat(cs.borderRightWidth);
          const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
          while (walker.nextNode()) {
            const n = walker.currentNode;
            if (n.parentElement.closest('pre,button') || n.textContent.length < 300) continue;
            const range = document.createRange(); range.selectNodeContents(n);
            const rects = [...range.getClientRects()];
            nodes.push({ role: container.className, lines: rects.length,
              fits: rects.every(r => r.left >= left - 1 && r.right <= right + 1),
              exact: [${JSON.stringify(longUrl)}, ${JSON.stringify(longWord)}, ${JSON.stringify(label)}].some(t => n.textContent.includes(t)) });
          }
        }
        const pre = list.querySelector('.slot-chat-md-code'); const code = pre.querySelector('code');
        pre.scrollLeft = pre.scrollWidth;
        const link = [...list.querySelectorAll('a')].find(a => a.textContent === ${JSON.stringify(label)});
        return { nodes, transcriptFits: list.scrollWidth <= list.clientWidth + 1,
          pageFits: document.documentElement.scrollWidth <= innerWidth + 1,
          hrefExact: link?.getAttribute('href') === ${JSON.stringify(longUrl)},
          fenced: { fits: pre.getBoundingClientRect().width <= pre.parentElement.clientWidth + 1,
            scrolls: pre.scrollWidth > pre.clientWidth && pre.scrollLeft > 0,
            exact: code.textContent === ${JSON.stringify(longWord)}, whitespace: getComputedStyle(code).whiteSpace } };
      })()`);
      result.geometry.push({ theme, width, ...observed });
      await page.screenshot(`${out}/wrap-${theme}-${width}.png`);
    }
    await page.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
    const mixed = await paste({ name: 'mixed-selection', draft: 'before AFTER tail', selection: [7, 12], text: 'caption', expected: 'before caption tail' });
    await page.waitFor(`document.querySelectorAll('${chips}').length === 1`);
    await page.screenshot(out + '/mixed-paste.png');
    if (expectRed) {
      assert.ok(result.geometry.some(g => g.nodes.some(n => !n.fits)), 'baseline must reproduce wrapping RED');
      assert.equal(mixed.textCorrect, false, 'baseline must reproduce lost mixed text');
      result.status = 'PRODUCT_FAIL'; return;
    }
    for (const g of result.geometry) {
      assert.ok(g.nodes.length >= 6, JSON.stringify(g));
      assert.ok(g.nodes.every(n => n.fits && n.lines > 1 && n.exact), JSON.stringify(g));
      assert.ok(g.transcriptFits && g.pageFits && g.hrefExact, JSON.stringify(g));
      assert.deepEqual(g.fenced, { fits: true, scrolls: true, exact: true, whitespace: 'pre' });
    }
    assert.ok(mixed.textCorrect); assert.equal(mixed.inputs, 1); assert.equal(mixed.caret, 14);
    const undo = await page.eval(`(() => { document.execCommand('undo'); const e = document.querySelector('${input}'); const ok = e.value === 'before AFTER tail'; document.execCommand('redo'); return ok && e.value === 'before caption tail'; })()`);
    assert.equal(undo, true, 'mixed paste participates in native undo/redo');
    await clear();
    for (const shape of ['items', 'files', undefined]) {
      const p = await paste({ name: shape || 'both', shape, mimes: ['image/png', 'image/jpeg'], draft: '' });
      assert.equal(p.prevented, true);
      await page.waitFor(`document.querySelectorAll('${chips}').length === 2`); await clear();
    }
    for (const [name, options] of [['null-item', { shape: 'null' }], ['non-image', { mimes: ['application/pdf'], text: 'native' }]]) {
      const p = await paste({ name, ...options }); assert.equal(p.prevented, false);
    }
    await paste({ name: 'existing-valid', draft: '' });
    await page.waitFor(`document.querySelectorAll('${chips}').length === 1`);
    for (const options of [{ mimes: ['image/gif'] }, { invalid: 'empty' }, { invalid: 'corrupt' }, { size: 25 * 1024 * 1024 + 1 }]) {
      const p = await paste({ name: 'rejected-retains-text', ...options, draft: 'old', selection: [0, 3], text: 'retained', expected: 'retained' });
      assert.ok(p.textCorrect); await wait(150);
      assert.equal(await page.eval(`document.querySelectorAll('${chips}').length`), 1);
    }
    await clear();
    await paste({ name: '25MiB-boundary', size: 25 * 1024 * 1024, draft: '' });
    await page.waitFor(`document.querySelectorAll('${chips}').length === 1`, { timeoutMs: 15000 }); await clear();
    // Four concurrent paste handlers all decode, but their destination remains capped.
    await page.eval(`(() => { window.__pasteUrls = new Set(); const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
      URL.createObjectURL = value => { const url = create(value); window.__pasteUrls.add(url); return url; };
      URL.revokeObjectURL = url => { window.__pasteUrls.delete(url); return revoke(url); }; })()`);
    await page.eval(`(() => { const paste = ${pasteFixture.toString()}; for (let i = 0; i < 4; i++) paste({ mimes: ['image/png', 'image/jpeg'], draft: '' }); })()`);
    await page.waitFor(`document.querySelectorAll('${chips}').length === 5`); await wait(200);
    assert.equal(await page.eval('window.__pasteUrls.size'), 5);
    await paste({ name: 'sixth-image' }); await wait(100);
    assert.equal(await page.eval(`document.querySelectorAll('${chips}').length`), 5);
    await clear(); assert.equal(await page.eval('window.__pasteUrls.size'), 0);
    // Route changes while a real FileReader is pending preserve the original draft.
    await page.eval(`(() => { const read = FileReader.prototype.readAsDataURL;
      window.__resumePasteReads = []; FileReader.prototype.readAsDataURL = function(file) { window.__resumePasteReads.push(() => read.call(this, file)); };
      window.__restorePasteReads = () => { FileReader.prototype.readAsDataURL = read; for (const resume of window.__resumePasteReads) resume(); }; })()`);
    await paste({ name: 'pending-origin', draft: 'origin' });
    await page.click(`.session-item[data-stream-id="${daemon.sourceId}"]`);
    await page.waitFor(`document.querySelector('${input}')?.value !== 'origin'`);
    await page.eval('window.__restorePasteReads()'); await wait(150);
    assert.equal(await page.eval(`document.querySelectorAll('${chips}').length`), 0);
    await page.click(`.session-item[data-stream-id="${targetId}"]`);
    await page.waitFor(`document.querySelectorAll('${chips}').length === 1 && document.querySelector('${input}')?.value === 'origin'`);
    await clear();
    // Picker, drop and paste share the exact intake; remaining bytes/order survive upload.
    await paste({ name: 'send-png-jpeg', mimes: ['image/png', 'image/jpeg'], draft: 'image caption' });
    await page.waitFor(`document.querySelectorAll('${chips}').length === 2`);
    await page.click('#cell-0 .slot-chat-compose-send');
    await page.waitFor(`document.querySelectorAll('${chips}').length === 0`);
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-media-img')?.naturalWidth > 0", { timeoutMs: 15000 });
    const sent = daemon.requests.filter(r => r.type === 'send').at(-1);
    assert.deepEqual(sent.attachments.map(a => a.mime), ['image/png', 'image/jpeg']);
    assert.equal(daemon.requests.filter(r => r.type === 'upload_blob_init').length, 2);
    const chunks = daemon.requests.filter(r => r.type === 'upload_blob_chunk');
    for (const [index, attachment] of sent.attachments.entries()) {
      const bytes = Buffer.from(chunks[index].data_b64, 'base64');
      assert.equal(attachment.bytes, bytes.length); assert.equal(attachment.sha256, crypto.createHash('sha256').update(bytes).digest('hex'));
    }
    await page.click('#cell-0 .slot-chat-media-button');
    await page.waitFor("document.querySelector('.slot-chat-image-viewer.is-open .slot-chat-image-viewer-img')?.naturalWidth > 0");
    await page.screenshot(out + '/sent-media.png');
    await page.click('.slot-chat-image-viewer-close');
    // A failed counterpart upload keeps the draft retryable and sends nothing.
    daemon.setUploadFailure(true);
    await paste({ name: 'upload-retry', draft: 'retry caption' });
    await page.waitFor(`document.querySelectorAll('${chips}').length === 1`);
    const sendsBeforeFailure = daemon.requests.filter(r => r.type === 'send').length;
    await page.click('#cell-0 .slot-chat-compose-send');
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-error')?.textContent.includes('Disposable upload failure')");
    assert.equal(daemon.requests.filter(r => r.type === 'send').length, sendsBeforeFailure);
    assert.equal(await page.eval(`document.querySelectorAll('${chips}').length`), 1);
    assert.equal(await page.eval(`document.querySelector('${input}').value === 'retry caption'`), true);
    daemon.setUploadFailure(false);
    await page.click('#cell-0 .slot-chat-compose-send');
    await page.waitFor(`document.querySelectorAll('${chips}').length === 0`);
    assert.equal(daemon.requests.filter(r => r.type === 'send').length, sendsBeforeFailure + 1);
    // File picker and drag/drop still use the same real intake and image-only send.
    for (const via of ['picker', 'drop']) {
      await page.eval(`(() => {
        const c = document.createElement('canvas'); c.width = 2; c.height = 2;
        const bytes = Uint8Array.from(atob(c.toDataURL().split(',')[1]), x => x.charCodeAt(0));
        const dt = new DataTransfer(); dt.items.add(new File([bytes], '${via}.png', { type: 'image/png' }));
        if (${JSON.stringify(via)} === 'picker') {
          const e = document.querySelector('#cell-0 .slot-chat-attachment-input'); e.files = dt.files;
          e.dispatchEvent(new Event('change', { bubbles: true }));
        } else document.querySelector('#cell-0 .slot-chat-shell').dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
      })()`);
      await page.waitFor(`document.querySelectorAll('${chips}').length === 1`);
      if (via === 'picker') await clear();
    }
    await page.click('#cell-0 .slot-chat-compose-send');
    await page.waitFor(`document.querySelectorAll('${chips}').length === 0`);
    const imageOnly = daemon.requests.filter(r => r.type === 'send').at(-1);
    assert.equal(imageOnly.text, ''); assert.equal(imageOnly.attachments.length, 1);
    // Assistant entry uses its existing direct route, including the same upload.
    await page.click(`.session-item[data-stream-id="${daemon.sourceId}"]`);
    await page.waitFor(`document.querySelector('${input}')`);
    await paste({ name: 'assistant-entry', draft: 'assistant caption' });
    await page.waitFor(`document.querySelectorAll('${chips}').length === 1`);
    await page.click('#cell-0 .slot-chat-compose-send');
    await page.waitFor(`document.querySelectorAll('${chips}').length === 0`);
    const assistant = daemon.requests.filter(r => r.type === 'send').at(-1);
    assert.equal(assistant.session_name, 'live'); assert.equal(assistant.attachments.length, 1);
    result.send = { mixed: true, uploadFailureRetry: true, imageOnly: true, picker: true, drop: true, assistantDirect: true };
    const compositeDir = path.join(out, 'composite'); fs.mkdirSync(compositeDir);
    const composite = await startDaemon({ artifactsDir: compositeDir, directAssistant: false });
    let compositeWeb;
    try {
      compositeWeb = await require(root + '/server').main(['--profile', composite.configFile, '--bind', '127.0.0.1', '--port', '0']);
      await page.send('Page.navigate', { url: compositeWeb.url });
      await page.waitFor(`document.querySelector('.session-item[data-stream-id="${composite.sourceId}"]')`);
      await page.click(`.session-item[data-stream-id="${composite.sourceId}"]`);
      await page.waitFor(`document.querySelector('${input}')`);
      const caption = '  composite caption  ';
      await paste({ name: 'assistant-composite', draft: caption });
      await page.waitFor(`document.querySelectorAll('${chips}').length === 1`);
      await page.click('#cell-0 .slot-chat-compose-send');
      await page.waitFor(`document.querySelectorAll('${chips}').length === 0`);
      const sentComposite = composite.requests.filter(r => r.type === 'send').at(-1);
      assert.equal(sentComposite.stream_id, composite.sourceId);
      assert.equal(sentComposite.message, caption); assert.equal(sentComposite.attachments.length, 1);
      await page.waitFor("document.querySelector('#cell-0 .slot-chat-media-img')?.naturalWidth > 0");
      result.send.assistantComposite = true;
    } finally { await compositeWeb?.close(); await composite.stop(); }
    result.status = 'PASS';
  } finally {
    fs.writeFileSync(out + '/observations.json', JSON.stringify(result, null, 2));
    page?.close();
    if (browser && browser.exitCode === null) {
      const exited = new Promise(resolve => browser.once('exit', resolve)); browser.kill('SIGTERM'); await exited;
    }
    await web?.close(); await daemon.stop();
  }
}
main().then(() => console.log(JSON.stringify({ status: result.status, evidence: out })))
  .catch(error => { console.error(error); process.exitCode = 1; });
