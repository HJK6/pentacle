'use strict';

// Web renderer work lanes (spec_pentacle__first_class_work_lanes_2026_10, M4):
// header stats line, "Lanes (N)" sidebar section, and tap routing per
// visible_chat.available, driven by the daemon projection fixture through the
// real renderer/app.js.
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

// Local stream ids so the fixture lanes resolve to rows in the harness roster (host map local -> hostc).
const SESSION = (name, extra = {}) => ({
  stream_id: `hostc:${name}`, host: 'hostc', session_name: name, name, title: name, provider: 'claude',
  visibility: 'default', session_generation: `gen-${name}`, working: false, ...extra,
});
const COMPOSITE = SESSION('assistant', { session_kind: 'assistant_composite', provider: 'composite', visibility: 'visible' });

function frameWith(mutate) {
  const frame = JSON.parse(JSON.stringify(fixture.inventory_frame));
  mutate?.(frame);
  return frame;
}

function localFrame() {
  return frameWith((frame) => {
    const byId = Object.fromEntries(frame.lanes.map((lane) => [lane.lane_id, lane]));
    byId['wl-blocked-0001'].visible_chat.stream_id = 'hostc:lead-one';
    byId['wl-active-0002'].visible_chat.stream_id = 'hostc:assistant';
    byId['wl-paused-0003'].visible_chat.stream_id = 'hostc:lead-three';
  });
}

async function boot(t, { snapshot = {}, sessions = [SESSION('lead-one'), COMPOSITE, SESSION('other')], requestStreamEvents, realStore = false, manualTimers = false, lanesSidebar = true } = {}) {
  let onFrame = null;
  const calls = [];
  const timers = [];
  const h = installRenderer({
    ...(manualTimers ? { setTimeout: (fn) => { timers.push(fn); return timers.length; } } : {}),
    initialSessions: sessions,
    requestStreamEvents: async (args) => { calls.push(args); return requestStreamEvents ? requestStreamEvents(args) : { ok: true, count: 0 }; },
    ccOverrides: {
      onChatStreamFrame(cb) { onFrame = cb; },
      getChatStreamState: async () => ({ connected: true, events: [], sessions, schedules: [], ...snapshot }),
    },
  });
  t.after(() => h.dom.window.close());
  // The sidebar lanes view is off by default; these tests cover it switched on.
  if (lanesSidebar) h.dom.window.__PENTACLE_WORK_LANES_SIDEBAR__ = true;
  vm.runInContext(bundle, h.context);
  h.dom.window.PentacleChatCore = h.context.PentacleChatCore;
  if (realStore) {
    for (const key of ['PentacleChatStore', 'PentacleChatView']) h.dom.window[key] = h.context[key];
    h.dom.window.PentacleChatStore.applyFrame({ type: 'snapshot', connected: true, sessions, events: [] });
  }
  await flush(); await flush();
  const doc = h.dom.window.document;
  const api = {
    h, doc, calls, timers,
    push: async (frame) => { onFrame(frame); await flush(); },
    stats: () => doc.getElementById('stats').textContent,
    rows: () => [...doc.querySelectorAll('#session-list .lane-row')],
    row: (id) => doc.querySelector(`#session-list .lane-row[data-lane-id="${id}"]`),
    click: async (id) => { api.row(id).dispatchEvent(new h.dom.window.MouseEvent('click', { bubbles: true })); await flush(); await flush(); },
    run: (code) => vm.runInContext(code, h.context),
    // Ordinary sessions open an xterm pane the harness cannot host; record the slot assignment instead.
    spyAssign: () => {
      vm.runInContext('globalThis.__assigned = []; assignToSlot = (name, display, host) => { globalThis.__assigned.push({ name, host }); return 0; };', h.context);
      return () => JSON.parse(vm.runInContext('JSON.stringify(globalThis.__assigned)', h.context));
    },
  };
  return api;
}

test('by default the sidebar shows no lane count and no Lanes section, from a push or a snapshot', async (t) => {
  const app = await boot(t, { lanesSidebar: false, snapshot: { work_lanes: fixture.inventory_frame, state_version: 1 } });
  const sessionRows = () => app.doc.querySelectorAll('#session-list .session-item').length;
  assert.match(app.stats(), /^\d+ sessions \| \d+ need answer \| \d+ working$/);
  const before = sessionRows();
  await app.push({ ...localFrame(), state_version: 2 });
  assert.match(app.stats(), /^\d+ sessions \| \d+ need answer \| \d+ working$/);
  assert.equal(app.rows().length, 0);
  assert.equal(app.doc.querySelector('#session-list .lanes-label, #session-list .lanes-truncated'), null);
  assert.equal(sessionRows(), before);
  assert.ok(before > 0);
});

test('stats line and lanes section appear when the daemon pushes the inventory', async (t) => {
  const app = await boot(t);
  assert.doesNotMatch(app.stats(), /lanes/);
  assert.equal(app.rows().length, 0);
  await app.push(localFrame());
  assert.match(app.stats(), /^4 lanes \| \d+ sessions \| \d+ need answer \| \d+ working$/);
  assert.deepEqual(app.rows().map((r) => r.dataset.laneId), fixture.expected.order);
  assert.match(app.doc.querySelector('#session-list .lanes-label').textContent, /^Lanes \(4\)$/);
  // Lanes sit above the session tiers.
  assert.equal(app.doc.querySelector('#session-list').firstElementChild.classList.contains('lanes-label'), true);
});

test('startup snapshot work_lanes seeds the panel without waiting for a push', async (t) => {
  const app = await boot(t, { snapshot: { work_lanes: localFrame() } });
  assert.deepEqual(app.rows().map((r) => r.dataset.laneId), fixture.expected.order);
  assert.match(app.stats(), /^4 lanes \|/);
});

test('a lane that completes leaves the header and the panel on the next frame', async (t) => {
  const app = await boot(t);
  await app.push(localFrame());
  await app.push(frameWith((frame) => {
    frame.lanes = frame.lanes.filter((lane) => lane.lane_id !== 'wl-active-0002');
    frame.counts = { open: 3, active: 0, paused: 2, blocked: 1 };
  }));
  assert.match(app.stats(), /^3 lanes \|/);
  assert.equal(app.row('wl-active-0002'), null);
});

test('lane count is not the session count', async (t) => {
  const app = await boot(t);
  await app.push(localFrame());
  assert.match(app.stats(), /^4 lanes \| 3 sessions/);
});

test('tap on an open session lane assigns that session to a slot', async (t) => {
  const app = await boot(t);
  const assigned = app.spyAssign();
  await app.push(localFrame());
  await app.click('wl-blocked-0001');
  assert.deepEqual(assigned(), [{ name: 'lead-one', host: 'local' }]);
});

test('tap on an open composite lane opens the composite chat, not a lane-specific chat', async (t) => {
  const app = await boot(t);
  await app.push(localFrame());
  await app.click('wl-active-0002');
  assert.equal(app.run('state.slots.filter(Boolean).map((s) => s.name).join(",")'), 'assistant');
});

test('tap on a history lane opens a read-only slot bound to that generation', async (t) => {
  const app = await boot(t);
  await app.push(localFrame());
  await app.click('wl-paused-0003');
  const slot = app.run('state.slots.findIndex(Boolean)');
  assert.ok(slot >= 0);
  assert.deepEqual(JSON.parse(app.run(`JSON.stringify(state.slots[${slot}].laneHistory)`)),
    { streamId: 'hostc:lead-three', generation: 'gen-lead-0003', laneId: 'wl-paused-0003' });
  assert.equal(app.run(`state.slots[${slot}].popoutStreamId`), 'hostc:lead-three');
  assert.equal(app.run(`!!state.terminals[${slot}]`), false);
  assert.ok(app.doc.getElementById(`cell-${slot}`).classList.contains('lane-history'));
  // History is fetched with the lane's generation and never as a different stream.
  const reads = app.calls.filter((c) => c.streamId === 'hostc:lead-three');
  assert.ok(reads.length >= 1);
  assert.ok(reads.every((c) => c.generation === 'gen-lead-0003'));
  assert.equal(app.calls.some((c) => c.streamId === 'hostc:assistant'), false);
});

test('history slot cannot send: no send is possible and the composer is hidden by lane-history', async (t) => {
  const app = await boot(t);
  await app.push(localFrame());
  await app.click('wl-paused-0003');
  const slot = app.run('state.slots.findIndex(Boolean)');
  const cell = app.doc.getElementById(`cell-${slot}`);
  assert.ok(cell.classList.contains('lane-history'));
  assert.equal(app.h.sendCalls.length, 0);
  assert.match(cell.textContent, /read-only|History/i);
});

test('tap on an unavailable lane shows an explicit unavailable card and never opens another chat', async (t) => {
  const app = await boot(t);
  await app.push(localFrame());
  await app.click('wl-paused-0004');
  const slot = app.run('state.slots.findIndex(Boolean)');
  assert.ok(slot >= 0);
  assert.equal(app.run(`state.slots[${slot}].laneHistory.unavailable`), true);
  const cell = app.doc.getElementById(`cell-${slot}`);
  assert.match(cell.textContent, /Chat unavailable/);
  assert.equal(app.calls.length, 0);
  assert.equal(app.run('state.slots.filter(Boolean).filter((s) => s.name === "assistant").length'), 0);
  assert.ok(app.row('wl-paused-0004'), 'the lane stays visible');
});

test('keyboard activation (Enter) routes like a click', async (t) => {
  const app = await boot(t);
  const assigned = app.spyAssign();
  await app.push(localFrame());
  app.row('wl-blocked-0001').dispatchEvent(new app.h.dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  await flush(); await flush();
  assert.deepEqual(assigned(), [{ name: 'lead-one', host: 'local' }]);
});

test('a lane whose open chat is not in the roster does not fall back to another chat', async (t) => {
  const app = await boot(t, { sessions: [COMPOSITE, SESSION('other')] });
  const assigned = app.spyAssign();
  await app.push(localFrame());
  await app.click('wl-blocked-0001');
  assert.deepEqual(assigned(), []);
  assert.equal(app.run('state.slots.filter(Boolean).length'), 0);
});

test('history slot is not retired by the closed-slot tracker when the same name is open again', async (t) => {
  const app = await boot(t, { sessions: [SESSION('lead-three', { session_generation: 'gen-NEW' }), COMPOSITE] });
  await app.push(localFrame());
  await app.click('wl-paused-0003');
  const slot = app.run('state.slots.findIndex(Boolean)');
  assert.ok(slot >= 0);
  await app.push({ type: 'session.inventory', sessions: [COMPOSITE] });
  assert.equal(app.run(`!!state.slots[${slot}]`), true);
});

test('fetched closed-chat rows render in the history slot and the composer stays unusable', async (t) => {
  const events = [1, 2, 3].map((n) => ({ stream_id: 'hostc:lead-three', host: 'hostc', session_name: 'lead-three', provider: 'claude',
    kind: n === 2 ? 'USER' : 'ASSIST_TEXT', daemon_seq: n, message_id: `m-${n}`,
    timestamp: new Date(1700000000000 + n * 1000).toISOString(), text: `Closed history row ${n}` }));
  let app;
  // Production order: the main process forwards the stream_events frame, then resolves the request.
  app = await boot(t, { realStore: true, sessions: [COMPOSITE, SESSION('other')],
    requestStreamEvents: async () => { await app.push({ type: 'stream_events', stream_id: 'hostc:lead-three', events });
      return { ok: true, count: 3, received: 3, exhausted: true, nextBeforeDaemonSeq: null }; } });
  await app.push(localFrame());
  await app.click('wl-paused-0003');
  await flush(); await flush();
  const slot = app.run('state.slots.findIndex(Boolean)');
  const cell = app.doc.getElementById(`cell-${slot}`);
  const text = cell.querySelector('.slot-chat-list').textContent;
  assert.match(text, /Closed history row 1/);
  assert.match(text, /Closed history row 3/);
  assert.match(cell.querySelector('.lane-history-banner').textContent, /Read-only history/);
  app.run(`state.slotChatRefs[${slot}].inputEl.value = 'hello'; sendChatComposer(${slot});`);
  await flush();
  assert.equal(app.h.sendCalls.length, 0);
});

test('closing the history slot leaves nothing that bends a later ordinary read', async (t) => {
  const app = await boot(t);
  await app.push(localFrame());
  await app.click('wl-paused-0003');
  const slot = app.run('state.slots.findIndex(Boolean)');
  app.run(`detachSlot(${slot})`);
  assert.equal(app.doc.getElementById(`cell-${slot}`).classList.contains('lane-history'), false);
  const before = app.calls.length;
  app.run(`state.chatStream.eventsLoadedFor.delete('hostc:lead-three'); ensureChatEventsLoaded('hostc:lead-three', true);`);
  await flush();
  assert.ok(app.calls.slice(before).every((c) => c.generation === undefined));
});

test('tapping the same history lane again reuses its slot instead of opening another', async (t) => {
  const app = await boot(t);
  await app.push(localFrame());
  await app.click('wl-paused-0003');
  await app.click('wl-paused-0003');
  assert.equal(app.run('state.slots.filter(Boolean).length'), 1);
});

test('history slot reports a failed or empty closed-chat load instead of "Loading chat…" forever', async (t) => {
  const failed = await boot(t, { realStore: true, requestStreamEvents: async () => ({ ok: false, error: 'unknown_session' }) });
  await failed.push(localFrame());
  await failed.click('wl-paused-0003');
  await flush(); await flush();
  let slot = failed.run('state.slots.findIndex(Boolean)');
  assert.match(failed.doc.getElementById(`cell-${slot}`).querySelector('.slot-chat-list').textContent, /Messages could not be loaded/);
  const empty = await boot(t, { realStore: true, requestStreamEvents: async () => ({ ok: true, count: 0, received: 0 }) });
  await empty.push(localFrame());
  await empty.click('wl-paused-0003');
  await flush(); await flush();
  slot = empty.run('state.slots.findIndex(Boolean)');
  assert.match(empty.doc.getElementById(`cell-${slot}`).querySelector('.slot-chat-list').textContent, /No retained messages/);
});

test('stylesheet hides the composer for a lane-history cell', () => {
  const css = require('node:fs').readFileSync(path.join(__dirname, '../renderer/styles.css'), 'utf8');
  assert.match(css, /\.grid-cell\.lane-history \.slot-chat-dock[^{]*\{\s*display:\s*none/);
});

test('a lane whose history generation changed re-reads at the new generation in the same slot', async (t) => {
  const app = await boot(t);
  await app.push(localFrame());
  await app.click('wl-paused-0003');
  await app.push(frameWith((frame) => {
    const lane = frame.lanes.find((item) => item.lane_id === 'wl-paused-0003');
    lane.visible_chat = { stream_id: 'hostc:lead-three', generation: 'gen-lead-0003-b', kind: 'session', available: 'history' };
  }));
  await app.click('wl-paused-0003');
  assert.equal(app.run('state.slots.filter(Boolean).length'), 1);
  const last = app.calls.filter((c) => c.streamId === 'hostc:lead-three').pop();
  assert.equal(last.generation, 'gen-lead-0003-b');
  assert.equal(app.run('state.slots.find(Boolean).laneHistory.generation'), 'gen-lead-0003-b');
});

test('the lane generation rides only the lane-history slot reads; an ordinary read of the same stream omits it', async (t) => {
  const app = await boot(t);
  await app.push(localFrame());
  await app.click('wl-paused-0003');
  const before = app.calls.length;
  app.run(`state.chatStream.eventsLoadedFor.delete('hostc:lead-three'); ensureChatEventsLoaded('hostc:lead-three', true);`);
  await flush();
  const ordinary = app.calls.slice(before).filter((c) => c.streamId === 'hostc:lead-three');
  assert.ok(ordinary.length >= 1);
  // The explicit ordinary call is the first request; later ones are the history slot's own repaint reads.
  assert.equal(ordinary[0].generation, undefined, JSON.stringify(ordinary));
  assert.ok(ordinary.slice(1).every((c) => c.generation === 'gen-lead-0003'), JSON.stringify(ordinary));
  // The slot's own repaint-driven reads still carry the generation.
  const own = app.calls.slice(0, before).filter((c) => c.streamId === 'hostc:lead-three');
  assert.ok(own.length >= 1 && own.every((c) => c.generation === 'gen-lead-0003'));
});

test('detaching one lane-history slot cannot change another stream binding', async (t) => {
  const app = await boot(t);
  await app.push(localFrame());
  await app.click('wl-paused-0003');
  await app.click('wl-paused-0004');
  const unavailableSlot = app.run('state.slots.findIndex((s) => s && s.laneHistory && s.laneHistory.unavailable)');
  app.run(`detachSlot(${unavailableSlot})`);
  const historySlot = app.run('state.slots.findIndex((s) => s && s.laneHistory)');
  assert.ok(historySlot >= 0);
  app.run(`state.chatStream.eventsLoadedFor.delete('hostc:lead-three'); scheduleSlotChatRender(${historySlot}); renderSlotChat(${historySlot});`);
  await flush();
  const last = app.calls.filter((c) => c.streamId === 'hostc:lead-three').pop();
  assert.equal(last.generation, 'gen-lead-0003');
});

test('a stale snapshot cannot roll the lane inventory back; stale lane pushes are ignored too', async (t) => {
  const app = await boot(t);
  const newer = (open) => frameWith((frame) => {
    frame.lanes = frame.lanes.slice(0, open);
    frame.counts = { open, active: 0, paused: 0, blocked: open };
  });
  await app.push({ ...newer(3), state_version: 10 });
  assert.match(app.stats(), /^3 lanes \|/);
  await app.push({ type: 'snapshot', connected: true, sessions: [], events: [], state_version: 9, work_lanes: newer(1) });
  assert.match(app.stats(), /^3 lanes \|/, 'stale snapshot must not replace lanes');
  await app.push({ ...newer(2), state_version: 9 });
  assert.match(app.stats(), /^3 lanes \|/, 'stale push must be ignored');
  await app.push({ ...newer(2), state_version: 11 });
  assert.match(app.stats(), /^2 lanes \|/);
  await app.push({ type: 'snapshot', connected: true, sessions: [], events: [], state_version: 12, work_lanes: newer(1) });
  assert.match(app.stats(), /^1 lanes \|/, 'an accepted snapshot carries its lanes');
});

test('a pending lane-history retry dies with its slot; a surviving ordinary slot of the same stream never sends the detached generation', async (t) => {
  const app = await boot(t, { manualTimers: true, sessions: [SESSION('lead-three'), COMPOSITE] });
  await app.push(localFrame());
  await app.click('wl-paused-0003');
  const historySlot = app.run('state.slots.findIndex((s) => s && s.laneHistory)');
  // An ordinary chat slot bound to the same stream id stays attached.
  app.run(`state.slots[3] = { name: 'lead-three', displayName: 'Lead three', hostId: 'local' };
    state.slotViewModes[3] = 'chat'; state.slotChatBoundStream[3] = 'hostc:lead-three';`);
  app.run(`state.chatStream.historyLoads['hostc:lead-three'] = { status: 'error', error: 'x', attempt: 0 };
    scheduleChatHistoryRetry('hostc:lead-three', false, 'gen-lead-0003');`);
  assert.ok(app.timers.length >= 1, 'a retry is queued');
  app.run(`detachSlot(${historySlot})`);
  const before = app.calls.length;
  for (const fn of app.timers.splice(0)) fn();
  await flush();
  assert.deepEqual(app.calls.slice(before).filter((c) => c.generation === 'gen-lead-0003'), []);
});

test('an ordinary retry is not kept alive by a lane-history slot of the same stream', async (t) => {
  const app = await boot(t, { manualTimers: true, sessions: [SESSION('lead-three'), COMPOSITE] });
  await app.push(localFrame());
  await app.click('wl-paused-0003');
  app.run(`state.chatStream.historyLoads['hostc:lead-three'] = { status: 'error', error: 'x', attempt: 0 };
    scheduleChatHistoryRetry('hostc:lead-three', false);`);
  const before = app.calls.length;
  for (const fn of app.timers.splice(0)) fn();
  await flush();
  assert.deepEqual(app.calls.slice(before).filter((c) => c.generation === undefined && c.streamId === 'hostc:lead-three'), []);
});

test('an ordinary slot retry of a shared failed load does not suppress or cancel the lane-history retry', async (t) => {
  const app = await boot(t, { manualTimers: true, sessions: [SESSION('lead-three'), COMPOSITE] });
  await app.push(localFrame());
  await app.click('wl-paused-0003');
  app.timers.splice(0); // retries the history slot's own first render queued against the previous load
  app.run(`state.slots[3] = { name: 'lead-three', displayName: 'Lead three', hostId: 'local' };
    state.slotViewModes[3] = 'chat'; state.slotChatBoundStream[3] = 'hostc:lead-three';
    state.slotChatBoundStream[${app.run('state.slots.findIndex((s) => s && s.laneHistory)')}] = 'hostc:lead-three';
    state.chatStream.historyLoads['hostc:lead-three'] = { status: 'error', error: 'x', attempt: 0 };
    scheduleChatHistoryRetry('hostc:lead-three', false);
    scheduleChatHistoryRetry('hostc:lead-three', false, 'gen-lead-0003');`);
  assert.equal(app.timers.length, 2, 'one retry per owner');
  const before = app.calls.length;
  for (const fn of app.timers.splice(0)) { fn(); await flush(); }
  const sent = app.calls.slice(before).filter((c) => c.streamId === 'hostc:lead-three');
  assert.ok(sent.some((c) => c.generation === 'gen-lead-0003'), JSON.stringify(sent));
});

test('a pending lane-history retry belongs to its slot instance: unmount + remount at the same generation never lets the stale timer act', async (t) => {
  const app = await boot(t, { manualTimers: true, sessions: [SESSION('lead-three'), COMPOSITE] });
  await app.push(localFrame());
  await app.click('wl-paused-0003');
  app.timers.splice(0);
  const slot = app.run('state.slots.findIndex((s) => s && s.laneHistory)');
  app.run(`state.slotChatBoundStream[${slot}] = 'hostc:lead-three';
    state.chatStream.historyLoads['hostc:lead-three'] = { status: 'error', error: 'x', attempt: 0 };
    scheduleChatHistoryRetry('hostc:lead-three', false, 'gen-lead-0003', ${slot});`);
  assert.equal(app.timers.length, 1);
  const [stale] = app.timers.splice(0);
  app.run(`globalThis.__sharedLoad = state.chatStream.historyLoads['hostc:lead-three'];`);
  // Unmount and remount the same lane at the same generation; the shared failed load object persists.
  app.run(`detachSlot(${slot})`);
  await app.click('wl-paused-0003');
  const remounted = app.run('state.slots.findIndex((s) => s && s.laneHistory)');
  app.run(`state.slotChatBoundStream[${remounted}] = 'hostc:lead-three';
    state.chatStream.historyLoads['hostc:lead-three'] = globalThis.__sharedLoad;`);
  const before = app.calls.length;
  stale();
  await flush();
  assert.equal(app.calls.length, before, 'the stale timer sent a read on behalf of the replacement');
  // The replacement schedules and runs its own retry (drop retries its own first render queued against the old load).
  app.timers.splice(0);
  app.run(`scheduleChatHistoryRetry('hostc:lead-three', false, 'gen-lead-0003', ${remounted});`);
  assert.equal(app.timers.length, 1);
  app.timers.splice(0)[0]();
  await flush();
  assert.ok(app.calls.slice(before).some((c) => c.generation === 'gen-lead-0003'));
});
