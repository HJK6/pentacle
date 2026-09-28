'use strict';
// spec_pentacle__web_update_available_refresh_icon_2026_09 — renderer decision.
// The window compares its OWN injected build id with the id the host serves; the
// icon shows only when both are known and they differ (an up-to-date or freshly
// opened window, where own === served, never shows it). Full show/hide/click and
// the live one-stale/one-fresh proof are covered by the headless web gate + live
// Runtime proof (see spec Validation).

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

function recoveryFixture() {
  let servedId = 'new-build';
  let recoveries = 0;
  let fail = false;
  let release;
  const context = {
    document: { getElementById: () => ({ hidden: true }) },
    window: { __PENTACLE_CONFIG__: { buildId: 'old-build' }, cc: { getBuild: async () => ({ buildId: servedId }) } },
    console: { info() {} },
    resyncWebChatState: async () => { recoveries++; if (fail) throw new Error('fixture offline'); if (release) await release; },
  };
  vm.runInNewContext('let webRehydratedBuildId = null; let webRehydrationPending = null;\n' +
    slice('function webUpdateAvailable(') + '\n' + slice('function setWebRefreshVisible(') + '\n' + slice('async function checkForWebUpdate('), context);
  return { context, recoveries: () => recoveries, setServed: id => { servedId = id; }, setFail: value => { fail = value; }, setPending: promise => { release = promise; } };
}

test('changed served build automatically rehydrates once, and a later build rehydrates again', async () => {
  const f = recoveryFixture();
  await f.context.checkForWebUpdate(); await f.context.checkForWebUpdate();
  assert.equal(f.recoveries(), 1, 'one recovery per observed build');
  f.setServed('next-build'); await f.context.checkForWebUpdate();
  assert.equal(f.recoveries(), 2);
});

test('failed changed-build rehydration retries, and concurrent checks do not duplicate it', async () => {
  const f = recoveryFixture(); f.setFail(true);
  await f.context.checkForWebUpdate(); f.setFail(false);
  let resolve; f.setPending(new Promise(r => { resolve = r; }));
  const first = f.context.checkForWebUpdate(); await new Promise(r => setImmediate(r));
  await f.context.checkForWebUpdate(); resolve(); await first;
  assert.equal(f.recoveries(), 2, 'failed attempt then one coalesced retry');
});
