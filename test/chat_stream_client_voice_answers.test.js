'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createCcHandlers, createCollector } = require('../main/cc_handlers');
const fixtures = require('./fixtures/voice_answers_meta/accept.json');
const binding = fixtures[0].input;

// A real client/command/host handler, with only its socket replaced. No listener,
// daemon, auth file, microphone, or live connection is opened.
function harness(t) {
  const clientPath = require.resolve('../main/chat_stream_client');
  delete require.cache[clientPath];
  const client = require(clientPath);
  const sent = [];
  client.connected = true;
  client._isAlive = true;
  client._sessions = [{ stream_id: 'fixture:assistant', session_kind: 'assistant_composite' }];
  client._ws = { readyState: 1, ping() {}, send(raw) {
    const payload = JSON.parse(raw);
    sent.push(payload);
    const pending = client._pending.get(payload.request_id);
    client._pending.delete(payload.request_id);
    pending.resolve({ type: 'send.result', request_id: payload.request_id, delivery: 'landed' });
  } };
  t.after(() => { client._ws = null; client.destroy(); delete require.cache[clientPath]; });
  const collector = createCollector();
  createCcHandlers({ CONFIG: { chatStream: {} }, chatStreamClient: client, harness: true }).register(collector);
  const send = (meta, request = 'request-1') => collector.table['chat-stream:send'].handler(
    {}, 'fixture', 'assistant', 'Ship it', request, 'optimistic-1', [], { stream_id: 'fixture:assistant', meta });
  return { client, sent, send };
}

test('valid composite send carries normalized voice_answers beside voice and no other meta', async t => {
  const h = harness(t);
  const reply = await h.send({ voice: { duration_s: 7, forbidden: true }, voice_answers: fixtures[1].input, forbidden: true });
  assert.equal(reply.ok, true);
  assert.deepEqual(h.sent[0], { type: 'send', stream_id: 'fixture:assistant', message: 'Ship it', msg_id: 'optimistic-1',
    request_id: 'request-1', meta: { voice: { duration_s: 7 }, voice_answers: fixtures[1].expected } });
});

for (const file of fs.readdirSync(path.join(__dirname, 'fixtures/voice_answers_meta')).filter(name => name.startsWith('reject_'))) {
  test(`host refusal through chat-stream:send: ${file}`, async t => {
    const h = harness(t);
    for (const fixture of require(`./fixtures/voice_answers_meta/${file}`)) {
      const reply = await h.send({ voice: { duration_s: 7 }, voice_answers: fixture.input });
      assert.deepEqual(reply, { ok: false, error_code: 'voice_answers_invalid', error: 'Voice answers binding is invalid' }, fixture.name);
    }
    assert.deepEqual(h.sent, [], 'invalid binding is never stripped and silently sent');
  });
}

test('explicit undefined binding is refused and sends nothing', async t => {
  const h = harness(t);
  assert.equal((await h.send({ voice: { duration_s: 7 }, voice_answers: undefined })).error_code, 'voice_answers_invalid');
  assert.equal(h.sent.length, 0);
});

test('voice-only validation and metadata whitelist remain unchanged', async t => {
  const h = harness(t);
  for (const duration of [7, 300]) {
    await h.send({ voice: { duration_s: duration, ignored: true }, voice_answers_status: { state: 'bound' }, ignored: true });
    assert.deepEqual(h.sent.at(-1).meta, { voice: { duration_s: duration } });
  }
  for (const duration of [0, -1, 301, '7', true, NaN, Infinity]) {
    await h.send({ voice: { duration_s: duration }, ignored: true });
    assert.equal(h.sent.at(-1).meta, undefined);
  }
  await h.send({ ignored: true });
  assert.equal(h.sent.at(-1).meta, undefined);
});

test('binding send retry preserves recording id, optimistic id, text and item order', async t => {
  const h = harness(t);
  await h.send({ voice: { duration_s: 7 }, voice_answers: binding }, 'first');
  await h.send({ voice: { duration_s: 7 }, voice_answers: binding }, 'retry');
  const [first, retry] = h.sent;
  assert.deepEqual(retry, { ...first, request_id: 'retry' });
});

test('explicit plain-note fallback sends only voice after refusal', async t => {
  const h = harness(t);
  await h.send({ voice: { duration_s: 7 }, voice_answers: { ...binding, version: 2 } });
  assert.equal(h.sent.length, 0);
  await h.send({ voice: { duration_s: 7 } }, 'operator-plain-retry');
  assert.equal(h.sent.length, 1);
  assert.deepEqual(h.sent[0].meta, { voice: { duration_s: 7 } });
});

function rendererStore(t) {
  const os = require('node:os');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'voice-answers-store-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const output = path.join(directory, 'controller.cjs');
  require('esbuild').buildSync({ entryPoints: [path.join(__dirname, '../renderer/src/chat_store_controller.ts')],
    outfile: output, bundle: true, platform: 'node', format: 'cjs', logLevel: 'silent' });
  const { ChatStoreController } = require(output);
  const store = new ChatStoreController();
  store.applyFrame({ type: 'snapshot', sessions: [{ stream_id: 'fixture:assistant', host: 'fixture', session_name: 'assistant', provider: 'claude' }], events: [] });
  return store;
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test('renderer branches on host refusal code, retains binding for explicit retry, and never retries itself', async t => {
  const store = rendererStore(t);
  const calls = [];
  store.setSendBridge(async payload => { calls.push(payload); return { ok: false, error_code: 'voice_answers_invalid', error: 'Voice answers binding is invalid' }; });
  const meta = { voice: { duration_s: 7 }, voice_answers: binding };
  const id = store.sendTurn('fixture:assistant', 'Ship it', [], { meta });
  await flush();
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].meta, meta);
  assert.equal(store.getState().optimisticSends[id].status, 'failed');
  assert.equal(store.getState().optimisticSends[id].failure_reason, 'voice_answers_invalid');
  await flush();
  assert.equal(calls.length, 1, 'no hidden fallback or retry loop');
  assert.equal(store.retryOptimisticSend(id), true);
  await flush();
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1].meta, meta);
  assert.equal(calls[1].optimisticId, calls[0].optimisticId);
  assert.notEqual(calls[1].requestId, calls[0].requestId);
});

test('renderer unchanged errors retain their existing reason fallback', async t => {
  const store = rendererStore(t);
  store.setSendBridge(async () => ({ ok: false, error: 'backend_busy' }));
  const id = store.sendTurn('fixture:assistant', 'Ship it');
  await flush();
  assert.equal(store.getState().optimisticSends[id].failure_reason, 'backend_busy');
});
