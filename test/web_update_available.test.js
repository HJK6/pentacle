'use strict';
// spec_pentacle__web_update_available_refresh_icon_2026_09 — renderer decision.
// The window compares its OWN injected build id with the id the host serves; the
// icon shows only when both are known and they differ (an up-to-date or freshly
// opened window, where own === served, never shows it). Full show/hide/click and
// the live one-stale/one-fresh proof are covered by the headless web gate + live
// Thoth proof (see spec Validation).

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');

function slice(signature) {
  const source = fs.readFileSync(require.resolve('../renderer/app.js'), 'utf8');
  const start = source.indexOf(signature);
  assert.notEqual(start, -1, `expected to find: ${signature}`);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}

function load(documentStub) {
  const code = slice('function webUpdateAvailable(') + '\n' + slice('function setWebRefreshVisible(');
  const context = { document: documentStub };
  vm.runInNewContext(code, context);
  return context;
}

test('webUpdateAvailable is true only when both ids are known and differ', () => {
  const { webUpdateAvailable } = load(null);
  assert.equal(webUpdateAvailable('a1b2', 'a1b2'), false, 'same id => up to date');
  assert.equal(webUpdateAvailable('a1b2', 'c3d4'), true, 'different id => update available');
  assert.equal(webUpdateAvailable(null, 'c3d4'), false, 'unknown own id => no nag');
  assert.equal(webUpdateAvailable('a1b2', null), false, 'unknown served id => no nag');
  assert.equal(webUpdateAvailable('', ''), false, 'freshly opened / empty => no nag');
});

test('setWebRefreshVisible toggles the titlebar refresh control', () => {
  const document = new JSDOM('<button id="web-refresh-btn" hidden></button>').window.document;
  const { setWebRefreshVisible } = load(document);
  const btn = document.getElementById('web-refresh-btn');
  assert.equal(btn.hidden, true, 'hidden by default');
  setWebRefreshVisible(true);
  assert.equal(btn.hidden, false, 'shown when an update is available');
  setWebRefreshVisible(false);
  assert.equal(btn.hidden, true, 'hidden again when up to date');
});
