'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { SegmentTracker, createSegments, selectedSet, isVoiceEligible, registerVoiceAnswersBinding, releaseVoiceAnswersBinding, buildVoiceAnswersMeta, voiceAnswersItemCount } = require('../renderer/voice_answers_binding');
const page = (id, extra = {}) => ({ source: 'durable', key: `n${id}:0`, model: { prompt: `Question ${id}?` }, notification: { id: `n${id}`, question: { question_id: `q${id}`, producer_stream_id: 'synthetic:producer' } }, ...extra });

test('single-visit threshold is inclusive at 1.50 s; revisits never accumulate', () => {
  const tracker = new SegmentTracker(['a']);
  tracker.enter('a', 0);
  assert.equal(tracker.covered(1490).size, 0);
  assert.deepEqual(tracker.covered(1500).get('a'), { start_s: 0, end_s: 1.5 });
  const short = new SegmentTracker(['a']);
  short.enter('a', 0); short.enter(null, 1000); short.enter('a', 1100); short.finish(2100);
  assert.equal(short.covered(9999).size, 0);
});
test('longest qualifying visit wins, first wins a tie, and time matches mobile one-decimal precision', () => {
  const tracker = new SegmentTracker(['a']);
  tracker.enter('a', 13); tracker.enter(null, 1513); tracker.enter('a', 2222); tracker.enter(null, 4777);
  tracker.enter('a', 5000); tracker.finish(7555);
  assert.deepEqual(tracker.covered(8000).get('a'), { start_s: 2.2, end_s: 4.8 });
});
test('start freezes durable n, excludes legacy and arrivals, and drops closed pages', () => {
  let now = 10000;
  const eligible = page(1), unbindable = page(2, { notification: { id: 'n2', question: {} } });
  const legacy = { source: 'pane', key: 'legacy' };
  const session = createSegments(() => now);
  session.start([eligible, unbindable, legacy], eligible.key);
  now += 1500;
  assert.equal(session.n, 2);
  assert.equal(session.covered().size, 1);
  session.enter('n3:0'); now += 3000;
  assert.equal(selectedSet(session, [eligible, unbindable, legacy, page(3)], 'assistant:synthetic').length, 1);
  assert.equal(selectedSet(session, [unbindable, page(3)], 'assistant:synthetic').length, 0);
  assert.equal(isVoiceEligible(legacy), false);
  assert.equal(isVoiceEligible(unbindable), false);
});
test('non-sequential 3 → 1 → 2 navigation emits segment-start order, not deck order', () => {
  let now = 0; const pages = [page(1), page(2), page(3)]; const session = createSegments(() => now);
  session.start(pages, pages[2].key); now = 1600; session.enter(pages[0].key); now = 3200; session.enter(pages[1].key); now = 4800; session.finish();
  const items = selectedSet(session, pages, 'assistant:synthetic');
  assert.deepEqual(items.map(x => x.question_id), ['q3', 'q1', 'q2']);
  assert.deepEqual(items.map(x => x.segment), [{ start_s: 0, end_s: 1.6 }, { start_s: 1.6, end_s: 3.2 }, { start_s: 3.2, end_s: 4.8 }]);
  assert.ok(items.every(x => x.surface_stream_id === 'assistant:synthetic'));
});
test('more than 20 covered pages binds earliest 20 while n stays full durable count', () => {
  let now = 0; const pages = Array.from({ length: 23 }, (_, i) => page(i)); const session = createSegments(() => now);
  session.start(pages, pages[22].key);
  for (const entry of pages.slice(0, 22)) { now += 1500; session.enter(entry.key); }
  now += 1500; session.finish();
  assert.equal(session.n, 23);
  assert.deepEqual(selectedSet(session, pages, 'assistant:synthetic').map(x => x.question_id), ['q22', ...Array.from({ length: 19 }, (_, i) => `q${i}`)]);
});
test('binding is frozen, read repeatedly without mutation, and released explicitly', () => {
  const item = { key: 'key', question_id: 'q', notification_id: 'n', producer_stream_id: 'producer', surface_stream_id: 'assistant', prompt: 'Prompt', segment: { start_s: 0, end_s: 2 } };
  registerVoiceAnswersBinding('take', [item]); item.prompt = 'changed'; item.segment.end_s = 99;
  const expected = { version: 1, recording_id: 'take', blob_sha: 'sha', duration_s: 2, items: [{ ...item, prompt: 'Prompt', segment: { start_s: 0, end_s: 2 } }] };
  const first = buildVoiceAnswersMeta('take', { blobSha: 'sha', durationS: 2 });
  assert.deepEqual(first, expected); first.items[0].segment.end_s = 42;
  assert.deepEqual(buildVoiceAnswersMeta('take', { blobSha: 'sha', durationS: 2 }), expected);
  assert.equal(voiceAnswersItemCount('take'), 1);
  releaseVoiceAnswersBinding('take'); assert.equal(buildVoiceAnswersMeta('take', { blobSha: 'sha', durationS: 2 }), null);
});
test('rebinding empty selections removes registration and registry stays bounded', () => {
  for (let i = 0; i < 33; i++) registerVoiceAnswersBinding(`take-${i}`, [{ segment: { start_s: 0, end_s: 2 } }]);
  assert.equal(voiceAnswersItemCount('take-0'), 0);
  registerVoiceAnswersBinding('take-32', []); assert.equal(voiceAnswersItemCount('take-32'), 0);
});
test('re-entering the active page keeps its visit; threshold timer ends once covered', () => {
  const tracker = new SegmentTracker(['a']); tracker.enter('a', 0); tracker.enter('a', 1000);
  assert.equal(tracker.msUntilCovered(1490), 10);
  assert.equal(tracker.msUntilCovered(1500), null);
  assert.equal(tracker.covered(1500).size, 1);
  tracker.enter(null, 1500); assert.equal(tracker.msUntilCovered(1600), null);
});

test('mobile Q1: cap earliest 20 first, then drop closed pages without backfilling; n remains 21', () => {
  let now = 0;
  const pages = Array.from({ length: 21 }, (_, i) => page(i));
  const session = createSegments(() => now);
  session.start(pages, pages[0].key);
  for (const entry of pages.slice(1)) { now += 1500; session.enter(entry.key); }
  now += 1500; session.finish();
  assert.equal(session.covered().size, 20, 'mobile SegmentTracker caps before consulting the live deck');
  const livePages = pages.slice(1); // The earliest covered question closes on the daemon.
  const items = selectedSet(session, livePages, 'assistant:synthetic');
  assert.equal(items.length, 19);
  assert.deepEqual(items.map(item => item.question_id), Array.from({ length: 19 }, (_, i) => `q${i + 1}`));
  assert.equal(items.some(item => item.question_id === 'q20'), false, 'the 21st visit must never backfill the removed first item');
  assert.equal(session.n, 21, 'the full durable start count remains frozen');
});

test('wire key uses the unique notification ID at the 200-code-point cap, never the longer pager key', () => {
  const { validateVoiceAnswers } = require('../main/voice_answers_meta');
  let now = 0;
  const id = '😀'.repeat(200);
  const entry = page(1, { key: `assistant:stream:durable:${id}:q1`, notification: { notification_id: id, question: { question_id: 'q1', producer_stream_id: 'fixture:producer' } } });
  const session = createSegments(() => now); session.start([entry], entry.key); now = 1500; session.finish();
  const items = selectedSet(session, [entry], 'fixture:assistant');
  assert.ok([...entry.key].length > 200);
  assert.equal(items[0].key, id); assert.equal([...items[0].key].length, 200);
  assert.equal(session.covered().has(entry.key), true, 'coverage retains the separate internal identity');
  assert.ok(validateVoiceAnswers({ version: 1, recording_id: 'cap-take', blob_sha: 'sha', duration_s: 1.5, items }));
});
test('distinct near-cap notification identities remain distinct; invalid over-cap IDs are never truncated', () => {
  const { validateVoiceAnswers } = require('../main/voice_answers_meta');
  let now = 0;
  const entries = ['a', 'b'].map((suffix, i) => { const id = 'x'.repeat(199) + suffix; return page(i, {
    key: `fixture:assistant:durable:${id}:q${i}`, notification: { notification_id: id, question: { question_id: `q${i}`, producer_stream_id: 'fixture:producer' } },
  }); });
  const session = createSegments(() => now); session.start(entries, entries[0].key); now = 1500; session.enter(entries[1].key); now = 3000; session.finish();
  const items = selectedSet(session, entries, 'fixture:assistant');
  assert.equal(new Set(items.map(item => item.key)).size, 2);
  assert.ok(validateVoiceAnswers({ version: 1, recording_id: 'cap-pair', blob_sha: 'sha', duration_s: 3, items }));
  const invalid = { ...entries[0], notification: { ...entries[0].notification, notification_id: 'x'.repeat(201) } };
  const oversized = selectedSet(session, [invalid], 'fixture:assistant');
  assert.equal(oversized[0].key, 'x'.repeat(201));
  assert.equal(validateVoiceAnswers({ version: 1, recording_id: 'invalid-id', blob_sha: 'sha', duration_s: 3, items: oversized }), null);
});
