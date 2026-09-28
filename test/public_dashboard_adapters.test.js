'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const { JSDOM } = require('jsdom');

function load(name, { HOST = {}, cc = {}, board } = {}) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>');
  const win = dom.window;
  win.DASHBOARDS = []; win.HOST = HOST; win.cc = cc;
  if (board) win.TriforceDashboards = board;
  const file = path.join(__dirname, '../renderer/dashboards', name + '.js');
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), { window: win, document: win.document,
    require: createRequire(file), module: { exports: {} }, console, setTimeout, clearTimeout,
    setInterval, clearInterval, navigator: win.navigator });
  return { win, dashboard: win.DASHBOARDS[0], root: win.document.getElementById('root'), close: () => win.close() };
}

for (const name of ['business', 'foreclosure']) {
  test(`${name} registers only for configured hub or remote client`, () => {
    for (const HOST of [{ hostname: 'unconfigured-desktop' }, { hasDashboardHub: true }, { isClient: true, hasRemote: true }]) {
      const loaded = load(name, { HOST });
      assert.equal(!!loaded.dashboard, !!HOST.hasDashboardHub || (!!HOST.isClient && !!HOST.hasRemote));
      loaded.close();
    }
  });
}
test('scraper requires explicit config and embeds only supplied URL', () => {
  const absent = load('scraper-bot'); assert.equal(absent.dashboard, undefined); absent.close();
  const loaded = load('scraper-bot', { HOST: { dashboardHubConfig: { scraperBotUrl: 'http://127.0.0.1:9031/example' } } });
  const refs = loaded.dashboard.mount(loaded.root);
  assert.equal(refs.frame.src, 'http://127.0.0.1:9031/example');
  assert.equal(refs.frame.getAttribute('sandbox'), 'allow-same-origin allow-scripts');
  loaded.dashboard.unmount(refs); assert.equal(loaded.root.children.length, 0); loaded.close();
});
test('foreclosure unavailable shared board mounts fallback without private vendor', () => {
  const loaded = load('foreclosure', { HOST: { hasDashboardHub: true } });
  const refs = loaded.dashboard.mount(loaded.root);
  loaded.dashboard.update(refs, { pipeline_stages: [] });
  assert.match(loaded.root.textContent, /shared|unavailable|loaded/i);
  loaded.dashboard.unmount(refs); loaded.close();
});
test('foreclosure injected board receives interactive state and IPC actions', async () => {
  const calls = []; const mutate = [];
  const board = { boards: { foreclosure: {} }, mountBoard: (...args) => { calls.push(['mount', args]); return {}; },
    updateBoard: (...args) => calls.push(['update', args]), unmountBoard: (...args) => calls.push(['unmount', args]) };
  const loaded = load('foreclosure', { HOST: { hasDashboardHub: true }, board,
    cc: { setBatchGate: (...args) => { mutate.push(args); return Promise.resolve({ ok: true }); }, getPipelineStats: async () => ({ batch: 'example' }) } });
  const refs = loaded.dashboard.mount(loaded.root); loaded.dashboard.update(refs, { batch: 'example' });
  assert.equal(calls[0][0], 'mount');
  assert.equal(calls[0][1][3].mode, 'interactive');
  await calls[0][1][3].actions.setBatchGate('example', 'open');
  assert.deepEqual(mutate, [['example', 'open', undefined]]);
  assert.equal(calls[1][0], 'update');
  assert.equal(calls[1][1][2].batch, 'example');
  loaded.dashboard.unmount(refs); loaded.close();
});
for (const name of ['notifications', 'pentacle-mobile-testing', 'pi-control', 'specs', 'chat-stream', 'ui-review', '0dte']) {
  test(`${name} loads against public dependencies without private vendor`, () => {
    const loaded = load(name, { HOST: { hasDashboardHub: true } }); assert.ok(loaded.dashboard); loaded.close();
  });
}
test('Specs uses configured identity instead of the OS display label', async () => {
  const loaded = load('specs', { HOST: { hostname: 'different-os-label' }, cc: {
    getConfig: async () => ({ localHostId: 'node2', chatStream: { hostMap: { local: 'node2' } } }),
    specsCapabilities: async () => ({ ok: true, hosts: { node1: { supports_spec_id: true }, node2: { supports_spec_id: true } } }),
    specsGet: async () => ({ ok: true, spec: {} }), specsActivity: async () => ({ ok: true, events: [] }) } });
  const refs = loaded.dashboard.mount(loaded.root);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(refs.config.localHostId, 'node2');
  loaded.dashboard.update(refs, { specs: [{ spec_id: 'example-spec', title: 'Example', status: 'in_progress' }] });
  const card = loaded.root.querySelector('[data-spec-id]'); assert.ok(card); card.click();
  await new Promise(resolve => setImmediate(resolve));
  const hostSelect = loaded.win.document.querySelector('select[data-role="host-select"]') || loaded.win.document.querySelector('select');
  assert.ok(hostSelect); assert.equal(hostSelect.value, 'node2');
  loaded.dashboard.unmount(refs); assert.equal(loaded.win.__pentacleSpecsChangedListeners.length, 0); loaded.close();
});
test('Specs config read failure keeps no guessed local identity', async () => {
  const loaded = load('specs', { HOST: { hostname: 'arbitrary-label' }, cc: {
    getConfig: async () => { throw new Error('unconfigured'); }, specsCapabilities: async () => ({ ok: true, hosts: {} }) } });
  const refs = loaded.dashboard.mount(loaded.root); await new Promise(resolve => setImmediate(resolve));
  assert.equal(refs.config, null); loaded.dashboard.unmount(refs); loaded.close();
});

for (const [name, id, method] of [['chat-stream', 'chat-stream', 'getChatStreamState'], ['ui-review', 'ui-review', 'listUiReviewArtifacts'], ['0dte', '0dte-trading', 'get0dteStats'], ['notifications', 'notifications', 'notificationList']]) {
  test(`${name} injected board receives update, poll and teardown`, async () => {
    const calls = []; const polls = [];
    const board = { boards: { [id]: {} }, mountBoard: (...args) => { calls.push(['mount', args]); return {}; },
      updateBoard: (...args) => calls.push(['update', args]), unmountBoard: (...args) => calls.push(['unmount', args]) };
    const loaded = load(name, { board, cc: { [method]: async (...args) => { polls.push(args); return { ok: true, synthetic: 7 }; } } });
    const refs = loaded.dashboard.mount(loaded.root);
    const state = { notifications: [], connected: true, synthetic: 7 };
    loaded.dashboard.update(refs, state);
    assert.equal(calls[0][1][3].mode, name === 'notifications' ? 'display' : 'interactive');
    assert.equal(calls[1][1][2], state);
    assert.equal((await loaded.dashboard.pollFn(refs)).synthetic, 7);
    assert.equal(polls.length, 1);
    if (name === '0dte') assert.equal(polls[0][0], undefined);
    loaded.dashboard.unmount(refs); assert.equal(calls[2][0], 'unmount');
    if (name === 'notifications') assert.equal(loaded.win.__pentacleNotificationsListeners.length, 0);
    loaded.close();
  });
  test(`${name} missing shared board renders an unavailable notice`, () => {
    const loaded = load(name); const refs = loaded.dashboard.mount(loaded.root);
    loaded.dashboard.update(refs, {}); assert.match(loaded.root.textContent, /unavailable/i);
    loaded.dashboard.unmount(refs); loaded.close();
  });
}
test('business real renderer shows synthetic totals and polls the configured bridge', async () => {
  const loaded = load('business', { HOST: { hasDashboardHub: true }, cc: { getBusinessPipelineStats: async () => ({ scraped: 7 }) } });
  const refs = loaded.dashboard.mount(loaded.root);
  loaded.dashboard.update(refs, { scraped: 7, pipeline_stages: [], scraper_queue: {} });
  assert.match(loaded.root.textContent, /7/);
  assert.equal((await loaded.dashboard.pollFn(refs)).scraped, 7);
  loaded.dashboard.unmount(refs); loaded.close();
});
