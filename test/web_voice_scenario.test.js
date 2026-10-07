'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const { recordingChecks, deliveryChecks, observeRecording, reportChecks } = require('./e2e/lib/web_voice_scenario');

// Explicit oracle negative controls, not evidence that BASE regressed. Some
// requirements (timer, single-send, no interim text) already worked on BASE.
function observations() {
  return {
    recording: { firstBarMs: 150, maxBars: 10, timers: ['0:00', '0:01'],
      stop: { label: 'Stop and send', background: 'rgb(61, 255, 102)', width: 40, height: 40, radius: 20 },
      textSamples: 40, unexpectedText: [], transcribed: 0, sent: 0 },
    pending: { visible: true, inTranscript: true, text: 'TRANSCRIBING 0:01', bars: 30, sent: 0 },
    result: { rows: 1, domRows: 1, sentCount: 1, meta: { voice: { duration_s: 1 } }, rowVoice: { duration_s: 1 } },
  };
}
function checks({ recording, pending, result }) {
  return [...recordingChecks(recording), ...deliveryChecks(pending, result)];
}
test('M3 oracle accepts a complete synthetic observation', () => {
  for (const [name, passed] of checks(observations())) assert.equal(passed, true, name);
});
for (const [label, index, mutate] of [
  ['missing metering bars', 0, o => { o.recording.firstBarMs = null; o.recording.maxBars = 0; }],
  ['late metering bars', 0, o => { o.recording.firstBarMs = 1001; }],
  ['frozen timer', 1, o => { o.recording.timers = ['0:00']; }],
  ['non-green stop button', 2, o => { o.recording.stop.background = 'rgb(255, 0, 0)'; }],
  ['non-circular stop button', 2, o => { o.recording.stop.radius = 4; }],
  ['interim text recorded by observer', 3, o => { o.recording.unexpectedText = ['Synthetic interim marker']; }],
  ['premature transcription', 3, o => { o.recording.transcribed = 1; }],
  ['missing pending row', 4, o => { o.pending.visible = false; }],
  ['missing TRANSCRIBING caption', 4, o => { o.pending.text = '0:01'; }],
  ['missing pending waveform', 4, o => { o.pending.bars = 0; }],
  ['duplicate ordinary text row', 5, o => { o.result.rows = 2; o.result.domRows = 2; }],
  ['duplicate text dispatch', 5, o => { o.result.sentCount = 2; }],
  ['missing wire voice duration', 5, o => { o.result.meta = {}; }],
  ['missing projected voice duration', 5, o => { o.result.rowVoice = undefined; }],
]) test(`M3 oracle rejects ${label}`, () => {
  const observation = observations(); mutate(observation);
  assert.equal(!!checks(observation)[index][1], false, label);
});

for (const mutation of ['append-remove', 'character-data', 'numeric-text']) test(`M3 text observer catches transient ${mutation} before its next sample`, async () => {
  const dom = new JSDOM('<button class="is-recording"></button><div id="panel">Recording · <span>0:00</span>Cancel</div>');
  const panel = dom.window.document.querySelector('#panel');
  const gate = {};
  observeRecording(panel, dom.window.document.querySelector('button'), gate);
  try {
    assert.deepEqual(gate.recording.unexpectedText, []);
    const marker = mutation === 'numeric-text' ? '123456' : 'SYNTHETIC_INTERIM_SENTINEL';
    if (mutation === 'character-data') {
      const node = panel.querySelector('span').firstChild;
      node.data = marker; node.data = '0:00';
    } else {
      const node = dom.window.document.createTextNode(marker);
      panel.appendChild(node); node.remove();
    }
    await Promise.resolve();
    assert.ok(gate.recording.unexpectedText.some(text => text.includes(marker)));
    assert.equal(recordingChecks({ ...gate.recording, transcribed: 0, sent: 0 })[3][1], false);
  } finally { gate.stopObserving(); dom.window.close(); }
});
test('M3 text observer allows UI chrome and disconnects before pending text', async () => {
  const dom = new JSDOM('<button class="is-recording"></button><div id="panel">Recording · 0:00Cancel recording</div>');
  const panel = dom.window.document.querySelector('#panel');
  const gate = {};
  observeRecording(panel, dom.window.document.querySelector('button'), gate);
  panel.textContent = 'Recording · 0:01×Cancel recording';
  await Promise.resolve();
  gate.stopObserving();
  panel.textContent = 'TRANSCRIBING';
  await Promise.resolve();
  assert.deepEqual(gate.recording.unexpectedText, []);
  assert.deepEqual(gate.recording.timers, ['0:00', '0:01']);
  dom.window.close();
});

test('M3 report records every independent predicate before reporting aggregate failure', () => {
  const seen = [];
  const report = { ok(name, passed, detail) {
    seen.push({ name, passed, detail });
    if (!passed) throw new Error(name);
  } };
  const observation = observations();
  observation.recording.firstBarMs = null;
  observation.recording.stop.background = 'transparent';
  observation.pending.bars = 0;
  assert.throws(() => reportChecks(report, checks(observation)), error => {
    assert.ok(error instanceof AggregateError);
    assert.equal(error.errors.length, 3);
    return true;
  });
  assert.equal(seen.length, 6);
  assert.deepEqual(seen.filter(check => !check.passed).map(check => check.name), [
    'recording strip renders a live metering bar within one second',
    'stop-and-send control is the mobile green 40px circle',
    'stopping paints a pending TRANSCRIBING voice row before completion',
  ]);
});
