import test from 'node:test';
import assert from 'node:assert/strict';
import { initialPentacleStreamState } from 'pentacle-chat-core';
import { ChatStoreController, type ChatSendBridge } from '../renderer/src/chat_store_controller';

// Synthetic transcript, metadata and bridge replies only: no capture, upload,
// transcription, daemon, credentials or physical microphone is involved.
const streamId = 'fixture:voice-answers';
const transcript = '  Synthetic answer transcript  ';
const flush = () => new Promise(resolve => setImmediate(resolve));
const refusal = { ok: false, error_code: 'voice_answers_invalid', error: 'Voice answers binding is invalid' };
type Send = Parameters<ChatSendBridge>[0];

function deferred() {
  let resolve: (value: any) => void = () => {};
  const promise = new Promise<any>(done => { resolve = done; });
  return { promise, resolve };
}

function bindingMeta(duration: any = 3.25) {
  return {
    voice: { duration_s: duration },
    voice_answers: {
      version: 1, recording_id: 'fixture-recording', blob_sha: 'a'.repeat(64), duration_s: 3.25,
      items: [{ key: 'fixture-key', question_id: 'fixture-question', notification_id: 'fixture-notification',
        producer_stream_id: 'fixture:producer', surface_stream_id: streamId,
        prompt: 'x'.repeat(2001), segment: { start_s: 0, end_s: 2 } }],
    },
    extra: 'must not survive conversion',
  };
}

function deepFreeze<T>(value: T): T {
  if (value && typeof value === 'object') {
    for (const nested of Object.values(value)) deepFreeze(nested);
    Object.freeze(value);
  }
  return value;
}

function fixture(t: any, connected = true) {
  const store = new ChatStoreController({
    ...initialPentacleStreamState,
    connected,
    sessions: [{ stream_id: streamId, host: 'fixture', session_name: 'voice-answers',
      provider: 'composite', session_kind: 'assistant_composite', online: true }],
  });
  const calls: Send[] = [];
  store.setSendBridge(async args => { calls.push(args); return refusal; });
  t.after(() => store.dispose());
  return { store, calls };
}

function userRows(store: ChatStoreController) {
  return store.selectSessionDetail(streamId)!.transcriptItems.filter(item => item.isUser);
}

test('explicit plain-note conversion re-arms the same row once with fresh metadata and request identity', async t => {
  const { store, calls } = fixture(t);
  const meta = deepFreeze(bindingMeta());
  const captured = structuredClone(meta);
  const id = store.sendTurn(streamId, transcript, [], { meta });
  await flush();
  const refused = store.getState();
  const originalSend = refused.optimisticSends![id];
  const originalEvent = refused.events.find(event => event.optimistic_id === id)!;
  const originalRow = userRows(store)[0];
  assert.equal(originalSend.status, 'failed');
  assert.equal(originalSend.failure_reason, 'voice_answers_invalid');
  assert.equal(originalEvent.pending, false);
  assert.equal(calls.length, 1, 'a refused binding never resends without the user action');
  assert.equal(calls[0].meta, meta, 'the supplied original send metadata is retained');
  assert.equal(userRows(store).length, 1);

  const plainResponse = deferred();
  store.setSendBridge(async args => { calls.push(args); return plainResponse.promise; });
  const reentrantResults: boolean[] = [];
  const unsubscribe = store.subscribe(state => {
    if (state.optimisticSends?.[id]?.status === 'dispatched') {
      reentrantResults.push(store.sendVoiceAsPlainNote(id));
    }
  });
  assert.equal(store.sendVoiceAsPlainNote(id), true);
  const converted = store.getState();
  const nextSend = converted.optimisticSends![id];
  assert.equal(nextSend.status, 'dispatched', 'the failed row is re-armed synchronously');
  assert.equal(nextSend.failure_reason, undefined);
  assert.equal(nextSend.failed_at, undefined);
  assert.equal(nextSend.optimistic_id, id);
  assert.equal(nextSend.text, transcript, 'reuse the existing transcript verbatim');
  assert.notEqual(nextSend.request_id, originalSend.request_id);
  assert.equal(converted.optimisticByRequestId![originalSend.request_id], undefined);
  assert.equal(converted.optimisticByRequestId![nextSend.request_id], id);
  assert.equal(converted.events.find(event => event.optimistic_id === id)!.pending, true);
  assert.deepEqual(reentrantResults, [false], 'listeners cannot double-dispatch during re-arm');
  assert.equal(store.sendVoiceAsPlainNote(id), false, 'a synchronous second click is a no-op');
  assert.equal(store.getState(), converted);
  assert.equal(calls.length, 1, 'the bridge dispatch remains on the existing microtask path');
  unsubscribe();
  await flush();

  assert.equal(calls.length, 2, 'exactly one plain-note dispatch');
  assert.equal(calls[1].streamId, streamId);
  assert.equal(calls[1].optimisticId, id);
  assert.equal(calls[1].requestId, nextSend.request_id);
  assert.equal(calls[1].text, transcript);
  assert.deepEqual(calls[1].meta, { voice: { duration_s: 3.25 } });
  assert.equal(Object.hasOwn(calls[1].meta!, 'voice_answers'), false, 'omit the key entirely; present-but-undefined is invalid');
  assert.notEqual(calls[1].meta, meta);
  assert.notEqual(calls[1].meta!.voice, meta.voice);
  assert.deepEqual(meta, captured, 'no supplied metadata, recording identity or blob reference is changed');
  assert.deepEqual(calls[0].meta, captured, 'the captured first bridge arguments remain immutable');
  assert.equal(originalSend.status, 'failed', 'prior immutable state is untouched');
  assert.equal(originalSend.request_id, calls[0].requestId);
  assert.equal(originalEvent.pending, false);
  assert.equal(userRows(store).length, 1);
  assert.equal(userRows(store)[0].eventKey, originalRow.eventKey);
  assert.equal(store.sendVoiceAsPlainNote(id), false, 'a later click is also a no-op');
  plainResponse.resolve({ ok: true });
  await flush();
  assert.equal(calls.length, 2);
});

for (const connected of [false, true]) test(`voice_answers_invalid is terminal while connected=${connected} and never replays on reconnect`, async t => {
  const { store, calls } = fixture(t, connected);
  // Even disconnect-looking text cannot weaken this typed terminal refusal.
  store.setSendBridge(async args => { calls.push(args); return { ...refusal, error: 'socket disconnected' }; });
  const id = store.sendTurn(streamId, transcript, [], { meta: bindingMeta() });
  await flush();
  assert.equal(store.getState().optimisticSends![id].status, 'failed');
  assert.equal(store.getState().optimisticSends![id].failure_reason, 'voice_answers_invalid');
  store.applyFrame({ type: '__reconnect', generation: 0, next_generation: 1 });
  await flush();
  assert.equal(calls.length, 1, 'the refused binding is not eligible for automatic reconnect replay');
  assert.equal(store.getState().optimisticSends![id].status, 'failed');
  assert.equal(store.sendVoiceAsPlainNote(id), true, 'only an explicit conversion can send again');
  await flush();
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1].meta, { voice: { duration_s: 3.25 } });
});

for (const [connected, error, status] of [
  [true, 'backend_busy', 'failed'],
  [true, 'socket disconnected', 'indeterminate'],
  [false, 'backend_busy', 'indeterminate'],
] as const) test(`other error codes keep existing ${status} behavior: connected=${connected}, ${error}`, async t => {
  const { store, calls } = fixture(t, connected);
  store.setSendBridge(async args => { calls.push(args); return { ok: false, error_code: 'other_code', error }; });
  const id = store.sendTurn(streamId, transcript, [], { meta: bindingMeta() });
  await flush();
  const before = store.getState();
  assert.equal(before.optimisticSends![id].status, status);
  if (status === 'failed') assert.equal(before.optimisticSends![id].failure_reason, error);
  assert.equal(store.sendVoiceAsPlainNote(id), false);
  assert.equal(store.getState(), before);
  store.applyFrame({ type: '__reconnect', generation: 0, next_generation: 1 });
  await flush();
  assert.equal(calls.length, status === 'indeterminate' ? 2 : 1, 'ordinary replay rules are unchanged');
});

for (const voice of [undefined, null, {}, { duration_s: -1 }, { duration_s: Infinity },
  { duration_s: NaN }, { duration_s: true }, { duration_s: '3.25' }]) {
  test(`conversion requires retained valid voice metadata: ${JSON.stringify(voice)}`, async t => {
    const { store, calls } = fixture(t);
    const meta = { ...bindingMeta(), voice } as any;
    const id = store.sendTurn(streamId, transcript, [], { meta });
    await flush();
    const before = store.getState();
    assert.equal(before.optimisticSends![id].failure_reason, 'voice_answers_invalid');
    assert.equal(store.sendVoiceAsPlainNote(id), false);
    assert.equal(store.getState(), before);
    await flush();
    assert.equal(calls.length, 1);
  });
}

test('missing, pending, indeterminate and acknowledged rows cannot convert; zero duration is valid', async t => {
  const { store, calls } = fixture(t);
  const pending = deferred();
  store.setSendBridge(async args => { calls.push(args); return pending.promise; });
  assert.equal(store.sendVoiceAsPlainNote('missing'), false);
  const id = store.sendTurn(streamId, transcript, [], { meta: bindingMeta(0) });
  assert.equal(store.sendVoiceAsPlainNote(id), false);
  await flush();
  store.applyFrame({ type: 'send.indeterminate', request_id: calls[0].requestId });
  assert.equal(store.sendVoiceAsPlainNote(id), false);
  pending.resolve(refusal);
  await flush();
  store.setSendBridge(async args => { calls.push(args); return { ok: true }; });
  assert.equal(store.sendVoiceAsPlainNote(id), true);
  await flush();
  assert.deepEqual(calls[1].meta, { voice: { duration_s: 0 } });
  store.applyFrame({ type: 'send.result', request_id: calls[1].requestId, delivery: 'landed' });
  assert.equal(store.sendVoiceAsPlainNote(id), false);
  assert.equal(calls.length, 2);
});

for (const lateResponse of [{ ok: true }, refusal]) test(`late original bridge response (${lateResponse.ok}) and frames cannot affect the fresh attempt`, async t => {
  const { store, calls } = fixture(t);
  const original = deferred();
  const plain = deferred();
  store.setSendBridge(async args => { calls.push(args); return calls.length === 1 ? original.promise : plain.promise; });
  const id = store.sendTurn(streamId, transcript, [], { meta: bindingMeta() });
  await flush();
  const oldRequestId = calls[0].requestId;
  store.applyFrame({ type: 'send.result', request_id: oldRequestId, delivery: 'not_landed', reason: 'voice_answers_invalid' });
  assert.equal(store.sendVoiceAsPlainNote(id), true);
  await flush();
  const converted = store.getState();
  original.resolve(lateResponse);
  await flush();
  assert.equal(store.getState(), converted);
  for (const frame of [
    { type: 'send.result', delivery: 'landed' },
    { type: 'send.result', delivery: 'not_landed', reason: 'voice_answers_invalid' },
    { type: 'send.indeterminate' },
  ]) {
    store.applyFrame({ ...frame, request_id: oldRequestId });
    assert.equal(store.getState(), converted);
  }
  assert.equal(calls.length, 2);
  plain.resolve({ ok: true });
  await flush();
  assert.equal(store.getState().optimisticSends![id].status, 'dispatched');
});

test('the plain-note attempt reconciles normally to exactly one server USER row', async t => {
  const { store, calls } = fixture(t);
  const id = store.sendTurn(streamId, transcript, [], { meta: bindingMeta() });
  await flush();
  store.setSendBridge(async args => { calls.push(args); return { ok: true }; });
  assert.equal(store.sendVoiceAsPlainNote(id), true);
  await flush();
  store.applyFrame({ type: 'send.result', request_id: calls[1].requestId, delivery: 'landed' });
  const event = { stream_id: streamId, host: 'fixture', provider: 'composite', session_id: 'voice-answers',
    session_name: 'voice-answers', kind: 'USER', daemon_seq: 42, timestamp: new Date().toISOString(),
    text: transcript, optimistic_id: id, request_id: calls[1].requestId, message_id: 'fixture-plain-message',
    meta: { voice: { duration_s: 3.25 } } };
  store.applyFrame({ type: 'chat.event', event });
  store.applyFrame({ type: 'chat.event', event });
  assert.equal(store.getState().optimisticSends![id], undefined);
  assert.equal(userRows(store).length, 1);
  assert.equal(userRows(store)[0].text, transcript.trim());
  assert.deepEqual((userRows(store)[0] as any).voice, { duration_s: 3.25 });
  assert.equal(userRows(store)[0].voiceAnswersStatus, undefined);
  assert.equal(store.sendVoiceAsPlainNote(id), false);
  assert.equal(calls.length, 2);
});

test('an ordinary later Retry retains the converted voice-only metadata and transcript', async t => {
  const { store, calls } = fixture(t);
  const meta = deepFreeze(bindingMeta());
  const id = store.sendTurn(streamId, transcript, [], { meta });
  await flush();
  store.setSendBridge(async args => { calls.push(args); return { ok: false, error: 'backend_busy' }; });
  assert.equal(store.sendVoiceAsPlainNote(id), true);
  await flush();
  assert.equal(store.getState().optimisticSends![id].failure_reason, 'backend_busy');
  assert.equal(store.sendVoiceAsPlainNote(id), false);
  assert.equal(store.retryOptimisticSend(id), true);
  await flush();
  assert.equal(calls.length, 3);
  assert.notEqual(calls[2].requestId, calls[1].requestId);
  assert.equal(calls[2].optimisticId, id);
  assert.equal(calls[2].text, transcript);
  assert.deepEqual(calls[2].meta, { voice: { duration_s: 3.25 } });
  assert.equal(userRows(store).length, 1);
  assert.equal(meta.voice_answers.recording_id, 'fixture-recording');
  assert.equal(meta.voice_answers.blob_sha, 'a'.repeat(64));
});

test('a repeated refusal of the already-plain attempt cannot enable another conversion', async t => {
  const { store, calls } = fixture(t);
  const id = store.sendTurn(streamId, transcript, [], { meta: bindingMeta() });
  await flush();
  assert.equal(store.sendVoiceAsPlainNote(id), true);
  await flush();
  assert.equal(store.getState().optimisticSends![id].failure_reason, 'voice_answers_invalid');
  const before = store.getState();
  assert.equal(store.sendVoiceAsPlainNote(id), false);
  assert.equal(store.getState(), before);
  await flush();
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1].meta, { voice: { duration_s: 3.25 } });
});

test('a binding must be an own retained metadata key before the row can convert', async t => {
  const { store, calls } = fixture(t);
  for (const meta of [{ voice: { duration_s: 2 } },
    Object.assign(Object.create({ voice_answers: bindingMeta().voice_answers }), { voice: { duration_s: 2 } })]) {
    const id = store.sendTurn(streamId, transcript, [], { meta });
    await flush();
    const before = store.getState();
    assert.equal(before.optimisticSends![id].failure_reason, 'voice_answers_invalid');
    assert.equal(store.sendVoiceAsPlainNote(id), false);
    assert.equal(store.getState(), before);
  }
  await flush();
  assert.equal(calls.length, 2);
});
