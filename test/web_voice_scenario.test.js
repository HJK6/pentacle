'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const { recordingChecks, deliveryChecks, observeRecording, reportChecks, webVoice, readStopControl } = require('./e2e/lib/web_voice_scenario');

// Explicit oracle negative controls, not evidence that BASE regressed. Some
// requirements (timer, single-send, no interim text) already worked on BASE.
function observations() {
  return {
    recording: { firstBarMs: 150, maxBars: 10, timers: ['0:00', '0:01'],
      stop: { label: 'Stop and send', background: 'rgb(61, 255, 102)', width: 40, height: 40, corners: Array(4).fill('20px') },
      composer: { background: 'rgba(61, 255, 102, 0.055)', border: 'rgba(61, 255, 102, 0.4)' },
      textSamples: 40, unexpectedText: [], transcribed: 0, sent: 0 },
    pending: { visible: true, inTranscript: true, text: 'TRANSCRIBING 0:01', bars: 30, sent: 0 },
    result: { rows: 1, domRows: 1, sentCount: 1, meta: { voice: { duration_s: 1 } }, rowVoice: { duration_s: 1 }, captionVisible: true, captionText: '0:01' },
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
  ['non-circular stop button', 2, o => { o.recording.stop.corners = Array(4).fill('4px'); }],
  ['interim text recorded by observer', 3, o => { o.recording.unexpectedText = ['Synthetic interim marker']; }],
  ['premature transcription', 3, o => { o.recording.transcribed = 1; }],
  ['missing pending row', 5, o => { o.pending.visible = false; }],
  ['missing TRANSCRIBING caption', 5, o => { o.pending.text = '0:01'; }],
  ['missing pending waveform', 5, o => { o.pending.bars = 0; }],
  ['duplicate ordinary text row', 6, o => { o.result.rows = 2; o.result.domRows = 2; }],
  ['duplicate text dispatch', 6, o => { o.result.sentCount = 2; }],
  ['missing wire voice duration', 6, o => { o.result.meta = {}; }],
  ['missing projected voice duration', 6, o => { o.result.rowVoice = undefined; }],
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
  assert.equal(seen.length, 7);
  assert.deepEqual(seen.filter(check => !check.passed).map(check => check.name), [
    'recording strip renders a live metering bar within one second',
    'stop-and-send control is the mobile green 40px circle',
    'stopping paints a pending TRANSCRIBING voice row before completion',
  ]);
});

for (const mutation of ['append-remove', 'character-data']) test(`M3 stop drains queued ${mutation} before disconnect`, () => {
  const dom = new JSDOM('<button class="is-recording"></button><div id="panel">Recording · <span>0:00</span>Cancel</div>');
  const panel = dom.window.document.querySelector('#panel'); const gate = {};
  observeRecording(panel, dom.window.document.querySelector('button'), gate);
  try {
    const marker = 'SYNTHETIC_INTERIM_BEFORE_STOP';
    if (mutation === 'character-data') {
      const node = panel.querySelector('span').firstChild;
      node.data = marker; node.data = '0:00';
    } else {
      const node = dom.window.document.createTextNode(marker);
      panel.appendChild(node); node.remove();
    }
    gate.stopObserving();
    assert.ok(gate.recording.unexpectedText.some(text => text.includes(marker)));
    assert.equal(recordingChecks({ ...gate.recording, transcribed: 0, sent: 0 })[3][1], false);
  } finally { gate.stopObserving(); dom.window.close(); }
});

for (const [label, corners] of [
  ['20 percent rounded square', Array(4).fill('20%')],
  ['elliptical percentage corners', Array(4).fill('50% 20%')],
  ['elliptical pixel corners', Array(4).fill('20px 40px')],
  ['one square corner', ['20px', '20px', '4px', '20px']],
]) test(`M3 circle oracle rejects ${label}`, () => {
  const observation = observations();
  observation.recording.stop.corners = corners;
  assert.equal(recordingChecks(observation.recording)[2][1], false);
});

function scenarioHarness({ retryAvailable = true, resultOverrides = {} } = {}) {
  const observation = observations(); const seen = []; const expressions = []; const notes = [];
  const result = { ...observation.result, text: 'Hermetic voice transcript', stream: 'local:fixture',
    ids: ['transcribe-fixture', 'transcribe-fixture'], mime: 'audio/wav', sha: 'a'.repeat(64),
    roomClicks: 0, tracksEnded: true, pendingHidden: true, rows: 2, domRows: 2, ...resultOverrides };
  const session = {
    waitFor: async expression => {
      if (expression.startsWith('window.__voiceGate.sent.length')) {
        const matches = new Function('window', `return ${expression}`)({ __voiceGate: { sent: Array(result.sentCount) } });
        if (!matches) throw new Error('Simulated send-count prerequisite timeout');
      }
      return true;
    }, click: async () => true, send: async () => undefined,
    eval: async expression => {
      expressions.push(expression);
      if (expression === 'window.__voiceGate.recordingAtStop') return observation.recording;
      if (expression.includes('bars: panel.querySelectorAll')) return observation.pending;
      if (expression.includes('const args = gate.sent[0]')) return result;
      if (expression.includes(".textContent.includes('0:00')")) return true;
      if (expression.includes(".textContent.includes('Retry')")) return retryAvailable;
      return undefined;
    },
  };
  const report = { note(message) { notes.push(message); }, ok(name, passed, detail) {
    seen.push({ name, passed, detail });
    if (!passed) throw new Error(name);
  } };
  return { ctx: { session, report, fixture: { streamId: 'local:fixture' }, cdp: { sleep: async () => {} } }, seen, expressions, notes };
}
test('M3 retained duplicate-row failure still reports all seven available new predicates', async () => {
  const { ctx, seen } = scenarioHarness();
  await assert.rejects(webVoice(ctx));
  const names = seen.map(check => check.name);
  for (const [name] of checks(observations())) assert.ok(names.includes(name), name);
  assert.ok(names.includes('chat mic leaves room mic untouched and releases media tracks'));
  assert.equal(seen.length, 12);
});

for (const corner of ['20px', '50%', '100%', '9999px', '20px 20px']) test(`M3 circle oracle accepts true circular ${corner} corners`, () => {
  const observation = observations(); observation.recording.stop.corners = Array(4).fill(corner);
  assert.equal(recordingChecks(observation.recording)[2][1], true);
});
test('M3 missing Retry prerequisite reports available observations without clicking or fabricating delivery', async () => {
  const { ctx, seen, expressions, notes } = scenarioHarness({ retryAvailable: false });
  await assert.rejects(webVoice(ctx), /Retry control is missing/);
  assert.equal(expressions.some(expression => expression.includes(".find(b => b.textContent === 'Retry').click()")), false);
  assert.equal(expressions.some(expression => expression.includes('const args = gate.sent[0]')), false);
  assert.equal(seen.length, 8);
  assert.ok(notes.some(note => note.includes('Unavailable voice checks: completed delivery; prerequisite failed: Retry control is missing')));
  for (const [name] of recordingChecks(observations().recording)) assert.ok(seen.some(check => check.name === name), name);
  assert.ok(seen.some(check => check.name === deliveryChecks(observations().pending)[0][0]));
  assert.equal(seen.some(check => check.name === deliveryChecks(null, observations().result)[0][0]), false);
});
test('M3 duplicate dispatch reaches the exact-one assertion and reports every available predicate', async () => {
  const { ctx, seen } = scenarioHarness({ resultOverrides: { rows: 1, domRows: 1, sentCount: 2 } });
  await assert.rejects(webVoice(ctx), /completed voice take paints exactly one/);
  assert.equal(seen.length, 12);
  assert.equal(seen.find(check => check.name === deliveryChecks(null, observations().result)[0][0]).passed, false);
});

for (const [corner, passes] of [['20%', false], ['50% 20%', false], ['50%', true], ['20px', true]]) test(`M3 style observation preserves ${corner} units for the circle oracle`, () => {
  const dom = new JSDOM('<button aria-label="Stop and send" style="width:40px;height:40px;background:rgb(61, 255, 102)"></button>');
  try {
    const button = dom.window.document.querySelector('button');
    for (const cornerName of ['borderTopLeftRadius', 'borderTopRightRadius', 'borderBottomRightRadius', 'borderBottomLeftRadius']) button.style[cornerName] = corner;
    // JSDOM does not lay out rectangles; only geometry is supplied, while the
    // actual computed CSS and shipped observation conversion are exercised.
    button.getBoundingClientRect = () => ({ width: 40, height: 40 });
    const observation = observations(); observation.recording.stop = readStopControl(button);
    assert.deepEqual(observation.recording.stop.corners, Array(4).fill(corner));
    assert.equal(recordingChecks(observation.recording)[2][1], passes);
  } finally { dom.window.close(); }
});

test('M3 oracle rejects a recording capsule without its computed green tint', () => {
  const o = observations(); o.recording.composer.background = 'rgba(0, 0, 0, 0)';
  assert.equal(recordingChecks(o.recording).find(([name]) => name.includes('composer capsule'))?.[1], false);
});
test('M3 oracle rejects missing DOM caption despite valid voice metadata', () => {
  const o = observations(); o.result.captionVisible = false;
  assert.equal(deliveryChecks(o.pending, o.result)[1][1], false);
});
