'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const { createVoiceTake, createBrowserRecorder, bindComposerMic } = require('../renderer/web_voice');
const flush = () => new Promise(resolve => setImmediate(resolve));
function io(overrides = {}) {
  const calls = []; const states = [];
  return { calls, states, onState: s => states.push({ ...s }),
    upload: async blob => { calls.push(['upload', blob.type]); return { ok: true, blob_sha: 'a'.repeat(64) }; },
    transcribe: async p => { calls.push(['transcribe', p]); return { ok: true, text: ' hello ', duration_s: 2 }; },
    send: p => { calls.push(['send', p]); return 'optimistic-1'; }, ...overrides };
}
test('one take uploads, transcribes and dispatches transcript with voice metadata to captured stream', async () => {
  const deps = io(); const take = createVoiceTake({ streamId: 'bart:assistant', blob: new Blob(['audio'], { type: 'audio/wav' }), durationS: 2, id: 'take-1' }, deps);
  await take.run(); await take.run();
  assert.deepEqual(deps.calls.map(c => c[0]), ['upload', 'transcribe', 'send']);
  assert.deepEqual(deps.calls[2][1], { streamId: 'bart:assistant', text: 'hello', meta: { voice: { duration_s: 2 } } });
  assert.equal(deps.states.at(-1).status, 'sent');
});
for (const failure of ['upload', 'backend_unavailable']) test(`${failure} is visible and Retry retains take and transcription identity`, async () => {
  let fail = true; const deps = io();
  const original = failure === 'upload' ? deps.upload : deps.transcribe;
  deps[failure === 'upload' ? 'upload' : 'transcribe'] = async p => fail ? { ok: false, error: failure } : original(p);
  const take = createVoiceTake({ streamId: 'local:seat', blob: new Blob(['x'], { type: 'audio/mp4' }), durationS: 1, id: 'retry-take' }, deps);
  await take.run(); assert.equal(take.snapshot().status, 'failed'); assert.match(take.snapshot().error, failure === 'upload' ? /upload/i : /unavailable/i);
  fail = false; await take.run(); assert.equal(take.snapshot().status, 'sent');
  assert.equal(deps.calls.find(c => c[0] === 'transcribe')[1].request_id, 'transcribe-retry-take');
});
test('empty transcript does not send', async () => {
  const deps = io({ transcribe: async () => ({ ok: true, text: '   ' }) });
  const take = createVoiceTake({ streamId: 'local:seat', blob: new Blob(['x']), durationS: 1, id: 'empty' }, deps);
  await take.run(); assert.equal(take.snapshot().error, 'Nothing was recognized.'); assert.equal(deps.calls.some(c => c[0] === 'send'), false);
});
test('discard during transcription prevents sending', async () => {
  let resolve; const deps = io({ transcribe: () => new Promise(r => { resolve = r; }) });
  const take = createVoiceTake({ streamId: 'local:seat', blob: new Blob(['x']), durationS: 1, id: 'cancel' }, deps);
  const running = take.run(); await flush(); take.discard(); resolve({ ok: true, text: 'late' }); await running;
  assert.equal(deps.calls.some(c => c[0] === 'send'), false);
});
function ui(web, recorder, env = { isSecureContext: true, navigator: { mediaDevices: { getUserMedia() {} } } }) {
  const dom = new JSDOM('<button id="mic"></button><button id="room"></button><div id="panel"></div>');
  let roomClicks = 0; dom.window.document.querySelector('#room').onclick = () => roomClicks++;
  env = { setInterval: () => 1, clearInterval() {}, ...env };
  const deps = io();
  bindComposerMic({ web, env, button: dom.window.document.querySelector('#mic'), panel: dom.window.document.querySelector('#panel'), roomToggle: () => dom.window.document.querySelector('#room').click(), getStreamId: () => 'local:seat', recorder, ...deps });
  return { dom, deps, button: dom.window.document.querySelector('#mic'), panel: dom.window.document.querySelector('#panel'), roomClicks: () => roomClicks };
}
test('web button records and stops; desktop keeps room-mic binding', async () => {
  let starts = 0; let stops = 0;
  const u = ui(true, { start: async () => { starts++; }, stop: async () => { stops++; return { blob: new Blob(['x'], { type: 'audio/mp4' }), durationS: 2 }; }, discard: async () => {} });
  u.button.click(); await flush(); assert.match(u.panel.textContent, /Recording.*0:00/); assert.match(u.panel.textContent, /Cancel/);
  u.button.click(); await flush(); assert.equal(starts, 1); assert.equal(stops, 1); assert.equal(u.roomClicks(), 0); assert.equal(u.deps.calls.filter(c => c[0] === 'send').length, 1);
  const desktop = ui(false); desktop.button.click(); assert.equal(desktop.roomClicks(), 1);
});
test('permission denied exposes Retry and unsupported browser disables button', async () => {
  let denied = true; const u = ui(true, { start: async () => { if (denied) throw Object.assign(new Error('denied'), { name: 'NotAllowedError' }); }, discard: async () => {} });
  u.button.click(); await flush(); assert.match(u.panel.textContent, /permission.*Retry/i); denied = false;
  u.panel.querySelector('button').click(); await flush(); assert.match(u.panel.textContent, /Recording/); await u.panel.querySelector('button').click(); await flush();
  const unsupported = ui(true, null, { navigator: {} }); assert.equal(unsupported.button.disabled, true); assert.match(unsupported.button.title, /unavailable/i);
});
test('recorder selects MP4 and releases tracks', async () => {
  let stopped = 0; class Recorder {
    static isTypeSupported(type) { return type === 'audio/mp4'; }
    start() {} stop() { this.ondataavailable({ data: new Blob(['mp4']) }); this.onstop(); }
  }
  class AudioContext {
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createAnalyser() { return { getFloatTimeDomainData(samples) { samples.fill(0); }, disconnect() {} }; }
    async resume() {} async close() {}
  }
  const recorder = createBrowserRecorder({ AudioContext, MediaRecorder: Recorder, navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop: () => stopped++ }] }) } } });
  await recorder.start(); const take = await recorder.stop(); assert.equal(take.blob.type, 'audio/mp4'); assert.equal(stopped, 1);
});
test('unsupported MP4 captures PCM into valid WAV and cleans up on cancel', async () => {
  let processor; let tracks = 0; let closed = 0;
  class AudioContext {
    sampleRate = 8000; destination = {};
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createAnalyser() { return { getFloatTimeDomainData(samples) { samples.fill(0); }, disconnect() {} }; }
    createScriptProcessor() { processor = { connect() {}, disconnect() {} }; return processor; }
    async resume() {} async close() { closed++; }
  }
  const recorder = createBrowserRecorder({ AudioContext, MediaRecorder: { isTypeSupported: () => false }, navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop: () => tracks++ }] }) } } });
  await recorder.start(); processor.onaudioprocess({ inputBuffer: { getChannelData: () => new Float32Array([0, .5, -1]) } });
  const take = await recorder.stop(); const bytes = new Uint8Array(await take.blob.arrayBuffer());
  assert.equal(take.blob.type, 'audio/wav'); assert.equal(new TextDecoder().decode(bytes.slice(0, 4)), 'RIFF'); assert.equal(bytes.length, 50);
  await recorder.start(); await recorder.discard(); assert.equal(tracks, 2); assert.equal(closed, 2);
});
test('WAV fallback downsamples device PCM so five minutes fits the daemon 16 MiB cap', async () => {
  const { encodeWav } = require('../renderer/web_voice');
  const wav = encodeWav([new Float32Array(48000)], 48000);
  const view = new DataView(await wav.arrayBuffer());
  assert.equal(view.getUint32(24, true), 16000);
  assert.equal(wav.size, 44 + 16000 * 2);
  assert.ok((wav.size - 44) * 300 + 44 < 16 * 1024 * 1024);
});
test('cancel while microphone permission is pending releases late capture and never records a take', async () => {
  let resolve; let discards = 0;
  const u = ui(true, { start: () => new Promise(r => { resolve = r; }), discard: async () => { discards++; } });
  u.button.click(); await flush(); u.panel.querySelector('button').click(); await flush();
  assert.equal(u.button.disabled, true);
  resolve(); await flush(); assert.equal(discards, 1); assert.equal(u.panel.hidden, true); assert.equal(u.button.disabled, false);
  assert.equal(u.deps.calls.length, 0);
});
test('cancel while recorder stop is pending prevents upload and dispatch', async () => {
  let resolve;
  const dom = new JSDOM('<button></button><div></div>'); const deps = io();
  const button = dom.window.document.querySelector('button'); const panel = dom.window.document.querySelector('div');
  const controller = bindComposerMic({ web: true, env: { isSecureContext: true, navigator: { mediaDevices: { getUserMedia() {} } }, setInterval: () => 1, clearInterval() {} }, button, panel, getStreamId: () => 'local:seat', recorder: { start: async () => {}, stop: () => new Promise(r => { resolve = r; }), discard: async () => {} }, ...deps });
  button.click(); await flush(); button.click(); await flush(); await controller.cancel();
  resolve({ blob: new Blob(['late'], { type: 'audio/wav' }), durationS: 1 }); await flush();
  assert.equal(deps.calls.length, 0); assert.equal(panel.hidden, true);
});
