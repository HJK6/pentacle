'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const householdSelectors = require('../renderer/household/selectors');
const { createHouseholdStore } = require('../renderer/household/store');
const { FakeHousehold, fakeClock, flush, rpcError } = require('./fixtures/household_support');

const app = fs.readFileSync(path.join(__dirname, '../renderer/app.js'), 'utf8');
const pollSource = app.slice(app.indexOf('function startDashboardPolling()'), app.indexOf('function updateDashboardStatusBadge()'));
const mountSource = app.slice(app.indexOf('function selectDashboard(id)'), app.indexOf('function renderDashboardList()'));
// Exercise the actual shell poller with a synthetic adapter, never a private board.
function setup(source = pollSource, shellSource = mountSource) {
  const clock = fakeClock(), server = new FakeHousehold(), store = createHouseholdStore(clock);
  store.configure({ household: server.handle });
  const intervals = new Map(); let timerId = 0, updates = 0;
  function setInterval(fn, ms) {
    const id = ++timerId;
    const tick = () => { if (!intervals.has(id)) return; fn(); if (intervals.has(id)) intervals.set(id, clock.setTimeout(tick, ms)); };
    intervals.set(id, clock.setTimeout(tick, ms)); return id;
  }
  function clearInterval(id) { clock.clearTimeout(intervals.get(id)); intervals.delete(id); }
  const mounts = [], unmounts = [], events = [];
  const config = { dashboards: { catalogSpecId: 'spec_example' } };
  const container = { innerHTML: 'Previous content' };
  const adapter = { id: 'example-board', name: 'Example Board', actions: ['household'], pollInterval: 30000,
    mount(node, ctx) { const refs = { store: ctx.household.store }; mounts.push({ node, ctx, refs }); return refs; },
    unmount(refs) { unmounts.push(refs); },
    pollFn: refs => refs.store.refresh(), update() { updates++; } };
  const adapters = [adapter];
  const state = { selectedDashboard: adapter.id, dashboardRefs: { store }, dashboardPollToken: 0,
    dashboardState: 'loading', dashboardLastData: null, dashboardError: null };
  const context = vm.createContext({ window: { DASHBOARDS: adapters,
    visibleDashboards: () => adapters,
    PentacleHarness: { emit(name, value) { events.push({ name, value }); } } },
    document: { getElementById(id) { assert.equal(id, 'dashboard-content'); return container; } },
    CONFIG: config, householdSelectors, householdStore: store, state, setInterval, clearInterval,
    updateDashboardStatusBadge() {}, renderDashboardList() {} });
  vm.runInContext(`${shellSource}\n${source}`, context);
  return { clock, server, store, state, config, container, adapters, mounts, unmounts, events,
    intervals, updates: () => updates, mount: () => context.mountAndPoll(adapter.id),
    select: id => context.selectDashboard(id), unmount: () => context.unmountCurrentDashboard(),
    start: () => context.startDashboardPolling(), stop: () => context.stopDashboardPolling() };
}
test('actual shell mount passes the config, clears content and owns the single initial poll', async t => {
  const h = setup(); t.after(h.stop);
  h.mount();
  assert.equal(h.mounts.length, 1);
  assert.equal(h.mounts[0].node, h.container);
  assert.equal(h.mounts[0].ctx.config, h.config);
  assert.equal(h.mounts[0].ctx.household.selectors, householdSelectors);
  assert.equal(h.mounts[0].ctx.household.store, h.store);
  assert.equal(h.container.innerHTML, '');
  assert.equal(h.state.dashboardRefs, h.mounts[0].refs);
  assert.equal(h.server.reads().length, 1);
  assert.equal(h.intervals.size, 1);
  await flush(); assert.equal(h.updates(), 1);
  assert.deepEqual(h.events.map(event => event.name), ['dashboard:mount', 'dashboard:loaded']);
  h.select('example-board'); h.select('missing-board');
  assert.equal(h.mounts.length, 1, 'reselect and unknown ids do not remount');
  await h.clock.advance(30000); assert.equal(h.server.reads().length, 2);
  h.stop(); h.unmount();
  assert.deepEqual(h.unmounts, [h.mounts[0].refs]);
  assert.equal(h.state.dashboardRefs, null); assert.equal(h.intervals.size, 0);
  await h.clock.advance(90000); assert.equal(h.server.reads().length, 2);
});
test('actual shell selection cleans up the old mount and rejects its late poll result', async t => {
  const h = setup(); t.after(h.stop); let settle;
  h.server.overrides.set('household.snapshot', fields => new Promise(resolve => {
    settle = () => h.server.apply('household.snapshot', fields).then(resolve);
  }));
  let nextReads = 0, nextUpdates = 0;
  const nextRefs = {}, nextData = { synthetic: 'new mount' };
  h.adapters.push({ id: 'example-next', name: 'Example Next', pollInterval: 30000,
    mount() { return nextRefs; }, pollFn: async refs => { assert.equal(refs, nextRefs); nextReads++; return nextData; },
    update(refs, data) { assert.equal(refs, nextRefs); assert.equal(data, nextData); nextUpdates++; } });
  h.mount(); h.select('example-next'); await flush();
  assert.deepEqual(h.unmounts, [h.mounts[0].refs]);
  assert.equal(h.state.selectedDashboard, 'example-next');
  assert.equal(h.state.dashboardRefs, nextRefs); assert.equal(h.intervals.size, 1);
  assert.equal(nextReads, 1); assert.equal(nextUpdates, 1);
  await settle(); await flush();
  assert.equal(h.updates(), 0, 'the old pending poll cannot update the new mount');
  assert.equal(h.state.dashboardLastData, nextData);
  assert.equal(h.state.dashboardState, 'loaded');
  await h.clock.advance(30000);
  assert.equal(h.server.reads().length, 1); assert.equal(nextReads, 2); assert.equal(nextUpdates, 2);
});
test('shell negative controls expose a missing initial poll and skipped unmount', async () => {
  const noPoll = setup(pollSource, mountSource.replace('  startDashboardPolling();', '  /* suppressed initial poll */'));
  noPoll.mount(); assert.equal(noPoll.server.reads().length, 0); noPoll.stop();
  const noUnmount = setup(pollSource, mountSource.replace('  unmountCurrentDashboard();', '  /* suppressed unmount */'));
  noUnmount.adapters.push({ id: 'example-next', name: 'Example Next', mount: () => ({}) });
  noUnmount.mount(); await flush(); noUnmount.select('example-next');
  assert.equal(noUnmount.unmounts.length, 0, 'the same cleanup assertion would reject this mutant');
  noUnmount.stop();
});
test('synthetic adapter: initial poll reads once, next at 30 seconds, none after stop', async t => {
  const h = setup(); t.after(h.stop); assert.equal(h.server.reads().length, 0);
  h.start(); assert.equal(h.server.reads().length, 1); await flush();
  assert.equal(h.state.dashboardState, 'loaded'); assert.equal(h.store.getState().status, 'ready');
  await h.clock.advance(29999); assert.equal(h.server.reads().length, 1);
  await h.clock.advance(1); assert.equal(h.server.reads().length, 2);
  h.stop(); await h.clock.advance(90000); assert.equal(h.server.reads().length, 2);
});
test('synthetic adapter: pending read skips ticks; failed attempt settles and releases inFlight', async t => {
  const h = setup(); t.after(h.stop); let settle;
  h.server.overrides.set('household.snapshot', () => new Promise(resolve => { settle = resolve; }));
  h.start(); await h.clock.advance(60000); assert.equal(h.server.reads().length, 1);
  settle(rpcError('timed_out')); await flush(); assert.equal(h.state.dashboardState, 'error');
  assert.equal(h.store.getState().status, 'unavailable');
  h.server.overrides.clear(); await h.clock.advance(30000); assert.equal(h.server.reads().length, 2);
  assert.equal(h.state.dashboardState, 'loaded');
});
test('synthetic adapter: unauthorized first read is Error; failed later read is Stale', async t => {
  const h = setup(); t.after(h.stop);
  h.server.overrides.set('household.snapshot', () => rpcError('unauthorized')); h.start(); await flush();
  assert.equal(h.state.dashboardState, 'error'); assert.equal(h.store.getState().status, 'unauthorized');
  h.server.overrides.clear(); await h.clock.advance(30000); assert.equal(h.state.dashboardState, 'loaded');
  h.server.overrides.set('household.snapshot', () => rpcError('unavailable')); await h.clock.advance(30000);
  assert.equal(h.state.dashboardState, 'stale'); assert.equal(h.store.getState().status, 'unavailable');
});
test('synthetic adapter: stop invalidates a late result without another update or read', async t => {
  const h = setup(); t.after(h.stop); let settle;
  h.server.overrides.set('household.snapshot', fields => new Promise(resolve => { settle = () => h.server.apply('household.snapshot', fields).then(resolve); }));
  h.start(); h.stop(); await settle(); await flush(); await h.clock.advance(60000);
  assert.equal(h.state.dashboardState, 'loading'); assert.equal(h.updates(), 0); assert.equal(h.server.reads().length, 1);
});
test('synthetic adapter: each poll settles while a separate unknown-outcome readback remains pending', async t => {
  const h = setup(); t.after(h.stop); await h.store.refresh();
  h.server.overrides.set('household.item.add', () => rpcError('unknown_outcome'));
  h.server.overrides.set('household.snapshot', () => rpcError('unavailable'));
  const mutation = h.store.addItem('tasks', 'Synthetic pending item'); await flush();
  const reads = h.server.reads().length; h.start(); await flush();
  assert.equal(h.server.reads().length, reads + 1); assert.equal(h.state.dashboardState, 'error');
  assert.equal(h.store.getState().unresolved, 1);
  h.stop(); h.server.overrides.delete('household.snapshot'); await h.clock.advance(6000);
  assert.equal(await mutation, 'not_saved'); assert.equal(h.server.mutations().length, 1);
});
test('poller negative controls detect missing initial read and broken interval cleanup', async () => {
  const noInitial = setup(pollSource.replace('  poll();\n  state.dashboardPollTimer', '  /* suppressed initial poll */\n  state.dashboardPollTimer'));
  noInitial.start(); assert.equal(noInitial.server.reads().length, 0); noInitial.stop();
  const brokenStop = setup(pollSource.replace('    clearInterval(state.dashboardPollTimer);', '    /* suppressed cleanup */').replace('  state.dashboardPollToken++; // invalidates', '  /* suppressed token */; // invalidates'));
  brokenStop.start(); await flush(); brokenStop.stop(); await brokenStop.clock.advance(30000);
  assert.equal(brokenStop.server.reads().length, 2, 'the same no-traffic assertion would reject this mutant');
});
