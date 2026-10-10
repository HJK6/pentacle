'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const app = fs.readFileSync(path.join(__dirname, '../renderer/app.js'), 'utf8');

function contextHarness(actions, sessions = [], mutate = source => source) {
  const start = app.indexOf('function dashboardMountContext(db)');
  assert.notEqual(start, -1, 'the host must construct the pinned context');
  const source = app.slice(start, app.indexOf('function unmountCurrentDashboard()', start));
  const selectors = Object.freeze({ synthetic: true }), store = Object.freeze({ synthetic: true });
  const calls = [], warnings = [], sigils = [], mounts = [], config = { dashboards: { catalogSpecId: 'spec_example' } };
  const reply = { ok: true, synthetic: 'daemon reply' }, dom = new (require('jsdom').JSDOM)('<main>old content</main>'), container = dom.window.document.querySelector('main');
  const db = { id: 'example-board', name: 'Example Board', actions,
    mount(node, ctx) { mounts.push({ node, ctx }); return { synthetic: true }; } };
  const state = { sessions };
  let polls = 0;
  const context = vm.createContext({ householdSelectors: selectors, householdStore: store, state, CONFIG: config,
    window: { MutationObserver: dom.window.MutationObserver, visibleDashboards: () => [db], cc: {
      async assetList(params) { calls.push({ action: 'assetList', params }); return reply; },
      async assetGet(params) { calls.push({ action: 'assetGet', params }); return reply; },
    } },
    console: { warn(...args) { warnings.push(args); } },
    isProtectedAssistantSession(session) { return session.protected === true; },
    machineSigilMarkup(...args) { sigils.push(args); return '<svg data-synthetic="true"></svg>'; },
    document: { createElement: name => dom.window.document.createElement(name), getElementById(id) { assert.equal(id, 'dashboard-content'); return container; } },
    updateDashboardStatusBadge() {}, startDashboardPolling() { polls++; },
  });
  vm.runInContext(mutate(source), context);
  return { selectors, store, state, config, db, calls, warnings, sigils, mounts, container, reply,
    bridge: context.window.cc,
    context: () => context.dashboardMountContext(db), mount: () => context.mountAndPoll(db.id), polls: () => polls };
}

test('startup configures the public singleton exactly once without reading or mutating household data', async () => {
  const source = app.match(/const householdSelectors = require\('\.\/household\/selectors'\);\nconst \{ householdStore \} = require\('\.\/household\/store'\);\nhouseholdStore\.configure\([\s\S]*?\n\}\);/);
  assert.ok(source, 'startup owns the one-time store bridge configuration');
  assert.equal((app.match(/householdStore\.configure\(/g) || []).length, 1);
  let options; const requires = [], calls = [], reply = { ok: true, synthetic: true };
  vm.runInNewContext(source[0], {
    require(id) { requires.push(id); return id.endsWith('/store') ? { householdStore: {
      configure(value) { assert.equal(options, undefined); options = value; },
    } } : {}; },
    window: { cc: { householdCommand(...args) { calls.push(args); return Promise.resolve(reply); } } },
  });
  assert.deepEqual(requires, ['./household/selectors', './household/store']);
  assert.equal(calls.length, 0);
  const fields = { month: '2042-06' };
  assert.equal(await options.household('household.snapshot', fields), reply);
  assert.deepEqual(calls, [['household.snapshot', fields]]);
});

for (let mask = 0; mask < 16; mask++) {
  const capabilities = ['household', 'assistantState', 'assetList', 'assetGet'];
  const allowed = capabilities.filter((_, index) => mask & (1 << index));
  test(`pinned context grants only descriptor actions: ${allowed.join(',') || 'none'}`, async () => {
    const h = contextHarness(allowed, [{ name: 'Assistant', display_name: 'Assistant', hostId: 'fixture-host', protected: true }]);
    const ctx = h.context();
    assert.deepEqual(Object.keys(ctx).sort(), ['actions', 'assistant', 'config', 'household']);
    assert.equal(ctx.config, h.config);
    if (allowed.includes('household')) {
      assert.deepEqual(Object.keys(ctx.household).sort(), ['selectors', 'store']);
      assert.equal(ctx.household.selectors, h.selectors); assert.equal(ctx.household.store, h.store);
      assert.equal(ctx.household.command, undefined);
    } else assert.equal(ctx.household, undefined);
    if (allowed.includes('assistantState')) {
      assert.deepEqual(Object.keys(ctx.assistant).sort(), ['hostId', 'name', 'sigilMarkup']);
      assert.equal(ctx.assistant.name, 'Assistant'); assert.equal(ctx.assistant.hostId, 'fixture-host');
    } else assert.equal(ctx.assistant, undefined);
    assert.deepEqual(Object.keys(ctx.actions).sort(), ['assetGet', 'assetList']);
    for (const action of ['assetList', 'assetGet']) {
      const params = { spec_id: 'spec_example' }, pending = ctx.actions[action](params);
      assert.equal(typeof pending.then, 'function');
      const result = await pending;
      if (allowed.includes(action)) {
        assert.equal(result, h.reply);
        assert.deepEqual(h.calls.at(-1), { action, params });
      } else {
        assert.deepEqual(JSON.parse(JSON.stringify(result)), { ok: false, error: 'action_not_allowed' });
        assert.ok(h.warnings.some(args => JSON.stringify(args).includes(action)));
      }
    }
    assert.equal(h.calls.length, allowed.filter(action => action.startsWith('asset')).length);
    assert.equal(h.warnings.length, 2 - h.calls.length);
  });
}

test('missing or malformed descriptor actions fail closed; familiar names do not imply grants', async () => {
  for (const actions of [undefined, null, 'household', { household: true }, ['unknownAction']]) {
    const h = contextHarness(actions), ctx = h.context();
    assert.equal(ctx.household, undefined); assert.equal(ctx.assistant, undefined);
    assert.equal((await ctx.actions.assetGet({})).error, 'action_not_allowed');
    assert.equal(h.calls.length, 0);
  }
});

test('allowed assistant state is null without a protected session, never a fabricated identity', () => {
  for (const sessions of [[], [{ name: 'Assistant', hostId: 'fixture-host' }]]) {
    const h = contextHarness(['assistantState'], sessions);
    assert.equal(h.context().assistant, null); assert.equal(h.sigils.length, 0);
  }
});

test('assistant is captured once per mount; sigil uses the protected host and identity family', () => {
  const session = { name: 'Assistant', display_name: 'Partner Fixture', hostId: 'fixture-host', protected: true };
  const h = contextHarness(['household', 'assistantState'], [{ name: 'Partner', hostId: 'other-host' }, session]);
  h.mount(); assert.equal(h.mounts.length, 1); assert.equal(h.polls(), 1);
  const ctx = h.mounts[0].ctx;
  assert.equal(h.mounts[0].node, h.container.querySelector('.dashboard-inner')); assert.equal(h.container.querySelector('.dashboard-panel-header h1').textContent, 'Example Board');
  assert.equal(ctx.assistant.name, 'Partner Fixture');
  session.hostId = 'next-host'; session.display_name = 'Assistant';
  assert.equal(ctx.assistant.hostId, 'fixture-host'); assert.equal(ctx.assistant.name, 'Partner Fixture');
  assert.equal(ctx.assistant.sigilMarkup('Assistant', 12), '<svg data-synthetic="true"></svg>');
  assert.deepEqual(h.sigils, [['fixture-host', 'Assistant', 12, 'djinni']]);
  h.mount(); assert.equal(h.mounts[1].ctx.assistant.hostId, 'next-host');
  assert.equal(h.mounts[1].ctx.assistant.name, 'Assistant');
  assert.equal(h.mounts[1].ctx.household.store, ctx.household.store);
  assert.equal(h.mounts[1].ctx.household.selectors, ctx.household.selectors);
});

test('assistant name falls back only to the protected session name', () => {
  const h = contextHarness(['assistantState'], [{ name: 'Assistant', hostId: 'fixture-host', protected: true }]);
  assert.equal(h.context().assistant.name, 'Assistant');
});

test('asset action errors propagate unchanged and descriptor edits cannot widen an existing mount', async () => {
  const grants = ['assetList'];
  const h = contextHarness(grants), ctx = h.context();
  const error = new Error('Synthetic daemon failure');
  h.bridge.assetList = async () => { throw error; };
  await assert.rejects(ctx.actions.assetList({}), actual => actual === error);
  grants.push('assetGet', 'household');
  assert.equal((await ctx.actions.assetGet({})).error, 'action_not_allowed');
  assert.equal(ctx.household, undefined);
  const remount = h.context();
  assert.equal(remount.household.store, h.store);
  assert.equal(await remount.actions.assetGet({}), h.reply);
});

test('negative controls expose broadened grants and a suppressed assistant identity family', async () => {
  const broad = contextHarness([], [], source => source.replace(
    'new Set(Array.isArray(db.actions) ? db.actions : [])',
    "new Set(['household', 'assistantState', 'assetList', 'assetGet'])"));
  assert.notEqual(broad.context().household, undefined, 'the ungranted-household assertion would reject this mutant');
  assert.equal(await broad.context().actions.assetGet({}), broad.reply);
  const wrongSigil = contextHarness(['assistantState'], [{ name: 'Assistant', hostId: 'fixture-host', protected: true }],
    source => source.replace("machineSigilMarkup(hostId, label, size, 'djinni')", 'machineSigilMarkup(hostId, label, size)'));
  wrongSigil.context().assistant.sigilMarkup('Assistant', 12);
  assert.equal(wrongSigil.sigils[0].length, 3, 'the exact sigil identity assertion would reject this mutant');
});
