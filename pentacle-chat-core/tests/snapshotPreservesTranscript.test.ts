import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyPentacleSnapshotMessage,
  applySnapshotWithOptimisticReconciliation,
  initialPentacleStreamState,
  sendOptimisticMessage,
  type PentacleEvent,
  type PentacleNotification,
  type PentacleSessionSummary,
  type PentacleStreamState,
} from '../src/index.ts';

// Repro harness for the non-self-resolving chat-loading bugs.
//
// Mobile connects with subscribe.events_mode='summary', so the daemon's hello
// AND every reconnect-resync snapshot carry `events: []` (present but empty) —
// the transcript itself is fetched lazily via request_stream_events. The bug:
// applyPentacleSnapshotMessage treated *any* events array (including []) as an
// authoritative transcript replacement and wiped the in-memory events for all
// streams. On a lossy link (frequent reconnects), each resync re-wiped the
// already-fetched transcript; locally-inserted optimistic USER rows survived the
// wipe, producing the "I only see the messages I sent" state that never
// self-resolves until the app is restarted.
//
// Desired contract: an empty events array is NOT authoritative for transcript
// replacement — it means "this snapshot carries no transcript", so existing
// events for surviving streams must be preserved. A non-empty events array
// (full-events clients, e.g. desktop) still replaces authoritatively.

const STREAM_ID = 'host_c:codex:one';

function session(overrides: Partial<PentacleSessionSummary> = {}): PentacleSessionSummary {
  return {
    stream_id: STREAM_ID,
    host: 'host_c',
    provider: 'codex',
    session_name: 'one',
    last_event_at: '2026-06-19T12:00:00.000Z',
    last_text: '',
    last_kind: '',
    draft: '',
    pending: false,
    working: false,
    online: true,
    ...overrides,
  };
}

function evt(overrides: Partial<PentacleEvent> = {}): PentacleEvent {
  return {
    daemon_seq: 1,
    host: 'host_c',
    provider: 'codex',
    session_id: STREAM_ID,
    session_name: 'one',
    stream_id: STREAM_ID,
    timestamp: '2026-06-19T12:00:00.000Z',
    kind: 'ASSIST',
    text: 'assistant reply',
    ...overrides,
  };
}

function stateWithHistory(extra: Partial<PentacleStreamState> = {}): PentacleStreamState {
  return {
    ...initialPentacleStreamState,
    connected: true,
    hasHydrated: true,
    sessions: [session()],
    events: [
      evt({ daemon_seq: 10, kind: 'USER', text: 'what is the status?' }),
      evt({ daemon_seq: 11, kind: 'ASSIST', text: 'all green' }),
    ],
    eventContentVersionByStream: { [STREAM_ID]: 1 },
    ...extra,
  };
}

function eventsFor(state: PentacleStreamState, streamId = STREAM_ID) {
  return state.events.filter((e) => e.stream_id === streamId);
}

function notification(overrides: Partial<PentacleNotification> = {}): PentacleNotification {
  return {
    notification_id: 'n1',
    producer: 'agent_question.v1',
    title: 'Question',
    body: 'Pick one',
    severity: 'info',
    state: 'open',
    actions: [],
    created_at: '2025-01-14T10:00:00.000Z',
    updated_at: '2025-01-14T10:00:00.000Z',
    question: {
      question_id: 'q1',
      producer_stream_id: STREAM_ID,
      response_mode: 'single_choice',
      options: [{ label: 'Yes', value: 'yes' }],
      state: 'open',
      answer: null,
    },
    ...overrides,
  } as PentacleNotification;
}

test('empty summary-mode snapshot preserves the already-fetched transcript (does not wipe)', () => {
  const before = stateWithHistory();
  const after = applyPentacleSnapshotMessage(before, { sessions: [session()], events: [] });

  // The transcript that was lazily fetched must survive an events:[] resync.
  assert.equal(eventsFor(after).length, 2);
  assert.deepEqual(eventsFor(after).map((e) => e.text), ['what is the status?', 'all green']);
});

test('summary snapshots preserve model, effort, and the separately fetched status palette', () => {
  const before = stateWithHistory({
    specStatuses: [{ name: 'in_progress', display_label: 'In Progress', color: '#3dff66' }],
  });
  const after = applyPentacleSnapshotMessage(before, {
    sessions: [session({ model: 'claude-opus-4-6', effort: 'high' })],
    events: [],
  });

  assert.equal(after.sessions[0]?.model, 'claude-opus-4-6');
  assert.equal(after.sessions[0]?.effort, 'high');
  assert.deepEqual(after.specStatuses, before.specStatuses);
});

test('symptom A: empty resync does not strand the chat as "only my messages"', () => {
  // History (assistant + user) plus a locally-inserted optimistic USER send.
  const seeded = sendOptimisticMessage(stateWithHistory(), {
    streamId: STREAM_ID,
    text: 'a message I just sent',
    optimisticId: 'optimistic_one_1',
    requestId: 'req-1',
    createdAt: Date.parse('2026-06-19T12:00:05.000Z'),
    windowStartedAt: Date.parse('2026-06-19T12:00:05.000Z'),
  });

  const after = applySnapshotWithOptimisticReconciliation(
    seeded,
    { sessions: [session()], events: [] },
    Date.parse('2026-06-19T12:00:06.000Z'),
  );

  const texts = eventsFor(after).map((e) => e.text);
  // Both the assistant history AND the optimistic row must remain — not just
  // the user's own message.
  assert.ok(texts.includes('all green'), `assistant history was wiped: ${JSON.stringify(texts)}`);
  assert.ok(texts.includes('a message I just sent'));
});

test('a non-empty events array still replaces authoritatively (full-events clients)', () => {
  const before = stateWithHistory();
  const after = applyPentacleSnapshotMessage(before, {
    sessions: [session()],
    events: [evt({ daemon_seq: 99, kind: 'ASSIST', text: 'authoritative replacement' })],
  });

  assert.deepEqual(eventsFor(after).map((e) => e.text), ['authoritative replacement']);
});

test('snapshot notifications replace the notification slice newest first', () => {
  const before = stateWithHistory({
    notifications: [notification({ notification_id: 'old', created_at: '2026-07-07T10:00:00.000Z' })],
  });
  const after = applyPentacleSnapshotMessage(before, {
    sessions: [session()],
    events: [],
    notifications: [
      notification({ notification_id: 'older', created_at: '2025-01-14T09:00:00.000Z' }),
      notification({ notification_id: 'newer', created_at: '2025-01-14T11:00:00.000Z' }),
    ],
  });

  assert.deepEqual(after.notifications.map((item) => item.notification_id), ['newer', 'older']);
});

test('an absent events field still preserves surviving-stream transcript (unchanged behavior)', () => {
  const before = stateWithHistory();
  const after = applyPentacleSnapshotMessage(before, { sessions: [session()] });

  assert.equal(eventsFor(after).length, 2);
});

test('an empty-events snapshot still drops events for streams no longer present', () => {
  const before = stateWithHistory();
  // Snapshot whose session set no longer includes STREAM_ID.
  const after = applyPentacleSnapshotMessage(before, {
    sessions: [session({ stream_id: 'host_c:codex:two', session_name: 'two' })],
    events: [],
  });

  assert.equal(eventsFor(after).length, 0);
});
