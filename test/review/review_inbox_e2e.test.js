'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { main } = require('../../server');
const store = require('../../main/review_store');
const cdp = require('../e2e/lib/cdp');
const { JSDOM } = require('jsdom');
const { getFreePort } = require('../e2e/lib/disposable_web_daemon_config');
const browserBinary = process.env.PENTACLE_TEST_BROWSER;
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'review-browser-'));
function isolatedInbox(feed, options = {}) {
  const dom = new JSDOM(fs.readFileSync(path.join(__dirname, '../../renderer/index.html'), 'utf8'), { url: 'https://example.test' });
  const win = dom.window;
  Object.defineProperty(win.document.querySelector('.main'), 'clientWidth', { value: 1400 });
  Object.defineProperty(win.HTMLElement.prototype, 'offsetParent', { get() { return this.parentElement; } });
  const previous = { window: global.window, document: global.document, localStorage: global.localStorage, matchMedia: global.matchMedia, fetch: global.fetch };
  global.window = win; global.document = win.document; global.localStorage = win.localStorage;
  global.matchMedia = () => ({ matches: true });
  win.cc = { reviewInboxRead: async () => feed.current, onReviewInboxChanged() {} };
  const ui = require('../../renderer/review_inbox').createReviewInbox(options);
  return { win, ui, close() { win.dispatchEvent(new win.Event('beforeunload')); dom.window.close(); Object.assign(global, previous); } };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const baseItem = (state = 'pending') => ({ id: `ri_${'e'.repeat(32)}`, kind: 'review', title: 'Review item',
  created_at: new Date().toISOString(), source: {}, state, seq: 1, acked: state === 'done' });
const feedFor = (item, extra = {}) => ({ state: 'ok', read_at: new Date().toISOString(),
  updated_at: new Date(Date.now() - 26 * 3600000).toISOString(), pending_count: item.state === 'pending' ? 1 : 0,
  items: [item], stale: true, warning: 'Inbox nearing capacity', ...extra });

test('transaction notices stay visible with stale and capacity status', async () => {
  const feed = { current: feedFor(baseItem()) }, env = isolatedInbox(feed);
  try {
    await tick();
    const banner = () => env.win.document.querySelector('#review-inbox-banner').textContent;
    const click = selector => env.win.document.querySelector(selector).click();
    const pending = '#review-inbox-pending button[aria-label^="Got it"]';
    global.fetch = async () => ({ json: async () => ({ ok: false, code: 'write_failed' }) });
    click(pending); await tick(); await tick();
    assert.match(banner(), /Couldn't save/); assert.match(banner(), /out of date/); assert.match(banner(), /nearing capacity/);
    feed.current = feedFor(baseItem('done')); await env.ui.refresh();
    click('#review-inbox-done-list button[aria-label^="Undo"]'); await tick(); await tick();
    assert.match(banner(), /Couldn't undo/); assert.match(banner(), /out of date/);
    feed.current = feedFor(baseItem()); await env.ui.refresh();
    global.fetch = async () => { throw Error('network lost'); };
    click(pending); await tick(); await tick();
    assert.match(banner(), /Couldn't confirm/); assert.match(banner(), /out of date/);
    global.fetch = async () => ({ json: async () => ({ ok: false, code: 'resolved' }) });
    click(pending); await tick(); await tick();
    assert.match(banner(), /Lews resolved/); assert.match(banner(), /nearing capacity/);
    global.fetch = async () => ({ json: async () => ({ ok: true, result: { seq_seen: 1 }, current: { seq: 2 } }) });
    click(pending); await tick(); await tick();
    assert.match(banner(), /Updated in another tab/); assert.match(banner(), /out of date/);
  } finally { env.close(); }
});

test('retries use one ID only within receipt window and pending fetches release the control', async () => {
  const feed = { current: feedFor(baseItem(), { stale: false, warning: null }) };
  let clock = Date.now();
  const env = isolatedInbox(feed, { now: () => clock, requestDeadlineMs: 30 });
  try {
    await tick();
    const pending = () => env.win.document.querySelector('#review-inbox-pending button[aria-label^="Got it"]');
    const sent = [];
    global.fetch = async (_url, options) => { sent.push(JSON.parse(options.body)); if (sent.length < 3) throw Error('network');
      return { json: async () => ({ ok: false, code: 'write_failed' }) }; };
    pending().click(); await tick(); await tick();
    assert.equal(sent.length, 3); assert.equal(new Set(sent.map(x => x.op_id)).size, 1);
    sent.length = 0;
    global.fetch = async (_url, options) => { sent.push(JSON.parse(options.body)); clock += 25 * 3600000; throw Error('late failure'); };
    pending().click(); await tick(); await tick();
    assert.equal(sent.length, 1, 'expired operation must not be replayed');
    assert.match(env.win.document.querySelector('#review-inbox-banner').textContent, /Couldn't confirm/);
    const expiredId = sent[0].op_id;
    sent.length = 0;
    global.fetch = async (_url, options) => { sent.push(JSON.parse(options.body)); return { json: async () => ({ ok: false, code: 'write_failed' }) }; };
    pending().click(); await tick(); await tick();
    assert.equal(sent.length, 1);
    assert.notEqual(sent[0].op_id, expiredId, 'fresh click needs a fresh operation ID');
    global.fetch = () => new Promise(() => {});
    pending().click();
    await new Promise(resolve => setTimeout(resolve, 150));
    assert.equal(pending().disabled, false, 'deadline must release busy control');
    assert.match(env.win.document.querySelector('#review-inbox-banner').textContent, /Couldn't confirm/);
  } finally { env.close(); }
});

test('chat popout hides every inbox control without reserving rail space', async () => {
  const feed = { current: feedFor(baseItem()) }, env = isolatedInbox(feed);
  try {
    await tick();
    const style = env.win.document.createElement('style');
    style.textContent = fs.readFileSync(path.join(__dirname, '../../renderer/review_inbox.css'), 'utf8');
    env.win.document.head.append(style);
    env.win.document.body.classList.add('chat-popout');
    for (const selector of ['#review-inbox', '#review-inbox-scrim', '#review-inbox-title-toggle', '#review-inbox-phone-toggle'])
      assert.equal(env.win.getComputedStyle(env.win.document.querySelector(selector)).display, 'none', selector);
  } finally { env.close(); }
});
function config(root, dir) {
  const file = path.join(root, 'config.js');
  fs.writeFileSync(file, `module.exports={appName:'Pentacle',hosts:{local:{kind:'local'}},agents:{},features:{mic:false},reviewInbox:{dir:${JSON.stringify(dir)}}};\n`);
  return file;
}
async function viewport(page, width, height) {
  await page.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
  await page.waitFor(`document.querySelector('.main')?.clientWidth === ${width}`, { timeoutMs: 5000 });
}

test('browser rail persists receipts, keeps panes, and handles desktop, overlay and phone states', { timeout: 120000 }, async () => {
  assert.ok(browserBinary, 'PENTACLE_TEST_BROWSER is required');
  const root = tmp(), dir = path.join(root, 'inbox');
  const id = store.addItem(dir, { kind: 'answer', title: 'Answer for review', reply_to_message_id: 'optimistic_local_example', link: 'https://example.org/review' }).item.id;
  const web = await main(['--profile', config(root, dir), '--port', '0']);
  let browser, page, second, popout;
  try {
    const debugPort = await getFreePort();
    browser = spawn(browserBinary, ['--headless=new', '--no-first-run', '--no-default-browser-check', '--no-sandbox', '--disable-gpu',
      '--window-size=1440,900', `--user-data-dir=${path.join(root, 'profile')}`, `--remote-debugging-port=${debugPort}`, web.url], { stdio: 'ignore' });
    page = await cdp.connect(debugPort, { timeoutMs: 15000 });
    await page.waitFor(`document.querySelector('#review-inbox-pending .review-inbox-card')?.textContent.includes('Answer for review')`, { timeoutMs: 15000 });
    assert.equal(await page.eval(`document.querySelector('#review-inbox-count').textContent`), '1');
    assert.equal(await page.eval(`document.querySelector('.main').classList.contains('review-inbox-expanded')`), true);
    assert.equal(await page.eval(`document.querySelector('#review-inbox-pending .review-inbox-card a')?.rel`), 'noopener noreferrer');
    const paneBefore = await page.eval(`[...document.querySelectorAll('.grid-cell')].map(x=>x.id).join(',')`);
    await page.click(`#review-inbox-pending [data-review-id="${id}"] button[aria-label^="Got it"]`);
    await page.waitFor(`document.querySelector('#review-inbox-done summary')?.textContent === 'Done (1)'`, { timeoutMs: 5000 });
    await page.send('Page.reload', { ignoreCache: true });
    await page.waitFor(`document.querySelector('#review-inbox-done summary')?.textContent === 'Done (1)'`, { timeoutMs: 10000 });
    await page.send('Target.createTarget', { url: web.url });
    second = await cdp.connect(debugPort, { excludeTargetId: page.target.id, timeoutMs: 10000 });
    await second.waitFor(`document.querySelector('#review-inbox-done summary')?.textContent === 'Done (1)'`, { timeoutMs: 10000 });
    await second.click('#review-inbox-done summary');
    await second.click(`#review-inbox-done-list [data-review-id="${id}"] button[aria-label^="Undo"]`);
    await second.waitFor(`document.querySelector('#review-inbox-count')?.textContent === '1'`, { timeoutMs: 5000 });
    await page.waitFor(`document.querySelector('#review-inbox-count')?.textContent === '1'`, { timeoutMs: 5000 });
    assert.equal(await page.eval(`[...document.querySelectorAll('.grid-cell')].map(x=>x.id).join(',')`), paneBefore);
    await page.eval(`(() => { const root=document.createElement('div'); root.className='slot-chat-list'; root.dataset.streamId='fixture:visible';
      root.style.cssText='position:absolute;top:80px;left:320px;width:100px;height:40px;z-index:1';
      const row=document.createElement('article'); row.dataset.transcriptKey=JSON.stringify(['fixture:visible','optimistic_local_example']);
      row.textContent='Exact painted source'; root.append(row); document.querySelector('.grid').append(root); })()`);
    await page.click(`#review-inbox-pending [data-review-id="${id}"] button:last-child`);
    assert.equal(await page.eval(`document.activeElement?.textContent`), 'Exact painted source');
    await page.eval(`document.querySelector('.slot-chat-list')?.remove()`);
    await page.click(`#review-inbox-pending [data-review-id="${id}"] button:last-child`);
    assert.match(await page.eval(`document.querySelector('#review-inbox-banner').textContent`), /Chat message not open here/);
    await page.click('#review-inbox-title-toggle');
    assert.equal(await page.eval(`document.querySelector('.main').classList.contains('review-inbox-collapsed')`), true);
    await viewport(page, 1000, 800);
    assert.equal(await page.eval(`document.querySelector('.main').classList.contains('review-inbox-collapsed')`), true);
    await page.click('#review-inbox-edge');
    assert.equal(await page.eval(`document.querySelector('.main').classList.contains('review-inbox-overlay-open')`), true);
    await page.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
    assert.equal(await page.eval(`document.activeElement?.id`), 'review-inbox-edge');
    for (const [width, height] of [[390, 844], [360, 740]]) {
      await viewport(page, width, height);
      await page.waitFor(`!document.querySelector('#review-inbox-phone-toggle').hidden`, { timeoutMs: 5000 });
      await page.click('#review-inbox-phone-toggle');
      assert.equal(await page.eval(`document.querySelector('.main').classList.contains('review-inbox-overlay-open')`), true);
      assert.equal(await page.eval(`document.body.classList.contains('sidebar-open')`), false);
      if (width === 390) {
        await page.click(`#review-inbox-pending [data-review-id="${id}"] button[aria-label^="Got it"]`);
        await page.waitFor(`document.querySelector('#review-inbox-done summary')?.textContent === 'Done (1)'`, { timeoutMs: 5000 });
        await page.click('#review-inbox-done summary');
        await page.click(`#review-inbox-done-list [data-review-id="${id}"] button[aria-label^="Undo"]`);
        await page.waitFor(`document.querySelector('#review-inbox-count')?.textContent === '1'`, { timeoutMs: 5000 });
      }
      await page.click('#sidebar-toggle');
      assert.equal(await page.eval(`document.querySelector('.main').classList.contains('review-inbox-overlay-open') && document.body.classList.contains('sidebar-open')`), false);
      await page.click('#sidebar-scrim');
      await page.click('#review-inbox-phone-toggle');
      await page.click('#review-inbox-close');
      assert.equal(await page.eval(`document.activeElement?.id`), 'review-inbox-phone-toggle');
      assert.equal(await page.eval(`document.querySelector('.main').classList.contains('review-inbox-overlay-open')`), false);
      assert.equal(await page.eval(`document.querySelector('.grid')?.getBoundingClientRect().height > 0`), true);
    }
    await viewport(page, 1400, 900);
    await page.eval(`(() => { window.__reviewFetch=window.fetch; window.fetch=async (url,opts) =>
      String(url).includes('/api/review-inbox/ack') ? new Response(JSON.stringify({ok:false,code:'write_failed'}),{status:507,headers:{'Content-Type':'application/json'}}) : window.__reviewFetch(url,opts); })()`);
    await page.click(`#review-inbox-pending [data-review-id="${id}"] button[aria-label^="Got it"]`);
    await page.waitFor(`document.querySelector('#review-inbox-banner')?.textContent.includes("Couldn't save")`, { timeoutMs: 5000 });
    await page.eval(`window.fetch=window.__reviewFetch`);
    await page.eval(`(() => { const original=window.cc.reviewInboxRead; window.__reviewRead=original;
      window.cc.reviewInboxRead=async()=>({state:'ok',read_at:new Date().toISOString(),updated_at:new Date().toISOString(),pending_count:0,items:[]});
      window.dispatchEvent(new Event('focus')); })()`);
    await page.waitFor(`document.querySelector('#review-inbox-pending')?.textContent.includes('Nothing waiting on you')`, { timeoutMs: 5000 });
    await page.eval(`(() => { window.cc.reviewInboxRead=async()=>({state:'ok',read_at:new Date().toISOString(),updated_at:new Date().toISOString(),pending_count:1,items:[
        {id:'ri_${'f'.repeat(32)}',kind:'review',title:'<img src=x onerror=window.__reviewInjected=1>',created_at:new Date().toISOString(),source:{},link:'javascript:alert(1)',state:'pending'}]});
      window.dispatchEvent(new Event('focus')); })()`);
    await page.waitFor(`document.querySelector('#review-inbox-pending')?.textContent.includes('<img src=x')`, { timeoutMs: 5000 });
    assert.equal(await page.eval(`document.querySelector('#review-inbox-pending h5')?.textContent`), '<img src=x onerror=window.__reviewInjected=1>');
    assert.equal(await page.eval(`document.querySelector('#review-inbox-pending img') === null`), true);
    assert.equal(await page.eval(`window.__reviewInjected === undefined`), true);
    assert.equal(await page.eval(`document.querySelector('#review-inbox-pending a') === null`), true);
    assert.match(await page.eval(`document.querySelector('#review-inbox-pending').textContent`), /Link unavailable/);
    await page.eval(`(() => { window.cc.reviewInboxRead=window.__reviewRead; window.dispatchEvent(new Event('focus')); })()`);
    await page.waitFor(`document.querySelector('#review-inbox-pending')?.textContent.includes('Answer for review')`, { timeoutMs: 5000 });
    const items = store.readItems(dir).value;
    items.updated_at = new Date(Date.now() - 26 * 3600 * 1000).toISOString();
    fs.writeFileSync(path.join(dir, 'items.json'), JSON.stringify(items));
    await page.eval(`window.dispatchEvent(new Event('focus'))`);
    await page.waitFor(`document.querySelector('#review-inbox-banner')?.textContent.includes('out of date')`, { timeoutMs: 5000 });
    await page.eval(`(() => { window.__reviewFetch=window.fetch; window.fetch=async (url,opts) =>
      String(url).includes('/api/review-inbox/ack') ? new Response(JSON.stringify({ok:false,code:'write_failed'}),{status:507,headers:{'Content-Type':'application/json'}}) : window.__reviewFetch(url,opts); })()`);
    await page.click(`#review-inbox-pending [data-review-id="${id}"] button[aria-label^="Got it"]`);
    await page.waitFor(`document.querySelector('#review-inbox-banner')?.textContent.includes("Couldn't save")`, { timeoutMs: 5000 });
    assert.match(await page.eval(`document.querySelector('#review-inbox-banner').textContent`), /out of date/);
    await page.eval(`window.fetch=window.__reviewFetch`);
    fs.writeFileSync(path.join(dir, 'items.json'), Buffer.from([0xff]));
    await page.eval(`window.dispatchEvent(new Event('focus'))`);
    await page.waitFor(`document.querySelector('#review-inbox-banner')?.textContent.includes('invalid')`, { timeoutMs: 5000 });
    assert.equal(await page.eval(`document.querySelector('#review-inbox-pending button[aria-label^="Got it"]')?.disabled`), true);
    await page.eval(`(() => { window.cc.reviewInboxRead=async()=>{ throw new Error('offline') }; window.dispatchEvent(new Event('focus')); })()`);
    await page.waitFor(`document.querySelector('#review-inbox-banner')?.textContent.includes("Can't reach Pentacle")`, { timeoutMs: 5000 });
    const context = { stream_id: 'fixture:popout', host: 'local', session_name: 'fixture', title: 'Review popout test' };
    await page.send('Target.createTarget', { url: `${web.url}/?pentacle-chat-popout=${encodeURIComponent(JSON.stringify(context))}` });
    popout = await cdp.connect(debugPort, { excludeTargetId: page.target.id, timeoutMs: 10000 });
    await popout.waitFor(`document.body.classList.contains('chat-popout')`, { timeoutMs: 15000 });
    assert.equal(await popout.eval(`document.querySelector('#review-inbox').getBoundingClientRect().width`), 0);
    for (const selector of ['#review-inbox', '#review-inbox-scrim', '#review-inbox-title-toggle', '#review-inbox-phone-toggle'])
      assert.equal(await popout.eval(`getComputedStyle(document.querySelector(${JSON.stringify(selector)})).display`), 'none', selector);
  } finally {
    popout?.close(); second?.close(); page?.close();
    if (browser && browser.exitCode === null) browser.kill('SIGTERM');
    await web.close();
  }
});

test('project cards group and refocus on desktop and phone, with read-model fallback', { timeout: 120000 }, async () => {
  assert.ok(browserBinary, 'PENTACLE_TEST_BROWSER is required');
  const root = tmp(), dir = path.join(root, 'inbox');
  const first = store.addItem(dir, { kind: 'review', title: 'Agenteach review', project: 'Agenteach' }).item.id;
  const middle = store.addItem(dir, { kind: 'input', title: 'DUMBO input', project: 'DUMBO' }).item.id;
  const defaultId = store.addItem(dir, { kind: 'answer', title: 'Personal fallback' }).item.id;
  const web = await main(['--profile', config(root, dir), '--port', '0']);
  let browser, page;
  try {
    const debugPort = await getFreePort();
    browser = spawn(browserBinary, ['--headless=new', '--no-first-run', '--no-default-browser-check', '--no-sandbox', '--disable-gpu',
      '--window-size=1440,900', `--user-data-dir=${path.join(root, 'profile')}`, `--remote-debugging-port=${debugPort}`, web.url], { stdio: 'ignore' });
    page = await cdp.connect(debugPort, { timeoutMs: 15000 });
    await page.waitFor(`document.querySelectorAll('.review-inbox-project-card').length === 3`, { timeoutMs: 15000 });
    const groups = () => page.eval(`[...document.querySelectorAll('#review-inbox-pending .review-inbox-project-card')].map(x => [x.dataset.project, x.querySelector('.review-inbox-project-heading').getAttribute('aria-label'), x.querySelectorAll('.review-inbox-card').length])`);
    assert.deepEqual(await groups(), [['Agenteach', 'Agenteach, 1 waiting', 1], ['DUMBO', 'DUMBO, 1 waiting', 1], ['Other', 'Other, 1 waiting', 1]]);
    assert.equal(await page.eval(`document.querySelector('#review-inbox-count').textContent`), '3');
    assert.equal(await page.eval(`document.querySelector('#review-inbox-phone-toggle').textContent`), 'To do (3)');
    await page.eval(`document.querySelector('#review-inbox-pending [data-review-id="${middle}"] button[aria-label^="Got it"]').focus()`);
    await page.click(`#review-inbox-pending [data-review-id="${middle}"] button[aria-label^="Got it"]`);
    await page.waitFor(`document.querySelectorAll('.review-inbox-project-card').length === 2`, { timeoutMs: 5000 });
    assert.deepEqual((await groups()).map(x => x[0]), ['Agenteach', 'Other']);
    assert.equal(await page.eval(`document.activeElement?.getAttribute('aria-label')`), 'Other, 1 waiting');
    await page.click('#review-inbox-done summary');
    assert.equal(await page.eval(`document.querySelector('#review-inbox-done-list .review-inbox-project-label')?.textContent`), 'DUMBO');
    await page.click(`#review-inbox-done-list [data-review-id="${middle}"] button[aria-label^="Undo"]`);
    await page.waitFor(`document.querySelectorAll('.review-inbox-project-card').length === 3`, { timeoutMs: 5000 });
    assert.deepEqual((await groups()).map(x => x[0]), ['Agenteach', 'DUMBO', 'Other']);
    store.resolveItem(dir, first, 'Closed');
    await page.eval(`window.dispatchEvent(new Event('focus'))`);
    await page.waitFor(`!!document.querySelector('#review-inbox-resolved-list [data-review-id="${first}"]')`, { timeoutMs: 5000 });
    await page.click('#review-inbox-resolved summary');
    assert.equal(await page.eval(`document.querySelector('#review-inbox-resolved-list .review-inbox-project-label')?.textContent`), 'Agenteach');
    store.addItem(dir, { kind: 'review', title: 'Next Agenteach review', project: 'Agenteach' });
    store.addItem(dir, { kind: 'review', title: 'Pentacle review', project: 'Pentacle' });
    store.addItem(dir, { kind: 'review', title: 'Personal review', project: 'Personal' });
    await page.eval(`window.dispatchEvent(new Event('focus'))`);
    await page.waitFor(`document.querySelectorAll('.review-inbox-project-card').length === 5`, { timeoutMs: 5000 });
    for (const [width, height] of [[390, 844], [360, 740]]) {
      await viewport(page, width, height);
      await page.waitFor(`!document.querySelector('#review-inbox-phone-toggle').hidden`, { timeoutMs: 5000 });
      await page.click('#review-inbox-phone-toggle');
      assert.deepEqual((await groups()).map(x => x[0]), ['Agenteach', 'DUMBO', 'Pentacle', 'Personal', 'Other']);
      assert.equal(await page.eval(`document.querySelector('#review-inbox-phone-toggle').textContent`), 'To do (5)');
      assert.equal(await page.eval(`document.querySelector('.review-inbox-scroll').scrollHeight >= document.querySelector('.review-inbox-scroll').clientHeight`), true);
      if (width === 360) {
        await page.eval(`document.querySelector('#review-inbox-pending .review-inbox-project-card:last-child').scrollIntoView({block:'end'})`);
        assert.equal(await page.eval(`(() => { const card=document.querySelector('#review-inbox-pending .review-inbox-project-card:last-child').getBoundingClientRect(); const area=document.querySelector('.review-inbox-scroll').getBoundingClientRect(); return card.bottom <= area.bottom + 1 && card.top >= area.top - 1; })()`), true);
      }
      await page.click('#sidebar-toggle');
      assert.equal(await page.eval(`document.querySelector('.main').classList.contains('review-inbox-overlay-open') && document.body.classList.contains('sidebar-open')`), false);
      await page.click('#sidebar-scrim');
      await page.click('#review-inbox-phone-toggle');
      await page.click('#review-inbox-close');
    }
    await viewport(page, 1400, 900);
    await page.eval(`(() => { window.__reviewReadOriginal = window.cc.reviewInboxRead; window.cc.reviewInboxRead = async () => ({
      state:'ok',read_at:new Date().toISOString(),updated_at:new Date().toISOString(),pending_count:1,
      items:[{id:'ri_${'e'.repeat(32)}',kind:'review',title:'Fallback item',project:'<img onerror=alert(1)>',
        created_at:new Date().toISOString(),source:{},state:'pending'}]}); window.dispatchEvent(new Event('focus')); })()`);
    await page.waitFor(`document.querySelector('#review-inbox-pending .review-inbox-project-card')?.dataset.project === 'Other'`, { timeoutMs: 5000 });
    assert.equal(await page.eval(`document.querySelector('#review-inbox-pending').outerHTML.includes('<img onerror')`), false);
    assert.equal(await page.eval(`document.querySelector('#review-inbox-pending').textContent.includes('Fallback item')`), true);
    await page.eval(`(() => { window.cc.reviewInboxRead = window.__reviewReadOriginal; window.dispatchEvent(new Event('focus')); })()`);
    await page.waitFor(`!!document.querySelector('#review-inbox-pending [data-review-id="${defaultId}"]')`, { timeoutMs: 5000 });
    const file = path.join(dir, 'items.json');
    const corrupt = store.readItems(dir).value;
    corrupt.items.find(x => x.id === defaultId).project = 'Out of list';
    fs.writeFileSync(file, JSON.stringify(corrupt));
    await page.eval(`window.dispatchEvent(new Event('focus'))`);
    await page.waitFor(`document.querySelector('#review-inbox-banner')?.textContent.includes('invalid')`, { timeoutMs: 5000 });
    assert.equal(await page.eval(`document.querySelector('#review-inbox-pending').textContent.includes('Out of list')`), false);
  } finally {
    page?.close(); if (browser && browser.exitCode === null) browser.kill('SIGTERM');
    await web.close();
  }
});
