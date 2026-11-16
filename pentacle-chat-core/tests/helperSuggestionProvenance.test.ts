import assert from 'node:assert/strict';
import test from 'node:test';
import {
  applyFetchedStreamEvents, applyPentacleEvent, applyPentacleSessionSummary,
  applyPentacleSnapshotMessage, classifyDrop, initialPentacleStreamState,
  interpretPentacleEvent, isHelperSuggestionEvent, reconcileOptimisticSendWithServerEvent, sendOptimisticMessage,
  type PentacleEvent, type PentacleSessionSummary,
} from '../src/index.ts';

const STREAM = 'peer:helper-test';
const session: PentacleSessionSummary = {
  stream_id: STREAM, host: 'peer', provider: 'codex', session_name: 'helper-test',
  draft: '', pending: false, working: false, online: true,
  last_event_at: '2026-10-30T12:00:00Z', last_kind: 'ASSIST_TEXT', last_text: 'previous reply',
};
const fresh = () => ({ ...initialPentacleStreamState, sessions: [session] });
const event = (extra: Partial<PentacleEvent> = {}): PentacleEvent => ({
  daemon_seq: 10, session_id: STREAM, stream_id: STREAM, host: 'peer', provider: 'codex', session_name: 'helper-test',
  timestamp: '2026-10-30T12:01:00Z', kind: 'USER', text: 'explore the repository structure', ...extra,
});

test('helper-shaped optimistic send inserts and reconciles to one durable row', () => {
  const text = 'review with em before we implement';
  const sent = sendOptimisticMessage(fresh(), { streamId: STREAM, text,
    optimisticId: 'optimistic-helper', requestId: 'request-helper', createdAt: Date.parse('2026-10-30T12:00:30Z') });
  assert.deepEqual(sent.events.map(row => row.text), [text]);
  const echoed = reconcileOptimisticSendWithServerEvent(sent, 'optimistic-helper', event({ text,
    optimistic_id: 'optimistic-helper', raw: { source: 'structured', transport: 'codex-rollout' } }));
  assert.equal(echoed.events.length, 1);
  assert.equal(echoed.events[0].correlatedDaemonSeq, 10);
  assert.equal(echoed.optimisticSends?.['optimistic-helper'], undefined);
  assert.equal(interpretPentacleEvent(echoed.events[0]).hidden, false);
});

test('client-origin provenance alone excludes a row from helper suppression', () => {
  const row = event({ client_origin: true });
  assert.equal(isHelperSuggestionEvent(row), false);
  assert.equal(interpretPentacleEvent(row).hidden, false);
  assert.equal(classifyDrop(row), null);
});

for (const [label, extra] of [
  ['operator correlation', { optimistic_id: 'operator-id' }],
  ['structured terminal', { raw: { source: 'structured', transport: 'codex-rollout' } }],
] as const) {
  test(`${label} keeps helper-shaped USER rows in live, fetched and snapshot history`, () => {
    const row = event(extra);
    assert.equal(applyPentacleEvent(fresh(), row).events.length, 1);
    assert.equal(applyFetchedStreamEvents(fresh(), [row]).events.length, 1);
    assert.equal(applyPentacleSnapshotMessage(fresh(), { sessions: [session], events: [row] }).events.length, 1);
    assert.equal(interpretPentacleEvent(row).hidden, false);
    assert.equal(classifyDrop(row), null);
  });
}

for (const prefix of ['', '› ', '❯ ']) {
  test(`unprovenanced splash suggestion stays hidden with prefix ${JSON.stringify(prefix)}`, () => {
    const row = event({ text: prefix + 'explore the repository structure' });
    assert.equal(applyPentacleEvent(fresh(), row).events.length, 0);
    assert.equal(applyFetchedStreamEvents(fresh(), [row]).events.length, 0);
    assert.equal(applyPentacleSnapshotMessage(fresh(), { sessions: [session], events: [row] }).events.length, 0);
    assert.equal(interpretPentacleEvent(row).hidden, true);
    assert.equal(classifyDrop(row), 'helper_suggestion');
  });
}

test('helper-shaped USER summary replaces the previous preview; transcript noise stays stripped', () => {
  const updated = applyPentacleSessionSummary(fresh(), { ...session, last_kind: 'USER',
    last_text: 'review the spec', last_event_at: '2026-10-30T12:01:00Z' });
  assert.equal(updated.sessions[0].last_text, 'review the spec');
  const noisy = applyPentacleSessionSummary(updated, { ...session, last_text: 'Working (2s • esc to interrupt)',
    last_event_at: '2026-10-30T12:02:00Z' });
  assert.equal(noisy.sessions[0].last_text, 'review the spec');
});
