'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const source = fs.readFileSync(require.resolve('../renderer/dashboards/modeler-3d'), 'utf8');
const policy = { tailnetSuffix: 'example.ts.net', pentacleOrigin: 'https://chat.example.ts.net' };
const url = 'https://board.example.ts.net:8444/project-map/';
function harness(mode, pending) {
 const dom = new JSDOM('<main></main>', { url: policy.pentacleOrigin });
 const root = dom.window; const calls = []; let fetches = 0;
 root.cc = { openExternal: async value => { calls.push(value); return { ok: true }; } };
 root.fetch = async (path, options) => {
   assert.equal(path, '/api/config'); assert.equal(options.cache, 'no-store'); fetches++;
   if(pending) return pending;
   if(mode === 'failed') throw Error('unavailable');
   return { ok: true, json: async () => mode === 'malformed' ? null : ({ hostedDashboardAuthMode: mode }) };
 };
 root.DashboardCatalogLoader = require('../renderer/dashboards/catalog_loader');
 const context = { window: root, document: root.document, URL, setTimeout, clearTimeout, module: { exports: {} } };
 vm.runInNewContext(source, context);
 const renderer = context.module.exports;
 const container = root.document.querySelector('main');
 const refs = renderer.mount(container, { hostedUrl: url, getHostedPolicy: () => policy });
 return { root, refs, container, calls, renderer, fetches: () => fetches,
  frame: () => container.querySelector('iframe'),
  open: () => container.querySelector('[data-modeler-open]').click(),
  reload: () => container.querySelector('[data-modeler-reload]').click(),
  close: () => { renderer.unmount(refs); root.close(); } };
}
const settled = () => new Promise(resolve => setImmediate(resolve));
for (const mode of ['token', 'unknown', undefined, 'other', 'malformed', 'failed']) {
 test(`hosted ${String(mode)} denies mount/reload/external opening`, async t => {
  const h = harness(mode); t.after(h.close); await settled();
  assert.equal(h.frame(), null); assert.match(h.container.textContent, /identity mode/i);
  h.reload(); h.open(); await settled();
  assert.equal(h.frame(), null); assert.deepEqual(h.calls, []);
  assert.equal(h.container.querySelector('[data-modeler-open]').hasAttribute('href'), false);
 });
}
test('identity fetches before every frame assignment and opener; preserves sandbox', async t => {
 const h = harness('identity'); t.after(h.close); assert.equal(h.frame(), null); await settled();
 assert.equal(h.frame().src, url); assert.equal(h.frame().getAttribute('sandbox'), 'allow-scripts allow-same-origin');
 assert.equal(h.frame().referrerPolicy || h.frame().getAttribute('referrerpolicy'), 'no-referrer');
 h.open(); await settled(); assert.deepEqual(h.calls, [url]);
 h.reload(); assert.equal(h.frame(), null); await settled(); assert.equal(h.fetches(), 3);
});
test('invalidated pending identity result cannot open after connection loss', async t => {
 let resolve; const pending = new Promise(r => { resolve = r; }); const h = harness('identity', pending); t.after(h.close);
 h.root.dispatchEvent(new h.root.Event('pentacle:hosted-dashboard-invalidate'));
 resolve({ ok: true, json: async () => ({ hostedDashboardAuthMode: 'identity' }) }); await settled();
 assert.equal(h.frame(), null); assert.deepEqual(h.calls, []);
});
test('active frame is removed immediately on auth/config invalidation', async t => {
 const h = harness('identity'); t.after(h.close); await settled(); assert.ok(h.frame());
 h.root.dispatchEvent(new h.root.Event('pentacle:hosted-dashboard-invalidate'));
 assert.equal(h.frame(), null); assert.match(h.container.textContent, /identity mode/i);
});

test('cached identity catalog/config cannot authorize a later token action', async t => {
 const h=harness('identity');t.after(h.close);await settled();assert.ok(h.frame());
 h.root.__PENTACLE_CONFIG__={hostedDashboardAuthMode:'identity'};
 h.root.sessionStorage.setItem('dashboard-catalog:example__catalog',JSON.stringify({catalog:{hostedDashboardAuthMode:'identity'},savedAt:1}));
 h.root.fetch=async()=>({ok:true,json:async()=>({hostedDashboardAuthMode:'token'})});
 h.open();await settled();assert.equal(h.frame(),null);assert.deepEqual(h.calls,[]);
 h.reload();await settled();assert.equal(h.frame(),null);
});
test('current policy is revalidated on every action and delayed opener dies on invalidation', async t => {
 const h=harness('identity');t.after(h.close);await settled();
 let resolve;h.root.fetch=()=>new Promise(r=>{resolve=r});h.open();
 h.root.dispatchEvent(new h.root.Event('pentacle:hosted-dashboard-invalidate'));
 resolve({ok:true,json:async()=>({hostedDashboardAuthMode:'identity'})});await settled();assert.deepEqual(h.calls,[]);
});
