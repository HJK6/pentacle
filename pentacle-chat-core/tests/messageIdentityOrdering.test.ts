import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyFetchedStreamEvents,
  applyPentacleEvent,
  applySnapshotWithOptimisticReconciliation,
  coalesceInterpretedEvents,
  initialPentacleStreamState,
  interpretPentacleEvent,
  invalidateSessionDetailCache,
  selectSessionDetail,
  sendOptimisticMessage,
  type PentacleEvent,
  type PentacleSessionSummary,
  type PentacleStreamState,
} from '../src/index.ts';

const STREAM_ID = 'fixture-host-a:v2-order-fixture';
const SESSION_ID = 'fixture-order-generation';
const NOTICE = `[pentacle-notice:d2:${'b'.repeat(64)}]\nChild session fixture-host-b:v2-child-fixture reached an inactivity threshold at 2026-01-02T03:04:05.000000Z.`;
const NOTICE_SUMMARY = 'Child session fixture-host-b:v2-child-fixture reached an inactivity threshold at 2026-01-02T03:04:05.000000Z.';
const FIRST_REPLY = 'I will inspect the synthetic child fixture and retain its distinct durable identity.';
const SECOND_REPLY = 'The synthetic transport fixture remains unavailable, so this independently delivered response must remain a separate transcript row.';
const OPERATOR_QUOTE = `${NOTICE}\n\nThis quoted synthetic notice must remain a distinct user-authored row.\n\nPlease retain the complete fixture text and its independent identity.`;
const OPTIMISTIC_ID = 'optimistic_fixture-host-a_order_fixture_1';

function event(overrides: Partial<PentacleEvent>): PentacleEvent {
  return {
    daemon_seq: 1,
    host: 'fixture-host-a',
    provider: 'codex',
    session_id: SESSION_ID,
    session_name: 'v2-order-fixture',
    stream_id: STREAM_ID,
    timestamp: '2026-01-02T03:04:57.811Z',
    kind: 'USER',
    text: '',
    ...overrides,
  };
}

const fixtureEvents: PentacleEvent[] = [
  event({
    daemon_seq: 4100,
    text: NOTICE,
    raw: { source: 'structured', transport: 'codex-rollout', ordinal: 10, uuid: 'fixture-notice-uuid' },
  }),
  event({
    daemon_seq: 4110,
    timestamp: '2026-01-02T03:05:03.170Z',
    kind: 'ASSIST_TEXT',
    text: FIRST_REPLY,
    raw: { source: 'structured', transport: 'codex-rollout', ordinal: 20, uuid: 'fixture-reply-one-uuid' },
  }),
  event({
    daemon_seq: 4120,
    timestamp: '2026-01-02T03:05:27.788Z',
    kind: 'ASSIST_TEXT',
    text: SECOND_REPLY,
    raw: { source: 'structured', transport: 'codex-rollout', ordinal: 30, uuid: 'fixture-reply-two-uuid' },
  }),
  event({
    daemon_seq: 4130,
    timestamp: '2026-01-02T03:07:55.647Z',
    text: OPERATOR_QUOTE,
    optimistic_id: OPTIMISTIC_ID,
    raw: { source: 'structured', transport: 'codex-rollout', ordinal: 40, uuid: 'fixture-operator-uuid' },
  }),
];

const session: PentacleSessionSummary = {
  stream_id: STREAM_ID,
  host: 'fixture-host-a',
  provider: 'codex',
  session_name: 'v2-order-fixture',
  last_event_at: fixtureEvents.at(-1)!.timestamp,
  last_text: OPERATOR_QUOTE,
  last_kind: 'USER',
  draft: '',
  pending: false,
  working: false,
  online: true,
};

function seed(): PentacleStreamState {
  return {
    ...initialPentacleStreamState,
    connected: true,
    sessions: [session],
  };
}

function assertFixtureOrder(state: PentacleStreamState) {
  invalidateSessionDetailCache(STREAM_ID);
  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' })?.transcriptItems ?? [];
  assert.deepEqual(items.map((item) => item.text), [NOTICE_SUMMARY, FIRST_REPLY, SECOND_REPLY, OPERATOR_QUOTE]);
  assert.deepEqual(items.map((item) => item.id), [
    '4100',
    '4110',
    '4120',
    OPTIMISTIC_ID,
  ]);
  assert.equal(
    state.events.find((item) => Number(item.daemon_seq) === 4100)?.text,
    NOTICE,
    'presentation does not mutate the retained daemon envelope',
  );
  const clock = (timestamp: string) => new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  assert.deepEqual(items.map((item) => item.timestampLabel), [
    clock(fixtureEvents[0]!.timestamp),
    clock(fixtureEvents[1]!.timestamp),
    '',
    clock(fixtureEvents[3]!.timestamp),
  ]);
}

const serverState = () => fixtureEvents.reduce((state, item) => applyPentacleEvent(state, item), seed());
const fetchedState = () => applyFetchedStreamEvents(seed(), fixtureEvents, { requestedStreamId: STREAM_ID });
const snapshotState = () => applySnapshotWithOptimisticReconciliation(seed(), { sessions: [session], events: fixtureEvents });
const backfillState = () => applyFetchedStreamEvents(
  applyFetchedStreamEvents(seed(), fixtureEvents.slice(2), { requestedStreamId: STREAM_ID }),
  fixtureEvents.slice(0, 2),
  { requestedStreamId: STREAM_ID },
);
const optimisticState = () => sendOptimisticMessage(
  fixtureEvents.slice(0, -1).reduce((state, item) => applyPentacleEvent(state, item), seed()),
  {
    streamId: STREAM_ID,
    text: OPERATOR_QUOTE,
    optimisticId: OPTIMISTIC_ID,
    requestId: 'send-fixture-request-1',
    createdAt: Date.parse('2026-01-02T03:07:55.647Z'),
  },
);

test('synthetic regression: live reducer preserves the notice, replies, and later quoted operator message', () => {
  assertFixtureOrder(serverState());
});

test('synthetic regression: fetched replay preserves distinct message identities and daemon order', () => {
  assertFixtureOrder(fetchedState());
});

test('synthetic regression: reconnect snapshot preserves distinct message identities and daemon order', () => {
  assertFixtureOrder(snapshotState());
});

test('synthetic regression: reverse-page backfill preserves distinct message identities and daemon order', () => {
  assertFixtureOrder(backfillState());
});

test('synthetic regression: optimistic message stays after the earlier replies before confirmation', () => {
  assertFixtureOrder(optimisticState());
});

test('synthetic regression: optimistic confirmation stays after the earlier replies', () => {
  assertFixtureOrder(applyPentacleEvent(optimisticState(), fixtureEvents.at(-1)!));
});

test('synthetic regression: confirmed message replay remains stable', () => {
  const confirmed = applyPentacleEvent(optimisticState(), fixtureEvents.at(-1)!);
  assertFixtureOrder(applyFetchedStreamEvents(confirmed, fixtureEvents, { requestedStreamId: STREAM_ID }));
});

function interpreted(overrides: Partial<PentacleEvent>) {
  return interpretPentacleEvent(event(overrides), 'Host A');
}

for (const kind of ['USER', 'ASSIST_TEXT'] as const) {
  test(`${kind}: distinct prefix-related messages do not coalesce, including equal-clock rows`, () => {
    const prefix = 'This independently delivered message is intentionally longer than forty characters.';
    const rows = [
      interpreted({ daemon_seq: 20, kind, text: prefix, timestamp: '2026-09-16T02:00:00.000Z' }),
      interpreted({ daemon_seq: 21, kind, text: `${prefix} It has separate durable identity.`, timestamp: '2026-09-16T02:00:00.000Z' }),
    ];

    assert.deepEqual(coalesceInterpretedEvents(rows).map((item) => item.text), rows.map((item) => item.text));
  });
}

test('a progressive extension coalesces only when replay identity and scope are shared', () => {
  const prefix = 'A verified progressive assistant message longer than forty characters';
  const rows = [
    interpreted({ daemon_seq: 30, kind: 'ASSIST_TEXT', text: prefix, jsonl_record_uuid: 'shared-record' }),
    interpreted({ daemon_seq: 31, kind: 'ASSIST_TEXT', text: `${prefix} is complete.`, jsonl_record_uuid: 'shared-record' }),
  ];

  const result = coalesceInterpretedEvents(rows);
  assert.equal(result.length, 1);
  assert.equal(result[0]?.text, rows[1]?.text);
});

test('composite message identity fences progressive coalescence across same and different lanes', () => {
  const prefix = 'Done — the first composite publication completed its task';
  for (const [firstLane, secondLane] of [['lane-a', 'lane-a'], ['lane-a', 'lane-b']] as const) {
    const rows = [
      interpreted({
        daemon_seq: 35,
        kind: 'ASSIST_TEXT',
        text: prefix,
        message_id: 'composite-message-a',
        lane_id: firstLane,
      }),
      interpreted({
        // The daemon sequence may be shared by a coalesced publication batch;
        // message_id remains the authoritative composite publication identity.
        daemon_seq: 35,
        kind: 'ASSIST_TEXT',
        text: `${prefix} with the second task.`,
        message_id: 'composite-message-b',
        lane_id: secondLane,
      }),
    ];

    assert.equal(
      coalesceInterpretedEvents(rows).length,
      2,
      `distinct composite message ids survive lanes ${firstLane}/${secondLane}`,
    );
  }
});

test('the same replay token cannot coalesce across stream, provider, or session scope', () => {
  const prefix = 'A scoped progressive assistant message longer than forty characters';
  const first = interpreted({ daemon_seq: 40, kind: 'ASSIST_TEXT', text: prefix, jsonl_record_uuid: 'reused-record' });
  const variants = [
    interpreted({ daemon_seq: 41, kind: 'ASSIST_TEXT', text: `${prefix} across stream.`, jsonl_record_uuid: 'reused-record', stream_id: 'fixture-host-c:v2-other-fixture' }),
    interpreted({ daemon_seq: 42, kind: 'ASSIST_TEXT', text: `${prefix} across provider.`, jsonl_record_uuid: 'reused-record', provider: 'claude' }),
    interpreted({ daemon_seq: 43, kind: 'ASSIST_TEXT', text: `${prefix} across session.`, jsonl_record_uuid: 'reused-record', session_id: 'different-session' }),
  ];

  for (const variant of variants) {
    assert.equal(coalesceInterpretedEvents([first, variant]).length, 2);
  }
});

test('null and NaN sequences never coerce to daemon sequence zero identity', () => {
  const prefix = 'A sequence-sensitive assistant message longer than forty characters';
  const zero = interpreted({ daemon_seq: 0, kind: 'ASSIST_TEXT', text: `${prefix} at zero.` });
  const invalid = [
    interpreted({ daemon_seq: null as unknown as number, kind: 'ASSIST_TEXT', text: prefix }),
    interpreted({ daemon_seq: Number.NaN, kind: 'ASSIST_TEXT', text: prefix }),
  ];

  for (const candidate of invalid) {
    assert.equal(coalesceInterpretedEvents([candidate, zero]).length, 2);
  }
});

test('exact replay with shared scoped daemon identity remains one row', () => {
  const text = 'An exact replayed assistant message intentionally longer than forty characters.';
  const rows = [
    interpreted({ daemon_seq: 50, kind: 'ASSIST_TEXT', text }),
    interpreted({ daemon_seq: 50, kind: 'ASSIST_TEXT', text }),
  ];

  assert.equal(coalesceInterpretedEvents(rows).length, 1);
});

test('distinct optimistic sends sharing one composite correlation remain separate rows', () => {
  const text = 'Two separately queued sends may intentionally contain exactly the same text.';
  const rows = [
    interpreted({
      daemon_seq: Number.NaN,
      correlatedDaemonSeq: 60,
      kind: 'USER',
      text,
      optimistic_id: 'optimistic_fixture-host-a_fixture_1',
    }),
    interpreted({
      daemon_seq: Number.NaN,
      correlatedDaemonSeq: 60,
      kind: 'USER',
      text,
      optimistic_id: 'optimistic_fixture-host-a_fixture_2',
    }),
  ];

  assert.equal(coalesceInterpretedEvents(rows).length, 2);
});

test('correlated-only aliases do not establish replay identity', () => {
  const text = 'A correlation alias without an actual event identity remains ambiguous.';
  const rows = [
    interpreted({ daemon_seq: Number.NaN, correlatedDaemonSeq: 61, kind: 'USER', text }),
    interpreted({ daemon_seq: Number.NaN, correlatedDaemonSeq: 61, kind: 'USER', text }),
  ];

  assert.equal(coalesceInterpretedEvents(rows).length, 2);
});

test('distinct optimistic identities override a shared record alias', () => {
  const text = 'Explicitly different local sends cannot become one record-alias replay.';
  const rows = [
    interpreted({
      daemon_seq: Number.NaN,
      kind: 'USER',
      text,
      optimistic_id: 'optimistic_fixture-host-a_fixture_3',
      jsonl_record_uuid: 'shared-but-conflicting-record',
    }),
    interpreted({
      daemon_seq: Number.NaN,
      kind: 'USER',
      text,
      optimistic_id: 'optimistic_fixture-host-a_fixture_4',
      jsonl_record_uuid: 'shared-but-conflicting-record',
    }),
  ];

  assert.equal(coalesceInterpretedEvents(rows).length, 2);
});

test('an actual daemon sequence and its correlated alias retain replay identity', () => {
  const text = 'A server event and its reconciled local projection are one logical message.';
  const rows = [
    interpreted({ daemon_seq: 62, kind: 'USER', text }),
    interpreted({ daemon_seq: Number.NaN, correlatedDaemonSeq: 62, kind: 'USER', text }),
  ];

  assert.equal(coalesceInterpretedEvents(rows).length, 1);
});
