const { test } = require('node:test');
const assert = require('node:assert/strict');
const { installRenderer, mountRaceSlot } = require('./helpers/renderer_chat');
const client = require('../main/chat_stream_client');
const { createCcHandlers, createCollector } = require('../main/cc_handlers');
test('actual web composer never toggles the room mic; desktop still does', () => {
  for (const isWeb of [true, false]) {
    const { context, dom } = installRenderer(); dom.window.HOST.isWeb = isWeb; mountRaceSlot(context);
    let clicks = 0; dom.window.document.getElementById('mic-btn-toggle').onclick = () => clicks++;
    dom.window.document.querySelector('.slot-chat-compose-mic').click();
    assert.equal(clicks, isWeb ? 0 : 1);
  }
});
test('shared host handler and client pass voice metadata to ordinary and composite sends', async () => {
  const collector = createCollector(); let input;
  createCcHandlers({ CONFIG: { chatStream: {} }, chatStreamClient: { sendMessage: async p => { input = p; return { delivery: 'landed' }; } } }).register(collector);
  const meta = { voice: { duration_s: 2 } };
  await collector.table['chat-stream:send'].handler({}, 'home', 'assistant', 'voice', 'request', 'message', [], { stream_id: 'home:assistant', meta });
  assert.deepEqual(input.meta, meta);
  const fake = Object.create(client); fake.sendCommand = async p => p; fake.noteInteraction = () => {};
  for (const composite of [false, true]) {
    fake._sessions = [{ stream_id: 'home:assistant', session_kind: composite ? 'assistant_composite' : 'agent' }];
    const frame = await fake.sendMessage({ host: 'home', sessionName: 'assistant', text: 'voice', optimisticId: 'message', meta });
    assert.deepEqual(frame.meta, meta);
  }
});
test('transcription bridge preserves request identity and reports backend errors', async () => {
  const fake = Object.create(client); fake.sendCommand = async (p, type, options) => ({ p, type, options });
  const result = await fake.transcribeBlob({ request_id: 'take', blob_sha: 'a'.repeat(64), mime: 'audio/wav' });
  assert.equal(result.type, 'transcribe_blob'); assert.equal(result.options.requestId, 'take'); assert.equal(result.p.mime, 'audio/wav');
  const collector = createCollector();
  createCcHandlers({ CONFIG: { chatStream: {} }, chatStreamClient: { transcribeBlob: async () => { throw { error_code: 'backend_unavailable' }; } } }).register(collector);
  const reply = await collector.table['chat-stream:transcribe-blob'].handler({}, {});
  assert.equal(reply.ok, false); assert.equal(reply.error_code, 'backend_unavailable');
});
test('outbound commit bridge sends only to a daemon advertising outbound_commit_v1', async () => {
  const fake = Object.create(client); const frames = []; fake.sendCommand = async (p, type) => { frames.push([type, p]); return { type: `${type}.ok`, server_send: true }; };
  fake._capabilities = {};
  await assert.rejects(fake.outboundCommit({ voice_operation: {}, send_mandate: {} }), { error_code: 'unsupported' });
  assert.equal(frames.length, 0);
  fake._capabilities = { outbound_commit_v1: true };
  await fake.outboundCommit({ voice_operation: { operation_id: 'op' }, send_mandate: { request_id: 'r' } });
  await fake.outboundGet('op'); await fake.outboundCancel('op');
  assert.deepEqual(frames.map(f => [f[0], f[1].type, f[1].operation_id]), [['outbound.commit', 'outbound.commit', undefined], ['outbound.get', 'outbound.get', 'op'], ['outbound.cancel', 'outbound.cancel', 'op']]);
  const collector = createCollector();
  createCcHandlers({ CONFIG: { chatStream: {} }, chatStreamClient: { outboundCommit: async () => { throw { error_code: 'unsupported' }; }, outboundGet: async () => ({ state: 'committed' }) } }).register(collector);
  assert.deepEqual(await collector.table['chat-stream:outbound-commit'].handler({}, {}), { ok: false, error_code: 'unsupported' });
  assert.deepEqual(await collector.table['chat-stream:outbound-get'].handler({}, 'op'), { state: 'committed', ok: true });
});
