'use strict';
// Send-leg vectors ported from pentacle-mobile/tests/voiceAnswersCarry.test.ts
// at b9f4564. Web's bar is the installed carrier; the unchanged web_voice unit
// owns capture/upload/ASR. All media, bridge responses and clocks are synthetic.
const test = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const { createQuestionVoiceBar } = require('../renderer/question_voice_bar');
const { createVoiceTake } = require('../renderer/web_voice');
const { voiceAnswersItemCount } = require('../renderer/voice_answers_binding');
const flush = () => new Promise(resolve => setImmediate(resolve));
const sha = 'a'.repeat(64), text = 'Hold the deploy. Go with option two.';
const page = n => ({ source: 'durable', key: `n-${n}:0`, model: { prompt: `Prompt ${n}?` },
  notification: { notification_id: `n-${n}`, question: { question_id: `q-${n}`, producer_stream_id: `fixture:producer-${n}` } } });
function fixture(t, id, { failAsr = false, failAppend = false } = {}) {
  const dom = new JSDOM('<header></header><section id="deck"><article></article></section><section id="takes"></section>');
  const doc = dom.window.document, calls = { upload: [], transcribe: [], send: [], dispatched: [] };
  let now = 0, asrFails = failAsr, appendFails = failAppend;
  const env = { isSecureContext: true, navigator: { mediaDevices: { getUserMedia() {} } },
    MutationObserver: dom.window.MutationObserver, performance: { now: () => now }, crypto: { randomUUID: () => id },
    setInterval: () => 1, clearInterval() {}, setTimeout: () => 1, clearTimeout() {} };
  const entries = [page(1), page(2)];
  const controller = createQuestionVoiceBar({ env, mount: doc.querySelector('#deck'), takeMount: doc.querySelector('#takes'),
    getStreamId: () => 'fixture:assistant', recorder: {
      async start() {}, async stop() { return { blob: new Blob(['synthetic'], { type: 'audio/mp4' }), durationS: now / 1000 }; }, async discard() {},
    },
    async upload(blob) { calls.upload.push(blob); return { ok: true, blob_sha: sha }; },
    async transcribe(payload) { calls.transcribe.push(payload); if (asrFails) { asrFails = false; throw new Error('backend down'); } return { ok: true, text }; },
    send(payload) { calls.send.push(payload); if (appendFails) { appendFails = false; return ''; } calls.dispatched.push(payload); return 'optimistic-carry'; },
  });
  const update = key => controller.update({ entries, activeKey: key, header: doc.querySelector('header'), stateMount: doc.querySelector('article') });
  update(entries[0].key);
  t.after(async () => { await controller.dispose(); dom.window.close(); });
  return { doc, calls, controller,
    async finishBound() {
      doc.querySelector('[data-question-voice-mic]').click(); await flush();
      now = 4000; update(entries[1].key); now = 8000;
      const selected = controller.snapshot().selected;
      doc.querySelector('[data-question-voice-done]').click(); await flush(); return selected;
    },
    async retry() { doc.querySelector('.voice-pending-retry').click(); await flush(); },
  };
}
test('mobile carrier vector: the web send-leg adapter exposes a labelled eligible mic', async t => {
  const f = fixture(t, 'carry-carrier');
  const mic = f.doc.querySelector('[data-question-voice-mic]');
  assert.equal(mic.hidden, false); assert.equal(mic.disabled, false); assert.ok(mic.getAttribute('aria-label'));
  await f.finishBound(); assert.ok(f.calls.dispatched[0].meta.voice_answers, 'an available carrier actually binds the send');
});
test('mobile bound-send vector: voice and binding use uploaded SHA; dispatch releases registration', async t => {
  const id = 'carry-bound', f = fixture(t, id); const selected = await f.finishBound();
  assert.equal(f.calls.dispatched.length, 1);
  assert.deepEqual(f.calls.dispatched[0], { streamId: 'fixture:assistant', text, meta: {
    voice: { duration_s: 8 }, voice_answers: { version: 1, recording_id: id, blob_sha: sha, duration_s: 8, items: selected },
  } });
  assert.equal(f.calls.upload.length, 1); assert.equal(f.calls.transcribe.length, 1);
  assert.equal(voiceAnswersItemCount(id), 0);
});
test('mobile plain-send vector: ordinary unbound voice omits voice_answers entirely', async () => {
  const sent = [];
  const take = createVoiceTake({ id: 'carry-plain', streamId: 'fixture:assistant', blob: new Blob(['synthetic'], { type: 'audio/mp4' }), durationS: 8 }, {
    upload: async () => ({ ok: true, blob_sha: sha }), transcribe: async () => ({ ok: true, text }),
    send: payload => { sent.push(payload); return 'optimistic-plain'; },
  });
  await take.run();
  assert.equal(sent.length, 1); assert.deepEqual(sent[0].meta, { voice: { duration_s: 8 } });
  assert.equal(Object.hasOwn(sent[0].meta, 'voice_answers'), false);
});
test('mobile stage-1 retry vector: same ASR id/blob/binding, one eventual send, then registration released', async t => {
  const id = 'carry-asr-retry', f = fixture(t, id, { failAsr: true }); const selected = await f.finishBound();
  assert.equal(f.calls.dispatched.length, 0); assert.equal(f.controller.snapshot().phase, 'failed');
  assert.equal(voiceAnswersItemCount(id), 2);
  await f.retry();
  assert.equal(f.calls.upload.length, 1); assert.equal(f.calls.transcribe.length, 2);
  assert.deepEqual(f.calls.transcribe[1], f.calls.transcribe[0]);
  assert.equal(f.calls.transcribe[0].request_id, `transcribe-${id}`);
  assert.equal(f.calls.dispatched.length, 1);
  assert.deepEqual(f.calls.dispatched[0].meta.voice_answers, { version: 1, recording_id: id, blob_sha: sha, duration_s: 8, items: selected });
  assert.equal(voiceAnswersItemCount(id), 0);
});
test('mobile pre-dispatch vector: failed optimistic insertion retains binding and cached transcript for explicit retry', async t => {
  const id = 'carry-append-retry', f = fixture(t, id, { failAppend: true }); await f.finishBound();
  assert.equal(f.calls.dispatched.length, 0); assert.equal(f.controller.snapshot().phase, 'failed');
  assert.equal(voiceAnswersItemCount(id), 2);
  await f.retry();
  assert.equal(f.calls.send.length, 2); assert.deepEqual(f.calls.send[1], f.calls.send[0]);
  assert.equal(f.calls.dispatched.length, 1);
  assert.equal(f.calls.transcribe.length, 1, 'no repeated ASR once a transcript exists');
  assert.equal(f.calls.upload.length, 1); assert.equal(voiceAnswersItemCount(id), 0);
});
test('mobile discard vector: a failed pending take forgets binding and never sends', async t => {
  const id = 'carry-discard', f = fixture(t, id, { failAsr: true }); await f.finishBound();
  assert.equal(voiceAnswersItemCount(id), 2); await f.controller.cancel();
  assert.equal(voiceAnswersItemCount(id), 0); assert.equal(f.calls.send.length, 0);
  assert.equal(f.doc.querySelector('.slot-chat-voice-take').hidden, true);
});
