'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const chatStreamClient = require('../main/chat_stream_client');
const { createCcHandlers, createCollector } = require('../main/cc_handlers');
const { buildCc, createTransport } = require('../renderer/web_cc');
const { createWsBridge } = require('../server/ws_bridge');

const VERBS = [
  'household.snapshot', 'household.item.add', 'household.item.done',
  'household.item.remove', 'household.event.add', 'household.event.remove',
];

function handlerTable(t, householdCommand) {
  const collector = createCollector();
  const stop = createCcHandlers({
    CONFIG: { chatStream: {} }, chatStreamClient: { householdCommand }, harness: false,
  }).register(collector);
  t.after(stop);
  return collector.table;
}

function householdHandler(t, householdCommand) {
  const entry = handlerTable(t, householdCommand)['chat-stream:household'];
  assert.equal(entry?.mode, 'invoke', 'household uses the shared invoke table');
  return entry.handler;
}

function wireClient() {
  const sent = [];
  const client = Object.create(Object.getPrototypeOf(chatStreamClient));
  Object.assign(client, {
    connected: true, _pending: new Map(), _streamEventChunks: new Map(), _fetchBlobChunks: new Map(),
    _requestId: () => 'household-fixture-request',
    _ws: { readyState: 1, send(raw) { sent.push(JSON.parse(raw)); } },
  });
  return { client, sent };
}

test('householdCommand rejects every non-household namespace before sending', () => {
  const sent = [];
  const client = Object.create(chatStreamClient);
  client.sendCommand = (...args) => sent.push(args);
  for (const verb of [undefined, null, 1, {}, new String('household.snapshot'), '',
    'household', 'householdish.snapshot', 'session.close']) {
    assert.throws(() => client.householdCommand(verb, {}), /^Error: Invalid household verb$/);
  }
  assert.deepEqual(sent, []);
});

test('all six household verbs use the correlated raw-response path with a 12-second bound', async () => {
  const sent = [];
  const reply = { type: 'household.snapshot.ok', request_id: 'fixture-request', snapshot: {} };
  const client = Object.create(chatStreamClient);
  client.sendCommand = (...args) => { sent.push(args); return Promise.resolve(reply); };
  for (const verb of VERBS) assert.equal(await client.householdCommand(verb, {}), reply);
  assert.deepEqual(sent, VERBS.map((type) => [
    { type }, 'household', { timeoutMs: 12000, rawResponse: true },
  ]));
});

test('householdCommand assigns type last so caller fields cannot escape the namespace', async () => {
  const fields = Object.freeze({ list: 'grocery', label: 'Synthetic item', type: 'session.close' });
  const client = Object.create(chatStreamClient);
  let payload;
  client.sendCommand = (value) => { payload = value; return Promise.resolve(value); };
  await client.householdCommand('household.item.add', fields);
  assert.deepEqual(payload, { list: 'grocery', label: 'Synthetic item', type: 'household.item.add' });
  assert.equal(fields.type, 'session.close', 'caller object is not mutated');
});

test('shared household handler wraps the untouched successful daemon reply and defaults fields', async (t) => {
  const calls = [];
  const reply = { type: 'household.item.add.ok', item: { id: 'fixture-item' }, request_id: 'fixture-request' };
  const handler = householdHandler(t, async (...args) => { calls.push(args); return reply; });
  const fields = { list: 'grocery', label: 'Synthetic item' };
  assert.deepEqual(await handler(null, 'household.item.add', fields), { ok: true, reply });
  assert.deepEqual(await handler(null, 'household.snapshot'), { ok: true, reply });
  assert.equal(calls[0][1], fields);
  assert.deepEqual(calls, [['household.item.add', fields], ['household.snapshot', {}]]);
});

for (const error_code of ['unauthorized', 'unavailable', 'invalid_request', 'invalid_range',
  'not_found', 'forbidden', 'unknown_outcome']) {
  test(`shared household handler preserves daemon ${error_code} exactly`, async (t) => {
    const frame = {
      type: 'household.item.add.error', request_id: 'fixture-request', error_code,
      error: 'Synthetic daemon refusal', code: 'must_not_replace_error_code',
    };
    const handler = householdHandler(t, async () => { throw frame; });
    assert.deepEqual(await handler(null, 'household.item.add', {}), {
      ok: false, error_code, error: 'Synthetic daemon refusal',
    });
  });
}

test('shared household handler maps a client timeout to timed_out', async (t) => {
  const handler = householdHandler(t, async () => {
    throw { type: 'household.error', request_id: 'fixture-request', error: 'timed_out' };
  });
  assert.deepEqual(await handler(null, 'household.snapshot'), {
    ok: false, error_code: 'timed_out', error: 'timed_out',
  });
});

test('shared household handler maps disconnected and stopped client rejections', async (t) => {
  for (const error of ['Stream disconnected', 'Chat stream client stopped', 'Chat stream is not connected']) {
    const handler = householdHandler(t, async () => {
      throw { type: 'household.error', request_id: 'fixture-request', error };
    });
    assert.deepEqual(await handler(null, 'household.snapshot'), { ok: false, error_code: 'disconnected', error });
  }
});

test('unexpected rejections and unclassified daemon frames stay unknown_outcome', async (t) => {
  const errors = [new Error('Synthetic bridge failure'), { code: 'forbidden', message: 'Synthetic failure' },
    { type: 'household.item.add.error', request_id: 'fixture-request', error: 'Unclassified daemon error' },
    'Synthetic failure', null];
  for (const error of errors) {
    const handler = householdHandler(t, async () => { throw error; });
    const result = await handler(null, 'household.item.add', {});
    assert.equal(result.ok, false);
    assert.equal(result.error_code, 'unknown_outcome');
    assert.equal(typeof result.error, 'string');
  }
});

test('actual RPC correlates by request_id, preserves raw envelopes, and sends once', async (t) => {
  const { client, sent } = wireClient();
  t.after(() => client._rejectPending('Chat stream client stopped'));
  const pending = client.householdCommand('household.snapshot', {});
  assert.deepEqual(sent, [{ type: 'household.snapshot', request_id: 'household-fixture-request' }]);
  assert.equal(client._handleCommandResponse({ type: 'household.snapshot.ok', request_id: 'unrelated' }), false);
  const reply = { type: 'household.snapshot.ok', request_id: sent[0].request_id, snapshot: {}, session: {} };
  assert.equal(client._handleCommandResponse(reply), true);
  assert.equal(await pending, reply, 'rawResponse must not unwrap the session field');
  assert.equal(client._pending.size, 0);
  assert.equal(sent.length, 1);
});

test('actual daemon error reaches the shared handler without losing its error_code', async (t) => {
  const { client, sent } = wireClient();
  const handler = householdHandler(t, client.householdCommand.bind(client));
  const pending = handler(null, 'household.item.remove', { item_id: 'fixture-item' });
  client._handleCommandResponse({
    type: 'household.item.remove.error', request_id: sent[0].request_id,
    error_code: 'not_found', error: 'Synthetic missing item',
  });
  assert.deepEqual(await pending, { ok: false, error_code: 'not_found', error: 'Synthetic missing item' });
});

test('actual RPC times out at 12 seconds without retrying a mutation', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const { client, sent } = wireClient();
  const handler = householdHandler(t, client.householdCommand.bind(client));
  let settled = false;
  const pending = handler(null, 'household.item.done', { item_id: 'fixture-item' });
  pending.then(() => { settled = true; });
  t.mock.timers.tick(11999);
  await Promise.resolve();
  assert.equal(settled, false);
  t.mock.timers.tick(1);
  assert.deepEqual(await pending, { ok: false, error_code: 'timed_out', error: 'timed_out' });
  assert.equal(client._pending.size, 0);
  t.mock.timers.tick(24000);
  assert.equal(sent.length, 1);
});

test('actual _rejectPending disconnect reaches the handler as disconnected', async (t) => {
  const { client, sent } = wireClient();
  const handler = householdHandler(t, client.householdCommand.bind(client));
  const pending = handler(null, 'household.item.remove', { item_id: 'fixture-item' });
  client._rejectPending('Stream disconnected');
  assert.deepEqual(await pending, { ok: false, error_code: 'disconnected', error: 'Stream disconnected' });
  assert.equal(client._pending.size, 0);
  assert.equal(sent.length, 1);
});

test('the browser WebSocket dispatcher uses the same household error-preserving table', async (t) => {
  const table = handlerTable(t, async () => {
    throw { type: 'household.snapshot.error', error_code: 'unauthorized', error: 'Operator sign-in required' };
  });
  const bridge = createWsBridge({ table, logger: { warn() {} } });
  const socket = { frames: [], send(raw) { this.frames.push(JSON.parse(raw)); } };
  bridge.addSocket(socket);
  t.after(() => bridge.removeSocket(socket));
  await bridge.handleMessage(socket, JSON.stringify({ id: 1, method: 'chat-stream:household', args: ['household.snapshot'] }));
  assert.deepEqual(socket.frames, [{ id: 1, ok: true,
    result: { ok: false, error_code: 'unauthorized', error: 'Operator sign-in required' } }]);
});

test('web cc exposes householdCommand with exact arguments and no reply rewriting', async () => {
  const calls = [];
  const reply = { ok: false, error_code: 'unknown_outcome', error: 'Synthetic failure' };
  const transport = { on() {}, fire() {}, call(...args) { calls.push(args); return Promise.resolve(reply); } };
  const cc = buildCc(transport, { clipboard: {}, chatPopoutContext: null, reload() {} });
  const fields = { item_id: 'fixture-item' };
  assert.equal(await cc.householdCommand('household.item.done', fields), reply);
  assert.equal(await cc.householdCommand('household.snapshot'), reply);
  assert.deepEqual(calls, [['chat-stream:household', 'household.item.done', fields],
    ['chat-stream:household', 'household.snapshot', {}]]);
});

test('Electron preload exposes householdCommand with the same invocation contract', async () => {
  const calls = [];
  const reply = { ok: true, reply: { type: 'household.snapshot.ok', snapshot: {} } };
  const ipcRenderer = { on() {}, send() {}, removeAllListeners() {},
    invoke(...args) { calls.push(args); return Promise.resolve(reply); } };
  const context = {
    require(name) {
      if (name === 'electron') return { ipcRenderer };
      if (name === './config-loader') return { loadConfig: () => ({ config: {} }) };
      return require(name);
    },
    window: {}, process,
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8'), context,
    { filename: 'preload.js' });
  const fields = { event_id: 'fixture-event' };
  assert.equal(await context.window.cc.householdCommand('household.event.remove', fields), reply);
  assert.equal(await context.window.cc.householdCommand('household.snapshot'), reply);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['chat-stream:household', 'household.event.remove', fields],
    ['chat-stream:household', 'household.snapshot', {}]]);
});

function fakeWebTransport(t) {
  const sockets = [];
  class FakeSocket {
    static OPEN = 1;
    constructor() { this.readyState = 0; this.listeners = {}; this.sent = []; sockets.push(this); }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    send(raw) { this.sent.push(JSON.parse(raw)); }
    close() { this.readyState = 3; this.listeners.close(); }
    open() { this.readyState = 1; this.listeners.open(); }
    reply(message) { this.listeners.message({ data: JSON.stringify(message) }); }
  }
  const previousWebSocket = global.WebSocket;
  global.WebSocket = FakeSocket;
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const transport = createTransport({ url: 'ws://127.0.0.1/cc' });
  t.after(() => { transport.close(); global.WebSocket = previousWebSocket; });
  return { transport, sockets };
}

test('an offline household call rejects immediately with disconnected, including snapshot reads', async (t) => {
  const { transport, sockets } = fakeWebTransport(t);
  for (const verb of ['household.item.add', 'household.snapshot']) {
    let rejection;
    transport.call('chat-stream:household', verb, {}).catch((error) => { rejection = error; });
    await Promise.resolve();
    assert.ok(rejection instanceof Error, 'must settle without waiting for a socket or the host timeout');
    assert.equal(rejection.code, 'disconnected');
  }
  assert.deepEqual(sockets[0].sent, []);
});

test('a household call made while connecting is never transmitted after reconnect', async (t) => {
  const { transport, sockets } = fakeWebTransport(t);
  const pending = transport.call('chat-stream:household', 'household.item.add', {
    list: 'grocery', label: 'Synthetic item',
  }).catch((error) => error);
  sockets[0].close();
  await pending;
  t.mock.timers.tick(250);
  assert.equal(sockets.length, 2);
  sockets[1].open();
  assert.deepEqual(sockets[1].sent, [], 'a rejected household mutation cannot remain in the replay queue');
});

test('an in-flight household call rejects on close and is never resent', async (t) => {
  const { transport, sockets } = fakeWebTransport(t);
  sockets[0].open();
  const pending = transport.call('chat-stream:household', 'household.item.done', { item_id: 'fixture-item' });
  assert.equal(sockets[0].sent.length, 1);
  sockets[0].close();
  await assert.rejects(pending, /connection lost/);
  t.mock.timers.tick(250);
  sockets[1].open();
  assert.deepEqual(sockets[1].sent, []);
});

test('non-household invoke and fire calls retain their existing offline queue', async (t) => {
  const { transport, sockets } = fakeWebTransport(t);
  const pending = transport.call('get-config');
  transport.fire('app:reload');
  assert.deepEqual(sockets[0].sent, []);
  sockets[0].open();
  assert.deepEqual(sockets[0].sent, [{ id: 1, method: 'get-config', args: [] }, { method: 'app:reload', args: [] }]);
  sockets[0].reply({ id: 1, ok: true, result: { synthetic: true } });
  assert.deepEqual(await pending, { synthetic: true });
});
