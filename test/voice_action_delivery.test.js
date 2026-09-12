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
  c.action.version = 2; assert.throws(() => prepareVoiceSpawn(c, catalog));
});
test('one local spawn without Bart session; no chat injection', async () => {
  const f = fixture();
  await Promise.all([f.helper.tick(f.status), f.helper.tick(f.status)]);
  await f.helper.tick(f.status);
  assert.equal(f.spawns.length, 1); assert.equal(f.sends.length, 0);
  assert.deepEqual(f.requests[0], { actions_version: 1 });
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
