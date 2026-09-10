'use strict';
// Renderer smoke test for the shared dashboard layer. Loads the three scripts
// in renderer/index.html order (submodule UMD -> registry.js -> demo.js) inside a
// jsdom window and asserts: the global lands, the demo board self-registers into
// window.DASHBOARDS, and INTERACTIVE mode renders the manifest interactive-only
// control. This is the load-order invariant the foundation spec calls out
// (app.js must not enumerate window.DASHBOARDS synchronously at top level; here
// we read it after all scripts have run, as switchView()/CFG_READY.then() do).

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const R = path.resolve(__dirname, '..', 'renderer', 'dashboards');
const read = (f) => fs.readFileSync(path.join(R, f), 'utf8');

function loadRenderer() {
  const dom = new JSDOM('<!doctype html><body></body>', { runScripts: 'dangerously' });
  const { window } = dom;
  const run = (code) => {
    const s = window.document.createElement('script');
    s.textContent = code;
    window.document.body.appendChild(s);
  };
  // index.html order: submodule shared lib BEFORE registry + boards.
  run(read('registry.js'));
  run(read('demo.js'));
  return window;
}

test('standalone demo does not need an external dashboard package', () => {
  const window = loadRenderer();
  assert.equal(window.PublicDashboardLibrary, undefined);
  assert.equal(window.DASHBOARDS.length, 1);
});

test('demo board self-registers into window.DASHBOARDS', () => {
  const window = loadRenderer();
  const entry = (window.DASHBOARDS || []).find((d) => d.id === 'shared-demo');
  assert.ok(entry, 'shared-demo registered');
  assert.equal(entry.name, 'Shared Demo');
  assert.equal(typeof entry.mount, 'function');
});

test('desktop INTERACTIVE mount renders the interactive-only control', () => {
  const window = loadRenderer();
  const entry = window.DASHBOARDS.find((d) => d.id === 'shared-demo');
  const container = window.document.createElement('div');
  const refs = entry.mount(container);
  assert.match(container.textContent, /desktop fixture ready/);
  entry.update(refs, { demo: { message: '<img src=x onerror=alert(1)>' } });
  assert.equal(container.querySelector('img'), null);
  assert.match(container.textContent, /<img/);
  entry.update(refs, { demo: { message: 'changed' } });
  assert.match(container.innerHTML, /changed/);
  entry.unmount(refs);
  assert.equal(container.innerHTML, '');
});
