'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const catalog = require('../renderer/dashboards/catalog_loader');
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup(config = {}, descriptors) {
 const dom = new JSDOM('<button class="view-btn" data-view="chats"></button><button class="view-btn" data-view="dashboards"></button><div class="grid"><input value="draft intact"></div><div id="panel-sessions"></div><div id="panel-dashboards"><div id="dashboard-list"></div></div><div id="dashboard-content"></div>', { url: 'https://pentacle.example.ts.net' });
 const window = dom.window, state = { currentView: 'chats', selectedDashboard: null, dashboardPollToken: 0 }, calls = [];
 let boards = descriptors || [ { id: 'retired', name: 'Retired', kind: 'built-in' }, { id: 'active-a', name: 'Active A', kind: 'built-in' }, { id: 'active-b', name: 'Active B', kind: 'built-in', visible: false } ];
 const loader = catalog.createLoader({ root: window, storage: null, cc: {} });
 const context = vm.createContext({ window, document: window.document, CONFIG: config, CFG_READY: Promise.resolve(), state,
  dashboardCatalog: { createLoader: () => ({ ...loader, refresh: async () => ({ status: 'ready', catalog: { catalog_version: 'test', boards } }) }) }, dashboardReports: {}, assetRender: {},
  gridColResizers: [], sidebarResizer: null, scheduleVisibleSlotFits() {}, require() { return {}; },
  setInterval() { throw Error('fixtures must never poll'); }, clearInterval() {}, console });
 vm.runInContext(fs.readFileSync(require.resolve('../renderer/dashboards/registry'), 'utf8'), context);
 window.DASHBOARDS.push(...['retired','active-a','active-b','outside'].map(id => ({ id, name: id, retired: id === 'retired',
  mount(container) { calls.push(['mount', id]); container.textContent = id; container.dataset.boardState = 'ready'; return { container }; },
  unmount(refs) { calls.push(['unmount', id]); refs.container.textContent = ''; } })));
 const app = fs.readFileSync(require.resolve('../renderer/app'), 'utf8');
 vm.runInContext(app.slice(app.indexOf('// ── View Switcher (Chats / Dashboards)'), app.indexOf('// ── Toolbar Buttons')), context);
 return { window, state, calls, close: () => { vm.runInContext("if(state.currentView==='dashboards') switchView('chats')", context); window.close(); },
  run: code => vm.runInContext(code, context), setBoards: value => { boards = value; }, ids: () => Array.from(window.document.querySelectorAll('[data-dashboard-id]'), e => e.dataset.dashboardId) };
}
test('catalog controls mixed membership/order/visibility and profile overrides have no effect', async t => {
 const h = setup({ dashboards: { hidden: ['retired'], showRetired: false } }); t.after(h.close);
 h.run("switchView('dashboards')"); await tick();
 assert.deepEqual(h.ids(), ['retired', 'active-a']); assert.equal(h.state.selectedDashboard, 'retired');
 assert.equal(h.window.document.querySelector('[data-dashboard-id=retired]').tagName, 'BUTTON');
 assert.deepEqual(Array.from(h.window.document.querySelectorAll('.dashboard-group-title'), e => e.textContent), ['Dashboards']);
 assert.deepEqual(h.calls, [['mount','retired']]);
});
test('catalog hiding a selected board picks the next and cannot remount hidden code', async t => {
 const h = setup(); t.after(h.close); h.run("switchView('dashboards')"); await tick(); h.run("switchView('chats')");
 h.setBoards([{ id: 'retired', name: 'Retired', kind: 'built-in', visible: false }, { id: 'active-a', name: 'A', kind: 'built-in' }]);
 h.run("switchView('dashboards')"); await tick(); h.run("selectDashboard('retired')");
 assert.equal(h.state.selectedDashboard, 'active-a'); assert.deepEqual(h.ids(), ['active-a']);
 assert.deepEqual(h.calls, [['mount','retired'], ['unmount','retired'], ['mount','active-a']]);
});
test('empty catalog yields empty states without a registration fallback', async t => {
 const h = setup({}, []); t.after(h.close); h.run("switchView('dashboards')"); await tick();
 assert.deepEqual(h.ids(), []); assert.equal(h.state.selectedDashboard, null);
 assert.match(h.window.document.getElementById('dashboard-content').textContent, /No dashboards configured/);
 assert.deepEqual(h.calls, []);
});
test('missing implementation stays unavailable in its catalog position', async t => {
 const h = setup({}, [{ id: 'missing', name: 'Missing', kind: 'built-in' }, { id: 'active-a', name: 'A', kind: 'built-in' }]); t.after(h.close);
 h.run("switchView('dashboards')"); await tick(); assert.deepEqual(h.ids(), ['missing','active-a']);
 assert.match(h.window.document.getElementById('dashboard-content').textContent, /unavailable/);
 assert.equal(h.state.dashboardState, 'error');
});
test('selection, common reload and repeated view switches preserve draft and implementation lookup', async t => {
 const h = setup(); t.after(h.close); const input = h.window.document.querySelector('input');
 h.run("switchView('dashboards')"); await tick(); h.run("selectDashboard('active-a'); selectDashboard('active-a')");
 h.window.document.querySelector('.dashboard-panel-header button').click();
 h.run("switchView('chats'); switchView('dashboards')"); await tick();
 assert.deepEqual(h.ids(), ['retired','active-a']); assert.equal(h.state.selectedDashboard, 'active-a');
 assert.equal(h.window.document.querySelector('input'), input); assert.equal(input.value, 'draft intact');
 assert.equal(h.calls.filter(([action,id]) => action === 'mount' && id === 'active-a').length, 3);
});
test('names and descriptions render as text and built-in retirement code stays registered', async t => {
 const h = setup({}, [{ id: 'active-a', name: '<img src=x>', description: '<script>bad()</script>', kind: 'built-in' }]); t.after(h.close);
 h.run("switchView('dashboards')"); await tick(); assert.equal(h.window.document.querySelector('#dashboard-list img, #dashboard-list script'), null);
 for (const file of ['foreclosure','scraper-bot']) assert.match(fs.readFileSync(require.resolve('../renderer/dashboards/'+file), 'utf8'), /retired:\s*true/);
});
