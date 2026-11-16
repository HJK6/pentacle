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

// Real composite rows captured from the daemon after its summary advance
// (spec_pentacle__web_sidebar_preview_stale): the composite's last_text now
// tracks each committed row, which wakes this summary-fallback path (the 10-26
// rollback). A held composite row must never produce a second bubble.
type Frame = { session: PentacleSessionSummary; events: PentacleEvent[] };
const frames = JSON.parse(readFileSync(join(__dirname, 'fixtures/composite-summary-fallback-frames.json'), 'utf8')) as
  Record<'user_admission' | 'reply_publication' | 'mirror', Frame>;
const stream = frames.mirror.session.stream_id;
const withKind = (session: PentacleSessionSummary): PentacleSessionSummary => (
  { ...session, session_kind: 'assistant_composite' } as PentacleSessionSummary);
const rows = (session: PentacleSessionSummary, events: PentacleEvent[]) => selectSessionDetail(
  applyPentacleSnapshotMessage(initialPentacleStreamState, { sessions: [withKind(session)], events, drafts: {} }),
  stream, { visibleCount: 'all', includeDraft: false },
)!.transcriptItems;

for (const [name, frame] of Object.entries(frames)) {
  test(`${name}: last_text equal to the held composite row yields no summary fallback`, () => {
    const latest = frame.events.at(-1)!;
    assert.equal(frame.session.last_text, latest.text);
    assert.equal(frame.session.last_kind, latest.kind);
    const items = rows(frame.session, frame.events);
    assert.equal(items.some(item => item.eventKey === 'uncorrelated_fallback'), false);
    assert.equal(items.filter(item => item.text === latest.text).length, 1);
  });
}

test('inventory arriving before the composite event leaves no duplicate once the event lands', () => {
  const { session, events } = frames.mirror;
  const latest = events.at(-1)!;
  const held = events.slice(0, -1);
  const early = applyPentacleSnapshotMessage(initialPentacleStreamState,
    { sessions: [withKind(session)], events: held, drafts: {} });
  // Inventory is ahead of the transcript: the fallback may stand in for the row.
  const before = selectSessionDetail(early, stream, { visibleCount: 'all', includeDraft: false })!.transcriptItems;
  assert.equal(before.filter(item => item.text === latest.text).length, 1);
  const landed = applyPentacleEvent(early, latest);
  const after = selectSessionDetail(landed, stream, { visibleCount: 'all', includeDraft: false })!.transcriptItems;
  assert.equal(after.filter(item => item.text === latest.text).length, 1);
  assert.equal(after.some(item => item.eventKey === 'uncorrelated_fallback'), false);
  assert.deepEqual(after.map(item => item.text), events.map(event => event.text));
});
