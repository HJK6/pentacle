import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyPentacleSnapshotMessage,
  getPentacleSessionStatus,
  getPentacleSessionStatusLabel,
  initialPentacleStreamState,
  selectChatList,
  selectSessionDetail,
  type PentacleEvent,
  type PentacleSessionSummary,
} from '../src/index.ts';

const STREAM_ID = 'host_c:codex:tmux-timeout';

function session(overrides: Partial<PentacleSessionSummary> = {}): PentacleSessionSummary {
  return {
    stream_id: STREAM_ID,
    host: 'host_c',
    provider: 'codex',
    session_name: 'tmux-timeout',
    last_event_at: '2026-08-01T22:00:00.000Z',
    last_text: 'last good reply',
    last_kind: 'ASSIST',
    draft: '',
    pending: false,
    working: true,
    online: true,
    ...overrides,
  };
}

const transcript: PentacleEvent = {
  daemon_seq: 1,
  host: 'host_c',
  provider: 'codex',
  session_id: STREAM_ID,
  session_name: 'tmux-timeout',
  stream_id: STREAM_ID,
  timestamp: '2026-08-01T22:00:00.000Z',
  kind: 'ASSIST',
  text: 'last good reply',
};

test('tmux timeout state is unresponsive ahead of working and preserves the retained transcript through recovery', () => {
  const baseline = {
    ...initialPentacleStreamState,
    connected: true,
    hasHydrated: true,
    sessions: [session()],
    events: [transcript],
    eventContentVersionByStream: { [STREAM_ID]: 1 },
  };
  const timedOut = applyPentacleSnapshotMessage(baseline, {
    sessions: [session({
      pane_status: 'pane_unresponsive',
      pane_status_reason: 'tmux_probe_timeout',
      pane_status_since: '2026-08-01T22:00:20.000Z',
    })],
    events: [],
  });

  assert.equal(getPentacleSessionStatus(timedOut.sessions[0]), 'unresponsive');
  assert.equal(getPentacleSessionStatusLabel('unresponsive'), 'Unresponsive');
  assert.equal(selectChatList(timedOut)[0]?.statusLabel, 'Unresponsive');
  const detail = selectSessionDetail(timedOut, STREAM_ID, { includeDraft: false });
  assert.equal(detail?.statusLabel, 'Unresponsive');
  assert.deepEqual(detail?.transcriptItems.map((item) => item.text), ['last good reply']);

  const recovered = applyPentacleSnapshotMessage(timedOut, {
    sessions: [session({ pane_status: 'pane_confirmed', working: false })],
    events: [],
  });
  assert.equal(getPentacleSessionStatus(recovered.sessions[0]), 'live');
  assert.deepEqual(
    selectSessionDetail(recovered, STREAM_ID, { includeDraft: false })?.transcriptItems.map((item) => item.text),
    ['last good reply'],
  );
});

test('sessions without additive pane fields retain their prior status behavior', () => {
  assert.equal(getPentacleSessionStatus(session()), 'working');
  assert.equal(getPentacleSessionStatusLabel(getPentacleSessionStatus(session({ working: false }))), 'Live');
});
