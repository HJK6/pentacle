'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { JSDOM } = require('jsdom');
const { readQuestionVoiceDom, recordingChecks, pendingChecks, boundChecks, refusalChecks, plainChecks, cleanupChecks, environmentChecks } = require('./e2e/lib/voice_answers_scenario');

function domFixture() {
  const dom = new JSDOM(`<div class="session-item" data-stream-id="local:assistant"><span class="s-question-badge" aria-label="3 open questions">?3</span></div>
    <div class="slot-chat-question"><div class="desktop-question-dots"><button class="desktop-question-dot"></button><button class="desktop-question-dot"></button><button class="desktop-question-dot"></button></div>
    <div data-question-voice-bar role="status">2 of 3 answered by voice<span class="voice-recording-timer">0:04</span><i class="voice-recording-bar"></i><button data-question-voice-done aria-label="Done">Done</button></div>
    <div class="slot-chat-question-card" data-notification-id="n3"><button data-question-voice-mic aria-label="Record answers"></button><span data-question-voice-page-state>ANSWER RECORDED</span></div></div>
    <div class="slot-chat-scroll"><div data-voice-answers-count>ANSWERS 2 QUESTIONS</div><div data-voice-answers-status>Couldn't attach questions</div><button>Send as plain voice note</button></div>`);
  for (const node of dom.window.document.querySelectorAll('*')) node.getBoundingClientRect = () => ({ width: 80, height: 25 });
  return dom;
}
function complete() {
  const ui = { mic: { visible: true, label: 'Record answers', disabled: false }, bar: { visible: true, text: '2 of 3 answered by voice', role: 'status', timer: '0:04', meterBars: 3, doneLabel: 'Done' }, pageState: 'ANSWER RECORDED', pending: { visible: true, text: 'ANSWERS 2 QUESTIONS', inTranscript: true }, status: "Couldn't attach questions", plain: { visible: true, disabled: false }, badge: { text: '?3', label: '3 open questions' }, dots: [false, false, false], cardIds: ['n3'] };
  const item = (n, start) => ({ key: `k${n}`, question_id: `q${n}`, notification_id: `n${n}`, producer_stream_id: 'local:producer', surface_stream_id: 'local:assistant', prompt: `Question ${n}`, segment: { start_s: start, end_s: start + 1.8 } });
  const event = { daemon_seq: 101, kind: 'USER', stream_id: 'local:assistant', text: 'Synthetic bound take', meta: { voice: { duration_s: 4.2 }, voice_answers: { version: 1, recording_id: 'take-1', blob_sha: 'a'.repeat(64), duration_s: 4.2, items: [item(1, 0), item(3, 2.3)] }, voice_answers_status: { state: 'bound', stale_keys: [] } } };
  return { ui, recording: { first: { ...ui, bar: { ...ui.bar, text: '1 of 3 answered by voice' } }, middle: { ...ui, bar: { ...ui.bar, text: '1 of 3 answered by voice' }, pageState: 'RECORDING YOUR ANSWER…' }, last: ui }, pending: { ui, events: [], text: event.text }, bound: { events: [event], text: event.text, ids: ['q1', 'q3'], notificationIds: ['n1', 'n3'], producer: 'local:producer', surface: 'local:assistant', blobSha: 'a'.repeat(64), before: ui, after: ui, states: ['open', 'open', 'open'] }, refusal: { before: [event], after: [event], text: 'Synthetic refused take', failure: 'voice_answers_invalid', ui }, plain: { before: [event], after: [event, { kind: 'USER', text: 'Synthetic refused take', meta: { voice: { duration_s: 2 } } }], text: 'Synthetic refused take' }, cleanup: { uploadsBefore: 2, uploadsAfter: 2, tracks: ['ended'], transcribedBefore: 2, transcribedAfter: 2, eventsBefore: 2, eventsAfter: 2 }, environment: { origin: 'http://127.0.0.1:1234', responseUrl: 'http://127.0.0.1:1234/', status: 200, headers: { 'content-type': 'text/html' }, permission: 'granted', secure: true } };
}
const groups = { recording: recordingChecks, pending: pendingChecks, bound: boundChecks, refusal: refusalChecks, plain: plainChecks, cleanup: cleanupChecks, environment: environmentChecks };
test('M3 every complete synthetic observation satisfies the shared gate predicates', () => {
  const o = complete();
  for (const [group, checks] of Object.entries(groups)) for (const [name, pass] of checks(o[group])) assert.equal(pass, true, `${group}: ${name}`);
});
const negatives = [
  ['recording', 0, 'missing visible card mic', o => { o.first.mic = { visible: false }; }],
  ['recording', 1, 'first page not covered', o => { o.first.pageState = 'RECORDING YOUR ANSWER…'; }],
  ['recording', 2, 'brief page falsely covered', o => { o.middle.pageState = 'ANSWER RECORDED'; }],
  ['recording', 3, 'wrong final covered count', o => { o.last.bar = { ...o.last.bar, text: '3 of 3 answered by voice' }; }],
  ['recording', 4, 'missing status role', o => { o.last.bar = { ...o.last.bar, role: '' }; }],
  ['recording', 4, 'missing meter', o => { o.last.bar = { ...o.last.bar, meterBars: 0 }; }],
  ['pending', 0, 'missing count bubble', o => { o.ui.pending.visible = false; }],
  ['pending', 0, 'pending bubble outside transcript', o => { o.ui.pending.inTranscript = false; }],
  ['pending', 1, 'send before ASR release', o => { o.events = [{ kind: 'USER', text: o.text }]; }],
  ['bound', 0, 'optimistic row mistaken for daemon echo', o => { o.events[0].daemon_seq = -1; }],
  ['bound', 0, 'missing daemon USER', o => { o.events = []; }],
  ['bound', 0, 'duplicate USER', o => { o.events.push(structuredClone(o.events[0])); }],
  ['bound', 1, 'missing voice duration', o => { delete o.events[0].meta.voice; }],
  ['bound', 2, 'middle page included', o => { o.events[0].meta.voice_answers.items[1].question_id = 'q2'; }],
  ['bound', 2, 'surface mismatch', o => { o.events[0].meta.voice_answers.items[1].surface_stream_id = 'local:other'; }],
  ['bound', 2, 'notification mismatch', o => { o.events[0].meta.voice_answers.items[1].notification_id = 'n2'; }],
  ['bound', 2, 'ineligible short segment', o => { o.events[0].meta.voice_answers.items[0].segment.end_s = 1.49; }],
  ['bound', 2, 'wrong blob', o => { o.events[0].meta.voice_answers.blob_sha = 'b'.repeat(64); }],
  ['bound', 3, 'daemon binding dropped', o => { o.events[0].meta.voice_answers_status.state = 'dropped'; }],
  ['bound', 3, 'stale bound item', o => { o.events[0].meta.voice_answers_status.stale_keys = ['k1']; }],
  ['bound', 4, 'premature question close', o => { o.states[0] = 'answered'; }],
  ['bound', 4, 'premature sidebar count', o => { o.after = { ...o.after, badge: { text: '?2', label: '2 open questions' } }; }],
  ['bound', 4, 'premature answered pager dot', o => { o.after = { ...o.after, dots: [true, false, false] }; }],
  ['refusal', 0, 'wrong typed refusal', o => { o.failure = 'send_error'; }],
  ['refusal', 1, 'missing refusal note', o => { o.ui.status = ''; }],
  ['refusal', 1, 'missing explicit plain action', o => { o.ui.plain.visible = false; }],
  ['refusal', 2, 'silent send after refusal', o => { o.after.push({ kind: 'USER', text: o.text }); }],
  ['plain', 0, 'no plain send', o => { o.after = o.before; }],
  ['plain', 0, 'duplicate plain send', o => { o.after.push(structuredClone(o.after[1])); }],
  ['plain', 1, 'plain conversion keeps binding', o => { o.after[1].meta.voice_answers = {}; }],
  ['plain', 1, 'plain conversion loses duration', o => { delete o.after[1].meta.voice; }],
  ['cleanup', 0, 'vacuous absent tracks', o => { o.tracks = []; }],
  ['cleanup', 0, 'live track after discard', o => { o.tracks = ['live']; }],
  ['cleanup', 1, 'upload after discard', o => { o.uploadsAfter++; }],
  ['cleanup', 1, 'transcription after discard', o => { o.transcribedAfter++; }],
  ['cleanup', 1, 'send after discard', o => { o.eventsAfter++; }],
  ['environment', 0, 'external origin', o => { o.origin = 'https://example.com'; }],
  ['environment', 0, 'unobserved response headers', o => { o.headers = null; }],
  ['environment', 0, 'wrong response origin', o => { o.responseUrl = 'http://127.0.0.1:9999/'; }],
  ['environment', 1, 'unobserved microphone permission', o => { o.permission = null; }],
];
for (const [group, index, label, mutate] of negatives) test(`M3 oracle rejects ${label}`, () => {
  const o = structuredClone(complete()[group]); mutate(o);
  assert.equal(!!groups[group](o)[index][1], false);
});

test('M3 browser and unit use the same DOM reader for selectors, state, badges and pending rows', () => {
  const dom = domFixture();
  try {
    const o = readQuestionVoiceDom(dom.window.document, 'local:assistant');
    assert.equal(o.mic.visible, true); assert.equal(o.bar.role, 'status'); assert.equal(o.bar.meterBars, 1);
    assert.equal(o.bar.timer, '0:04'); assert.equal(o.pageState, 'ANSWER RECORDED');
    assert.deepEqual(o.badge, { text: '?3', label: '3 open questions' }); assert.deepEqual(o.dots, [false, false, false]);
    assert.equal(o.pending.inTranscript, true); assert.equal(o.status, "Couldn't attach questions"); assert.equal(o.plain.visible, true);
    dom.window.document.querySelector('[data-voice-answers-count]').hidden = true;
    assert.equal(readQuestionVoiceDom(dom.window.document, 'local:assistant').pending.visible, false);
    dom.window.document.querySelector('[data-question-voice-mic]').remove();
    assert.equal(readQuestionVoiceDom(dom.window.document, 'local:assistant').mic.visible, false);
  } finally { dom.window.close(); }
});
test('M3 scenario is registered exactly once after question-free-text and before closed-chat-slot', () => {
  const { SCENARIOS } = require('./e2e/lib/web_scenarios'); const names = SCENARIOS.map(([name]) => name);
  assert.equal(names.filter(name => name === 'web-voice-answers').length, 1);
  assert.equal(names.indexOf('web-voice-answers'), names.indexOf('question-free-text') + 1);
  assert.ok(names.indexOf('web-voice-answers') < names.indexOf('closed-chat-slot'));
});
test('M3 capture and dispatch are real; only ASR and non-mutating track-stop observation are installed', () => {
  const source = fs.readFileSync(require.resolve('./e2e/lib/voice_answers_scenario'), 'utf8');
  assert.doesNotMatch(source, /(?:getUserMedia|MediaRecorder|chatSendCorrelated|createPty)\s*=/);
  assert.match(source, /requestStreamEvents/); assert.match(source, /setBindingTransformForTest/);
  const launch = fs.readFileSync(require.resolve('./e2e/web_gate'), 'utf8');
  assert.match(launch, /--use-fake-device-for-media-stream/); assert.match(launch, /--use-fake-ui-for-media-stream/);
});
module.exports = { complete, groups, negatives };

test('M3 fixture isolates composite routing while retaining the real admission and existing credential lifecycle', () => {
  const seed = fs.readFileSync(require.resolve('./e2e/lib/seed_web_gate.py'), 'utf8');
  const daemon = fs.readFileSync(require.resolve('./e2e/lib/web_gate_daemon.py'), 'utf8');
  assert.match(seed, /visibility='hidden'/); assert.match(seed, /producer\['session_generation'\]/);
  assert.match(seed, /hashlib\.sha256\(token\.encode\(\)\)\.hexdigest\(\)/);
  assert.match(daemon, /PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID/);
  assert.match(daemon, /AssistantComposite\._wake_worker = fixture_wake_worker/);
  assert.doesNotMatch(daemon, /(?:accept_input|_prepare_voice_answers|validate_voice_answers)\s*=/);
  assert.match(daemon, /if self\.config\.stream_id == voice_fixture\['stream_id'\]/);
});
