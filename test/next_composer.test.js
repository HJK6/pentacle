'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const {
  mountComposer,
  VOICE_SAMPLE_MS,
  VOICE_VISIBLE_BARS,
  VOICE_BUBBLE_BARS,
} = require('../renderer/next/composer');

const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture(t, { streamId = 'fixture:chat', upload, sessions } = {}) {
  const dom = new JSDOM('<!doctype html><body><div id="host"></div><div id="thread"></div></body>');
  const document = dom.window.document;
  const timers = new Map();
  const calls = [];
  const drafts = new Map();
  const urls = [];
  const urlApi = {
    createObjectURL() { const url = `blob:fixture-${urls.length + 1}`; urls.push(url); return url; },
    revokeObjectURL() {},
  };
  let now = 0;
  let level = 0;
  let durationMs = 0;
  let timerId = 0;
  let takeId = 0;
  const env = {
    isSecureContext: true,
    navigator: { mediaDevices: { getUserMedia: async () => ({}) } },
    performance: { now: () => now },
    crypto: { randomUUID: () => `fixture-take-${++takeId}` },
    FileReader: dom.window.FileReader,
    Image: dom.window.Image,
    URL: urlApi,
    PentacleChatCore: { MAX_CHAT_ATTACHMENTS: 5 },
    setInterval(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearInterval(id) { timers.delete(id); },
  };
  const chatStore = {
    getState: () => ({ sessions: sessions || [{ stream_id: streamId, session_kind: 'agent' }] }),
    sendTurn(...args) { calls.push({ type: 'sendTurn', args }); return `optimistic-${calls.length}`; },
  };
  const cc = {
    async chatUploadBlob(payload) {
      calls.push({ type: 'chatUploadBlob', payload });
      return upload ? upload(payload) : { ok: true, blob_sha: 'a'.repeat(64) };
    },
    async chatTranscribeBlob(payload) {
      calls.push({ type: 'chatTranscribeBlob', payload });
      return { ok: true, text: 'recognized words' };
    },
  };
  const recorder = {
    interrupted: null,
    async start() { calls.push({ type: 'recorder.start' }); },
    poll() { calls.push({ type: 'recorder.poll' }); return { level, durationMs }; },
    async stop() {
      calls.push({ type: 'recorder.stop' });
      return { blob: new Blob(['synthetic audio'], { type: 'audio/wav' }), durationS: durationMs / 1000 };
    },
    async discard() { calls.push({ type: 'recorder.discard' }); },
    setInterruptionHandler(handler) { this.interrupted = handler; },
  };
  const controller = mountComposer({
    host: document.querySelector('#host'), transcript: document.querySelector('#thread'),
    streamId, drafts, cc, chatStore, env, recorder,
  });
  t.after(async () => { await controller.dispose(); dom.window.close(); });
  return {
    dom, document, env, timers, calls, drafts, urls, controller, cc, chatStore, recorder,
    setSample(nextLevel, elapsedMs) { level = nextLevel; durationMs = elapsedMs; now = elapsedMs; },
    tick() { for (const timer of [...timers.values()]) timer.fn(); },
    get input() { return document.querySelector('[data-next-message]'); },
    get mic() { return document.querySelector('[data-next-mic]'); },
    get send() { return document.querySelector('[data-next-send]'); },
    get panel() { return document.querySelector('[data-next-voice-panel]'); },
    get thread() { return document.querySelector('#thread'); },
  };
}

async function startRecording(u) {
  u.mic.click();
  await flush();
  await flush();
}

async function waitFor(predicate, description) {
  for (let index = 0; index < 50; index++) {
    if (predicate()) return;
    await flush();
  }
  assert.fail(`Timed out waiting for ${description}`);
}

test('U3: mic and send swap with text, Enter sends, and Shift+Enter keeps the line break path', async t => {
  const u = fixture(t);
  assert.equal(u.mic.hidden, false);
  assert.equal(u.send.hidden, true);
  assert.ok(u.mic.querySelector('svg rect'), 'microphone control uses a microphone glyph');
  assert.equal(u.send.querySelector('svg path')?.getAttribute('d'), 'M12 19V5M6 11l6-6 6 6');

  u.input.value = 'hello';
  u.input.dispatchEvent(new u.dom.window.Event('input', { bubbles: true }));
  assert.equal(u.mic.hidden, true);
  assert.equal(u.send.hidden, false);

  const shifted = new u.dom.window.KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true });
  u.input.dispatchEvent(shifted);
  assert.equal(shifted.defaultPrevented, false);
  assert.equal(u.calls.filter(call => call.type === 'sendTurn').length, 0);

  const entered = new u.dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
  u.input.dispatchEvent(entered);
  assert.equal(entered.defaultPrevented, true);
  await waitFor(() => u.calls.some(call => call.type === 'sendTurn'), 'the shared store send');
  assert.equal(u.calls.find(call => call.type === 'sendTurn').args[1], 'hello');
  assert.equal(u.input.value, '');
  assert.equal(u.mic.hidden, false);
  assert.equal(u.send.hidden, true);
});

test('U3: textarea growth stops at 160 px and scrolls after the cap', t => {
  const u = fixture(t);
  let measured = 240;
  Object.defineProperty(u.input, 'scrollHeight', { configurable: true, get: () => measured });
  u.input.value = 'a long message';
  u.input.dispatchEvent(new u.dom.window.Event('input', { bubbles: true }));
  assert.equal(u.input.style.height, '160px');
  assert.equal(u.input.style.overflowY, 'auto');

  measured = 40;
  u.input.dispatchEvent(new u.dom.window.Event('input', { bubbles: true }));
  assert.equal(u.input.style.height, '40px');
  assert.equal(u.input.style.overflowY, 'hidden');
});

test('U3: drafts are keyed by chat stream and survive composer disposal', async t => {
  const u = fixture(t);
  u.input.value = 'chat A draft';
  u.input.dispatchEvent(new u.dom.window.Event('input', { bubbles: true }));
  await u.controller.dispose();

  const host = u.document.querySelector('#host');
  const reopened = mountComposer({
    host, transcript: u.thread, streamId: 'fixture:chat', drafts: u.drafts,
    cc: u.cc, chatStore: u.chatStore, env: u.env, recorder: u.recorder,
  });
  assert.equal(host.querySelector('[data-next-message]').value, 'chat A draft');
  await reopened.dispose();

  const other = mountComposer({
    host, transcript: u.thread, streamId: 'fixture:other', drafts: u.drafts,
    cc: u.cc, chatStore: u.chatStore, env: u.env, recorder: u.recorder,
  });
  assert.equal(host.querySelector('[data-next-message]').value, '');
  await other.dispose();
});

test('Target State 6: picker keeps the current JPEG/PNG contract and sends through the bridge and shared store', async t => {
  const u = fixture(t);
  const fileInput = u.document.querySelector('[data-next-file-input]');
  assert.equal(fileInput.accept, 'image/jpeg,image/png');
  assert.equal(fileInput.multiple, true);

  u.env.Image = class FakeImage {
    naturalWidth = 16;
    naturalHeight = 12;
    set src(_value) { queueMicrotask(() => this.onload()); }
  };
  const image = new u.dom.window.File([new Uint8Array([1, 2, 3, 4])], 'fixture.png', { type: 'image/png' });
  Object.defineProperty(fileInput, 'files', { configurable: true, value: [image] });
  fileInput.dispatchEvent(new u.dom.window.Event('change', { bubbles: true }));
  await waitFor(() => u.document.querySelector('[data-attachment-id]'), 'the image draft chip');
  assert.equal(u.send.hidden, false);

  u.send.click();
  await waitFor(() => u.calls.some(call => call.type === 'sendTurn'), 'the shared store attachment send');
  const upload = u.calls.find(call => call.type === 'chatUploadBlob');
  assert.equal(upload.payload.sizeHintBytes, image.size);
  const sent = u.calls.find(call => call.type === 'sendTurn').args;
  assert.equal(sent[1], '');
  assert.equal(sent[2][0].mime, 'image/png');
  assert.equal(sent[2][0].key, 'a'.repeat(64));
});

test('clipboard images use the ordinary attachment path without cancelling native text paste', async t => {
  const u = fixture(t);
  u.env.Image = class FakeImage {
    naturalWidth = 16;
    naturalHeight = 12;
    set src(_value) { queueMicrotask(() => this.onload()); }
  };
  const image = new u.dom.window.File([new Uint8Array([1, 2, 3, 4])], 'clipboard.png', { type: 'image/png' });
  const paste = clipboardData => {
    const event = new u.dom.window.Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', { value: clipboardData });
    u.input.dispatchEvent(event);
    assert.equal(event.defaultPrevented, false);
  };

  paste({ items: [{ kind: 'string', type: 'text/plain' }], files: [] });
  assert.equal(u.send.hidden, true);
  paste({ items: [{ kind: 'file', type: image.type, getAsFile: () => image }], files: [image] });
  const chips = () => u.document.querySelectorAll('.next-composer-attachment');
  await waitFor(() => chips().length === 1, 'the pasted image draft chip');
  u.send.click();
  await waitFor(() => u.calls.some(call => call.type === 'sendTurn'), 'the pasted image send');
  assert.equal(u.calls.filter(call => call.type === 'chatUploadBlob').length, 1);
  assert.equal(u.calls.find(call => call.type === 'sendTurn').args[2][0].name, 'clipboard.png');
  await waitFor(() => chips().length === 0, 'the sent draft to clear');

  paste({ items: [{ kind: 'file', type: image.type, getAsFile: () => null }], files: [image] });
  await waitFor(() => chips().length === 1, 'the clipboard FileList fallback');
});

test('Target State 6: attachment validation rejects unsupported, empty, oversized and undecodable images', async t => {
  const u = fixture(t);
  const fileInput = u.document.querySelector('[data-next-file-input]');
  const select = async file => {
    Object.defineProperty(fileInput, 'files', { configurable: true, value: [file] });
    fileInput.dispatchEvent(new u.dom.window.Event('change', { bubbles: true }));
    await flush();
    await flush();
  };

  await select({ type: 'application/pdf', size: 10, name: 'report.pdf' });
  assert.match(u.document.querySelector('.next-composer-error').textContent, /Only JPEG and PNG/);
  await select({ type: 'image/png', size: 0, name: 'empty.png' });
  assert.match(u.document.querySelector('.next-composer-error').textContent, /Empty images/);
  await select({ type: 'image/png', size: 25 * 1024 * 1024 + 1, name: 'large.png' });
  assert.match(u.document.querySelector('.next-composer-error').textContent, /25 MiB/);

  u.env.Image = class InvalidImage {
    set src(_value) { queueMicrotask(() => this.onerror()); }
  };
  await select(new u.dom.window.File([new Uint8Array([1, 2, 3])], 'bad.png', { type: 'image/png' }));
  assert.match(u.document.querySelector('.next-composer-error').textContent, /could not be decoded/);
  assert.equal(u.document.querySelector('[data-attachment-id]'), null);
  assert.equal(u.calls.some(call => call.type === 'chatUploadBlob'), false);
});

test('U4: recorder samples once per 70 ms, shows the newest 110 bars, fades older bars and updates the clock', async t => {
  const u = fixture(t);
  await startRecording(u);
  assert.deepEqual([...u.timers.values()].map(timer => timer.ms), [VOICE_SAMPLE_MS]);
  assert.equal(VOICE_SAMPLE_MS, 70);

  for (let index = 0; index < 120; index++) {
    u.setSample(index / 119, (index + 1) * VOICE_SAMPLE_MS);
    u.tick();
  }
  let bars = [...u.panel.querySelectorAll('[data-next-voice-bar]')];
  assert.equal(bars.length, VOICE_VISIBLE_BARS);
  assert.equal(bars[0].dataset.sample, '10');
  assert.equal(bars.at(-1).dataset.sample, '119');
  assert.ok(Number(bars[0].style.opacity) < Number(bars.at(-1).style.opacity));

  u.setSample(0.7, 61_000);
  u.tick();
  assert.equal(u.panel.querySelector('[data-next-voice-time]').textContent, '1:01');
  bars = [...u.panel.querySelectorAll('[data-next-voice-bar]')];
  assert.equal(bars.length, VOICE_VISIBLE_BARS);
});

test('U4: sent take shows a Sending bubble with 22 reduced bars and its duration before transcript send', async t => {
  let releaseUpload;
  const uploadGate = new Promise(resolve => { releaseUpload = resolve; });
  const u = fixture(t, { upload: () => uploadGate });
  await startRecording(u);
  for (let index = 0; index < 44; index++) {
    u.setSample(index % 2 ? 1 : 0.1, (index + 1) * VOICE_SAMPLE_MS);
    u.tick();
  }
  u.panel.querySelector('[data-next-voice-send]').click();
  await waitFor(() => u.thread.querySelector('[data-next-voice-pending]'), 'the pending voice bubble');
  const pending = u.thread.querySelector('[data-next-voice-pending]');
  assert.equal(pending.querySelector('[data-next-voice-status]').textContent, 'Sending');
  assert.equal(pending.querySelectorAll('[data-next-voice-bar]').length, VOICE_BUBBLE_BARS);
  assert.equal(pending.querySelector('[data-next-voice-duration]').textContent, '0:03');
  assert.equal(VOICE_BUBBLE_BARS, 22);
  assert.equal(u.calls.some(call => call.type === 'sendTurn'), false);

  releaseUpload({ ok: true, blob_sha: 'b'.repeat(64) });
  await waitFor(() => u.calls.some(call => call.type === 'sendTurn'), 'the transcribed shared-store send');
  assert.equal(u.calls.find(call => call.type === 'sendTurn').args[1], 'recognized words');
  assert.equal(u.calls.find(call => call.type === 'sendTurn').args[3].meta.voice.duration_s, 3.08);
  assert.equal(u.thread.querySelector('[data-next-voice-pending]'), null);
});

for (const [name, action] of [
  ['pane switch', u => u.controller.cancelRecording()],
  ['terminal toggle', u => u.controller.cancelRecording()],
  ['close', u => u.controller.dispose()],
]) {
  test(`U4: ${name} discards the active capture without upload or send`, async t => {
    const u = fixture(t);
    await startRecording(u);
    assert.equal(u.timers.size, 1);
    await action(u);
    await flush();
    assert.equal(u.calls.filter(call => call.type === 'recorder.discard').length, 1);
    assert.equal(u.calls.some(call => call.type === 'chatUploadBlob'), false);
    assert.equal(u.calls.some(call => call.type === 'sendTurn'), false);
    assert.equal(u.timers.size, 0);
  });
}

test('U4: recording cancel button discards the take', async t => {
  const u = fixture(t);
  await startRecording(u);
  u.panel.querySelector('[data-next-voice-cancel]').click();
  await flush();
  assert.equal(u.calls.filter(call => call.type === 'recorder.discard').length, 1);
  assert.equal(u.calls.some(call => call.type === 'chatUploadBlob'), false);
  assert.equal(u.panel.hidden, true);
  assert.equal(u.timers.size, 0);
});

test('fluid voice: recording in another pane discards the first take and starts only after its microphone is released', async t => {
  const first = fixture(t, { streamId: 'fixture:first' });
  const second = fixture(t, { streamId: 'fixture:second' });
  let release;
  const originalDiscard = first.recorder.discard;
  first.recorder.discard = () => new Promise(resolve => { release = resolve; });
  await startRecording(first);
  assert.equal(first.controller.snapshot().voicePhase, 'recording');
  second.mic.click();
  await flush();
  first.recorder.discard = originalDiscard;
  if (release) release();
  await flush();
  assert.equal(first.controller.snapshot().voicePhase, 'idle', 'the other pane actually stopped');
  assert.equal(second.controller.snapshot().voicePhase, 'recording');
  assert.equal(first.calls.some(c => c.type === 'sendTurn'), false);
  await second.controller.cancelRecording();
});

test('fluid voice: a pending permission request is retired before the next pane acquires', async t => {
  const first = fixture(t, { streamId: 'fixture:pending' });
  const second = fixture(t, { streamId: 'fixture:next' });
  let finishStart;
  first.recorder.start = () => new Promise(resolve => { finishStart = resolve; });
  first.mic.click();
  assert.equal(first.controller.snapshot().voicePhase, 'starting');
  second.mic.click();
  await flush();
  assert.equal(second.calls.some(c => c.type === 'recorder.start'), false);
  finishStart();
  await flush();
  assert.equal(first.controller.snapshot().voicePhase, 'idle');
  assert.equal(second.controller.snapshot().voicePhase, 'recording');
  await second.controller.cancelRecording();
});
