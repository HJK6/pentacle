'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');

const sourceFile = path.join(__dirname, '../renderer/dashboards/modeler-3d.js');
const fixtureUrl = 'http://127.0.0.1:9031/modeler_viewer.html';
function setup(config = {}, cc) {
  // jsdom does not fetch frame resources here: every event is synthetic and the
  // fixture's actual browser load is covered separately by web-dashboards-revamp.
  const dom = new JSDOM('<!doctype html><main id="root"></main>');
  const timers = new Map(); let timerId = 0;
  const { window } = dom; window.DASHBOARDS = []; window.cc = cc;
  const context = { window, document: window.document, URL, console, module: { exports: {} },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); } };
  vm.runInNewContext(fs.readFileSync(sourceFile, 'utf8'), context);
  const adapter = window.DASHBOARDS[0]; const root = window.document.getElementById('root');
  const refs = adapter.mount(root, { config });
  return { window, dom, timers, adapter, root, refs, state: () => root.querySelector('[data-modeler-state]')?.dataset.modelerState,
    frame: () => root.querySelector('iframe'), text: () => root.textContent,
    reload: () => root.querySelector('[data-modeler-reload]').click(),
    timeout: () => { const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(({ fn }) => fn()); },
    close() { adapter.unmount(refs); window.close(); } };
}
const configured = { dashboards: { modeler3d: { url: fixtureUrl } } };

test('unconfigured modeler always registers with clear setup text and visible controls', t => {
  const h = setup(); t.after(h.close);
  assert.equal(h.adapter.id, 'modeler-3d'); assert.equal(h.adapter.name, '3D Modeler');
  assert.equal(h.state(), 'unconfigured'); assert.match(h.text(), /dashboards.modeler3d.url/);
  assert.equal(h.frame(), null); assert.equal(h.timers.size, 0);
  assert.equal(h.root.querySelector('[data-modeler-open]').textContent, 'Open in new window');
  assert.equal(h.root.querySelector('[data-modeler-open]').hasAttribute('href'), false);
  assert.ok(h.root.querySelector('[data-modeler-reload]'));
  assert.equal(h.adapter.pollFn, undefined); assert.equal(h.adapter.pollInterval, undefined);
});
test('configured frame is sandboxed, starts loading and completes on navigation load', t => {
  const h = setup(configured); t.after(h.close);
  assert.equal(h.state(), 'loading'); assert.equal(h.frame().src, fixtureUrl);
  assert.equal(h.frame().getAttribute('sandbox'), 'allow-scripts allow-same-origin');
  assert.equal(h.frame().getAttribute('referrerpolicy'), 'no-referrer');
  assert.equal(h.frame().getAttribute('allow'), 'xr-spatial-tracking; fullscreen');
  assert.equal(h.frame().getAttribute('loading'), 'lazy');
  assert.equal(h.frame().title, '3D Modeler viewer');
  assert.equal(h.timers.size, 1); assert.equal([...h.timers.values()][0].ms, 15000);
  h.frame().dispatchEvent(new h.window.Event('load'));
  assert.equal(h.state(), 'loaded'); assert.equal(h.timers.size, 0);
  assert.match(h.text(), /Loaded/);
  assert.equal(h.root.querySelector('[data-modeler-open]').href, fixtureUrl);
});
test('fixture navigation contract is synthetic and self-contained', () => {
  const fixture = fs.readFileSync(path.join(__dirname, 'fixtures/modeler_viewer.html'), 'utf8');
  assert.match(fixture, /Synthetic 3D model/);
  assert.doesNotMatch(fixture, /https?:\/\//);
});
test('a frame that never loads becomes blocked with reason and fallback link', t => {
  const h = setup(configured); t.after(h.close); h.timeout();
  assert.equal(h.state(), 'blocked'); assert.match(h.text(), /timed out/i);
  assert.match(h.text(), /Open in new window/); assert.equal(h.timers.size, 0);
  h.frame().dispatchEvent(new h.window.Event('load'));
  assert.equal(h.state(), 'blocked', 'a terminal timeout needs an explicit reload');
});
test('frame error becomes blocked and a later load cannot overwrite it', t => {
  const h = setup(configured); t.after(h.close); const frame = h.frame();
  frame.dispatchEvent(new h.window.Event('error'));
  assert.equal(h.state(), 'blocked'); assert.match(h.text(), /could not be loaded/i);
  assert.equal(h.timers.size, 0); frame.dispatchEvent(new h.window.Event('load'));
  assert.equal(h.state(), 'blocked');
});
test('reload replaces the frame and ignores every previous-generation event and timer', t => {
  const h = setup(configured); t.after(h.close);
  const oldFrame = h.frame(); const oldTimeout = [...h.timers.values()][0].fn;
  h.reload(); const nextFrame = h.frame();
  assert.notEqual(nextFrame, oldFrame); assert.equal(oldFrame.isConnected, false);
  oldFrame.dispatchEvent(new h.window.Event('load')); oldTimeout();
  assert.equal(h.state(), 'loading'); assert.equal(h.timers.size, 1);
  nextFrame.dispatchEvent(new h.window.Event('load'));
  assert.equal(h.state(), 'loaded'); h.reload(); assert.equal(h.state(), 'loading');
});
test('unmount removes frames, handlers and timers even while loading and is idempotent', t => {
  const h = setup(configured); t.after(() => h.window.close());
  const frame = h.frame(); const timeout = [...h.timers.values()][0].fn;
  h.adapter.unmount(h.refs); h.adapter.unmount(h.refs);
  assert.equal(h.root.children.length, 0); assert.equal(h.timers.size, 0);
  frame.dispatchEvent(new h.window.Event('load')); frame.dispatchEvent(new h.window.Event('error')); timeout();
  assert.equal(h.root.children.length, 0);
});
for (const url of ['javascript:alert(1)', 'data:text/html,unsafe', 'file:///tmp/model.html', '/viewer', 'http://user:password@127.0.0.1/viewer', 'broken-url']) {
  test(`reject unsupported or credential-bearing viewer URL: ${url.split(':')[0]}`, t => {
    const h = setup({ dashboards: { modeler3d: { url } } }); t.after(h.close);
    assert.equal(h.state(), 'unconfigured'); assert.equal(h.frame(), null); assert.equal(h.timers.size, 0);
    assert.equal(h.root.querySelector('[data-modeler-open]').hasAttribute('href'), false);
    assert.doesNotMatch(h.text(), /password|javascript:|file:\/\//);
  });
}
test('external open uses the existing bridge once for a user click', async t => {
  const calls = [];
  const h = setup(configured, { openExternal: url => { calls.push(url); return Promise.resolve({ ok: true }); } }); t.after(h.close);
  const link = h.root.querySelector('[data-modeler-open]');
  const event = new h.window.MouseEvent('click', { bubbles: true, cancelable: true }); link.dispatchEvent(event);
  await Promise.resolve();
  assert.equal(event.defaultPrevented, true); assert.deepEqual(calls, [fixtureUrl]);
  assert.equal(link.target, '_blank'); assert.equal(link.rel, 'noopener noreferrer');
});
test('external open failure is visible without leaking the URL or error payload', async t => {
  const h = setup(configured, { openExternal: async () => ({ ok: false, error: 'sensitive upstream details' }) }); t.after(h.close);
  h.root.querySelector('[data-modeler-open]').click(); await new Promise(resolve => setImmediate(resolve));
  assert.match(h.text(), /Could not open/i); assert.doesNotMatch(h.text(), /sensitive upstream|127.0.0.1/);
  assert.equal(h.state(), 'loading');
});
test('reload reads current config and can recover from unconfigured or blocked states', t => {
  const config = {}; const h = setup(config); t.after(h.close);
  config.dashboards = configured.dashboards; h.reload(); assert.equal(h.state(), 'loading');
  h.timeout(); assert.equal(h.state(), 'blocked'); h.reload(); assert.equal(h.state(), 'loading');
  h.frame().dispatchEvent(new h.window.Event('load')); assert.equal(h.state(), 'loaded');
});

test('actual dashboard view passes renderer config and never overwrites modeler state or polls', t => {
  const dom = new JSDOM(`<!doctype html><button class="view-btn" data-view="chats"></button>
    <button class="view-btn" data-view="dashboards"></button><div class="grid"><input value="unsent draft"></div>
    <div id="panel-sessions"></div><div id="panel-dashboards"><div id="dashboard-list"></div></div>
    <div id="dashboard-content"></div>`);
  const { window } = dom; t.after(() => window.close());
  const timers = new Map(); let timerId = 0;
  const state = { currentView: 'chats', selectedDashboard: null, dashboardPollToken: 0 };
  const context = vm.createContext({ window, document: window.document, URL, CONFIG: configured, state,
    gridColResizers: [], sidebarResizer: null, scheduleVisibleSlotFits() {}, console,
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; }, clearTimeout(id) { timers.delete(id); },
    setInterval() { throw new Error('Modeler must never poll'); }, clearInterval() {} });
  context.require = name => {
    assert.equal(name, './dashboards/modeler-3d');
    vm.runInContext(fs.readFileSync(sourceFile, 'utf8'), context);
  };
  const renderer = path.join(__dirname, '../renderer');
  vm.runInContext(fs.readFileSync(path.join(renderer, 'dashboards/registry.js'), 'utf8'), context);
  const app = fs.readFileSync(path.join(renderer, 'app.js'), 'utf8');
  vm.runInContext(app.slice(app.indexOf('// ── View Switcher (Chats / Dashboards)'), app.indexOf('// ── Toolbar Buttons')), context);
  const run = code => vm.runInContext(code, context);
  run("switchView('dashboards')");
  const frame = window.document.querySelector('iframe');
  assert.equal(frame.src, fixtureUrl);
  assert.equal(window.document.querySelector('[data-modeler-state]').dataset.modelerState, 'loading');
  assert.equal(timers.size, 1);
  frame.dispatchEvent(new window.Event('load'));
  assert.equal(window.document.querySelector('[data-modeler-state]').dataset.modelerState, 'loaded');
  run("switchView('chats')");
  assert.equal(window.document.querySelector('iframe'), null); assert.equal(timers.size, 0);
  assert.equal(window.document.querySelector('.grid input').value, 'unsent draft');
  run("switchView('dashboards'); switchView('chats')");
  assert.equal(window.document.querySelector('iframe'), null); assert.equal(timers.size, 0);
});

test('local config loader and public renderer bridge preserve synthetic dashboard settings', t => {
  const os = require('node:os');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'modeler-config-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const profile = path.join(dir, 'synthetic-config.js');
  fs.writeFileSync(profile, `module.exports = ${JSON.stringify(configured)};`);
  const { loadConfig } = require('../config-loader');
  const { createCcHandlers } = require('../main/cc_handlers');
  const loaded = loadConfig(dir, { PENTACLE_CONFIG: profile });
  assert.deepEqual(loaded.warnings, []);
  const config = createCcHandlers({ CONFIG: loaded.config, chatStreamClient: {} }).publicConfig();
  const h = setup(config); t.after(h.close);
  assert.equal(h.frame().src, fixtureUrl); assert.equal(h.state(), 'loading');
});

test('external-open rejection after unmount or reload cannot change the current state', async t => {
  let reject;
  const h = setup(configured, { openExternal: () => new Promise((_, fail) => { reject = fail; }) }); t.after(h.close);
  h.root.querySelector('[data-modeler-open]').click(); h.reload();
  reject(new Error('old error')); await new Promise(resolve => setImmediate(resolve));
  assert.doesNotMatch(h.text(), /Could not open|old error/);
  h.root.querySelector('[data-modeler-open]').click(); h.adapter.unmount(h.refs);
  reject(new Error('late error')); await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.root.children.length, 0);
});
