const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const Module = require('node:module');

function fixture(t) {
  class Socket extends EventEmitter {
    static OPEN = 1;
    static instances = [];
    constructor() { super(); this.readyState = 1; this.sent = []; Socket.instances.push(this); }
    send(raw) { this.sent.push(JSON.parse(raw)); }
    ping() {}
    terminate() { this.readyState = 3; }
  }
  const file = require.resolve('../main/chat_stream_client');
  const original = Module._load;
  delete require.cache[file];
  Module._load = function (name, parent, isMain) {
    return name === 'ws' ? Socket : original.call(this, name, parent, isMain);
  };
  let client;
  try { client = require(file); } finally { Module._load = original; }
  const frames = [];
  client._readToken = () => '';
  client.init({ chatStream: { url: 'ws://fixture' } }, frame => frames.push({ ...frame, transportReady: client.connected }));
  t.after(() => client.destroy());
  function open(socket = Socket.instances.at(-1)) {
    socket.emit('open');
    socket.emit('message', JSON.stringify({ type: 'welcome' }));
    return socket;
  }
  function ready(socket) {
    socket.emit('message', JSON.stringify({ type: 'snapshot', events: [], sessions: [] }));
  }
  const socket = open(); ready(socket);
  function drop(code = 1001) { socket.readyState = 3; socket.emit('close', code, Buffer.from('going away')); }
  return { client, frames, socket, Socket, open, ready, drop };
}

test('normal daemon shutdown preserves the current view through a brief authenticated recovery', t => {
  const f = fixture(t);
  f.drop();
  assert.equal(f.client.connected, false, 'the transport is really unavailable');
  assert.equal(f.client.snapshot().connected, true, 'the open conversation remains usable while recovery is pending');
  assert.equal(f.frames.some(frame => frame.connected === false), false, 'no reconnect banner for the brief restart');
  f.client._connect();
  const next = f.open(); f.ready(next);
  assert.equal(f.client.connected, true);
  assert.equal(f.frames.some(frame => frame.connected === false), false);
});

test('same-key replay is signaled only after authentication, never at socket-open', async t => {
  const f = fixture(t);
  const pending = f.client.sendMessage({ host: 'fixture', sessionName: 'owned', text: 'accepted before the lost reply', requestId: 'stable-request', optimisticId: 'stable-message' });
  f.drop();
  await assert.rejects(pending, error => error.error === 'Stream disconnected');
  const count = f.frames.filter(frame => frame.type === '__reconnect').length;
  f.client._connect();
  const next = f.open();
  assert.equal(f.frames.filter(frame => frame.type === '__reconnect').length, count, 'opening transport cannot trigger replay');
  f.ready(next);
  const recovered = f.frames.filter(frame => frame.type === '__reconnect');
  assert.equal(recovered.length, count + 1);
  assert.equal(recovered.at(-1).transportReady, true, 'replay can now reach the daemon receipt dedupe');
  const replay = f.client.sendMessage({ host: 'fixture', sessionName: 'owned', text: 'accepted before the lost reply', requestId: 'stable-request', optimisticId: 'stable-message' });
  assert.equal(next.sent.at(-1).request_id, 'stable-request');
  next.emit('message', JSON.stringify({ type: 'send.result', request_id: 'stable-request', delivery: 'landed', submission_confirmed: true }));
  assert.equal((await replay).submission_confirmed, true);
});

test('send during normal restart stays recoverable under its original identity', async t => {
  const f = fixture(t); f.drop();
  await assert.rejects(f.client.sendMessage({ host: 'fixture', sessionName: 'owned', text: 'queued while reconnecting', requestId: 'new-request', optimisticId: 'new-message' }),
    error => error.error === 'Stream disconnected', 'the existing optimistic reducer treats the offline attempt as transient');
  assert.equal(f.socket.sent.filter(frame => frame.type === 'send').length, 0, 'unaccepted input is never reported delivered');
});

test('a new send cannot terminate the replacement socket while its handshake is pending', async t => {
  const f = fixture(t); f.drop(); f.client._connect();
  const next = f.Socket.instances.at(-1); next.emit('open');
  await assert.rejects(f.client.sendMessage({ host: 'fixture', sessionName: 'owned', text: 'during authentication', requestId: 'pending-ready', optimisticId: 'pending-message' }),
    error => error.error === 'Stream disconnected');
  assert.equal(next.readyState, f.Socket.OPEN, 'the focused liveness probe must leave authentication alone');
  next.emit('message', JSON.stringify({ type: 'welcome' })); f.ready(next);
  assert.equal(f.client.connected, true);
});

test('persistent outage becomes visible after the bounded recovery window', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture(t); f.drop();
  t.mock.timers.tick(4999);
  assert.equal(f.client.snapshot().connected, true);
  t.mock.timers.tick(1);
  assert.equal(f.client.snapshot().connected, false);
  assert.equal(f.frames.filter(frame => frame.connected === false).length, 1);
});

test('failed transport reconnects cannot extend the outage deadline', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture(t); f.drop(); t.mock.timers.tick(4000);
  f.client._connect(); const failed = f.Socket.instances.at(-1);
  failed.readyState = 3; failed.emit('close', 1006);
  t.mock.timers.tick(1000);
  assert.equal(f.client.snapshot().connected, false, 'the original five-second deadline still applies');
});

test('authentication failure interrupts the grace immediately and does not consume a replay generation', t => {
  const f = fixture(t); const generation = f.frames.find(frame => frame.type === '__reconnect').next_generation;
  f.drop(); f.client._connect(); const refused = f.open();
  refused.emit('message', JSON.stringify({ type: 'auth.error', error: 'operator_auth_v2_invalid' }));
  assert.equal(f.client.snapshot().connected, false);
  assert.equal(f.client.snapshot().error, 'operator_auth_v2_invalid');
  f.client._connect(); const next = f.open(); f.ready(next);
  assert.equal(f.frames.filter(frame => frame.type === '__reconnect').at(-1).generation, generation);
});

test('cold start and non-shutdown socket failure are not hidden', t => {
  const f = fixture(t); f.drop(1011);
  assert.equal(f.client.snapshot().connected, false);
  assert.equal(f.frames.some(frame => frame.connected === false), true);
});

const hostedUrl = 'https://viewer.example.ts.net:8444/';
const hostedPolicy = { tailnetSuffix: 'example.ts.net', pentacleOrigin: 'https://chat.example.ts.net' };
const { admitHostedUrl } = require('../renderer/dashboards/catalog_loader');
function readyWithoutSnapshot(f, extra = {}) {
 f.client._helloPayload = () => ({ subscribe: { snapshot: false } });
 f.client._connect(); const next = f.open();
 next.emit('message', JSON.stringify({ type: 'ready', snapshot: false, ...extra }));
 return next;
}
test('snapshot without policy then reconnect ready publishes current hosted admission', t => {
 const f = fixture(t); assert.equal(f.client.snapshot().hostedDashboardPolicy, null); f.drop();
 readyWithoutSnapshot(f, { hostedDashboardPolicy: hostedPolicy });
 assert.equal(admitHostedUrl(hostedUrl, f.client.snapshot().hostedDashboardPolicy), hostedUrl);
 assert.deepEqual(f.frames.filter(x => Object.hasOwn(x, 'hostedDashboardPolicy')).at(-1).hostedDashboardPolicy, hostedPolicy);
});
for (const [label, value] of [['absent', undefined], ['null', null], ['malformed', {}],
 ['refusing', { ...hostedPolicy, tailnetSuffix: 'other.ts.net' }]]) {
 test('snapshot-less ready ' + label + ' cannot retain old admitting policy', t => {
  const f = fixture(t);
  f.socket.emit('message', JSON.stringify({ type: 'snapshot', hostedDashboardPolicy: hostedPolicy })); f.drop();
  readyWithoutSnapshot(f, value === undefined ? {} : { hostedDashboardPolicy: value });
  assert.equal(admitHostedUrl(hostedUrl, f.client.snapshot().hostedDashboardPolicy), null);
 });
}
test('disconnect clears hosted policy even during graceful restart recovery', t => {
 const f = fixture(t);
 f.socket.emit('message', JSON.stringify({ type: 'snapshot', hostedDashboardPolicy: hostedPolicy })); f.drop();
 assert.equal(f.client.snapshot().hostedDashboardPolicy, null);
 assert.equal(f.frames.filter(x => Object.hasOwn(x, 'hostedDashboardPolicy')).at(-1).hostedDashboardPolicy, null);
});

test('snapshot missing policy publishes cleared policy to renderer', t => {
 const f = fixture(t);
 f.socket.emit('message', JSON.stringify({ type: 'snapshot', hostedDashboardPolicy: hostedPolicy }));
 f.ready(f.socket);
 assert.equal(f.client.snapshot().hostedDashboardPolicy, null);
 assert.equal(f.frames.at(-1).hostedDashboardPolicy, null);
});

function rendererPolicyHarness() {
 const fs = require('node:fs'), vm = require('node:vm');
 const source = fs.readFileSync(require.resolve('../renderer/app.js'), 'utf8');
 const context = { state: { chatStream: { connected: true, stateVersion: -1 }, currentView: 'chats' },
  window: { hostedDashboardPolicy: hostedPolicy, Event: class {}, dispatchEvent() {} },
  withSharedWorkingProjection: p => p, setDegradedMode() {},
  reconcileChatReadScopes() {}, invalidateDashboardCatalog() {},
  applyVersionedConnectionState: require('../renderer/chat_stream_connection_state').applyVersionedConnectionState };
 vm.createContext(context);
 // Run the actual policy/state prefix and full dispatcher, without unrelated UI rendering.
 vm.runInContext(source.slice(source.indexOf('function applyChatStreamState(data)'), source.indexOf('  // Lanes ride the same ordering gate')) + '\n}', context);
 vm.runInContext(source.slice(source.indexOf('function applyChatStreamPayload(payload)'), source.indexOf('function chatEventsForSession(session)')), context);
 return { context, apply: payload => { context.payload = payload; vm.runInContext('applyChatStreamPayload(payload)', context); } };
}
test('renderer consumes reconnect policy, snapshot replacement and recovery clearing', t => {
 const f = fixture(t), renderer = rendererPolicyHarness();
 f.drop(); readyWithoutSnapshot(f, { hostedDashboardPolicy: hostedPolicy });
 const ready = f.frames.findLast(x => x.type === 'ready'); renderer.apply(ready);
 assert.deepEqual(renderer.context.window.hostedDashboardPolicy, hostedPolicy);
 renderer.apply({ type: 'snapshot', connected: true, state_version: ready.state_version });
 assert.equal(renderer.context.window.hostedDashboardPolicy, null);
 renderer.apply(ready);
 const socket = f.Socket.instances.at(-1); socket.emit('close', 1001);
 const cleared = f.frames.findLast(x => Object.hasOwn(x, 'hostedDashboardPolicy'));
 assert.equal(cleared.connected, true, 'graceful restart must not show a disconnect banner');
 assert.ok(cleared.state_version > ready.state_version);
 renderer.apply(cleared);
 assert.equal(renderer.context.window.hostedDashboardPolicy, null);
 assert.equal(renderer.context.state.chatStream.connected, true);
});


function rendererDraftHarness() {
 const fs = require('node:fs'), vm = require('node:vm');
 const source = fs.readFileSync(require.resolve('../renderer/app.js'), 'utf8');
 const session = { hostId: 'hostx', name: 'owned' };
 const stream = { host: 'hostx', session_name: 'owned', stream_id: 'stream-x' };
 const rendered = [];
 const context = {
  state: { chatStream: { connected: true, stateVersion: -1, drafts: {}, events: [], sessions: [stream] },
   currentView: 'chats', sessions: [session], slots: [session], botSlots: [], slotViewModes: ['chat'],
   locallyClosedStreamIds: new Set() },
  window: { Event: class {}, dispatchEvent() {}, cc: { chatClose: async () => ({ ok: true }) } },
  CHAT_STREAM_LIMIT: 100, SCHEDULE_EVENT_TYPES: [], IS_CLIENT: false,
  withSharedWorkingProjection: p => p, setDegradedMode() {},
  reconcileChatReadScopes() {}, invalidateDashboardCatalog() {},
  applyVersionedConnectionState: require('../renderer/chat_stream_connection_state').applyVersionedConnectionState,
  nextChatStreamSessions: (current, data) => Array.isArray(data.sessions) ? data.sessions : current,
  closedChatSlots: { update() {} }, reconcileComposerDraftInventory() {}, reconcileLocallyClosedStreamIds() {},
  _perfRecord() {}, syncSlotDisplayNames() {}, restoreWorkspaceSessions() {}, scheduleSidebarRerender() {},
  invalidateChatHistoryLoads() {}, refetchEventsForActiveChatSlots() {}, refetchAssetSnapshotsForActiveSlots() {},
  resyncSpawnCatalogAfterReconnect() {}, renderHostsStats() {}, renderScheduledSection() {},
  replaceSchedulesFromInventory: (_, next) => next,
  scheduleDurableQuestionSlotRenders() {},
  streamHostForHostId: id => id,
  chatSessionStateForSession: () => stream, chatSessionStateForNameHost: () => stream,
  isProtectedAssistantNameHost: () => false, decideUseChatClose: () => true,
  detachSlot: slot => { context.state.slots[slot] = null; },
  scheduleSlotChatRender: slot => {
   context.renderSession = context.state.slots[slot];
   rendered.push(vm.runInContext('chatDraftForSession(renderSession)', context));
  },
 };
 vm.createContext(context);
 vm.runInContext(source.slice(source.indexOf('function applyChatStreamState(data)'), source.indexOf('function chatEventsForSession(session)')), context);
 vm.runInContext(source.slice(source.indexOf('function chatDraftForSession(session)'), source.indexOf('function chatSessionStateForSession(session)')), context);
 // Execute the real chat-close path, ending before the unrelated legacy terminal branch.
 vm.runInContext(source.slice(source.indexOf('async function deleteSession(name, hostId)'), source.indexOf('  // Non-chat-stream session (legacy or degraded)')) + '\n}', context);
 return { context, session, stream, rendered,
  apply(payload) { context.payload = payload; vm.runInContext('applyChatStreamPayload(payload)', context); },
  draft() { context.session = session; return vm.runInContext('chatDraftStateForSession(session)', context); },
  close: () => vm.runInContext("deleteSession('owned', 'hostx')", context),
 };
}
function draftEntry(text = 'partial answer') {
 return { kind: 'DRAFT', stream_id: 'stream-x', host: 'hostx', session_name: 'owned', text };
}

test('renderer draft survives snapshot-less reconnect ready and remains in the slot', t => {
 const f = fixture(t), h = rendererDraftHarness(), draft = draftEntry();
 h.apply(draft); assert.equal(h.draft(), draft);
 f.drop(); readyWithoutSnapshot(f, { hostedDashboardPolicy: hostedPolicy });
 const ready = f.frames.findLast(frame => frame.type === 'ready');
 assert.equal(Object.hasOwn(ready, 'drafts'), false);
 h.apply(ready);
 assert.equal(h.draft(), draft);
 assert.equal(h.rendered.at(-1), draft.text, 'slot still receives the draft');
});
test('renderer snapshot with empty drafts clears the slot', () => {
 const h = rendererDraftHarness(); h.apply(draftEntry());
 h.apply({ type: 'snapshot', connected: true, drafts: {} });
 assert.equal(h.draft(), null); assert.equal(h.rendered.at(-1), '');
});
test('renderer snapshot replaces drafts rather than merging them', () => {
 const h = rendererDraftHarness(), replacement = draftEntry('replacement');
 h.apply(draftEntry()); h.context.state.chatStream.drafts['other-stream'] = draftEntry('old');
 h.apply({ type: 'snapshot', connected: true, drafts: { 'stream-x': replacement } });
 assert.equal(h.draft(), replacement);
 assert.deepEqual(Object.keys(h.context.state.chatStream.drafts), ['stream-x']);
 assert.equal(h.rendered.at(-1), replacement.text);
});
test('renderer DRAFT event still updates its stream entry', () => {
 const h = rendererDraftHarness(), updated = draftEntry('updated'); h.apply(draftEntry());
 h.apply({ type: 'chat.event', event: updated });
 assert.equal(h.draft(), updated); assert.equal(h.rendered.at(-1), updated.text);
});
test('renderer chat close removes draft and status ready cannot revive it', async () => {
 const h = rendererDraftHarness(); h.apply(draftEntry()); await h.close();
 assert.equal(h.draft(), null); assert.equal(h.context.state.slots[0], null);
 h.apply({ type: 'ready', snapshot: false, connected: true });
 assert.equal(h.draft(), null);
});
test('renderer disconnected status preserves drafts until an explicit snapshot replaces them', () => {
 const h = rendererDraftHarness(), draft = draftEntry(); h.apply(draft);
 h.apply({ connected: false });
 assert.equal(h.draft(), draft); assert.equal(h.rendered.at(-1), draft.text);
 h.apply({ type: 'ready', snapshot: false, connected: true });
 assert.equal(h.draft(), draft);
});
