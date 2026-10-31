'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '..');
const rendererRequire = createRequire(path.join(root, 'renderer/app.js'));
const storageKey = 'pentacle.settings.v1';

async function install(t, { record, web = true, storageThrows = false, popout = false } = {}) {
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'renderer/index.html'), 'utf8'), {
    url: `https://example.local/${popout ? '?pentacle-chat-popout=%7B%7D' : ''}`,
  });
  t.after(() => dom.window.close());
  const { window } = dom;
  if (record) window.localStorage.setItem(storageKey, JSON.stringify(record));
  if (storageThrows) {
    window.Storage.prototype.getItem = () => { throw new Error('unavailable'); };
    window.Storage.prototype.setItem = () => { throw new Error('full'); };
  }
  const config = { terminal: {}, chatStream: {}, hostNames: {}, hostColors: {}, features: {} };
  if (web) window.__PENTACLE_CONFIG__ = config;
  const events = [];
  window.PentacleHarness = { emit: (name, payload) => events.push({ name, ...payload }) };
  window.cc = {
    perfRecord() {}, getConfig: async () => config,
    getChatStreamState: async () => ({ connected: true, events: [], sessions: [], schedules: [] }),
    listChatSessions: async () => ({ ok: true, active: [], trashed: [] }),
    getLimits: async () => [], getMachineStats: async () => ({}),
    onPtyData() {}, onPtyExit() {}, onAssignSlot() {}, onAction() {}, onChatStreamFrame() {},
  };
  window.HOST = { hostname: 'example.local', platform: 'darwin', isClient: false };
  window.DASHBOARDS = [];
  const context = vm.createContext({
    Buffer, console, window, document: window.document, localStorage: window.localStorage,
    location: window.location, URLSearchParams, Node: window.Node, global: window,
    getComputedStyle: e => window.getComputedStyle(e),
    process: { ...process, env: { ...process.env, NODE_ENV: 'test' } },
    requestAnimationFrame: fn => { fn(); return 0; },
    setInterval: () => 0, clearInterval() {}, setTimeout: fn => { fn(); return 0; }, clearTimeout() {},
    __dirname: path.join(root, 'renderer'), __filename: path.join(root, 'renderer/app.js'),
    require(name) {
      if (name === '@xterm/xterm') return { Terminal: class { loadAddon() {} open() {} write() {} dispose() {} } };
      if (name === '@xterm/addon-fit') return { FitAddon: class { fit() {} } };
      if (name === '@xterm/addon-unicode11') return { Unicode11Addon: class {} };
      if (name === '@xterm/addon-webgl') return { WebglAddon: class {} };
      if (name === 'electron') return { clipboard: { readText: () => '', writeText() {} } };
      if (name === '../config-loader') return { loadConfig: () => ({ config }) };
      return rendererRequire(name);
    },
  });
  vm.runInContext(fs.readFileSync(path.join(root, 'renderer/app.js'), 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  return { window, context, events, row: window.document.querySelector('[data-setting="chatWidth"]'),
    cap: () => window.document.documentElement.style.getPropertyValue('--chat-content-max-width'),
    appearance: () => JSON.parse(vm.runInContext('JSON.stringify(state.appearance)', context)),
  };
}

test('legacy and invalid widths default without rewriting stored records', async t => {
  const app = await install(t, { record: { appearance: { theme: 'dark' } } });
  for (const value of [undefined, null, true, 600, {}, [], 'WIDE', 'unknown', 'toString']) {
    const raw = value === undefined ? '{}' : JSON.stringify({ chatWidth: value });
    assert.equal(vm.runInContext(`normalizeAppearance(${raw}).chatWidth`, app.context), 'default');
  }
  assert.equal(app.appearance().chatWidth, 'default');
  assert.equal(app.cap(), '760px');
  assert.deepEqual(JSON.parse(app.window.localStorage.getItem(storageKey)), { appearance: { theme: 'dark' } });
});

test('three live segments share persistence, selection and existing appearance evidence', async t => {
  const record = { features: { showTurnDuration: true }, other: 'retained',
    appearance: { theme: 'dark', density: 'compact', custom: 'retained', sidebarWidth: 310 } };
  const app = await install(t, { record });
  assert.ok(app.row, 'web has Chat width');
  assert.equal(app.row.querySelector('.settings-row-label').textContent, 'Chat width');
  assert.equal(app.row.querySelector('.settings-row-desc'), null);
  assert.deepEqual([...app.row.querySelectorAll('button')].map(b => b.textContent), ['Narrow', 'Default', 'Wide']);
  for (const [value, px] of [['narrow', 600], ['default', 760], ['wide', 1000]]) {
    app.row.querySelector(`[data-value="${value}"]`).click();
    assert.equal(app.appearance().chatWidth, value);
    assert.equal(app.cap(), `${px}px`);
    assert.equal(app.row.querySelector('[aria-pressed="true"]').dataset.value, value);
    assert.deepEqual(JSON.parse(app.window.localStorage.getItem(storageKey)), {
      ...record, appearance: { ...record.appearance, chatWidth: value },
    });
    app.window.document.getElementById('settings-close').click();
    app.window.document.getElementById('settings-btn').click();
    assert.equal(app.row.querySelector('[aria-pressed="true"]').dataset.value, value);
  }
  const last = app.events.filter(e => e.name === 'settings:toggle').at(-1);
  assert.equal(last.data.subsystem, 'settings');
  assert.equal(last.data.bug_ref, 'web_chat_max_width_setting');
  const restored = await install(t, { record: JSON.parse(app.window.localStorage.getItem(storageKey)) });
  assert.equal(restored.cap(), '1000px');
  assert.equal(restored.row.querySelector('[aria-pressed="true"]').dataset.value, 'wide');
});

test('web popout startup applies saved width; Electron keeps its fallback and no row', async t => {
  const record = { appearance: { chatWidth: 'wide' } };
  const popout = await install(t, { record, popout: true });
  assert.equal(popout.cap(), '1000px');
  const desktop = await install(t, { record, web: false });
  assert.equal(desktop.row, null);
  assert.equal(desktop.cap(), '');
});

test('storage exceptions keep live width changes usable', async t => {
  const app = await install(t, { storageThrows: true });
  assert.equal(app.cap(), '760px');
  assert.ok(app.row);
  assert.doesNotThrow(() => app.row.querySelector('[data-value="wide"]').click());
  assert.equal(app.cap(), '1000px');
});
