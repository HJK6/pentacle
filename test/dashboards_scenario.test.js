'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const { SCENARIOS } = require('./e2e/lib/web_scenarios');
const { readDashboardObservation, listChecks, viewerChecks, configChecks, chatChecks } = require('./e2e/lib/dashboard_scenario');
const { startModelerFixture } = require('./e2e/lib/modeler_fixture_server');
const { writeProfile } = require('./e2e/web_gate');
const fixtureUrl = 'http://127.0.0.1:9031/modeler_viewer.html';
function observation() {
  return { visible: true, ids: ['shared-demo', 'modeler-3d'], state: 'unconfigured', text: 'Set dashboards.modeler3d.url',
    frameUrl: null, sandbox: null, open: true, reload: true };
}
function loaded() { return { ...observation(), state: 'loaded', text: 'Loaded', frameUrl: fixtureUrl, sandbox: 'allow-scripts allow-same-origin', allow: 'xr-spatial-tracking; fullscreen', loading: 'lazy', referrerPolicy: 'no-referrer' }; }
function chat() { return { sameNodes: true, sameStream: true, sameTranscript: true, sameDraft: true, chatsVisible: true, dashboardsHidden: true, frames: 0 }; }

test('web gate collects the 16th dashboard scenario before destructive journeys', () => {
  assert.equal(SCENARIOS.length, 16);
  const names = SCENARIOS.map(([name]) => name);
  const index = names.indexOf('web-dashboards-revamp');
  assert.ok(index >= 0 && index < names.indexOf('closed-chat-slot'));
  assert.equal(names[names.length - 1], 'host-restart-restores-input');
});
test('dashboard gate accepts complete synthetic observations for both states and the chat round trip', () => {
  for (const [name, pass] of [...listChecks(observation(), true), ...viewerChecks(observation()), ...viewerChecks(loaded(), fixtureUrl), ...chatChecks(chat())]) assert.equal(pass, true, name);
});
for (const [label, group, index, mutate] of [
  ['hidden dashboard view', 'list', 0, o => { o.visible = false; }],
  ['retired foreclosure visible', 'list', 2, o => { o.ids.push('foreclosure-pipeline'); }],
  ['retired scraper visible', 'list', 2, o => { o.ids.push('scraper-bot'); }],
  ['missing modeler', 'list', 3, o => { o.ids = ['shared-demo']; }],
  ['wrong unconfigured state', 'unconfigured', 0, o => { o.state = 'loaded'; }],
  ['missing setup key', 'unconfigured', 0, o => { o.text = 'not configured'; }],
  ['unexpected unconfigured frame', 'unconfigured', 0, o => { o.frameUrl = fixtureUrl; }],
  ['viewer stuck loading', 'loaded', 0, o => { o.state = 'loading'; }],
  ['wrong configured URL', 'loaded', 0, o => { o.frameUrl = 'http://127.0.0.1:9031/other'; }],
  ['missing open link', 'loaded', 1, o => { o.open = false; }],
  ['missing reload', 'loaded', 1, o => { o.reload = false; }],
  ['missing XR/fullscreen permission', 'loaded', 2, o => { o.allow = null; }],
  ['missing lazy loading', 'loaded', 2, o => { o.loading = null; }],
  ['missing referrer protection', 'loaded', 2, o => { o.referrerPolicy = null; }],
  ['expanded sandbox', 'loaded', 2, o => { o.sandbox += ' allow-top-navigation'; }],
  ['recreated chat nodes', 'chat', 0, o => { o.sameNodes = false; }],
  ['changed chat identity', 'chat', 0, o => { o.sameStream = false; }],
  ['changed transcript', 'chat', 1, o => { o.sameTranscript = false; }],
  ['lost unsent draft', 'chat', 1, o => { o.sameDraft = false; }],
  ['hidden chats', 'chat', 2, o => { o.chatsVisible = false; }],
  ['visible dashboards after exit', 'chat', 2, o => { o.dashboardsHidden = false; }],
  ['leaked iframe', 'chat', 2, o => { o.frames = 1; }],
]) test(`dashboard oracle rejects ${label}`, () => {
  const value = group === 'chat' ? chat() : group === 'loaded' ? loaded() : observation();
  mutate(value);
  const checks = group === 'list' ? listChecks(value, true) : group === 'chat' ? chatChecks(value) : viewerChecks(value, group === 'loaded' ? fixtureUrl : null);
  assert.equal(checks[index][1], false, label);
});
test('retired-absent cannot pass through missing adapter registration', () => {
  assert.equal(listChecks(observation(), false)[1][1], false);
});
test('real DOM reader observes visible state, actual frame attributes and controls', t => {
  const dom = new JSDOM(`<!doctype html><section id="panel-dashboards"><div id="dashboard-list"><button data-dashboard-id="modeler-3d">3D Modeler</button></div></section>
    <main id="dashboard-content"><section data-modeler-state="loaded"><a data-modeler-open>Open in new window</a><button data-modeler-reload>Reload</button>
      <iframe src="${fixtureUrl}" sandbox="allow-scripts allow-same-origin" allow="xr-spatial-tracking; fullscreen" loading="lazy" referrerpolicy="no-referrer"></iframe></section></main>`);
  t.after(() => dom.window.close());
  const seen = readDashboardObservation(dom.window.document);
  assert.equal(seen.visible, true); assert.deepEqual(seen.ids, ['modeler-3d']);
  for (const [name, pass] of viewerChecks(seen, fixtureUrl)) assert.equal(pass, true, name);
  dom.window.document.getElementById('panel-dashboards').style.display = 'none';
  dom.window.document.querySelector('iframe').remove();
  assert.equal(readDashboardObservation(dom.window.document).visible, false);
  assert.equal(readDashboardObservation(dom.window.document).frameUrl, null);
});
test('hermetic profile enables dashboards and adds viewer URL only when explicitly configured', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dashboard-profile-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  function read(configured) {
    const profile = writeProfile(dir, 19031, fixtureUrl, configured);
    delete require.cache[profile]; return require(profile);
  }
  const off = read(false);
  assert.equal(off.features.dashboards, true); assert.equal(off.dashboards?.modeler3d?.url, undefined);
  assert.equal(off.dashboardHub.scraperBotUrl, fixtureUrl);
  assert.equal(read(true).dashboards.modeler3d.url, fixtureUrl);
  assert.equal(read(false).dashboards?.modeler3d?.url, undefined);
});
test('viewer server binds loopback and serves only the actual self-contained fixture', async t => {
  const fixture = await startModelerFixture(); t.after(() => fixture.close());
  const url = new URL(fixture.url); assert.equal(url.hostname, '127.0.0.1');
  const response = await fetch(url);
  assert.equal(response.status, 200); assert.match(response.headers.get('content-type'), /text\/html/);
  assert.equal(await response.text(), fs.readFileSync(path.join(__dirname, 'fixtures/modeler_viewer.html'), 'utf8'));
  assert.equal((await fetch(new URL('/unknown', url))).status, 404);
});

test('dashboard oracle rejects a profile URL that never reached the renderer bridge', () => {
  assert.equal(configChecks(undefined, fixtureUrl)[0][1], false);
  assert.equal(configChecks(fixtureUrl, fixtureUrl)[0][1], true);
});

const TRANSIENT_RELOAD_ERRORS = ['Execution context was destroyed. (-32000)', 'Cannot find context with specified id (-32000)', 'Inspected target navigated or closed (-32000)'];
for (const message of TRANSIENT_RELOAD_ERRORS) {
  test(`dashboard reload retries a transient context rollover: ${message}`, async () => {
    const { reloadDashboardPage } = require('./e2e/lib/dashboard_scenario');
    let evaluations = 0; let reloads = 0;
    const session = { send: async () => { reloads++; }, eval: async expression => {
      if (expression === 'window.__dashboardReloadMarker = true') return true;
      if (++evaluations === 1) throw new Error(message);
      return true;
    } };
    await reloadDashboardPage({ session, cdp: { sleep: async () => {} }, timeoutMs: 1000 });
    assert.equal(reloads, 1); assert.equal(evaluations, 2);
  });
}
for (const message of TRANSIENT_RELOAD_ERRORS) {
  test(`dashboard reload re-arms after a previous navigation is still settling: ${message}`, async () => {
    const { reloadDashboardPage } = require('./e2e/lib/dashboard_scenario');
    let arms = 0; let reloads = 0;
    const session = { send: async () => { reloads++; }, eval: async expression => {
      if (expression === 'window.__dashboardReloadMarker = true' && ++arms === 1) throw new Error(message);
      return true;
    } };
    await reloadDashboardPage({ session, cdp: { sleep: async () => {} }, timeoutMs: 1000 });
    assert.equal(arms, 2); assert.equal(reloads, 1);
  });
}
test('dashboard reload waits for the old document marker to disappear', async () => {
  const { reloadDashboardPage } = require('./e2e/lib/dashboard_scenario');
  let evaluations = 0;
  const session = { send: async () => {}, eval: async expression => {
    if (expression === 'window.__dashboardReloadMarker = true') return true;
    assert.match(expression, /__dashboardReloadMarker !== true/);
    return ++evaluations > 1;
  } };
  await reloadDashboardPage({ session, cdp: { sleep: async () => {} }, timeoutMs: 1000 });
  assert.equal(evaluations, 2);
});
test('dashboard reload fails within its bound when the fresh document never becomes ready', async () => {
  const { reloadDashboardPage } = require('./e2e/lib/dashboard_scenario');
  const session = { send: async () => {}, eval: async expression => expression === 'window.__dashboardReloadMarker = true' };
  await assert.rejects(() => reloadDashboardPage({ session, cdp: { sleep: async () => {} }, timeoutMs: 0 }), /fresh dashboard document/i);
});
test('dashboard reload fails promptly on unrelated CDP errors', async () => {
  const { reloadDashboardPage } = require('./e2e/lib/dashboard_scenario');
  let evaluations = 0;
  const session = { send: async () => {}, eval: async expression => {
    if (expression === 'window.__dashboardReloadMarker = true') return true;
    evaluations++; throw new Error('Target closed');
  } };
  await assert.rejects(() => reloadDashboardPage({ session, cdp: { sleep: async () => { throw Error('must not retry'); } }, timeoutMs: 1000 }), /Target closed/);
  assert.equal(evaluations, 1);
});
