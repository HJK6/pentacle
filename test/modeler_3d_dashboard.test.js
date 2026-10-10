'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const api = require('../renderer/dashboards/catalog_loader');
const fixtureUrl = 'https://viewer.example.ts.net:8444/modeler_viewer.html';
const policy = { tailnetSuffix: 'example.ts.net', pentacleOrigin: 'https://pentacle.example.ts.net' };
function setup({ url = fixtureUrl, cc, policyValue = policy } = {}) {
 const dom = new JSDOM('<main></main>', { url: policy.pentacleOrigin });
 const root = dom.window; root.DASHBOARDS = []; root.DashboardCatalogLoader = api; root.cc = cc;
 root.fetch = async () => ({ ok: true, json: async () => ({ hostedDashboardAuthMode: 'identity' }) });
 const timers = new Map(); let serial = 0;
 const context = vm.createContext({ window: root, URL, module: { exports: {} },
  setTimeout(fn, ms) { timers.set(++serial, { fn, ms }); return serial; }, clearTimeout(id) { timers.delete(id); } });
 vm.runInContext(fs.readFileSync(require.resolve('../renderer/dashboards/modeler-3d'), 'utf8'), context);
 const viewer = context.module.exports, container = root.document.querySelector('main');
 const refs = viewer.mount(container, { hostedUrl: url, name: '3D Modeler', getHostedPolicy: () => policyValue });
 return { root, container, refs, viewer, timers, frame: () => container.querySelector('iframe'),
  state: () => refs.shell.dataset.modelerState, reload: () => container.querySelector('[data-modeler-reload]').click(),
  timeout: () => [...timers.values()].forEach(({ fn }) => fn()), close: () => { viewer.unmount(refs); root.close(); } };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
test('Modeler is a generic hosted implementation and never self-registers', async t => {
 const h = setup(); t.after(h.close); await tick(); assert.deepEqual(h.root.DASHBOARDS, []);
 assert.equal(h.viewer.dashboard.pollFn, undefined); assert.equal(h.viewer.dashboard.pollInterval, undefined);
 assert.equal(h.frame().src, fixtureUrl); assert.equal(h.frame().title, '3D Modeler viewer');
 assert.equal(h.frame().getAttribute('sandbox'), 'allow-scripts allow-same-origin');
 assert.equal(h.frame().getAttribute('referrerpolicy'), 'no-referrer');
 assert.equal([...h.timers.values()][0].ms, 15000); h.frame().dispatchEvent(new h.root.Event('load'));
 assert.equal(h.state(), 'loaded'); assert.equal(h.container.dataset.boardState, 'ready'); assert.equal(h.timers.size, 0);
});
test('timeout/error stays terminal until explicit Retry; delayed load cannot overwrite it', async t => {
 for (const timeout of [true, false]) {
  const h = setup(); t.after(h.close); await tick(); const frame = h.frame();
  if (timeout) h.timeout(); else frame.dispatchEvent(new h.root.Event('error'));
  assert.equal(h.state(), 'blocked'); assert.match(h.container.textContent, /Could not open dashboard/);
  assert.equal(h.container.querySelector('[data-modeler-reload]').textContent, 'Retry');
  frame.dispatchEvent(new h.root.Event('load')); assert.equal(h.state(), 'blocked');
  h.reload(); await tick(); assert.equal(h.state(), 'loading'); h.frame().dispatchEvent(new h.root.Event('load')); assert.equal(h.state(), 'loaded');
 }
});
test('reload cancels old events/timer; unmount cancels pending actions and is idempotent', async t => {
 const h = setup(); t.after(() => h.root.close()); await tick();
 const frame = h.frame(), timeout = [...h.timers.values()][0].fn; h.reload(); await tick();
 assert.notEqual(h.frame(), frame); assert.equal(frame.isConnected, false); frame.dispatchEvent(new h.root.Event('load')); timeout();
 assert.equal(h.state(), 'loading'); assert.equal(h.timers.size, 1);
 h.viewer.unmount(h.refs); h.viewer.unmount(h.refs); assert.equal(h.timers.size, 0); assert.equal(h.container.children.length, 0);
});
for (const url of ['http://viewer.example.ts.net/', 'https://outside.test/', 'https://pentacle.example.ts.net:8444/', 'https://u:p@viewer.example.ts.net/', 'https://viewer.example.ts.net/?', 'https://viewer.example.ts.net/#', 'javascript:alert(1)', '/viewer']) {
 test('unsafe URL refused at mount/reload/opener: ' + url.split(':')[0], async t => {
  const calls = []; const h = setup({ url, cc: { openExternal: async u => { calls.push(u); } } }); t.after(h.close); await tick();
  h.reload(); h.container.querySelector('[data-modeler-open]').click(); await tick();
  assert.equal(h.frame(), null); assert.deepEqual(calls, []); assert.equal(h.state(), 'unavailable');
 });
}
test('missing URL policy refuses and external failure does not expose upstream details', async t => {
 const denied = setup({ policyValue: null }); t.after(denied.close); await tick(); assert.equal(denied.frame(), null);
 const h = setup({ cc: { openExternal: async () => { throw Error('sensitive sentinel'); } } }); t.after(h.close); await tick();
 h.container.querySelector('[data-modeler-open]').click(); await tick(); assert.match(h.container.textContent, /Could not open a new window/); assert.doesNotMatch(h.container.textContent, /sensitive sentinel/);
});
test('no legacy profile URL can authorize hosted opening', async t => {
 const dom = new JSDOM('<main></main>'); t.after(() => dom.window.close());
 const root = dom.window; root.DashboardCatalogLoader = api; root.fetch = async () => ({ ok: true, json: async () => ({ hostedDashboardAuthMode: 'identity' }) });
 const context = vm.createContext({ window: root, URL, module: { exports: {} }, setTimeout, clearTimeout });
 vm.runInContext(fs.readFileSync(require.resolve('../renderer/dashboards/modeler-3d'), 'utf8'), context);
 const refs = context.module.exports.mount(root.document.querySelector('main'), { config: { dashboards: { modeler3d: { url: fixtureUrl } }, hostedDashboardAuthMode: 'identity' } });
 await tick(); assert.equal(root.document.querySelector('iframe'), null); context.module.exports.unmount(refs);
});
