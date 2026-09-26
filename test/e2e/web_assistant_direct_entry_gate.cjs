#!/usr/bin/env node
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { startDaemon, sourceId, targetId, generation } = require('./scenarios/assistant_direct');
const { getFreePort } = require('./lib/disposable_web_daemon_config');
const cdp = require('./lib/cdp');
const ROOT = path.resolve(__dirname, '../..');
const INSTALLED = process.argv[2] === '--installed';
const OUT = path.resolve((INSTALLED ? process.argv[5] : process.argv[2]) || fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-direct-gate-')));
const browserBinary = process.env.PENTACLE_TEST_BROWSER || process.env.PENTACLE_CHROME || 'google-chrome';
const hash = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');

async function runInstalled() {
  const url = process.argv[3];
  const identityFile = process.argv[4];
  if (!url || !identityFile) throw new Error('Usage: --installed URL private-identity.json output-dir');
  const expected = JSON.parse(fs.readFileSync(identityFile, 'utf8'));
  for (const key of ['sourceStreamId', 'streamId', 'generation']) assert.ok(expected[key], `${key} required`);
  fs.mkdirSync(OUT, { recursive: true, mode: 0o700 });
  let browser, page;
  const cells = [];
  try {
    const debugPort = await getFreePort();
    browser = spawn(browserBinary, ['--headless=new', '--no-first-run', '--no-default-browser-check', '--no-sandbox',
      '--ignore-certificate-errors', '--disable-gpu', '--window-size=1600,900',
      `--user-data-dir=${path.join(OUT, 'browser-profile')}`, `--remote-debugging-port=${debugPort}`, url], { stdio: 'ignore' });
    page = await cdp.connect(debugPort, { timeoutMs: 15000 });
    await page.waitFor(`document.querySelector('.session-item[data-stream-id=${JSON.stringify(expected.sourceStreamId)}]')`, { timeoutMs: 15000 });
    const binding = await page.eval('window.__PENTACLE_CONFIG__.features.assistantDirectTarget');
    assert.deepEqual(binding, expected);
    const current = await page.eval(`window.PentacleChatStore?.snapshot?.()?.sessions?.find(s=>s.stream_id===${JSON.stringify(expected.streamId)})`);
    assert.equal(current?.session_generation, expected.generation);
    assert.equal(current?.online, true);
    for (const theme of ['dark', 'light']) for (const width of [1600, 850]) {
      await page.send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false });
      await page.eval(`document.documentElement.dataset.theme=${JSON.stringify(theme)}`);
      const row = `.session-item[data-stream-id="${expected.sourceStreamId}"]`;
      await page.click(row);
      await page.waitFor(`document.querySelector('#cell-0 .slot-chat-list')?.dataset.streamId === ${JSON.stringify(expected.streamId)}`, { timeoutMs: 10000 });
      await page.waitFor("document.querySelector('#cell-0 .slot-chat-row') || document.querySelector('#cell-0 .slot-chat-list article')", { timeoutMs: 10000 });
      const click = await page.eval(`(() => ({ width: innerWidth, theme: document.documentElement.dataset.theme,
        label:document.querySelector('#header-0 .cell-label')?.textContent,
        lamp:!!document.querySelector('#header-0 .cell-assistant-icon'),
        copyId:document.querySelector('#header-0 .cell-copyid')?.dataset.streamId,
        sourceTag:document.querySelector('#header-0 .cell-source-tag')?.textContent,
        rows:document.querySelectorAll('#cell-0 .slot-chat-row').length,
        officialName:document.querySelector(${JSON.stringify(row)})?.getAttribute('aria-label'),
        targetRow:!!document.querySelector('.session-item[data-stream-id="${expected.streamId}"]') }))()`);
      assert.equal(click.width, width);
      assert.equal(click.theme, theme);
      assert.equal(click.lamp, true);
      assert.equal(click.copyId, expected.streamId);
      assert.equal(click.targetRow, true);
      assert.ok(click.rows > 0);
      await page.eval(`document.querySelector(${JSON.stringify(row)}).focus()`);
      await page.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
      await page.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
      assert.equal(await page.eval("document.querySelector('#header-0 .cell-copyid')?.dataset.streamId"), expected.streamId);
      await page.screenshot(path.join(OUT, `installed-${theme}-${width}.png`));
      cells.push(click);
    }
    const assetHashes = await page.eval(`(async()=>{const files=['bundle.js','styles.css','chat_v3.css'];const out={};for(const file of files){const r=await fetch(file,{cache:'no-store'});const bytes=await r.arrayBuffer();const digest=await crypto.subtle.digest('SHA-256',bytes);out[file]=Array.from(new Uint8Array(digest)).map(x=>x.toString(16).padStart(2,'0')).join('')}return out})()`);
    const result = { status: 'PASS', scope: 'installed read-only direct entry; no real target send',
      url, source: require('node:child_process').execFileSync('git', ['rev-parse', 'HEAD'], { cwd: ROOT, encoding: 'utf8' }).trim(),
      buildId: await page.eval('window.__PENTACLE_CONFIG__.buildId'), binding, current: { stream_id: current.stream_id, session_generation: current.session_generation, online: current.online }, cells, assetHashes };
    fs.writeFileSync(path.join(OUT, 'verdict.json'), JSON.stringify(result, null, 2));
    console.log(`PASS installed assistant direct entry: ${OUT}`);
  } finally {
    page?.close();
    browser?.kill('SIGTERM');
  }
}

async function run() {
  fs.mkdirSync(OUT, { recursive: true, mode: 0o700 });
  const daemon = await startDaemon({ artifactsDir: OUT });
  let web, browser, page;
  const steps = [];
  try {
    process.env.PENTACLE_CONFIG = daemon.configFile;
    web = await require('../../server').main(['--profile', daemon.configFile, '--bind', '127.0.0.1', '--port', '0']);
    const debugPort = await getFreePort();
    browser = spawn(browserBinary, ['--headless=new', '--no-first-run', '--no-default-browser-check', '--no-sandbox',
      '--disable-gpu', '--window-size=1600,900', `--user-data-dir=${path.join(OUT, 'browser-profile')}`,
      `--remote-debugging-port=${debugPort}`, web.url], { stdio: 'ignore' });
    page = await cdp.connect(debugPort, { timeoutMs: 15000 });
    await page.waitFor(`document.querySelector('.session-item[data-stream-id=${JSON.stringify(sourceId)}]')`, { timeoutMs: 15000 });
    const configReadback = await page.eval('window.__PENTACLE_CONFIG__.features.assistantDirectTarget');
    assert.deepEqual(configReadback, { sourceStreamId: sourceId, streamId: targetId, generation });
    steps.push({ name: 'private profile exact binding readback', ok: true, configReadback });
    assert.equal(await page.click(`.session-item[data-stream-id="${sourceId}"]`), true);
    try { await page.waitFor(`document.querySelector('#cell-0 .slot-chat-list')?.dataset.streamId === ${JSON.stringify(targetId)}`, { timeoutMs: 10000 }); }
    catch (error) {
      console.error('alias diagnostic', await page.eval(`(() => ({ body: document.body.innerText.slice(0, 900), header: document.querySelector('#header-0')?.innerText, list: document.querySelector('#cell-0 .slot-chat-list')?.dataset.streamId, toast: document.querySelector('.toast')?.innerText, slot: document.querySelector('#cell-0')?.innerText.slice(0, 600) }))()`));
      await page.screenshot(path.join(OUT, 'direct-entry-error.png'));
      throw error;
    }
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Existing disposable transcript')", { timeoutMs: 10000 });
    const selected = await page.eval(`(() => { const h=document.querySelector('#header-0'); const s=document.querySelector('.session-item[data-stream-id="${sourceId}"]'); const t=document.querySelector('.session-item[data-stream-id="${targetId}"]'); return { label:h?.querySelector('.cell-label')?.textContent, lamp:!!h?.querySelector('.cell-assistant-icon'), copyId:h?.querySelector('.cell-copyid')?.dataset.streamId, sourceTag:h?.querySelector('.cell-source-tag')?.textContent, sourceIcon:s?.querySelector('.s-machine-avatar')?.className, targetIcon:t?.querySelector('.s-machine-avatar')?.className, targetRow:!!t, listText:document.querySelector('#cell-0 .slot-chat-list')?.textContent }; })()`);
    assert.equal(selected.label, 'Assistant');
    assert.equal(selected.lamp, true);
    assert.equal(selected.copyId, targetId);
    assert.equal(selected.targetRow, true);
    assert.match(selected.sourceIcon, /forest-green/);
    assert.doesNotMatch(selected.targetIcon, /forest-green/);
    assert.match(selected.listText, /Existing disposable transcript/);
    steps.push({ name: 'official click opens existing target with green lamp and real copy ID', ok: true, selected });
    await page.type('#cell-0 .slot-chat-compose-input', 'Disposable click send');
    assert.equal(await page.click('#cell-0 .slot-chat-compose-send'), true);
    try { await page.waitFor(`document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Echo: Disposable click send')`, { timeoutMs: 10000 }); }
    catch (error) {
      console.error('send diagnostic', { requests: daemon.requests.filter(r => ['send', 'request_stream_events'].includes(r.type)), events: daemon.events,
        ui: await page.eval("({ list: document.querySelector('#cell-0 .slot-chat-list')?.innerText, error: document.querySelector('#cell-0 .slot-chat-error')?.innerText, draft: document.querySelector('#cell-0 .slot-chat-compose-input')?.value })") });
      await page.screenshot(path.join(OUT, 'direct-send-error.png'));
      throw error;
    }
    assert.equal(daemon.requests.filter(r => r.type === 'send' && r.host === 'mock-host' && r.session_name === 'live').length, 1);
    assert.equal(daemon.requests.filter(r => r.type === 'send' && r.session_name === 'assistant').length, 0);
    steps.push({ name: 'real web bridge click send receives correlated disposable echo', ok: true });
    await page.screenshot(path.join(OUT, 'direct-entry-click.png'));
    assert.equal(await page.click(`.session-item[data-stream-id="${targetId}"]`), true);
    await page.waitFor("document.querySelector('#header-0 .cell-label')?.textContent === 'Disposable target'", { timeoutMs: 5000 });
    assert.equal(await page.eval("!!document.querySelector('#header-0 .cell-assistant-icon')"), false);
    await page.eval(`document.querySelector('.session-item[data-stream-id="${sourceId}"]').focus()`);
    await page.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
    await page.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
    await page.waitFor("document.querySelector('#header-0 .cell-label')?.textContent === 'Assistant' && !!document.querySelector('#header-0 .cell-assistant-icon')", { timeoutMs: 5000 });
    assert.equal(await page.eval("document.querySelector('#header-0 .cell-copyid')?.dataset.streamId"), targetId);
    await page.type('#cell-0 .slot-chat-compose-input', 'Disposable keyboard send');
    assert.equal(await page.click('#cell-0 .slot-chat-compose-send'), true);
    try { await page.waitFor("document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Echo: Disposable keyboard send')", { timeoutMs: 10000 }); }
    catch (error) {
      console.error('reload diagnostic', { requests: daemon.requests.filter(r => ['send', 'request_stream_events'].includes(r.type)), events: daemon.events,
        ui: await page.eval("({ list: document.querySelector('#cell-0 .slot-chat-list')?.innerText, id: document.querySelector('#cell-0 .slot-chat-list')?.dataset.streamId, header: document.querySelector('#header-0')?.innerText, error: document.querySelector('#cell-0 .slot-chat-error')?.innerText })") });
      throw error;
    }
    assert.equal(daemon.requests.filter(r => r.type === 'send' && r.session_name === 'live').length, 2);
    assert.equal(daemon.requests.filter(r => r.type === 'send' && r.session_name === 'assistant').length, 0);
    steps.push({ name: 'ordinary selection clears alias; keyboard reentry sends to same target', ok: true });
    await page.screenshot(path.join(OUT, 'direct-entry-keyboard.png'));
    daemon.setGeneration('replacement-generation');
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-error')?.textContent.includes('Direct assistant target is unavailable')", { timeoutMs: 5000 });
    await page.type('#cell-0 .slot-chat-compose-input', 'Keep this draft during replacement');
    const sendsBeforeStale = daemon.requests.filter(r => r.type === 'send').length;
    await page.click('#cell-0 .slot-chat-compose-send');
    assert.equal(daemon.requests.filter(r => r.type === 'send').length, sendsBeforeStale);
    assert.equal(await page.eval("document.querySelector('#cell-0 .slot-chat-compose-input')?.value"), 'Keep this draft during replacement');
    steps.push({ name: 'generation replacement fails closed and preserves draft', ok: true });
    daemon.setGeneration(generation);
    await page.waitFor("!document.querySelector('#cell-0 .slot-chat-error')?.textContent.includes('Direct assistant target is unavailable')", { timeoutMs: 5000 });
    await page.send('Page.reload');
    await page.waitFor("performance.getEntriesByType('navigation')[0]?.type === 'reload' && document.readyState === 'complete'", { timeoutMs: 15000 });
    await page.waitFor(`document.querySelector('.session-item[data-stream-id=${JSON.stringify(sourceId)}]')`, { timeoutMs: 15000 });
    await page.click(`.session-item[data-stream-id="${sourceId}"]`);
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Echo: Disposable keyboard send')", { timeoutMs: 10000 });
    steps.push({ name: 'reload retains target transcript with profile binding', ok: true });
    const addImage = async () => {
      const prepared = await page.eval(`(async () => {
        const canvas=document.createElement('canvas');canvas.width=2;canvas.height=2;
        canvas.getContext('2d').fillRect(0,0,2,2);
        const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
        const file=new File([blob],'disposable.png',{type:'image/png'});
        const transfer=new DataTransfer();transfer.items.add(file);
        const input=document.querySelector('#cell-0 .slot-chat-attachment-input');
        input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}));
        return {bytes:blob.size,mime:blob.type};
      })()`);
      assert.equal(prepared.mime, 'image/png');
      await page.waitFor("!!document.querySelector('#cell-0 .slot-chat-attachment-chip')", { timeoutMs: 5000 });
      return prepared;
    };
    const preparedCaption = await addImage();
    await page.type('#cell-0 .slot-chat-compose-input', 'Disposable image caption');
    await page.click('#cell-0 .slot-chat-compose-send');
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Echo: Disposable image caption')", { timeoutMs: 10000 });
    const imageCaptionSend = daemon.requests.filter(r => r.type === 'send').at(-1);
    assert.equal(imageCaptionSend.host, 'mock-host');
    assert.equal(imageCaptionSend.session_name, 'live');
    assert.equal(imageCaptionSend.attachments?.length, 1);
    assert.equal(imageCaptionSend.attachments[0].mime, 'image/png');
    const preparedOnly = await addImage();
    await page.click('#cell-0 .slot-chat-compose-send');
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Echo: [image]')", { timeoutMs: 10000 });
    const imageOnlySend = daemon.requests.filter(r => r.type === 'send').at(-1);
    assert.equal(imageOnlySend.host, 'mock-host');
    assert.equal(imageOnlySend.session_name, 'live');
    assert.equal(imageOnlySend.attachments?.length, 1);
    assert.equal(String(imageOnlySend.text || ''), '');
    assert.equal(daemon.requests.filter(r => r.type === 'send' && r.session_name === 'assistant').length, 0);
    steps.push({ name: 'image with caption and image only upload and send to exact target', ok: true,
      preparedCaption, preparedOnly, hashes: [imageCaptionSend.attachments[0].key, imageOnlySend.attachments[0].key] });
    // The disposable daemon is an event ingester: unlike a tmux `cat` fixture,
    // it persists USER attachment metadata and returns it on history reload.
    // Use a PNG larger than one wire chunk to cover upload, transcript reload,
    // and byte-exact blob retrieval in the same browser journey.
    const largeImage = await page.eval(`(async () => {
      const canvas=document.createElement('canvas');canvas.width=900;canvas.height=600;
      const context=canvas.getContext('2d');const pixels=context.createImageData(900,600);
      for(let i=0;i<pixels.data.length;i+=65536)crypto.getRandomValues(pixels.data.subarray(i,Math.min(i+65536,pixels.data.length)));
      context.putImageData(pixels,0,0);
      const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
      const digest=await crypto.subtle.digest('SHA-256',await blob.arrayBuffer());
      const sha=[...new Uint8Array(digest)].map(x=>x.toString(16).padStart(2,'0')).join('');
      const transfer=new DataTransfer();transfer.items.add(new File([blob],'large-disposable.png',{type:'image/png'}));
      const input=document.querySelector('#cell-0 .slot-chat-attachment-input');
      input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}));
      return {size:blob.size,sha};
    })()`);
    assert.ok(largeImage.size > 2 * 1024 * 1024, 'fixture must cross two 1 MiB wire chunks');
    await page.waitFor("!!document.querySelector('#cell-0 .slot-chat-attachment-chip')", { timeoutMs: 5000 });
    await page.type('#cell-0 .slot-chat-compose-input', 'Disposable large image');
    await page.click('#cell-0 .slot-chat-compose-send');
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Echo: Disposable large image')", { timeoutMs: 15000 });
    const largeSend = daemon.requests.filter(r => r.type === 'send').at(-1);
    assert.equal(largeSend.attachments?.length, 1);
    assert.equal(largeSend.attachments[0].key, largeImage.sha);
    await page.send('Page.reload');
    await page.waitFor("performance.getEntriesByType('navigation')[0]?.type === 'reload' && document.readyState === 'complete'", { timeoutMs: 15000 });
    await page.waitFor(`document.querySelector('.session-item[data-stream-id=${JSON.stringify(sourceId)}]')`, { timeoutMs: 15000 });
    await page.click(`.session-item[data-stream-id="${sourceId}"]`);
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Disposable large image')", { timeoutMs: 10000 });
    const largeReload = await page.eval(`(async () => {
      const row=[...document.querySelectorAll('#cell-0 .slot-chat-user-bubble')].find(x=>x.textContent.includes('Disposable large image'));
      const media=row?.closest('.slot-chat-row')?.querySelector('.slot-chat-media-button');
      const result=await window.cc.chatFetchBlob(${JSON.stringify(largeImage.sha)});
      const bytes=Uint8Array.from(atob(result.content_b64||''),ch=>ch.charCodeAt(0));
      const digest=await crypto.subtle.digest('SHA-256',bytes);
      return {row:!!row,media:!!media,mediaCount:document.querySelectorAll('#cell-0 .slot-chat-media-button').length,ok:result.ok,size:bytes.length,
        sha:[...new Uint8Array(digest)].map(x=>x.toString(16).padStart(2,'0')).join('')};
    })()`);
    assert.equal(largeReload.row, true);
    assert.equal(largeReload.media, true);
    assert.equal(largeReload.ok, true);
    assert.equal(largeReload.size, largeImage.size);
    assert.equal(largeReload.sha, largeImage.sha);
    steps.push({ name: 'multi-chunk PNG event ingested and exact media restored after browser reload', ok: true,
      size: largeImage.size, sha: largeImage.sha, reload: largeReload });
    await page.waitFor("!!document.querySelector('#cell-0 .slot-chat-reply-btn[data-reply-message-id=\"direct-event-1\"]')", { timeoutMs: 5000 });
    await page.click('#cell-0 .slot-chat-reply-btn[data-reply-message-id="direct-event-1"]');
    assert.equal(await page.eval("document.querySelector('#cell-0 .slot-chat-reply-preview')?.hidden"), false);
    await page.type('#cell-0 .slot-chat-compose-input', 'Disposable reply');
    await page.click('#cell-0 .slot-chat-compose-send');
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Echo: Disposable reply')", { timeoutMs: 10000 });
    const replySend = daemon.requests.filter(r => r.type === 'send').at(-1);
    assert.equal(replySend.session_name, 'live');
    assert.equal(replySend.reply_to_message_id, 'direct-event-1');
    steps.push({ name: 'reply metadata follows the ordinary target wire route', ok: true });
    daemon.setPaneQuestion({ question_key: 'pane-direct', header: 'Disposable', prompt: 'Confirm route',
      options: [{ index: 1, label: 'Yes' }] });
    await page.waitFor("window.PentacleChatStore?.getQuestion('mock-host:live')?.question_key === 'pane-direct'", { timeoutMs: 5000 });
    await page.type('#cell-0 .slot-chat-compose-input', 'Disposable pane answer');
    const sendsBeforeQuestion = daemon.requests.filter(r => r.type === 'send').length;
    await page.click('#cell-0 .slot-chat-compose-send');
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-compose-input')?.value === ''", { timeoutMs: 5000 });
    const dismissed = daemon.requests.filter(r => r.type === 'question.dismiss').at(-1);
    assert.equal(dismissed.host, 'mock-host');
    assert.equal(dismissed.session_name, 'live');
    assert.equal(dismissed.question_key, 'pane-direct');
    assert.equal(dismissed.text, 'Disposable pane answer');
    assert.equal(daemon.requests.filter(r => r.type === 'send').length, sendsBeforeQuestion);
    steps.push({ name: 'pane question answer dismisses only target question', ok: true });
    daemon.setDurableQuestion({ notification_id: 'durable-direct', producer: 'agent_question.v1',
      state: 'open', title: 'Disposable durable question', body: 'Confirm target identity',
      created_at: '2026-09-26T00:00:00Z', answer_to_stream_id: targetId,
      question: { question_id: 'durable-question-1', producer_stream_id: targetId,
        response_mode: 'free_text', state: 'open', options: [] } });
    await page.waitFor("window.PentacleDurableQuestions?.getOpenQuestionsForStream('mock-host:live')?.length === 1", { timeoutMs: 5000 });
    await page.waitFor("!!document.querySelector('#cell-0 .slot-chat-question-open')", { timeoutMs: 5000 });
    await page.click('#cell-0 .slot-chat-question-open');
    await page.waitFor("!!document.querySelector('.desktop-question-portal .slot-chat-question-freetext')", { timeoutMs: 5000 });
    await page.type('.desktop-question-portal .slot-chat-question-freetext', 'Disposable durable answer');
    await page.click('.desktop-question-portal .slot-chat-question-submit');
    await page.waitFor("window.PentacleDurableQuestions?.getOpenQuestionsForStream('mock-host:live')?.length === 0", { timeoutMs: 5000 });
    const durableResolve = daemon.requests.filter(r => r.type === 'notification.resolve').at(-1);
    assert.equal(durableResolve.notification_id, 'durable-direct');
    assert.equal(durableResolve.action_kind, 'resolved');
    assert.equal(durableResolve.text || durableResolve.custom_text, 'Disposable durable answer');
    assert.equal(daemon.requests.filter(r => r.type === 'send' && r.session_name === 'assistant').length, 0);
    steps.push({ name: 'durable question answer resolves target notification only', ok: true });
    const voice = await page.eval("window.PentacleHarnessActions.deliverVoiceText(0, 'Disposable voice text')");
    assert.equal(voice.ok, true);
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Echo: Disposable voice text')", { timeoutMs: 10000 });
    const voiceSend = daemon.requests.filter(r => r.type === 'send').at(-1);
    assert.equal(voiceSend.host, 'mock-host');
    assert.equal(voiceSend.session_name, 'live');
    assert.equal(voiceSend.text, 'Disposable voice text');
    steps.push({ name: 'transcribed voice text uses production alias delivery path', ok: true });
    for (const theme of ['dark', 'light']) {
      for (const width of [1600, 850]) {
        await page.send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false });
        await page.eval(`document.documentElement.dataset.theme=${JSON.stringify(theme)}`);
        await page.click(`.session-item[data-stream-id="${targetId}"]`);
        await page.click(`.session-item[data-stream-id="${sourceId}"]`);
        await page.waitFor(`document.querySelector('#cell-0 .slot-chat-list')?.dataset.streamId === ${JSON.stringify(targetId)}`, { timeoutMs: 5000 });
        const click = await page.eval(`(() => {
          const row=document.querySelector('.session-item[data-stream-id="${sourceId}"]');
          const target=document.querySelector('.session-item[data-stream-id="${targetId}"]');
          const header=document.querySelector('#header-0');
          return { width:innerWidth, theme:document.documentElement.dataset.theme,
            rowRole:row?.getAttribute('role'), rowLabel:row?.getAttribute('aria-label'),
            targetLabel:target?.getAttribute('aria-label'), targetIcon:target?.querySelector('.s-machine-avatar')?.className,
            label:header?.querySelector('.cell-label')?.textContent,
            lamp:!!header?.querySelector('.cell-assistant-icon'), copyId:header?.querySelector('.cell-copyid')?.dataset.streamId,
            host:header?.querySelector('.cell-source-tag')?.textContent,
            chatWidth:document.querySelector('#cell-0 .slot-chat-list')?.getBoundingClientRect().width,
            transcript:document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Existing disposable transcript') }; })()`);
        assert.equal(click.width, width);
        assert.equal(click.theme, theme);
        assert.equal(click.rowRole, 'button');
        assert.match(click.rowLabel, /Assistant/);
        assert.match(click.targetLabel, /Disposable target/);
        assert.equal(click.label, 'Assistant');
        assert.equal(click.lamp, true);
        assert.equal(click.copyId, targetId);
        assert.equal(click.host, 'Mock Host');
        assert.equal(click.transcript, true);
        assert.ok(click.chatWidth > 200);
        await page.click(`.session-item[data-stream-id="${targetId}"]`);
        await page.eval(`document.querySelector('.session-item[data-stream-id="${sourceId}"]').focus()`);
        await page.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
        await page.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
        await page.waitFor("document.querySelector('#header-0 .cell-label')?.textContent === 'Assistant' && !!document.querySelector('#header-0 .cell-assistant-icon')", { timeoutMs: 5000 });
        assert.equal(await page.eval("document.querySelector('#header-0 .cell-copyid')?.dataset.streamId"), targetId);
        await page.screenshot(path.join(OUT, `direct-entry-${theme}-${width}.png`));
        steps.push({ name: `click and keyboard official entry ${theme} ${width}`, ok: true, click });
      }
    }
    const servedBuildId = await page.eval('window.__PENTACLE_CONFIG__.buildId');
    const result = { status: 'PASS', source: require('node:child_process').execFileSync('git', ['rev-parse', 'HEAD'], { cwd: ROOT, encoding: 'utf8' }).trim(),
      servedBuildId,
      files: Object.fromEntries(['renderer/app.js', 'renderer/assistant_direct_entry.js', 'renderer/dist/web/bundle.js', 'renderer/dist/web/styles.css', 'test/e2e/web_assistant_direct_entry_gate.cjs'].map(file => [file, hash(path.join(ROOT, file))])),
      steps, requests: daemon.requests.filter(r => ['send', 'request_stream_events'].includes(r.type)).map(r => ({ type: r.type, stream_id: r.stream_id, request_id: r.request_id, optimistic_id: r.optimistic_id })) };
    fs.writeFileSync(path.join(OUT, 'verdict.json'), JSON.stringify(result, null, 2));
    console.log(`PASS assistant direct entry: ${OUT}`);
  } finally {
    page?.close();
    browser?.kill('SIGTERM');
    if (web) await web.close();
    await daemon.stop();
  }
}

(INSTALLED ? runInstalled() : run()).catch(error => { console.error(error); process.exitCode = 1; });
