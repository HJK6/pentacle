'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const { mountLanesCard } = require('../renderer/next/lanes');

function loadHelper() {
  const source = fs.readFileSync(path.join(__dirname, 'e2e/lib/next_lanes_e6_scenario.js'), 'utf8');
  const start = source.indexOf('async function closeToAllLanes(');
  const end = source.indexOf('\nasync function restorePage(', start);
  assert.ok(start >= 0 && end > start);
  return vm.runInNewContext(`(${source.slice(start, end).trim()})`, {
    json: JSON.stringify,
    wait: (ctx, expression, label) => ctx.session.waitFor(expression, { label }),
  });
}

for (const startLevel of ['lane', 'goal']) {
  test(`E6 navigation returns visibly from ${startLevel} to All lanes using Back`, async () => {
    const dom = new JSDOM('<!doctype html><div id="root"></div>', { runScripts: 'outside-only' });
    const root = dom.window.document.querySelector('#root');
    const goal = { goal_id: 'goal-a', title: 'Synthetic goal', rank: 1, state: 'in_progress', total: 1 };
    const lane = { lane_id: 'lane-a', title: 'Fixture E6 lane', icon: 'flask', detail_revision: 'rev-a',
      goals: [goal], goals_total: 1, progress: { members: [], members_total: 0 } };
    const reads = { subscribe: () => () => {}, getRead: () => ({ status: 'idle', reply: null }) };
    const control = mountLanesCard(root, {
      getState: () => ({ inventory: { lanes: [lane], counts: { open: 1 }, truncated: false } }),
      reads, navigation: { onClose: () => { root.hidden = true; } },
    });
    if (startLevel === 'lane') control.openLane('lane-a');
    else control.openGoal('lane-a', 'goal-a', 'Synthetic goal');
    const ctx = { session: {
      async click(selector) {
        const el = dom.window.document.querySelector(selector);
        if (!el) throw new Error(`missing visible control ${selector}`);
        el.click();
        return true;
      },
      async eval(expression) { return dom.window.eval(expression); },
      async waitFor(expression, { label }) {
        if (!dom.window.eval(expression)) throw new Error(`wait failed: ${label}; route=${control.getRoute().level}, hidden=${root.hidden}`);
      },
    } };
    try {
      await loadHelper()(ctx, startLevel, 'Fixture E6 lane');
      assert.equal(control.getRoute().level, 'all');
      assert.equal(root.hidden, false);
    } finally { control.destroy(); dom.window.close(); }
  });
}
