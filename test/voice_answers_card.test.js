'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const { createQuestionVoiceBar } = require('../renderer/question_voice_bar');
const { bindComposerMic } = require('../renderer/web_voice');
const { buildVoiceAnswersMeta, registerVoiceAnswersBinding } = require('../renderer/voice_answers_binding');
const flush = () => new Promise(resolve => setImmediate(resolve));
const page = (id, extra = {}) => ({ source: 'durable', key: `n${id}:0`, model: { prompt: `Question ${id}?` }, notification: { notification_id: `n${id}`, question: { question_id: `q${id}`, producer_stream_id: 'fixture:producer' } }, ...extra });
function fixture(t, { entries = [page(1), page(2), page(3)], isWeb, deny = false, hold = false, startPending = false } = {}) {
  const dom = new JSDOM('<header></header><section id="deck"><article></article></section><div id="takes"></div><button id="mic-btn-toggle"></button><button id="composer"></button><div id="composer-panel"></div>');
  const doc = dom.window.document; let now = 0; let nextId = 0; let startedAt = 0; let tracks = [];
  const timers = new Map(), intervals = new Map(), calls = [], sent = [], asr = [];
  let releaseAsr, releaseStart; let interrupted = false; let currentStream = 'fixture:assistant';
  const env = { HOST: isWeb === undefined ? {} : { isWeb }, isSecureContext: true, navigator: { mediaDevices: { getUserMedia() {} } },
    MutationObserver: dom.window.MutationObserver, performance: { now: () => now }, crypto: { randomUUID: () => 'synthetic-recording' },
    setTimeout(fn, ms) { const id = ++nextId; timers.set(id, { fn, at: now + ms }); return id; }, clearTimeout(id) { timers.delete(id); },
    setInterval(fn) { const id = ++nextId; intervals.set(id, fn); return id; }, clearInterval(id) { intervals.delete(id); } };
  const recorder = {
    async start() { calls.push('start'); if (deny) throw Object.assign(new Error('denied'), { name: 'NotAllowedError' });
      if (startPending) await new Promise(resolve => { releaseStart = resolve; });
      tracks.push({ readyState: 'live' }); startedAt = now;
    },
    poll: () => ({ level: .5, durationMs: now - startedAt, interrupted }),
    async stop() { calls.push('stop'); tracks.forEach(track => { track.readyState = 'ended'; }); return { blob: new Blob(['synthetic'], { type: 'audio/wav' }), durationS: (now - startedAt) / 1000 }; },
    async discard() { calls.push('discard'); tracks.forEach(track => { track.readyState = 'ended'; }); },
  };
  let roomClicks = 0; doc.querySelector('#mic-btn-toggle').onclick = () => roomClicks++;
  let recordingChanged = [];
  const ctrl = createQuestionVoiceBar({ env, mount: doc.querySelector('#deck'), takeMount: doc.querySelector('#takes'), getStreamId: () => currentStream, recorder,
    onRecordingChange: value => recordingChanged.push(value),
    async upload() { calls.push('upload'); return { ok: true, blob_sha: 'a'.repeat(64) }; },
    async transcribe(payload) { asr.push(payload); if (hold) await new Promise(resolve => { releaseAsr = resolve; }); return { ok: true, text: 'Synthetic voice answers' }; },
    send(value) { sent.push(value); return 'optimistic-synthetic'; },
  });
  const update = (pages = entries, key = pages[0]?.key) => { entries = pages; doc.querySelector('article').replaceChildren(); ctrl.update({ entries, activeKey: key, header: doc.querySelector('header'), stateMount: doc.querySelector('article') }); };
  update();
  t.after(async () => { releaseStart?.(); releaseAsr?.(); await ctrl.dispose(); dom.window.close(); });
  return { doc, env, ctrl, recorder, calls, sent, asr, update, tracks, recordingChanged, roomClicks: () => roomClicks,
    mic: () => doc.querySelector('[data-question-voice-mic]'),
    async start() { doc.querySelector('[data-question-voice-mic]').click(); await flush(); },
    async advance(ms) { now += ms; for (const [id, timer] of [...timers]) if (timer.at <= now) { timers.delete(id); timer.fn(); } for (const fn of [...intervals.values()]) fn(); await flush(); },
    releaseAsr: () => { hold = false; releaseAsr?.(); }, releaseStart: () => releaseStart?.(),
    interrupt: () => { interrupted = true; }, target: value => { currentStream = value; },
  };
}
test('mic visibility and legacy labels include unbindable durable pages only in n', async t => {
  const unbindable = page(2, { notification: { notification_id: 'n2', question: {} } });
  const legacy = { source: 'pane', key: 'legacy' };
  const u = fixture(t, { entries: [legacy, unbindable] });
  assert.equal(u.mic().hidden, true); assert.equal(u.doc.querySelector('[data-question-voice-legacy]').textContent, 'ANSWER BY TAP');
  u.update([page(1), unbindable, legacy]); assert.equal(u.mic().hidden, false);
  await u.start(); await u.advance(1500);
  assert.equal(u.doc.querySelector('[data-question-voice-progress]').textContent, '1 of 2 answered by voice');
  assert.equal(u.doc.querySelector('[data-question-voice-progress]').getAttribute('role'), 'status');
});
test('threshold state changes at exactly 1500 ms and Done freezes ordered binding', async t => {
  const u = fixture(t, { hold: true }); await u.start();
  await u.advance(1499); assert.equal(u.doc.querySelector('[data-question-voice-page-state]').textContent, 'RECORDING YOUR ANSWER…');
  await u.advance(1); assert.equal(u.doc.querySelector('[data-question-voice-page-state]').textContent, 'ANSWER RECORDED');
  u.update([page(1), page(2), page(3)], 'n3:0'); await u.advance(1500);
  const selected = u.ctrl.snapshot().selected;
  u.doc.querySelector('[data-question-voice-done]').click(); await flush();
  assert.equal(u.sent.length, 0); assert.equal(u.doc.querySelector('[data-voice-answers-count]').textContent, 'ANSWERS 2 QUESTIONS');
  registerVoiceAnswersBinding('expected', selected);
  const expected = buildVoiceAnswersMeta('expected', { blobSha: 'a'.repeat(64), durationS: 3 }); expected.recording_id = 'synthetic-recording';
  u.releaseAsr(); await flush();
  assert.deepEqual(u.sent[0], { streamId: 'fixture:assistant', text: 'Synthetic voice answers', meta: { voice: { duration_s: 3 }, voice_answers: expected } });
  assert.equal(u.sent.length, 1); assert.ok(u.tracks.every(track => track.readyState === 'ended'));
  assert.equal(u.doc.querySelector('[data-voice-answers-count]'), null);
});
test('Done with zero covered pages silently discards without upload or confirmation', async t => {
  const u = fixture(t); await u.start(); await u.advance(1490); u.doc.querySelector('[data-question-voice-done]').click(); await flush();
  assert.ok(u.calls.includes('discard')); assert.equal(u.calls.includes('upload'), false); assert.equal(u.sent.length, 0);
  assert.equal(u.doc.querySelector('[data-question-voice-confirm]').hidden, true);
});
test('discard asks first; Keep preserves take and held navigation; Discard stops before leaving', async t => {
  const u = fixture(t); await u.start(); await u.advance(1600); let left = 0;
  assert.equal(u.ctrl.requestLeave(() => { assert.ok(u.tracks.every(track => track.readyState === 'ended')); left++; }), true);
  assert.equal(left, 0); u.doc.querySelector('[data-question-voice-keep]').click();
  assert.equal(u.ctrl.snapshot().phase, 'recording'); assert.equal(left, 0); assert.equal(u.calls.includes('discard'), false);
  u.ctrl.requestLeave(() => { assert.ok(u.tracks.every(track => track.readyState === 'ended')); left++; });
  u.doc.querySelector('[data-question-voice-discard]').click(); await flush();
  assert.equal(left, 1); assert.equal(u.calls.includes('upload'), false); assert.equal(u.sent.length, 0);
  assert.equal(u.ctrl.requestLeave(() => left++), false); assert.equal(left, 1, 'no-op when no recorder is live');
});
test('strip X confirms instead of silently invoking the capture unit cancel', async t => {
  const u = fixture(t); await u.start(); u.doc.querySelector('.voice-recording-discard').click(); await flush();
  assert.equal(u.doc.querySelector('[data-question-voice-confirm]').hidden, false); assert.equal(u.calls.includes('discard'), false);
  u.doc.querySelector('[data-question-voice-discard]').click(); await flush(); assert.ok(u.calls.includes('discard'));
});
test('late permission result is cleaned before confirmed navigation occurs', async t => {
  const u = fixture(t, { startPending: true }); await u.start(); let left = false;
  u.ctrl.requestLeave(() => { left = true; assert.ok(u.tracks.every(track => track.readyState === 'ended')); });
  u.doc.querySelector('[data-question-voice-discard]').click(); await flush(); assert.equal(left, false);
  u.releaseStart(); await flush(); assert.equal(left, true); assert.equal(u.sent.length, 0);
});
for (const isWeb of [undefined, true]) test(`deck always captures with HOST.isWeb=${isWeb}, never roomToggle`, async t => {
  const u = fixture(t, { isWeb }); await u.start(); assert.equal(u.calls[0], 'start'); assert.equal(u.roomClicks(), 0);
  assert.equal(u.ctrl.snapshot().phase, 'recording'); assert.ok(u.mic().getAttribute('aria-label'));
});
test('busy composer rejects deck start, preserves composer and never discards it', async t => {
  const u = fixture(t); let composerStarts = 0, composerDiscards = 0;
  const composer = bindComposerMic({ web: true, env: u.env, button: u.doc.querySelector('#composer'), panel: u.doc.querySelector('#composer-panel'), getStreamId: () => 'fixture:assistant',
    recorder: { async start() { composerStarts++; }, async discard() { composerDiscards++; } } });
  t.after(() => composer.cancel()); u.doc.querySelector('#composer').click(); await flush(); await u.start();
  assert.match(u.doc.querySelector('[data-question-voice-bar]').textContent, /Another chat is recording/);
  assert.equal(composerStarts, 1); assert.equal(composerDiscards, 0); assert.equal(u.calls.includes('start'), false);
});
test('busy deck rejects composer start and leaves deck take untouched', async t => {
  const u = fixture(t); await u.start(); let composerStarts = 0;
  const composer = bindComposerMic({ web: true, env: u.env, button: u.doc.querySelector('#composer'), panel: u.doc.querySelector('#composer-panel'), getStreamId: () => 'fixture:assistant', recorder: { async start() { composerStarts++; }, async discard() {} } });
  t.after(() => composer.cancel()); u.doc.querySelector('#composer').click(); await flush();
  assert.match(u.doc.querySelector('#composer-panel').textContent, /Another chat is recording/);
  assert.equal(composerStarts, 0); assert.equal(u.calls.includes('discard'), false); assert.equal(u.ctrl.snapshot().phase, 'recording');
});
test('arrivals stay tap-only and daemon removals drop selection without changing frozen n', async t => {
  const u = fixture(t, { entries: [page(1), page(2)] }); await u.start(); await u.advance(1500);
  u.update([page(1), page(2), page(3)], 'n3:0'); await u.advance(2000);
  assert.equal(u.doc.querySelector('[data-question-voice-page-state]').textContent, 'ANSWER BY TAP');
  u.update([page(2), page(3)], 'n2:0');
  assert.equal(u.doc.querySelector('[data-question-voice-progress]').textContent, '0 of 2 answered by voice');
});
test('denied capture retains existing permission message and starts no send', async t => {
  const u = fixture(t, { deny: true }); await u.start();
  assert.match(u.doc.querySelector('[data-question-voice-bar]').textContent, /Microphone permission denied/); assert.equal(u.sent.length, 0);
});

// Full app adapter coverage: actual createBrowserRecorder with synthetic media,
// no fleet process or physical microphone. Only platform APIs are fake.
const vm = require('node:vm');
const { installRenderer, mountRaceSlot, STREAM } = require('./helpers/renderer_chat');
async function appFixture(t, isWeb) {
  const h = installRenderer({ questionOverride: null }); await flush(); await flush();
  const w = h.dom.window; let now = 0; let starts = 0, roomClicks = 0; const tracks = [];
  Object.defineProperty(w, 'isSecureContext', { value: true });
  Object.defineProperty(w.performance, 'now', { value: () => now });
  if (isWeb !== undefined) w.HOST.isWeb = isWeb;
  const timers = new Map(); let nextId = 0;
  w.setTimeout = (fn, ms) => { const id = ++nextId; timers.set(id, { fn, at: now + ms }); return id; }; w.clearTimeout = id => timers.delete(id);
  w.setInterval = () => ++nextId; w.clearInterval = () => {};
  Object.defineProperty(w.navigator, 'mediaDevices', { value: { getUserMedia: async () => {
    starts++; const track = { readyState: 'live', stop() { this.readyState = 'ended'; }, addEventListener() {}, removeEventListener() {} }; tracks.push(track); return { getTracks: () => [track] };
  } } });
  w.AudioContext = class {
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createAnalyser() { return { getFloatTimeDomainData(samples) { samples.fill(0); }, disconnect() {} }; }
    async resume() {} async close() {}
  };
  w.MediaRecorder = class {
    static isTypeSupported() { return true; }
    start() { this.state = 'recording'; }
    stop() { this.state = 'inactive'; this.ondataavailable({ data: new Blob(['synthetic']) }); this.onstop(); }
  };
  w.document.getElementById('mic-btn-toggle').addEventListener('click', () => roomClicks++);
  mountRaceSlot(h.context);
  for (let i = 1; i <= 3; i++) vm.runInContext(`indexDurableQuestionNotification(${JSON.stringify({ notification_id: `voice-${i}`, producer: 'agent_question.v1', state: 'open', title: `Question ${i}`,
    answer_to_stream_id: STREAM, question: { question_id: `q${i}`, producer_stream_id: STREAM, state: 'open', response_mode: 'single_choice', options: [{ label: 'Keep', value: 'keep' }] } })});`, h.context);
  vm.runInContext('renderSlotChat(0)', h.context); w.document.querySelector('.slot-chat-question-open').click();
  t.after(async () => { await vm.runInContext('state.slotChatRefs[0]?.questionVoiceController?.dispose()', h.context); h.dom.window.close(); });
  return { ...h, tracks, starts: () => starts, roomClicks: () => roomClicks,
    async start() { w.document.querySelector('[data-question-voice-mic]').click(); await flush(); },
    async advance(ms) { now += ms; for (const [id, timer] of [...timers]) if (timer.at <= now) { timers.delete(id); timer.fn(); } await flush(); },
  };
}
for (const isWeb of [undefined, true]) test(`actual app deck uses capture and keeps composer reachable (${isWeb})`, async t => {
  const h = await appFixture(t, isWeb); const doc = h.dom.window.document;
  await h.start(); assert.equal(h.starts(), 1); assert.equal(h.roomClicks(), 0);
  const portal = doc.querySelector('.desktop-question-portal'); assert.equal(portal.getAttribute('aria-modal'), 'false');
  doc.querySelector('.slot-chat-compose-input').focus(); assert.equal(doc.activeElement.className.includes('slot-chat-compose-input'), true);
  await h.advance(1500); assert.equal(doc.querySelector('[data-question-voice-page-state]').textContent, 'ANSWER RECORDED');
  const dotCount = doc.querySelectorAll('.desktop-question-dot').length;
  portal.dispatchEvent(new h.dom.window.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
  assert.equal(doc.querySelector('[data-notification-id]').dataset.notificationId, 'voice-2');
  assert.equal(doc.querySelectorAll('.desktop-question-dot').length, dotCount);
  doc.querySelector('.desktop-question-portal').dispatchEvent(new h.dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
  assert.equal(doc.querySelector('[data-question-voice-confirm]').hidden, false);
  doc.querySelector('[data-question-voice-keep]').click(); assert.ok(h.tracks.every(track => track.readyState === 'live'));
});
for (const action of ['view', 'detach', 'attach']) test(`actual ${action} admission keeps state until deck discard, then stops tracks`, async t => {
  const h = await appFixture(t); const doc = h.dom.window.document; await h.start();
  const before = vm.runInContext('JSON.stringify({slot:state.slots[0],view:state.slotViewModes[0],gen:state.slotGen[0]})', h.context);
  const expression = action === 'view' ? "updateSlotViewMode(0,'terminal')" : action === 'detach' ? 'detachSlot(0)' : "attachSession(0,'replacement','Replacement','local',{laneHistory:{streamId:'fixture:history'}})";
  vm.runInContext(expression, h.context); await flush();
  assert.equal(vm.runInContext('JSON.stringify({slot:state.slots[0],view:state.slotViewModes[0],gen:state.slotGen[0]})', h.context), before);
  doc.querySelector('[data-question-voice-keep]').click(); assert.equal(h.tracks[0].readyState, 'live');
  vm.runInContext(expression, h.context); doc.querySelector('[data-question-voice-discard]').click(); await flush();
  assert.equal(h.tracks[0].readyState, 'ended');
  if (action === 'view') assert.equal(vm.runInContext('state.slotViewModes[0]', h.context), 'terminal');
  if (action === 'detach') assert.equal(vm.runInContext('state.slots[0]', h.context), null);
  if (action === 'attach') assert.equal(vm.runInContext('state.slots[0].name', h.context), 'replacement');
  assert.equal(h.sendCalls.length, 0); assert.equal(h.notificationResolveCalls.length, 0);
});
test('actual Done/upload/ASR never closes a durable question or changes its count', async t => {
  const h = await appFixture(t, true), w = h.dom.window, doc = w.document;
  let release, sends = []; let uploads = 0;
  w.cc.chatUploadBlob = async () => { uploads++; return { ok: true, blob_sha: 'b'.repeat(64) }; };
  w.cc.chatTranscribeBlob = () => new Promise(resolve => { release = resolve; });
  w.PentacleChatStore.sendTurn = (streamId, text, attachments, reply) => { sends.push({ streamId, text, attachments, reply }); return 'optimistic-answer'; };
  const count = () => vm.runInContext(`getOpenQuestionsForStream(${JSON.stringify(STREAM)}).length`, h.context);
  assert.equal(count(), 3); await h.start(); await h.advance(1500);
  doc.querySelector('[data-question-voice-done]').click(); await flush();
  assert.equal(uploads, 1); assert.equal(count(), 3); assert.equal(sends.length, 0);
  release({ ok: true, text: 'Synthetic answer' }); await flush();
  assert.equal(sends.length, 1); assert.equal(count(), 3); assert.equal(h.notificationResolveCalls.length, 0);
  assert.equal(sends[0].reply.meta.voice_answers.items.length, 1);
  assert.equal(doc.querySelector('.slot-chat-question-count').textContent, '3');
  doc.querySelector('.slot-chat-question-open').click();
  assert.equal(doc.querySelectorAll('.desktop-question-dot').length, 3);
  assert.equal(doc.querySelectorAll('.desktop-question-dot.is-answered').length, 0);
});
test('card mic retains native Space/Enter semantics and labelled Done; Escape is only the discard confirmation', async t => {
  const h = await appFixture(t); const doc = h.dom.window.document; const mic = doc.querySelector('[data-question-voice-mic]');
  assert.equal(mic.tagName, 'BUTTON'); assert.equal(mic.type, 'button');
  for (const key of [' ', 'Enter']) {
    const event = new h.dom.window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }); mic.dispatchEvent(event);
    assert.equal(event.defaultPrevented, false, 'the browser keeps its native button activation');
  }
  await h.start(); assert.ok(doc.querySelector('[data-question-voice-done]').getAttribute('aria-label'));
  const escape = new h.dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }); mic.dispatchEvent(escape);
  assert.equal(escape.defaultPrevented, true); assert.equal(doc.querySelector('[data-question-voice-confirm]').hidden, false);
  assert.equal(h.tracks[0].readyState, 'live');
});
for (const elapsed of [1000, 1600]) test(`recorder interruption freezes eligible pages or discards empty take (${elapsed} ms)`, async t => {
  const u = fixture(t); await u.start(); u.interrupt(); await u.advance(elapsed); await flush();
  assert.equal(u.sent.length, elapsed >= 1500 ? 1 : 0);
  assert.equal(u.calls.includes('upload'), elapsed >= 1500);
  assert.ok(u.tracks.every(track => track.readyState === 'ended'));
  if (u.sent.length) assert.equal(u.sent[0].meta.voice_answers.items.length, 1);
});
for (const next of [null, 'fixture:changed-target']) test(`binding keeps captured surface when live target becomes ${next}`, async t => {
  const u = fixture(t); await u.start(); await u.advance(1600); u.target(next);
  u.doc.querySelector('[data-question-voice-done]').click(); await flush();
  assert.equal(u.sent.length, 1); assert.equal(u.sent[0].streamId, 'fixture:assistant');
  assert.equal(u.sent[0].meta.voice_answers.items[0].surface_stream_id, 'fixture:assistant');
});
test('Escape from reachable typed composer confirms deck discard and never interrupts a turn', async t => {
  const h = await appFixture(t); const doc = h.dom.window.document; await h.start(); let interrupts = 0;
  h.dom.window.PentacleChatStore.cancelCurrentTurn = () => interrupts++;
  const input = doc.querySelector('.slot-chat-compose-input'); input.focus();
  input.dispatchEvent(new h.dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
  assert.equal(doc.querySelector('[data-question-voice-confirm]').hidden, false); assert.equal(interrupts, 0); assert.equal(h.tracks[0].readyState, 'live');
});
test('empty live deck tolerates pager arrows and Done discards after all daemon closures', async t => {
  const h = await appFixture(t); const doc = h.dom.window.document; await h.start(); await h.advance(1600);
  vm.runInContext(`for(let i=1;i<=3;i++) indexDurableQuestionNotification({ notification_id:'voice-'+i, producer:'agent_question.v1', state:'answered', question:{state:'answered'}}); renderSlotChat(0);`, h.context);
  const portal = doc.querySelector('.desktop-question-portal'); assert.ok(portal);
  portal.dispatchEvent(new h.dom.window.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, cancelable: true }));
  assert.equal(doc.querySelector('[data-question-voice-progress]').textContent, '0 of 3 answered by voice');
  doc.querySelector('[data-question-voice-done]').click(); await flush();
  assert.equal(h.tracks[0].readyState, 'ended'); assert.equal(h.sendCalls.length, 0);
});
for (const interrupted of [false, true]) test(`finishing capture clears pending discard confirmation and held navigation (${interrupted})`, async t => {
  const u = fixture(t); await u.start(); await u.advance(1600); let departures = 0;
  u.ctrl.requestLeave(() => departures++);
  if (interrupted) { u.interrupt(); await u.advance(10); } else u.doc.querySelector('[data-question-voice-done]').click();
  await flush(); u.update();
  assert.equal(u.doc.querySelector('[data-question-voice-confirm]').hidden, true);
  assert.equal(departures, 0); assert.equal(u.sent.length, 1);
});
test('USER plain-voice control invokes conversion only on explicit click and is removed on slot teardown', async t => {
  const h = await appFixture(t); const doc = h.dom.window.document; const calls = [];
  h.dom.window.PentacleChatStore.sendVoiceAsPlainNote = id => { calls.push(id); return true; };
  const button = doc.createElement('button'); button.type = 'button'; button.dataset.questionVoicePlain = '';
  button.dataset.optimisticId = 'optimistic-refused'; button.textContent = 'Send as plain voice note';
  doc.querySelector('#cell-0 .slot-chat-list').appendChild(button);
  await flush(); assert.deepEqual(calls, [], 'neither render nor a timer converts automatically');
  doc.querySelector('#cell-0 .slot-chat-list').appendChild(button);
  assert.equal(button.isConnected, true);
  button.click(); button.click(); assert.deepEqual(calls, ['optimistic-refused']); assert.equal(button.disabled, true);
  await vm.runInContext('state.slotChatRefs[0].questionVoiceController.dispose()', h.context);
  button.disabled = false; button.click(); assert.deepEqual(calls, ['optimistic-refused'], 'disposed slot no longer handles its old transcript');
  assert.equal(h.sendCalls.length, 0); assert.equal(h.notificationResolveCalls.length, 0);
});
