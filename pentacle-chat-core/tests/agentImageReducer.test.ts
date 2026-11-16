import assert from 'node:assert/strict';
import test from 'node:test';

import {
  initialPentacleStreamState,
  applyPentacleEvent,
  applyFetchedStreamEvents,
  applyPentacleSnapshotMessage,
  selectSessionDetail,
  type PentacleEvent,
  type PentacleSessionSummary,
  type PentacleStreamState,
} from '../src/index.ts';

// Regression for the QA-found blocking defect: a caption-less agent-authored
// ASSIST image event was dropped by the reducer BEFORE the interpreter branch —
// live `applyPentacleEvent` filtered empty-text ASSIST as transient noise, and
// the fetched/snapshot filters exempted only USER attachments. Reproduction at
// the rejected SHA produced live/fetched/snapshot events=0. These pin the fix.

const STREAM_ID = 'host_c:claude-host_c-1b7d6cb6';
const SHA = 'a'.repeat(64);

function session(): PentacleSessionSummary {
  return {
    stream_id: STREAM_ID, host: 'host_c', provider: 'claude',
    session_name: 'claude-host_c-1b7d6cb6', last_event_at: '2026-09-13T22:00:00.000Z',
    last_text: '', last_kind: '', draft: '', pending: false, working: false, online: true,
  };
}

function assistImage(text = ''): PentacleEvent {
  return {
    daemon_seq: 7, host: 'host_c', provider: 'claude', session_id: STREAM_ID,
    session_name: 'claude-host_c-1b7d6cb6', stream_id: STREAM_ID,
    timestamp: '2026-09-13T22:00:00.000Z', kind: 'ASSIST', text,
    attachments: [{ key: SHA, mime: 'image/png', width: 800, height: 600 }],
    raw: { source: 'agent_image', request_id: 'req-1' },
  };
}

function seeded(): PentacleStreamState {
  return { ...initialPentacleStreamState, connected: true, sessions: [session()] };
}

function imageRows(state: PentacleStreamState) {
  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' })?.transcriptItems ?? [];
  return items.filter((i) => ((i as { attachments?: unknown[] }).attachments?.length ?? 0) > 0);
}

test('reducer keeps a caption-less agent image on the live path', () => {
  const rows = imageRows(applyPentacleEvent(seeded(), assistImage('')));
  assert.equal(rows.length, 1, 'exactly one image row survives live ingest');
  assert.equal((rows[0] as { attachments: Array<{ key: string }> }).attachments[0].key, SHA);
});

test('reducer keeps a caption-less agent image on fetched history replay', () => {
  const state = applyFetchedStreamEvents(seeded(), [assistImage('')], 500, STREAM_ID);
  assert.equal(imageRows(state).length, 1, 'image row survives fetched history');
});

test('reducer keeps a caption-less agent image on snapshot ingest — exactly one row', () => {
  const state = applyPentacleSnapshotMessage(seeded(), { events: [assistImage('')], sessions: [session()] });
  assert.equal(imageRows(state).length, 1, 'image row survives snapshot as exactly one row');
});

test('a captioned agent image survives all three reducer paths', () => {
  assert.equal(imageRows(applyPentacleEvent(seeded(), assistImage('the chart'))).length, 1);
  assert.equal(imageRows(applyFetchedStreamEvents(seeded(), [assistImage('the chart')], 500, STREAM_ID)).length, 1);
  assert.equal(imageRows(applyPentacleSnapshotMessage(seeded(), { events: [assistImage('the chart')], sessions: [session()] })).length, 1);
});
