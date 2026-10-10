'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const fs = require('node:fs');
const path = require('node:path');
const api = require('../renderer/dashboards/catalog_loader');
const cases = require('./fixtures/dashboard_catalog/catalog_cases.json');
const clone = value => JSON.parse(JSON.stringify(value));
const full = () => clone(cases.valid[0].catalog);
const ok = body => ({ ok: true, asset: { body: JSON.stringify(body) } });
const meta = { asset_id: 'dashboard-catalog', content_type: 'dashboard-catalog', stream_id: 'hostx:example-catalog-owner' };
function harness(options = {}) {
  const calls = [], saved = new Map(), root = new JSDOM('<!doctype html><head></head><body><main></main></body>', { url: 'https://example.test' }).window;
  root.DASHBOARDS = [];
  let list = { assets: [meta] }, get = ok(full());
  const cc = { async assetList(p) { calls.push(['list', p]); if (list instanceof Error) throw list; return list; }, async assetGet(p) { calls.push(['get', p]); if (get instanceof Error) throw get; return get; } };
  const storage = { getItem: k => saved.get(k), setItem: (k, v) => saved.set(k, v) };
  const loader = api.createLoader({ root, cc, storage, now: () => 10000, loadTimeoutMs: 80, ...options });
  return { root, cc, saved, storage, loader, calls, setList: v => { list = v; }, setGet: v => { get = v; } };
}
for (const c of cases.valid) test(`catalog fixture valid: ${c.name}`, () => assert.equal(api.validateCatalog(c.catalog), c.catalog));
for (const c of cases.invalid) test(`catalog fixture invalid: ${c.name}`, () => assert.throws(() => api.validateCatalog(c.catalog), error => error.message.includes(c.error), c.error));

test('host_api above 2 is unsupported only after full schema validation', () => {
  const catalog = full(); catalog.requires.host_api = 3;
  assert.throws(() => api.validateCatalog(catalog), e => e.unsupported === true);
  catalog.requires.host_api = 9007199254740992;
  assert.throws(() => api.validateCatalog(catalog), e => e.unsupported === true);
  catalog.boards[0].extra = true;
  assert.throws(() => api.validateCatalog(catalog), e => !e.unsupported);
});
test('parity: fixed classes, Unicode code points, full regex match and Python byte separators', () => {
  assert.equal(api.keyFormatWidth('[A-Z][0-9]{01}T\\.'), 4);
  for (const format of ['[0-9]{0}ABCD', '[0-9]{100}', '[0-9]{4}.', '\\d{4}', '[a-z]{4}']) assert.throws(() => api.keyFormatWidth(format));
  const catalog = full(); catalog.boards[0].name = '😀'.repeat(64); assert.doesNotThrow(() => api.validateCatalog(catalog));
  catalog.boards[0].name += '😀'; assert.throws(() => api.validateCatalog(catalog));
  catalog.boards[0].name = 'Example'; catalog.boards[0].id += '\n'; assert.throws(() => api.validateCatalog(catalog));
  const unicode = full(); unicode.boards[1].actions = Array(630).fill('assetList');
  assert.ok(Buffer.byteLength(JSON.stringify(unicode.boards[1])) < 8192);
  assert.throws(() => api.validateCatalog(unicode), /entry exceeds/);
});
test('hosted URLs reject parser normalization, userinfo, whitespace, invalid port and overlong URLs', () => {
  for (const url of ['http://:', 'https:///x', 'https://user@example.test', 'https://@example.test', 'https://example.test:99999', 'https://example.test/a b', 'https:\\example.test', 'https://example.test/' + 'x'.repeat(2048)]) assert.equal(api.plainHttpUrl(url), false, url);
  assert.equal(api.plainHttpUrl('http://[::1]:8080/app'), true);
  assert.equal(api.plainHttpUrl('HTTPS://viewer.example.test/app'), true);
  assert.equal(api.plainHttpUrl('https://example.test/' + '😀'.repeat(1500)), true);
  assert.equal(api.plainHttpUrl('https://example.test/' + '😀'.repeat(2048)), false);
});
test('unset catalog makes zero transport calls', async () => {
  const h = harness(); assert.equal((await h.loader.refresh()).status, 'unset'); assert.deepEqual(h.calls, []);
});
test('discovery selects exact id plus content type and gets the listed owner', async () => {
  const h = harness(); h.setList({ assets: [{ ...meta, asset_id: 'example-other' }, { ...meta, content_type: 'report' }, meta] });
  const r = await h.loader.refresh('example__catalog'); assert.equal(r.status, 'ready');
  assert.deepEqual(h.calls, [['list', { spec_id: 'example__catalog' }], ['get', { stream_id: meta.stream_id, asset_id: 'dashboard-catalog', spec_id: 'example__catalog' }]]);
});
test('transport unavailable without cache, malformed and unsupported cards are exact', async () => {
  const h = harness(); h.setList(new Error('offline'));
  let r = await h.loader.refresh('example__catalog'); assert.equal(r.message, 'Dashboard catalog unavailable: offline'); assert.equal(r.catalog, null);
  h.setList({ assets: [meta] }); h.setGet({ asset: { body: '{broken' } });
  r = await h.loader.refresh('example__catalog'); assert.equal(r.status, 'malformed'); assert.match(r.message, /^Dashboard catalog unsupported\/malformed: .* \(catalog unknown\)$/);
  const c = full(); c.requires.host_api = 3; h.setGet(ok(c)); r = await h.loader.refresh('example__catalog'); assert.equal(r.status, 'unsupported'); assert.match(r.message, /catalog 0\.1\.0\+aaaaaaa/);
});
test('validated cache survives malformed/unsupported fetches and is shown only on unavailability', async () => {
  const h = harness(); const good = await h.loader.refresh('example__catalog');
  h.setGet({ asset: { body: '{broken' } }); let r = await h.loader.refresh('example__catalog'); assert.equal(r.catalog, null); assert.equal(r.cached, false);
  const unsupported = full(); unsupported.requires.host_api = 3; h.setGet(ok(unsupported)); r = await h.loader.refresh('example__catalog'); assert.equal(r.catalog, null);
  h.setGet(new Error('offline')); r = await h.loader.refresh('example__catalog'); assert.equal(r.cached, true); assert.deepEqual(r.catalog, good.catalog); assert.equal(r.age, 0);
  r = await h.loader.refresh('example__other'); assert.equal(r.cached, false);
});
test('session cache is validated; denied storage still preserves memory cache', async () => {
  const h = harness(); await h.loader.refresh('example__catalog'); h.setList(new Error('offline'));
  const restored = api.createLoader({ root: h.root, cc: h.cc, storage: h.storage, now: () => 12000 });
  assert.equal((await restored.refresh('example__catalog')).age, 2000);
  h.saved.set('dashboard-catalog:example__catalog', '{invalid');
  assert.equal((await api.createLoader({ root: h.root, cc: h.cc, storage: h.storage }).refresh('example__catalog')).catalog, null);
  const blocked = harness({ storage: { getItem() { throw new Error('denied'); }, setItem() { throw new Error('quota'); } } });
  await blocked.loader.refresh('example__catalog'); blocked.setList(new Error('offline')); assert.equal((await blocked.loader.refresh('example__catalog')).cached, true);
});
test('valid fetch replaces cache atomically; cancelled stale response cannot replace it', async () => {
  const h = harness(); await h.loader.refresh('example__catalog'); const next = full(); next.catalog_version = '0.2.1+bbbbbbb'; h.setGet(ok(next)); await h.loader.refresh('example__catalog');
  let resolve; h.cc.assetGet = () => new Promise(r => { resolve = r; }); const pending = h.loader.refresh('example__catalog'); await Promise.resolve(); h.loader.cancel(); resolve(ok(full()));
  assert.equal((await pending).status, 'superseded'); h.setList(new Error('offline'));
  assert.equal((await h.loader.refresh('example__catalog')).catalog.catalog_version, next.catalog_version);
});
test('missing metadata, daemon failures and malformed response are unavailable', async () => {
  for (const list of [{ assets: [] }, { ok: false, error: 'asset_denied' }, {}, { assets: [{ ...meta, stream_id: '' }] }]) {
    const h = harness(); h.setList(list); assert.equal((await h.loader.refresh('example__catalog')).status, 'unavailable');
  }
});
function autoTags(h, register = () => {}) {
  const seen = [], append = h.root.document.head.appendChild.bind(h.root.document.head);
  h.root.document.head.appendChild = tag => {
    seen.push(tag); append(tag);
    queueMicrotask(() => { register(tag); tag.onload?.(); }); return tag;
  };
  return seen;
}
test('SRI libs then CSS then script use exact versioned paths, integrity and anonymous CORS', async () => {
  const h = harness(), catalog = full(), entry = catalog.boards[1];
  const seen = autoTags(h, tag => { if (tag.getAttribute('src')?.endsWith(entry.web.script)) h.root.DASHBOARDS.push({ id: entry.id, mount() {} }); });
  await h.loader.loadAdapter(catalog, entry);
  assert.deepEqual(seen.map(t => t.getAttribute('src') || t.getAttribute('href')), [catalog.libs[0].path, entry.web.css, entry.web.script].map(p => `/dashboards/private/${catalog.catalog_version}/${p}`));
  assert.deepEqual(seen.map(t => t.integrity), [catalog.libs[0].sha256, entry.web.css_sha256, entry.web.sha256].map(x => 'sha256-' + Buffer.from(x, 'hex').toString('base64')));
  assert.ok(seen.every(t => t.crossOrigin === 'anonymous')); assert.equal(seen[1].rel, 'stylesheet'); assert.deepEqual(h.root.DASHBOARDS, []);
});
test('N, N+1 and rollback adapters keep distinct version/hash registration identity', async () => {
  const h = harness(), first = full(), second = full(); first.catalog_version = '0.2.0+aaaaaaa'; second.catalog_version = '0.2.1+bbbbbbb'; second.boards[1].web.sha256 = 'b'.repeat(64);
  const seen = autoTags(h, tag => { if (tag.getAttribute('src')?.endsWith('/example-board.js')) h.root.DASHBOARDS.push({ id: 'example-board', src: tag.getAttribute('src'), mount() {} }); });
  const n = await h.loader.loadAdapter(first, first.boards[1]), n1 = await h.loader.loadAdapter(second, second.boards[1]);
  assert.notEqual(n, n1); assert.ok(n.src.includes(first.catalog_version)); assert.ok(n1.src.includes(second.catalog_version));
  const rollback = await h.loader.loadAdapter(first, first.boards[1]);
  assert.notEqual(rollback, n); assert.equal(rollback.src, n.src); assert.equal(seen.length, 8);
});
test('missing/wrong registration fails even when an old matching board exists; siblings survive', async () => {
  const h = harness(), c = full(), old = { id: 'example-board', mount() {} }; h.root.DASHBOARDS.push(old);
  autoTags(h, tag => { if (tag.getAttribute('src')?.endsWith('/example-board.js')) h.root.DASHBOARDS.push({ id: 'example-wrong', mount() {} }); });
  await assert.rejects(h.loader.loadAdapter(c, c.boards[1]), /register only example-board/); assert.deepEqual(h.root.DASHBOARDS, [old]);
});
test('tag failure preserves integrity and 404 copy; timeout removes failed tag', async () => {
  for (const status of [200, 404]) {
    const h = harness(), c = full(); h.root.fetch = async () => ({ status }); const append = h.root.document.head.appendChild.bind(h.root.document.head);
    h.root.document.head.appendChild = tag => { append(tag); queueMicrotask(() => tag.onerror()); return tag; };
    await assert.rejects(h.loader.loadTag(c, 'web/example.js', 'a'.repeat(64)), status === 404 ? /catalog files for version .* not installed/ : /integrity or load error/);
    assert.equal(h.root.document.head.children.length, 0);
  }
  const h = harness({ loadTimeoutMs: 1 }); await assert.rejects(h.loader.loadTag(full(), 'web/example.js', 'a'.repeat(64)), /timed out/);
});
test('allowlist has exactly asset actions and denied calls resolve plus warn', async () => {
  const warnings = [], cc = { assetList: async p => ({ ok: true, p }) }; const actions = api.actions({ id: 'example-board', actions: ['assetList', 'household'] }, cc, (...x) => warnings.push(x));
  assert.deepEqual(Object.keys(actions), ['assetList', 'assetGet']); assert.equal((await actions.assetList({})).ok, true); assert.deepEqual(await actions.assetGet({}), { ok: false, error: 'action_not_allowed' }); assert.equal(warnings.length, 1);
});
test('merge resolves built-ins only where the catalog references them', () => {
  const h = harness(), c = full(), builtin = { id: 'example-board', mount() {} };
  c.boards[1] = { id: builtin.id, name: 'Built in', kind: 'built-in' };
  const result = h.loader.merge({ catalog: c }, [builtin], { reportBoard: { createBoard: entry => entry }, hostedBoard: {} });
  assert.deepEqual(result.boards.map(b => b.id), ['example-report', 'example-board', 'example-hosted']); assert.equal(result.boards[1].mount, builtin.mount); assert.deepEqual(result.errors, []);
});
test('mount throws into an isolated board card; late loads after disposal never mount', async () => {
  for (const dispose of [false, true]) {
    const h = harness(), c = full(); c.libs = []; c.boards = [c.boards[1]]; delete c.boards[0].web.css; delete c.boards[0].web.css_sha256;
    let mounts = 0; autoTags(h, () => h.root.DASHBOARDS.push({ id: 'example-board', mount() { mounts++; throw new Error('synthetic mount'); } }));
    const board = h.loader.merge({ catalog: c }, []).boards[0], container = h.root.document.querySelector('main'), refs = board.mount(container, { config: {} });
    if (dispose) board.unmount(refs); await refs.ready;
    assert.equal(mounts, dispose ? 0 : 1); if (!dispose) { assert.equal(container.dataset.boardState, 'error'); assert.equal(container.querySelector('[data-testid="dashboard-board-error"]').textContent, 'Board failed to load: synthetic mount'); }
  }
});
function hostedViewer(h, timers) {
 const vm = require('node:vm'); h.root.DashboardCatalogLoader = api;
 h.root.fetch = async () => ({ ok: true, json: async () => ({ hostedDashboardAuthMode: 'identity' }) });
 h.root.hostedDashboardPolicy = cases.hosted_policy;
 let serial = 0;
 const context = vm.createContext({ window: h.root, module: { exports: {} }, URL,
  setTimeout: timers ? fn => { timers.set(++serial, fn); return serial; } : setTimeout,
  clearTimeout: timers ? id => timers.delete(id) : clearTimeout });
 vm.runInContext(fs.readFileSync(path.join(__dirname, '../renderer/dashboards/modeler-3d.js'), 'utf8'), context);
 return context.module.exports;
}
test('hosted entries use current policy/auth and leave profile configuration unchanged', async () => {
 const h = harness(), c = full(); c.boards = [c.boards[2]];
 c.boards[0].hosted.url = 'https://viewer.example.ts.net/app/';
 const board = h.loader.merge({ catalog: c }, [], { hostedBoard: hostedViewer(h) }).boards[0];
 const config = { dashboards: { modeler3d: { url: 'https://example.test/original' } } }, container = h.root.document.querySelector('main');
 const refs = board.mount(container, { config }); await new Promise(r => setImmediate(r));
 const frame = container.querySelector('iframe');
 assert.equal(frame.getAttribute('src'), c.boards[0].hosted.url); assert.equal(frame.getAttribute('sandbox'), 'allow-scripts allow-same-origin'); assert.equal(frame.getAttribute('referrerpolicy'), 'no-referrer');
 assert.equal(container.querySelector('h1').textContent, c.boards[0].name); assert.equal(config.dashboards.modeler3d.url, 'https://example.test/original'); board.unmount(refs); assert.equal(container.querySelector('iframe'), null); h.root.close();
});
test('app/build wiring loads optional catalog on view entry, preserves startup isolation and external scripts', () => {
  const app = fs.readFileSync(path.join(__dirname, '../renderer/app.js'), 'utf8'); const build = fs.readFileSync(path.join(__dirname, '../scripts/build-web.js'), 'utf8');
  assert.match(app, /async function enterDashboardView\(\)/); assert.match(app, /await CFG_READY/); assert.match(app, /void enterDashboardView\(\)/); assert.match(app, /catalogViewGeneration\+\+/); assert.match(build, /dashboards-catalog-loader\.js/); assert.match(build, /dashboards-report-board\.js/);
});

test('view refresh during script loading does not restore an obsolete registry', async () => {
  const h = harness(), c = full(); c.libs = []; delete c.boards[1].web.css; delete c.boards[1].web.css_sha256;
  const original = { id: 'example-static' }; h.root.DASHBOARDS.push(original);
  const promise = h.loader.loadAdapter(c, c.boards[1]); await Promise.resolve();
  const tag = h.root.document.querySelector('script');
  const current = { id: 'example-current', catalog: true }; h.root.DASHBOARDS.splice(1, 0, current);
  h.root.DASHBOARDS.push({ id: 'example-board', mount() {} }); tag.onload(); await promise;
  assert.deepEqual(h.root.DASHBOARDS, [original, current]);
});
test('switching versions and rolling back disables stale catalog styles', async () => {
  const h = harness(), first = full(), second = full(); first.catalog_version = '0.2.0+aaaaaaa'; second.catalog_version = '0.2.1+bbbbbbb';
  first.boards = []; second.boards = []; autoTags(h);
  h.loader.merge({ catalog: first }, []); const n = await h.loader.loadTag(first, 'web/example.css', 'a'.repeat(64));
  h.loader.merge({ catalog: second }, []); const n1 = await h.loader.loadTag(second, 'web/example.css', 'b'.repeat(64)); assert.equal(n.disabled, true); assert.equal(n1.disabled, false);
  h.loader.merge({ catalog: first }, []); assert.equal(n.disabled, false); assert.equal(n1.disabled, true);
});

test('N to N+1 to N reruns actual fixture library and adapter source without stale globals', async () => {
  const vm = require('node:vm'), os = require('node:os');
  const { buildCatalogFixture } = require('./e2e/lib/dashboard_catalog_fixture');
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'example-catalog-'));
  try {
    const fixture = buildCatalogFixture(scratch), h = harness();
    const context = vm.createContext({ window: h.root, document: h.root.document });
    autoTags(h, tag => {
      const url = tag.getAttribute('src'); if (!url) return;
      const version = fixture.versions.find(v => url.includes(`/${v.version}/`));
      vm.runInContext(version.files[url.split(`/${version.version}/`)[1]], context);
    });
    const observations = [];
    for (const version of [fixture.versions[0], fixture.versions[1], fixture.versions[0]]) {
      const adapter = await h.loader.loadAdapter(version.catalog, version.catalog.boards[1]);
      const container = h.root.document.querySelector('main'); container.replaceChildren(); adapter.mount(container);
      observations.push(container.textContent);
      assert.ok(container.textContent.includes(`lib ${version.version}`));
    }
    assert.equal(observations[2], observations[0]);
  } finally { fs.rmSync(scratch, { recursive: true, force: true }); }
});
test('pending catalog refresh disables stale selection and disposes old refs before registry replacement', async () => {
  const vm = require('node:vm'), app = fs.readFileSync(path.join(__dirname, '../renderer/app.js'), 'utf8');
  const dom = new JSDOM('<head></head><body><button class="view-btn" data-view="chats"></button><button class="view-btn" data-view="dashboards"></button><div class="grid"></div><div id="panel-sessions"></div><div id="panel-dashboards"><div id="dashboard-list"></div></div><div id="dashboard-content"></div></body>', { url: 'https://example.test' });
  const window = dom.window, document = window.document, state = { currentView: 'chats', selectedDashboard: null, dashboardPollToken: 0 };
  const first = full(), next = full(); first.libs = []; next.libs = []; first.boards = [first.boards[1]]; next.boards = [next.boards[1]];
  delete first.boards[0].web.css; delete first.boards[0].web.css_sha256; delete next.boards[0].web.css; delete next.boards[0].web.css_sha256;
  next.catalog_version = '0.2.1+bbbbbbb'; next.boards[0].web.sha256 = 'b'.repeat(64); delete first.boards[0].poll_interval_ms; delete next.boards[0].poll_interval_ms;
  let hold = false, resolve; const cc = { assetList: async () => ({ assets: [meta] }), assetGet: () => hold ? new Promise(r => { resolve = r; }) : Promise.resolve(ok(first)) };
  const loader = api.createLoader({ root: window, cc, storage: null, loadTimeoutMs: 1000 });
  const context = vm.createContext({ window, document, state, CONFIG: { dashboards: { catalogSpecId: 'example__catalog' } }, CFG_READY: Promise.resolve(), dashboardCatalog: { createLoader: () => loader }, dashboardReports: {}, assetRender: {}, require() {}, gridColResizers: [], sidebarResizer: null, scheduleVisibleSlotFits() {}, setInterval, clearInterval, console });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../renderer/dashboards/registry.js'), 'utf8'), context);
  window.DASHBOARDS.push({ id: 'example-static', name: 'Example', mount(c) { c.textContent = 'static'; return {}; }, unmount() {} });
  vm.runInContext(app.slice(app.indexOf('// ── View Switcher (Chats / Dashboards)'), app.indexOf('// ── Toolbar Buttons')), context);
  const run = source => vm.runInContext(source, context), tick = () => new Promise(r => setImmediate(r));
  run("switchView('dashboards')"); await tick(); run("switchView('chats')"); hold = true; run("switchView('dashboards')"); await tick();
  assert.equal(document.querySelectorAll('[data-dashboard-id]').length, 0);
  run("selectDashboard('example-board')"); assert.equal(state.dashboardRefs, null);
  // Independently protect the commit boundary even if a caller already started a mount.
  run("state.selectedDashboard = 'example-board'; mountAndPoll('example-board')"); await tick();
  const oldRefs = state.dashboardRefs, oldTag = document.querySelector('script'); let oldMounts = 0;
  resolve(ok(next)); await tick(); const newRefs = state.dashboardRefs; assert.equal(oldRefs.disposed, true);
  window.DASHBOARDS.push({ id: 'example-board', mount() { oldMounts++; } }); oldTag.onload(); await tick();
  const newTag = [...document.querySelectorAll('script')].find(tag => tag !== oldTag); assert.ok(newTag);
  window.DASHBOARDS.push({ id: 'example-board', mount(c) { c.textContent = 'new adapter'; } }); newTag.onload(); await newRefs.ready;
  assert.equal(oldMounts, 0); assert.match(document.getElementById('dashboard-content').textContent, /new adapter/);
  assert.equal(document.getElementById('dashboard-content').dataset.catalogVersion, next.catalog_version); run("switchView('chats')"); window.close();
});
test('hosted panel synchronizes loading/error/timeout/reload/success and cancels events', async () => {
 const h = harness(), catalog = full(); catalog.boards = [catalog.boards[2]]; catalog.boards[0].hosted.url = 'https://viewer.example.ts.net/app/';
 const timers = new Map(); const viewer = hostedViewer(h, timers);
 const board = h.loader.merge({ catalog }, [], { hostedBoard: viewer }).boards[0], container = h.root.document.querySelector('main');
 const refs = board.mount(container), flush = () => new Promise(r => setImmediate(r));
 assert.equal(container.dataset.boardState, 'loading'); await flush();
 container.querySelector('iframe').dispatchEvent(new h.root.Event('error'));
 assert.equal(container.dataset.boardState, 'error'); assert.match(container.textContent, /Could not open dashboard/);
 container.querySelector('[data-modeler-reload]').click(); await flush(); assert.equal(container.dataset.boardState, 'loading');
 [...timers.values()][0](); assert.equal(container.dataset.boardState, 'error'); assert.match(container.textContent, /timed out/);
 container.querySelector('[data-modeler-reload]').click(); await flush(); const frame = container.querySelector('iframe'); frame.dispatchEvent(new h.root.Event('load'));
 assert.equal(container.dataset.boardState, 'ready');
 board.unmount(refs); frame.dispatchEvent(new h.root.Event('error')); assert.equal(container.querySelector('iframe'), null); assert.equal(timers.size, 0); h.root.close();
});

test('mixed catalog alone supplies membership, array order and visibility', () => {
 const h = harness();
 const catalog = full(); catalog.requires.host_api = 2;
 catalog.boards = [
  { id: 'missing-board', name: 'Missing', kind: 'built-in' },
  { id: 'hosted-board', name: 'Hosted', kind: 'hosted-view', hosted: { url: 'https://board.example.ts.net/' } },
  { id: 'static-board', name: 'Static', kind: 'built-in' },
  { id: 'hidden-board', name: 'Hidden', kind: 'built-in', visible: false },
 ];
 api.validateCatalog(catalog);
 const result = h.loader.merge({ catalog }, [{ id: 'static-board', name: 'Code', mount() {} }, { id: 'extra-board', name: 'Extra' }], { hostedBoard: { mount() {} } });
 assert.deepEqual(result.boards.map(b => b.id), ['missing-board', 'hosted-board', 'static-board']);
 const container = h.root.document.querySelector('main'); result.boards[0].mount(container);
 assert.match(container.textContent, /unavailable/i); h.root.close();
});
test('hosted URL policy rejects unsafe URLs and missing policy at opening', () => {
 const policy = { tailnetSuffix: 'example.ts.net', pentacleOrigin: 'https://chat.example.ts.net' };
 assert.equal(api.admitHostedUrl('https://board.example.ts.net:8444/project-map/', policy), 'https://board.example.ts.net:8444/project-map/');
 for (const url of ['http://board.example.ts.net/', 'https://board.example.ts.net.evil.test/', 'https://badexample.ts.net/', 'https://chat.example.ts.net:8444/', 'https://u@board.example.ts.net/', 'https://board.example.ts.net/?', 'https://board.example.ts.net/#', 'https://board.example.ts.net/?token=x']) assert.equal(api.admitHostedUrl(url, policy), null);
 assert.equal(api.admitHostedUrl('https://board.example.ts.net/', null), null);
});

for (const item of cases.hosted_urls) test('shared URL opening: ' + item.name, () => assert.equal(!!api.admitHostedUrl(item.url, cases.hosted_policy), item.allowed));
