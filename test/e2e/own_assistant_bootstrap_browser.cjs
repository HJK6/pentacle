#!/usr/bin/env node
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const cdp = require('./lib/cdp');
const { getFreePort } = require('./lib/disposable_web_daemon_config');
const ROOT = path.resolve(__dirname, '../..');
const out = path.resolve(process.argv[2]);

function chrome() {
  if (process.env.PENTACLE_TEST_BROWSER) return process.env.PENTACLE_TEST_BROWSER;
  const candidates = process.platform === 'darwin'
    ? ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/Applications/Chromium.app/Contents/MacOS/Chromium']
    : ['google-chrome', 'chromium', 'chromium-browser'];
  for (const candidate of candidates) {
    if (path.isAbsolute(candidate) ? fs.existsSync(candidate) : spawnSync('which', [candidate]).status === 0) return candidate;
  }
  throw new Error('Chrome/Chromium required; set PENTACLE_TEST_BROWSER');
}

async function main() {
  let web, browser, page;
  try {
    const receipt = JSON.parse(fs.readFileSync(path.join(out, 'work/assistant-receipt.json')));
    const config = path.join(out, 'work/assistant-client.cjs');
    process.env.PENTACLE_CONFIG = config;
    web = await require(path.join(ROOT, 'server')).main(['--profile', config, '--bind', '127.0.0.1', '--port', '0']);
    const debugPort = await getFreePort();
    browser = spawn(chrome(), ['--headless=new', '--no-first-run', '--no-default-browser-check', '--no-sandbox',
      '--disable-gpu', `--user-data-dir=${path.join(out, 'browser-profile')}`, `--remote-debugging-port=${debugPort}`, web.url], { stdio: 'ignore' });
    page = await cdp.connect(debugPort, { timeoutMs: 15000 });
    await page.waitFor('window.PentacleChatStore?.getState().connected === true', { timeoutMs: 15000 });
    const stream = JSON.stringify(receipt.composite_stream_id);
    await page.waitFor(`document.querySelector('.session-item[data-stream-id="'+${stream}+'"]')`, { timeoutMs: 15000 });
    await page.click(`.session-item[data-stream-id="${receipt.composite_stream_id}"]`);
    try { await page.waitFor(`document.querySelector('.slot-chat-list[data-stream-id="${receipt.composite_stream_id}"]')`, { timeoutMs: 15000 }); }
    catch (error) {
      fs.writeFileSync(path.join(out, 'selection-diagnostic.json'), JSON.stringify(await page.eval(`({sessions:window.PentacleChatStore.getState().sessions,config:window.__PENTACLE_CONFIG__,body:document.body.innerText,rows:Array.from(document.querySelectorAll('.session-item')).map(e=>({...e.dataset})),lists:Array.from(document.querySelectorAll('.slot-chat-list')).map(e=>({...e.dataset}))})`), null, 2));
      await page.screenshot(path.join(out, 'selection-error.png'));
      throw error;
    }
    const cell = await page.eval(`document.querySelector('.slot-chat-list[data-stream-id="${receipt.composite_stream_id}"]')?.closest('.grid-cell')?.id`);
    assert.ok(cell, 'selected composite must own a real cell');
    const session = await page.eval(`window.PentacleChatStore.getState().sessions.find(s=>s.stream_id===${stream})`);
    const visibleName = await page.eval(`document.querySelector('.session-item[data-stream-id="${receipt.composite_stream_id}"]')?.textContent`);
    assert.ok(visibleName.includes(receipt.name), visibleName);
    const backend = await page.eval(`window.PentacleChatStore.getState().sessions.find(s=>s.stream_id===${JSON.stringify(receipt.backend_stream_id)})`);
    assert.equal(backend.session_generation, receipt.backend_generation);
    const input = 'Hello from my own assistant bootstrap';
    await page.type(`#${cell} .slot-chat-compose-input`, input);
    await page.click(`#${cell} .slot-chat-compose-send`);
    const expected = receipt.name + ': ' + input;
    await page.waitFor(`document.querySelector('#${cell} .slot-chat-list')?.textContent.includes(${JSON.stringify(expected)})`, { timeoutMs: 30000 });
    await page.screenshot(path.join(out, 'reply.png'));
    const servedAssets = await page.eval(`(async()=>{const out={};for(const file of ['bundle.js','chat_core.bundle.js','styles.css']){
      const response=await fetch(file,{cache:'no-store'});if(!response.ok)throw new Error('asset unavailable: '+file);
      const digest=await crypto.subtle.digest('SHA-256',await response.arrayBuffer());
      out[file]=Array.from(new Uint8Array(digest)).map(byte=>byte.toString(16).padStart(2,'0')).join('');}return out})()`);
    fs.writeFileSync(path.join(out, 'browser-proof.json'), JSON.stringify({ connected: true, input, visibleReply: expected,
      composite: receipt.composite_stream_id, name: receipt.name, visibleName, backend: backend.stream_id,
      generation: backend.session_generation, browserPid: browser.pid, webPort: web.port, servedAssets }, null, 2));
  } finally {
    page?.close();
    if (browser) {
      browser.kill('SIGTERM');
      await new Promise(resolve => { if (browser.exitCode !== null || browser.signalCode) return resolve();
        const timer = setTimeout(() => browser.kill('SIGKILL'), 3000);
        browser.once('exit', () => { clearTimeout(timer); resolve(); }); });
    }
    await web?.close();
    fs.writeFileSync(path.join(out, 'browser-cleanup.json'), JSON.stringify({ browserStopped: !browser || browser.exitCode !== null || !!browser.signalCode, webClosed: true }));
  }
}
main().catch(error => { console.error(error.stack); process.exitCode = 1; });
