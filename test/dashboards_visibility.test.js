'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');

const renderer = path.join(__dirname, '../renderer');
const registry = fs.readFileSync(path.join(renderer, 'dashboards/registry.js'), 'utf8');
const app = fs.readFileSync(path.join(renderer, 'app.js'), 'utf8');
const viewCode = app.slice(app.indexOf('// ── View Switcher (Chats / Dashboards)'), app.indexOf('// ── Toolbar Buttons'));

function setup(config = {}, boards) {
  const dom = new JSDOM(`<!doctype html><button class="view-btn" data-view="chats"></button>
    <button class="view-btn" data-view="dashboards"></button><div class="grid"><input value="draft intact"></div>
    <div id="panel-sessions"></div><div id="panel-dashboards"><div id="dashboard-list"></div></div>
    <div id="dashboard-content"></div>`);
  const { window } = dom;
  const state = { currentView: 'chats', selectedDashboard: null, dashboardPollToken: 0 };
  const calls = [];
  const context = vm.createContext({ window, document: window.document, CONFIG: config, state,
    gridColResizers: [], sidebarResizer: null, scheduleVisibleSlotFits() {},
    require: name => { assert.equal(name, './dashboards/modeler-3d'); },
    setInterval: () => { throw Error('non-polling fixtures must not poll'); }, clearInterval() {}, console });
  vm.runInContext(registry, context);
  window.DASHBOARDS.push(...(boards || [
    { id: 'retired', name: 'Retired', retired: true },
    { id: 'active-a', name: 'Active A', description: 'Synthetic description', color: 'var(--cosmic-green)' },
    { id: 'active-b', name: 'Active B' },
  ]).map(board => ({ ...board, mount(container) { calls.push(['mount', board.id]); container.textContent = board.name; return { container }; },
    unmount(refs) { calls.push(['unmount', board.id]); refs.container.textContent = ''; } })));
  vm.runInContext(viewCode, context);
  return { window, state, calls, context, dom, close: () => window.close(),
    run: code => vm.runInContext(code, context),
    ids: () => Array.from(window.document.querySelectorAll('[data-dashboard-id]'), el => el.dataset.dashboardId) };
}
function ids(context, config) {
  return Array.from(context.window.visibleDashboards(config), d => d.id);
}

test('retired dashboards register but stay hidden by default', t => {
  const h = setup(); t.after(h.close);
  assert.equal(h.window.DASHBOARDS.length, 3);
  assert.deepEqual(ids(h, {}), ['active-a', 'active-b']);
});
test('showRetired opt-in groups active dashboards first without mutating registration', t => {
  const h = setup(); t.after(h.close);
  assert.deepEqual(ids(h, { dashboards: { showRetired: true } }), ['active-a', 'active-b', 'retired']);
  assert.equal(h.window.DASHBOARDS[0].id, 'retired');
  assert.deepEqual(ids(h, { dashboards: { showRetired: 'true' } }), ['active-a', 'active-b']);
});
test('explicit hidden IDs take precedence over showRetired and ignore unknown IDs', t => {
  const h = setup(); t.after(h.close);
  assert.deepEqual(ids(h, { dashboards: { hidden: ['active-a', 'retired', 'missing'], showRetired: true } }), ['active-b']);
  assert.deepEqual(ids(h, { dashboards: { hidden: 'active-a' } }), ['active-a', 'active-b']);
});
test('first visible active dashboard is default even when a retired dashboard registered first', t => {
  const h = setup({ dashboards: { showRetired: true } }); t.after(h.close);
  h.run("switchView('dashboards')");
  assert.equal(h.state.selectedDashboard, 'active-a');
  assert.deepEqual(h.calls, [['mount', 'active-a']]);
  assert.equal(h.window.document.querySelector('[data-dashboard-id="active-a"]').getAttribute('aria-current'), 'true');
});
test('list uses ACTIVE and opt-in RETIRED groups, descriptions and keyboard-native buttons', t => {
  const h = setup({ dashboards: { showRetired: true } }); t.after(h.close);
  h.run("switchView('dashboards')");
  assert.deepEqual(Array.from(h.window.document.querySelectorAll('.dashboard-group-title'), el => el.textContent), ['ACTIVE', 'RETIRED']);
  assert.equal(h.window.document.querySelector('[data-dashboard-id="active-a"]').tagName, 'BUTTON');
  assert.match(h.window.document.getElementById('dashboard-list').textContent, /Synthetic description/);
  h.window.document.querySelector('[data-dashboard-id="active-b"]').click();
  assert.equal(h.state.selectedDashboard, 'active-b');
});
test('no retired group appears when retired dashboards are hidden', t => {
  const h = setup(); t.after(h.close); h.run("switchView('dashboards')");
  assert.deepEqual(h.ids(), ['active-a', 'active-b']);
  assert.doesNotMatch(h.window.document.getElementById('dashboard-list').textContent, /RETIRED/);
});
test('a previously selected dashboard becoming hidden cannot remount', t => {
  const config = { dashboards: { hidden: [] } }; const h = setup(config); t.after(h.close);
  h.run("switchView('dashboards'); switchView('chats')");
  config.dashboards.hidden.push('active-a');
  h.run("switchView('dashboards')");
  assert.equal(h.state.selectedDashboard, 'active-b');
  assert.deepEqual(h.calls, [['mount', 'active-a'], ['unmount', 'active-a'], ['mount', 'active-b']]);
  h.run("selectDashboard('active-a')");
  assert.equal(h.state.selectedDashboard, 'active-b');
});
test('all-hidden registry clears stale selection and renders the empty state in list and content', t => {
  const config = { dashboards: { hidden: [] } }; const h = setup(config); t.after(h.close);
  h.run("switchView('dashboards'); switchView('chats')");
  config.dashboards.hidden.push('active-a', 'active-b');
  h.run("switchView('dashboards')");
  assert.equal(h.state.selectedDashboard, null);
  for (const id of ['dashboard-list', 'dashboard-content']) assert.match(h.window.document.getElementById(id).textContent, /No dashboards configured/);
  assert.deepEqual(h.ids(), []);
  assert.equal(h.state.dashboardRefs, null);
});
test('empty registration and only-retired registration both have no default', t => {
  for (const boards of [[], [{ id: 'retired', name: 'Retired', retired: true }]]) {
    const h = setup({}, boards); t.after(h.close); h.run("switchView('dashboards')");
    assert.equal(h.state.selectedDashboard, null);
    assert.match(h.window.document.getElementById('dashboard-content').textContent, /No dashboards configured/);
  }
});
test('repeated selection and view switching mount once per entry and leave chat draft untouched', t => {
  const h = setup(); t.after(h.close); const input = h.window.document.querySelector('.grid input');
  h.run("switchView('dashboards'); switchView('dashboards'); selectDashboard('active-a'); switchView('chats'); switchView('chats'); switchView('dashboards'); switchView('chats')");
  assert.deepEqual(h.calls, [['mount', 'active-a'], ['unmount', 'active-a'], ['mount', 'active-a'], ['unmount', 'active-a']]);
  assert.equal(h.window.document.querySelector('.grid input'), input);
  assert.equal(input.value, 'draft intact');
});
test('manifest names and descriptions are text, never interpreted as markup', t => {
  const h = setup({}, [{ id: 'safe', name: '<img src=x onerror=alert(1)>', description: '<script>bad()</script>' }]); t.after(h.close);
  h.run("switchView('dashboards')");
  assert.equal(h.window.document.querySelector('#dashboard-list img, #dashboard-list script'), null);
  assert.match(h.window.document.getElementById('dashboard-list').textContent, /<img/);
});
test('foreclosure and scraper retain their implementations and declare retired manifests', () => {
  for (const file of ['foreclosure.js', 'scraper-bot.js']) {
    assert.match(fs.readFileSync(path.join(renderer, 'dashboards', file), 'utf8'), /retired:\s*true/);
  }
});

test('showRetired selects the first visible retired board when no active board remains', t => {
  const h = setup({ dashboards: { showRetired: true, hidden: ['active-a', 'active-b'] } }); t.after(h.close);
  h.run("switchView('dashboards')");
  assert.equal(h.state.selectedDashboard, 'retired');
  assert.deepEqual(h.ids(), ['retired']);
});
