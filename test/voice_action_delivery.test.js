'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { prepareVoiceSpawn, spawnOutcome } = require('../renderer/voice_action_delivery');
const { createWakeDelivery } = require('../renderer/wake_delivery');
const catalog = { catalog_version: 'v1', models: { codex: { 'gpt-6-astra': { efforts: ['high'] } } } };
const capture = () => ({ id: 'a-unique-capture', generation: 'one', text: 'spawn an agent to review tests',
  action: { version: 1, route: 'spawn_agent', task: 'review tests', provider: 'codex', model: 'gpt-6-astra', effort: 'high', host: 'amaterasu' } });
function fixture() {
  const f = { status: { mode: 'on', local_actions: { enabled: true }, wake: { enabled: true, generation: 'one', pending_count: 1 } },
    claims: [capture()], spawns: [], sends: [], outcomes: [], requests: [], state: { connected: true, sessions: [] } };
  f.api = async (_method, path, body) => {
    if (path === '/status') return structuredClone(f.status);
    if (path === '/actions/outcome') { f.outcomes.push(body); return { ok: true }; }
    f.requests.push(body); return { claim: f.claims.shift() || null };
  };
  f.make = (overrides = {}) => createWakeDelivery({
    config: { features: { mic: true, assistantRole: 'assistant' }, mic: { wakeTargetHost: 'bart' }, chatStream: {} },
    getState: async () => structuredClone(f.state), api: (...args) => f.api(...args),
    sendTurn: (...args) => f.sends.push(args), getSpawnCatalog: async () => catalog,
    spawnAgent: async request => { f.spawns.push(request); return { ok: true, state: 'ready', streamId: 'amaterasu:new' }; }, ...overrides,
  });
  f.helper = f.make(); return f;
}
test('spawn keeps tuple, exact task, bounded objective and stable idempotency', () => {
  const request = prepareVoiceSpawn(capture(), catalog);
  assert.equal(request.model, 'gpt-6-astra'); assert.equal(request.effort, 'high');
  assert.equal(request.initialPrompt, 'Operator voice request:\nreview tests');
  assert.equal(request.idempotencyKey, 'voice:a-unique-capture');
  const c = capture(); c.action.task = '🦊'.repeat(200);
  assert.equal(Array.from(prepareVoiceSpawn(c, catalog).objective).length, 120);
  c.action.command = 'anything'; assert.throws(() => prepareVoiceSpawn(c, catalog));
});
test('unknown tuple/action fails without fallback', () => {
  const c = capture(); c.action.model = 'unknown'; assert.throws(() => prepareVoiceSpawn(c, catalog));
  c.action.version = 3; assert.throws(() => prepareVoiceSpawn(c, catalog));
});
test('one local spawn without Bart session; no chat injection', async () => {
  const f = fixture();
  await Promise.all([f.helper.tick(f.status), f.helper.tick(f.status)]);
  await f.helper.tick(f.status);
  assert.equal(f.spawns.length, 1); assert.equal(f.sends.length, 0);
  assert.deepEqual(f.requests[0], { actions_version: 2 });
  assert.equal(f.outcomes[0].outcome, 'spawned');
});
test('Off while catalog resolves prevents action', async () => {
  const f = fixture();
  f.helper = f.make({ getSpawnCatalog: async () => { f.status.mode = 'off'; return catalog; } });
  await f.helper.tick(f.status); assert.equal(f.spawns.length, 0);
});
test('uncertain spawn is not retried or sent to Bart', async () => {
  const f = fixture(); let calls = 0;
  f.helper = f.make({ spawnAgent: async () => { calls++; throw Error('lost response'); } });
  await f.helper.tick(f.status); f.claims.push(capture()); await f.helper.tick(f.status);
  assert.equal(calls, 1); assert.equal(f.sends.length, 0);
  assert.equal(f.outcomes[0].outcome, 'unconfirmed');
});
test('queued differs from actual started', () => {
  assert.equal(spawnOutcome({ state: 'queued', stream_id: 'x' }), 'queued');
  assert.equal(spawnOutcome({ ok: false }), 'unconfirmed');
  assert.equal(spawnOutcome({ ok: true, state: 'failed', streamId: 'reserved' }), 'unconfirmed');
});
test('client forwards existing daemon prompt and dedup fields, preserving legacy spawn', async () => {
  const client = require('../main/chat_stream_client');
  const original = client.sendCommand;
  client.sendCommand = async payload => payload;
  try {
    const input = prepareVoiceSpawn(capture(), catalog);
    const payload = await client.spawnSession(input);
    assert.equal(payload.initial_prompt, input.initialPrompt);
    assert.equal(payload.objective, input.objective);
    assert.equal(payload.idempotency_key, input.idempotencyKey);
    assert.equal(payload.model, 'gpt-6-astra');
    const legacy = await client.spawnSession({ host: 'local', provider: 'codex' });
    assert.deepEqual(legacy, { type: 'spawn', host: 'local', provider: 'codex' });
  } finally { client.sendCommand = original; }
});

test('web shim and host bridge preserve voice prompt, tuple and idempotency without chat', async () => {
  const { buildCc } = require('../renderer/web_cc');
  const { createCcHandlers, createCollector } = require('../main/cc_handlers');
  const { createWsBridge } = require('../server/ws_bridge');
  const client = require('../main/chat_stream_client');
  const original = client.sendCommand;
  const payloads = [];
  client.sendCommand = async payload => {
    payloads.push(payload);
    return { state: 'ready', stream_id: 'amaterasu:voice-web-fixture' };
  };
  const stub = new Proxy({
    getSpawnCatalog: async () => catalog,
    spawnSession: input => client.spawnSession(input),
  }, { get: (obj, key) => obj[key] || (async () => ({})) });
  const collector = createCollector();
  const stop = createCcHandlers({ CONFIG: { chatStream: {} }, chatStreamClient: stub, harness: true }).register(collector);
  const bridge = createWsBridge({ table: collector.table });
  let response;
  const socket = { send: raw => { response = JSON.parse(raw); } };
  bridge.addSocket(socket);
  const cc = buildCc({
    async call(method, ...args) {
      response = null;
      await bridge.handleMessage(socket, JSON.stringify({ id: 1, method, args }));
      assert.equal(response.ok, true);
      return response.result;
    }, fire() {}, on() {},
  }, { clipboard: {}, chatPopoutContext: null, reload() {} });
  try {
    const f = fixture();
    const helper = f.make({ getSpawnCatalog: () => cc.chatSpawnCatalog(), spawnAgent: request => cc.chatSpawnV2(request) });
    await helper.tick(f.status);
    assert.equal(payloads.length, 1);
    assert.equal(payloads[0].type, 'spawn');
    assert.equal(payloads[0].initial_prompt, 'Operator voice request:\nreview tests');
    assert.equal(payloads[0].idempotency_key, 'voice:a-unique-capture');
    assert.equal(payloads[0].model, 'gpt-6-astra');
    assert.equal(payloads[0].effort, 'high');
    assert.equal(payloads[0].host, 'amaterasu');
    assert.equal(f.sends.length, 0);
    assert.equal(f.outcomes[0].outcome, 'spawned');
  } finally {
    bridge.removeSocket(socket);
    if (typeof stop === 'function') stop();
    client.sendCommand = original;
  }
});

test('model-only protocol2 spawn opens an idle agent without inventing a task', () => {
  const input = capture(); input.action.version = 2; input.action.task = '';
  input.action.effort = 'high'; input.action.effort_source = 'model_guidance_default';
  const request = prepareVoiceSpawn(input, catalog);
  assert.equal(request.model, 'gpt-6-astra');
  assert.equal(request.effort, 'high');
  assert.equal(request.host, 'amaterasu');
  assert.equal(request.initialPrompt, '');
  assert.equal(request.objective, 'Voice-opened gpt-6-astra agent');
  input.action.version = 1;
  assert.throws(() => prepareVoiceSpawn(input, catalog));
  input.action.version = 2; input.action.model = '';
  assert.throws(() => prepareVoiceSpawn(input, catalog));
});

test('follow-up status tracks waiting, capture and inference without consuming a claim', () => {
  const f = fixture(); f.status.wake.pending_count = 0;
  f.status.local_actions.pending = { state: 'waiting' };
  assert.match(f.helper.message(f.status), /Waiting for your answer/);
  f.status.capture_origin = 'followup';
  assert.match(f.helper.message(f.status), /Recording your answer/);
  f.status.capture_origin = null; f.status.local_actions.pending.state = 'in_flight';
  assert.match(f.helper.message(f.status), /Processing your answer/);
  f.status.local_actions.pending = null;
  assert.doesNotMatch(f.helper.message(f.status), /Waiting for your answer/);
  assert.equal(f.requests.length, 0);
});

test('resolved default effort and provenance are returned in the actual outcome receipt', async () => {
  const f = fixture(); f.claims[0].action.effort = 'high'; f.claims[0].action.effort_source = 'model_guidance_default';
  const live = { ...catalog, spawn_defaults: { providers: { codex: { effort: 'high' } } } };
  f.helper = f.make({ getSpawnCatalog: async () => live });
  await f.helper.tick(f.status);
  assert.equal(f.outcomes[0].receipt.effort, 'high');
  assert.equal(f.outcomes[0].receipt.effort_source, 'model_guidance_default');
  assert.equal(f.spawns[0].idempotencyKey, 'voice:a-unique-capture');
  assert.equal(f.sends.length, 0);
});
