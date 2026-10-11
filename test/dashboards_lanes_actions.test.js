'use strict';

// Dashboard host actions `lanes` and `navigate` (spec_pentacle__lanes_3d_web_dashboard_2026_10),
// driven through the real renderer/app.js with the invented lanes contract fixture.
// `lanes` is a read-only, frozen copy of the daemon projection; `navigate` opens only
// the lane's own daemon-resolved chat through the existing lane path.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const vm = require('node:vm');
const esbuild = require('esbuild');
const { installRenderer } = require('./helpers/renderer_chat');
const fixture = require('../pentacle-chat-core/tests/fixtures/work-lanes-inventory.json');

const bundle = esbuild.buildSync({ entryPoints: [path.join(__dirname, '../renderer/src/chat_core_entry.ts')],
  bundle: true, format: 'iife', target: 'chrome134', write: false, logLevel: 'silent' }).outputFiles[0].text;

const flush = () => new Promise((resolve) => setImmediate(resolve));
const SESSION = (name, extra = {}) => ({
  stream_id: `example-host:${name}`, host: 'example-host', session_name: name, name, title: name, provider: 'claude',
  visibility: 'default', session_generation: `gen-${name}`, working: false, ...extra,
});
const COMPOSITE = SESSION('assistant', { session_kind: 'assistant_composite', provider: 'composite', visibility: 'visible' });

// The v1 contract lanes, re-pointed at harness streams, plus the progress facts of the
// first progress_v2 cell on the first lane so member data rides through.
function laneFrame() {
  const frame = JSON.parse(JSON.stringify(fixture.inventory_frame));
  const byId = Object.fromEntries(frame.lanes.map((lane) => [lane.lane_id, lane]));
  byId['wl-blocked-0001'].visible_chat.stream_id = 'example-host:lead-one';
  byId['wl-active-0002'].visible_chat.stream_id = 'example-host:assistant';
  byId['wl-paused-0003'].visible_chat.stream_id = 'example-host:lead-three';
  const progress = fixture.progress_v2[0].frame.lanes[0];
  for (const key of ['members', 'members_total', 'items_total', 'items_open', 'items_completed', 'open_estimate_h', 'estimate_complete'])
    byId['wl-blocked-0001'][key] = JSON.parse(JSON.stringify(progress[key]));
  return frame;
}

async function boot(t) {
  let onFrame = null;
  const ccCalls = [];
  const sessions = [SESSION('lead-one'), COMPOSITE, SESSION('other')];
  const h = installRenderer({
    initialSessions: sessions,
    requestStreamEvents: async (args) => { ccCalls.push(['requestStreamEvents', args]); return { ok: true, count: 0 }; },
    ccOverrides: {
      onChatStreamFrame(cb) { onFrame = cb; },
      getChatStreamState: async () => ({ connected: true, events: [], sessions, schedules: [] }),
    },
  });
  t.after(() => h.dom.window.close());
  vm.runInContext(bundle, h.context);
  h.dom.window.PentacleChatCore = h.context.PentacleChatCore;
  await flush(); await flush();
  const run = (code) => vm.runInContext(code, h.context);
  // Count every daemon-bridge call made while a lanes snapshot is read.
  run(`globalThis.__ccCalls = 0; for (const key of Object.keys(window.cc)) { const fn = window.cc[key];
    if (typeof fn === 'function') window.cc[key] = (...args) => { globalThis.__ccCalls++; return fn(...args); }; }`);
  run('globalThis.__assigned = []; assignToSlot = (name, display, host) => { globalThis.__assigned.push({ name, host }); return 0; };');
  return {
    h, run, ccCalls,
    push: async (frame) => { onFrame(frame); await flush(); },
    ctx: (actions) => { h.context.__actions = actions; return run('dashboardMountContext({ id: "lanes-3d", actions: __actions })'); },
    assigned: () => JSON.parse(run('JSON.stringify(globalThis.__assigned)')),
    ccCount: () => run('globalThis.__ccCalls'),
  };
}

function deepFrozen(value) {
  if (!value || typeof value !== 'object') return true;
  return Object.isFrozen(value) && Object.values(value).every(deepFrozen);
}

test('lanes is unavailable before the first lane frame and makes no bridge call', async (t) => {
  const app = await boot(t);
  const before = app.ccCount();
  assert.deepEqual(JSON.parse(JSON.stringify(await app.ctx(['lanes']).actions.lanes())), { ok: false, error: 'lanes_unavailable' });
  assert.equal(app.ccCount(), before);
});

test('lanes returns a deep-frozen copy in server order with member facts and per-lane tap', async (t) => {
  const app = await boot(t);
  await app.push(laneFrame());
  const before = app.ccCount();
  const reply = await app.ctx(['lanes']).actions.lanes();
  assert.equal(app.ccCount(), before, 'reading the snapshot never calls the daemon bridge');
  assert.equal(reply.ok, true);
  assert.ok(deepFrozen(reply), 'snapshot is deep-frozen');
  const { inventory } = reply;
  assert.deepEqual(JSON.parse(JSON.stringify(inventory.lanes.map((lane) => lane.lane_id))), fixture.expected.order);
  assert.deepEqual(JSON.parse(JSON.stringify(inventory.counts)), fixture.inventory_frame.counts);
  assert.equal(inventory.truncated, false);
  const taps = Object.fromEntries(inventory.lanes.map((lane) => [lane.lane_id, lane.tap]));
  assert.deepEqual(JSON.parse(JSON.stringify(taps)), { 'wl-blocked-0001': 'open_chat', 'wl-active-0002': 'open_chat', 'wl-paused-0004': 'unavailable', 'wl-paused-0003': 'history' });
  const first = inventory.lanes[0];
  assert.deepEqual(JSON.parse(JSON.stringify(first.progress.members.map((m) => m.spec_id))), fixture.progress_v2[0].frame.lanes[0].members.map((m) => m.spec_id));
  assert.deepEqual(JSON.parse(JSON.stringify(first.progress.open_estimate_h)), fixture.progress_v2[0].frame.lanes[0].open_estimate_h);
  assert.equal(inventory.lanes[1].progress, null);
});

test('lanes snapshot is isolated from later projection changes and cannot change host state', async (t) => {
  const app = await boot(t);
  await app.push(laneFrame());
  const reply = await app.ctx(['lanes']).actions.lanes();
  assert.throws(() => { 'use strict'; reply.inventory.lanes[0].title = 'changed'; }, TypeError);
  const frame = laneFrame();
  frame.lanes = frame.lanes.slice(1);
  frame.counts = { open: 3, active: 1, paused: 2, blocked: 0 };
  await app.push(frame);
  assert.equal(reply.inventory.lanes.length, 4, 'an earlier snapshot is unchanged');
  assert.equal(app.run('state.workLanes.lanes.length'), 3);
  assert.equal((await app.ctx(['lanes']).actions.lanes()).inventory.lanes.length, 3);
});

test('lanes and navigate are denied unless the descriptor lists them', async (t) => {
  const app = await boot(t);
  await app.push(laneFrame());
  for (const actions of [[], ['assetList', 'assetGet'], ['household'], undefined]) {
    const ctx = app.ctx(actions);
    assert.deepEqual(JSON.parse(JSON.stringify(await ctx.actions.lanes())), { ok: false, error: 'action_not_allowed' });
    assert.deepEqual(JSON.parse(JSON.stringify(await ctx.actions.navigate({ lane_id: 'wl-blocked-0001' }))), { ok: false, error: 'action_not_allowed' });
  }
  assert.deepEqual(app.assigned(), []);
  assert.equal(app.run('state.slots.filter(Boolean).length'), 0);
});

test('navigate rejects malformed input and unknown lanes without opening anything', async (t) => {
  const app = await boot(t);
  await app.push(laneFrame());
  const { navigate } = app.ctx(['navigate']).actions;
  for (const params of [undefined, null, 'wl-blocked-0001', ['wl-blocked-0001'], {}, { lane_id: 7 }, { lane_id: '' },
    { lane_id: 'wl-blocked-0001', stream_id: 'example-host:other' }]) {
    assert.deepEqual(JSON.parse(JSON.stringify(await navigate(params))), { ok: false, error: 'navigate_invalid' }, JSON.stringify(params));
  }
  assert.deepEqual(JSON.parse(JSON.stringify(await navigate({ lane_id: 'wl-missing-9999' }))), { ok: false, error: 'unknown_lane' });
  assert.deepEqual(app.assigned(), []);
  assert.equal(app.run('state.slots.filter(Boolean).length'), 0);
});

test('navigate opens an open session lane in a slot and switches to the chats view', async (t) => {
  const app = await boot(t);
  await app.push(laneFrame());
  app.run("state.currentView = 'dashboards'"); // the dashboards registry is outside this harness
  const reply = await app.ctx(['navigate']).actions.navigate({ lane_id: 'wl-blocked-0001' });
  assert.deepEqual(JSON.parse(JSON.stringify(reply)), { ok: true, action: 'open_chat' });
  assert.deepEqual(app.assigned(), [{ name: 'lead-one', host: 'local' }]);
  assert.equal(app.run('state.currentView'), 'chats');
});

test('navigate opens a history lane read-only at exactly its generation', async (t) => {
  const app = await boot(t);
  await app.push(laneFrame());
  const reply = await app.ctx(['navigate']).actions.navigate({ lane_id: 'wl-paused-0003' });
  assert.deepEqual(JSON.parse(JSON.stringify(reply)), { ok: true, action: 'history' });
  const slot = app.run('state.slots.findIndex(Boolean)');
  assert.ok(slot >= 0);
  assert.deepEqual(JSON.parse(app.run(`JSON.stringify(state.slots[${slot}].laneHistory)`)),
    { streamId: 'example-host:lead-three', generation: 'gen-lead-0003', laneId: 'wl-paused-0003' });
  const reads = app.ccCalls.filter(([, args]) => args.streamId === 'example-host:lead-three');
  assert.ok(reads.length >= 1 && reads.every(([, args]) => args.generation === 'gen-lead-0003'));
  assert.deepEqual(app.assigned(), []);
});

test('navigate on an unavailable lane shows the unavailable card and opens no other chat', async (t) => {
  const app = await boot(t);
  await app.push(laneFrame());
  const reply = await app.ctx(['navigate']).actions.navigate({ lane_id: 'wl-paused-0004' });
  assert.deepEqual(JSON.parse(JSON.stringify(reply)), { ok: true, action: 'unavailable' });
  const slot = app.run('state.slots.findIndex(Boolean)');
  assert.equal(app.run(`state.slots[${slot}].laneHistory.unavailable`), true);
  assert.match(app.h.dom.window.document.getElementById(`cell-${slot}`).textContent, /Chat unavailable/);
  assert.equal(app.ccCalls.length, 0);
  assert.deepEqual(app.assigned(), []);
});
