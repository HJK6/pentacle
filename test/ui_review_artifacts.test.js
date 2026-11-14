'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const library = require('../vendor/dashboard-library/dist/dashboard-library.js');
const item = { id: 'home', title: 'Home', kind: 'view', entry: 'mobile/Home.html', surface: 'mobile', viewport: { width: 390, height: 844 }, approval_ref: null, compatibility: { status: 'offline-interactive', reason: 'Verified offline' }, screenshots: [{ name: 'Home', path: 'screenshots/home.png' }], prototype: true };
const catalog = { schema: 'design-catalog.v1', projects: ['example', 'sample', 'demo'].map(id => ({ id, title: id, sync: 'verified', revision_id: 'a'.repeat(64), revisions: [], manifest: { project_id: id, title: id, revision_id: 'a'.repeat(64), source_revision: 'source-v1', complete: true, views: [item, { ...item, id: 'detail', title: 'Detail', surface: 'web' }, { ...item, id: 'still', title: 'Still', prototype: false }], flows: [{ ...item, id: 'old-flow', kind: 'flow' }] } })) };
const tick = () => new Promise(resolve => setImmediate(resolve));
const documentFetch = async () => ({ ok: true, json: async () => ({ document: '<h1>Prototype</h1>' }) });
function setup(fetch = documentFetch) {
  const dom = new JSDOM('<main></main>', { url: 'https://app.example.test/designs/' });
  const refs = library.mountGallery(dom.window.document.querySelector('main'), { fetch });
  return { dom, refs, close() { library.unmountGallery(refs); dom.window.close(); } };
}
test('gallery opens only clickable views and preserves a running opaque prototype on polling', async () => {
  const { dom, refs, close } = setup();
  library.updateGallery(refs, catalog); await tick();
  assert.equal(refs.root.querySelectorAll('[data-project]').length, 3);
  assert.deepEqual([...refs.root.querySelectorAll('[data-item]')].map(el => el.dataset.item), ['home', 'detail']);
  assert.equal(refs.root.querySelectorAll('.dg-kinds button,img,video').length, 0);
  const frame = refs.root.querySelector('iframe'); assert.ok(frame);
  assert.equal(frame.getAttribute('sandbox'), 'allow-scripts');
  assert.equal(frame.getAttribute('referrerpolicy'), 'no-referrer');
  assert.equal(frame.srcdoc, '<h1>Prototype</h1>');
  assert.equal(frame.dataset.surface, 'mobile');
  assert.equal(frame.style.getPropertyValue('--dg-phone-width'), '390px');
  assert.equal(frame.style.getPropertyValue('--dg-phone-ratio'), '390 / 844');
  const changed = structuredClone(catalog); changed.projects[0].sync = 'pending';
  library.updateGallery(refs, changed); assert.equal(refs.root.querySelector('iframe'), frame);
  refs.root.querySelector('[data-item="detail"]').click(); await tick();
  assert.equal(refs.root.querySelector('h1').textContent, 'Detail');
  assert.equal(refs.root.querySelector('iframe').dataset.surface, 'web');
  refs.root.querySelector('[data-project="sample"]').click(); await tick();
  assert.equal(refs.state.project, 'sample'); assert.ok(refs.root.querySelector('iframe'));
  library.unmountGallery(refs); assert.equal(dom.window.document.querySelector('iframe'), null); dom.window.close();
});
test('saved screenshot and flow selections reopen on an available clickable prototype', async () => {
  for (const saved of [{ item: 'still', kind: 'views', mode: 'screens' }, { item: 'old-flow', kind: 'flows', mode: 'video' }]) {
    const dom = new JSDOM('<main></main>', { url: 'https://app.example.test/designs/' });
    dom.window.sessionStorage.setItem('design-gallery.selection', JSON.stringify({ project: 'example', ...saved }));
    const refs = library.mountGallery(dom.window.document.querySelector('main'), { fetch: documentFetch });
    library.updateGallery(refs, catalog); await tick();
    assert.equal(refs.state.item, 'home'); assert.ok(refs.root.querySelector('iframe'));
    assert.equal(refs.root.querySelectorAll('img,video').length, 0);
    library.unmountGallery(refs); dom.window.close();
  }
});
test('gallery handles empty, screenshot-only, search and unavailable documents without screenshots', async () => {
  const { dom, refs, close } = setup(async () => ({ ok: false }));
  library.updateGallery(refs, { projects: [] }); assert.match(refs.root.textContent, /library is empty/);
  const stills = structuredClone(catalog); stills.projects.forEach(p => p.manifest.views.forEach(v => { v.prototype = false; }));
  library.updateGallery(refs, stills); assert.equal(refs.root.querySelectorAll('[data-item],img,video,iframe').length, 0);
  assert.match(refs.root.querySelector('.dg-items').textContent, /No live designs/);
  library.updateGallery(refs, catalog); await tick(); assert.match(refs.root.querySelector('.dg-stage').textContent, /unavailable/);
  const input = refs.root.querySelector('input'); input.value = 'absent'; input.dispatchEvent(new dom.window.Event('input'));
  assert.match(refs.root.querySelector('.dg-items').textContent, /No matching/);
  assert.equal(refs.root.querySelectorAll('img,video').length, 0); close();
});
test('saved History waits for the retained manifest and shows only its clickable views', async () => {
  const dom = new JSDOM('<main></main>', { url: 'https://app.example.test/designs/' }), old = 'b'.repeat(64);
  const data = structuredClone(catalog); data.projects[0].revisions = [{ revision_id: old, source_revision: 'old-A', complete: true }];
  dom.window.sessionStorage.setItem('design-gallery.selection', JSON.stringify({ project: 'example', revision: old, item: 'still', kind: 'views', mode: 'screens' }));
  let resolve; const pending = new Promise(done => { resolve = done; }); const requests = [];
  const refs = library.mountGallery(dom.window.document.querySelector('main'), { fetch: url => { requests.push(url); return url.endsWith('/home') ? documentFetch() : pending; } });
  library.updateGallery(refs, data); assert.equal(refs.root.querySelectorAll('[data-item],iframe,img').length, 0);
  const retained = structuredClone(data.projects[0].manifest); retained.revision_id = old; retained.views[0].title = 'Retained A';
  resolve({ ok: true, json: async () => retained }); await tick();
  assert.deepEqual(requests, ['/api/designs/example/' + old, '/api/designs/example/' + old + '/home']);
  assert.equal(refs.root.querySelector('h1').textContent, 'Retained A'); assert.ok(refs.root.querySelector('iframe'));
  assert.equal(refs.root.querySelectorAll('[data-item="still"],img,video').length, 0);
  assert.equal(refs.root.querySelector('[aria-label="History"]').value, old);
  library.unmountGallery(refs); dom.window.close();
});

test('flow overview groups live routes, orders numerically and preserves frames during polling', async () => {
  const { refs, close } = setup(); const data = structuredClone(catalog);
  data.projects[0].manifest.views = [
    { ...item, group: 'Workspace A', variant: 'prototype' },
    { ...item, id: 'alternate', group: 'Workspace A', variant: 'prototype' },
    { ...item, id: 'last', title: 'Last', group: 'Workspace A', variant: 'flow-10' },
    { ...item, id: 'first', title: 'First', group: 'Workspace A', variant: 'flow-02' },
    { ...item, id: 'still', group: 'Workspace A', variant: 'flow-01', prototype: false },
    { ...item, id: 'other', group: 'Workspace B', variant: 'flow-01', surface: 'web', viewport: { width: 1280, height: 800 } },
  ];
  library.updateGallery(refs, data); await tick();
  assert.deepEqual([...refs.root.querySelectorAll('[data-item]')].map(el => el.dataset.item), ['home', 'alternate', 'other']);
  const overview = () => [...refs.root.querySelectorAll('.dg-modes button')].find(b => b.textContent === 'Flow overview');
  assert.ok(overview(), 'flow overview must be offered'); overview().click(); await tick();
  assert.deepEqual([...refs.root.querySelectorAll('.dg-flow-card')].map(el => el.dataset.view), ['first', 'last']);
  const frames = [...refs.root.querySelectorAll('iframe')]; assert.equal(frames.length, 2);
  assert.ok(frames.every(frame => frame.getAttribute('sandbox') === 'allow-scripts'));
  assert.equal(refs.root.querySelector('.dg-flow-card').style.width, '390px');
  const changed = structuredClone(data); changed.projects[0].sync = 'pending'; library.updateGallery(refs, changed);
  assert.deepEqual([...refs.root.querySelectorAll('iframe')], frames);
  refs.root.querySelector('[data-item="other"]').click(); await tick();
  assert.equal(refs.state.mode, 'prototype'); overview().click(); await tick();
  assert.deepEqual([...refs.root.querySelectorAll('.dg-flow-card')].map(el => el.dataset.view), ['other']);
  assert.equal(refs.root.querySelector('.dg-flow-card').style.width, '1280px');
  close();
});
test('mode switches cancel pending overview documents and expose missing live documents without screenshots', async () => {
  const requests = []; const { refs, close } = setup((url, options) => new Promise(resolve => requests.push({ url, signal: options.signal, resolve })));
  library.updateGallery(refs, catalog); requests[0].resolve({ ok: true, json: async () => ({ document: '<p>Ready</p>' }) }); await tick();
  [...refs.root.querySelectorAll('.dg-modes button')].find(b => b.textContent === 'Flow overview').click();
  const pending = requests.slice(1); assert.equal(pending.length, 2);
  [...refs.root.querySelectorAll('.dg-modes button')].find(b => b.textContent === 'Prototype').click();
  assert.ok(pending.every(r => r.signal.aborted));
  for (const r of pending) r.resolve({ ok: true, json: async () => ({ document: '<p>Stale</p>' }) });
  requests.at(-1).resolve({ ok: true, json: async () => ({ document: null }) }); await tick();
  assert.equal(refs.root.querySelectorAll('iframe,img,video').length, 0);
  assert.match(refs.root.querySelector('.dg-stage').textContent, /unavailable/); close();
});
