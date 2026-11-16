import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import {
  applyPentacleSnapshotMessage,
  applyPentacleEvent,
  initialPentacleStreamState,
  selectSessionDetail,
  type PentacleEvent,
  type PentacleSessionSummary,
} from '../src/index.ts';

// Captured from the real daemon's list_sessions and request_stream_events
// after provider JSONL ingestion: later tool activity retains the prose preview.
const fixture = JSON.parse(readFileSync(join(__dirname, 'fixtures/summary-fallback-frames.json'), 'utf8')) as {
  session: PentacleSessionSummary; events: PentacleEvent[];
};
const stream = fixture.session.stream_id;
const answer = fixture.events[0];
const rows = (events: PentacleEvent[], session = fixture.session) => selectSessionDetail(
  applyPentacleSnapshotMessage(initialPentacleStreamState, { sessions: [session], events, drafts: {} }),
  stream, { visibleCount: 'all', includeDraft: false },
)!.transcriptItems;

test('summary fallback preserves original Markdown and code before transcript backfill', () => {
  const result = rows([]);
  assert.equal(result.length, 1);
  assert.equal(result[0].eventKey, 'uncorrelated_fallback');
  assert.equal(result[0].text, answer.text);
});

test('later tool activity cannot duplicate a held Markdown answer through summary fallback', () => {
  assert.ok(Date.parse(fixture.session.last_event_at) > Date.parse(answer.timestamp));
  const result = rows(fixture.events);
  assert.deepEqual(result.map(item => item.text), [answer.text]);
  assert.equal(result.some(item => item.eventKey === 'uncorrelated_fallback'), false);
});

test('held answer stays singular through idle and working inventory changes', () => {
  for (const working of [false, true, false, true, false]) {
    const result = rows(fixture.events, { ...fixture.session, working });
    assert.equal(result.length, 1);
    assert.equal(result[0].text, answer.text);
    assert.notEqual(result[0].eventKey, 'uncorrelated_fallback');
  }
});

test('distinct text and different kind still allow an unavailable summary message', () => {
  const distinct = rows(fixture.events, { ...fixture.session, last_text: 'A different new answer' });
  assert.equal(distinct.at(-1)?.text, 'A different new answer');
  const otherKind = rows(fixture.events, { ...fixture.session, last_kind: 'USER' });
  assert.equal(otherKind.at(-1)?.kind, 'USER');
  assert.equal(otherKind.at(-1)?.eventKey, 'uncorrelated_fallback');
});

test('a genuine repeated answer renders from its own transcript event', () => {
  const state = applyPentacleSnapshotMessage(initialPentacleStreamState,
    { sessions: [fixture.session], events: fixture.events, drafts: {} });
  const nextTurn = applyPentacleEvent(state, { ...answer, daemon_seq: 3, kind: 'USER', text: 'Repeat that answer',
    timestamp: '2026-10-26T01:00:01.500Z', raw: { source: 'structured', uuid: 'next-turn', jsonl_record_uuid: 'next-turn' } });
  const repeated = applyPentacleEvent(nextTurn, { ...answer, daemon_seq: 4,
    timestamp: '2026-10-26T01:00:02Z', raw: { ...answer.raw, uuid: 'summary-fallback-repeat', jsonl_record_uuid: 'summary-fallback-repeat' } });
  const result = selectSessionDetail(repeated, stream, { visibleCount: 'all' })!.transcriptItems;
  assert.deepEqual(result.map(item => item.text), [answer.text, 'Repeat that answer', answer.text]);
  assert.ok(result.every(item => item.eventKey !== 'uncorrelated_fallback'));
  assert.notEqual(result[0].eventKey, result[2].eventKey);
});

test('unrelated timestamp advancement alone cannot append matching plain prose', () => {
  const text = 'First paragraph.\n\nSecond paragraph.';
  const result = rows([{ ...answer, text }, fixture.events[1]], { ...fixture.session, last_text: text });
  assert.deepEqual(result.map(item => item.text), [text]);
});

test('a matching answer outside the visible page still suppresses summary fallback', () => {
  const state = applyPentacleSnapshotMessage(initialPentacleStreamState,
    { sessions: [fixture.session], events: fixture.events, drafts: {} });
  assert.deepEqual(selectSessionDetail(state, stream, { visibleCount: 0 })!.transcriptItems, []);
});

test('ordinary USER and ASSIST fallbacks preserve the same Markdown whitespace', () => {
  for (const last_kind of ['USER', 'ASSIST']) {
    assert.equal(rows([], { ...fixture.session, last_kind })[0].text, answer.text);
  }
});

test('formatted fallbacks retain existing terminal expand-hint cleaning', () => {
  const text = 'Searched for 1 pattern (ctrl+o to expand).';
  assert.equal(rows([], { ...fixture.session, last_kind: 'ASSIST', last_text: text })[0].text, 'Searched for 1 pattern');
});
