import assert from 'node:assert/strict';
import test from 'node:test';

import {
  TELEMETRY_EVENT_BUG_REFS,
  TELEMETRY_EVENTS,
  CHAT_RENDER_STABILITY_REF,
  applyFetchedStreamEvents,
  applySnapshotWithOptimisticReconciliation,
  initialPentacleStreamState,
  applyPentacleSessionInventory,
  markOptimisticAckedByRequestId,
  markOptimisticCancelledByRequestId,
  markOptimisticDispatchedByRequestId,
  applyPentacleEvent,
  enqueueOptimisticMessage,
  getSessionSendingState,
  markOptimisticFailedByRequestId,
  optimisticMatchesServerUser,
  reconcileOptimisticSendWithServerEvent,
  selectSessionDetail,
  sendOptimisticMessage,
  markOptimisticIndeterminateByRequestId,
  markOptimisticReturnedToPromptByOptimisticId,
  type ChatAttachment,
  type PentacleEvent,
  type PentacleSessionSummary,
  type PentacleStreamState,
} from '../src/index.ts';

const STREAM_ID = 'host_c:codex:one';
const OPTIMISTIC_ID = 'optimistic_host_c_codex_one_1';
const REQUEST_ID = 'send-req-1';
const CREATED_AT = Date.parse('2026-05-16T12:00:00.000Z');

function session(overrides: Partial<PentacleSessionSummary> = {}): PentacleSessionSummary {
  return {
    stream_id: STREAM_ID,
    host: 'host_c',
    provider: 'codex',
    session_name: 'one',
    last_event_at: '2026-05-16T12:00:00.000Z',
    last_text: '',
    last_kind: '',
    draft: '',
    pending: false,
    working: false,
    online: true,
    ...overrides,
  };
}

function buildState(overrides: Partial<PentacleStreamState> = {}): PentacleStreamState {
  return {
    ...initialPentacleStreamState,
    connected: true,
    sessions: [session()],
    ...overrides,
  };
}

function createOptimistic(state = buildState()) {
  return sendOptimisticMessage(state, {
    streamId: STREAM_ID,
    text: 'hello',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: CREATED_AT,
    windowStartedAt: CREATED_AT,
  });
}

function optimisticAttachment(overrides: Partial<ChatAttachment & { uri: string }> = {}) {
  return {
    key: 'local:file:///tmp/photo-a.jpg',
    mime: 'image/jpeg',
    width: 120,
    height: 240,
    uri: 'file:///tmp/photo-a.jpg',
    ...overrides,
  };
}

function serverUserEvent(overrides: Partial<PentacleEvent> = {}): PentacleEvent {
  return {
    daemon_seq: 42,
    host: 'host_c',
    provider: 'codex',
    session_id: STREAM_ID,
    session_name: 'one',
    stream_id: STREAM_ID,
    timestamp: '2026-05-16T12:00:01.000Z',
    kind: 'USER',
    text: 'hello',
    ...overrides,
  };
}

function lateServerUserEvent(overrides: Partial<PentacleEvent> = {}): PentacleEvent {
  return serverUserEvent({
    timestamp: '2026-05-16T12:02:30.000Z',
    optimistic_id: undefined,
    ...overrides,
  });
}

test('direct optimistic reconcile preserves the client row id and records the correlated daemon sequence', () => {
  const queued = createOptimistic();
  const dispatched = markOptimisticDispatchedByRequestId(queued, REQUEST_ID, CREATED_AT + 10);
  const acked = markOptimisticAckedByRequestId(dispatched, REQUEST_ID, CREATED_AT + 20);

  const next = reconcileOptimisticSendWithServerEvent(
    acked,
    OPTIMISTIC_ID,
    serverUserEvent({ optimistic_id: OPTIMISTIC_ID }),
  );

  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID], undefined);
  assert.equal(next.optimisticByRequestId?.[REQUEST_ID], undefined);
  assert.equal(next.events.length, 1);
  assert.equal(next.events[0].optimistic_id, OPTIMISTIC_ID);
  assert.equal(Number.isNaN(next.events[0].daemon_seq), true);
  assert.equal((next.events[0] as any).correlatedDaemonSeq, 42);
  assert.equal(next.events[0].pending, false);
  assert.equal(next.events[0].text, 'hello');
});

test('latest direct-ID receipt caption matrix is deterministic', () => {
  const cases = [
    { name: 'landed proof delivery', raw: { receipt_state: 'landed', receipt_delivery: 'proof_unavailable' }, expected: 'sent' },
    { name: 'landed unknown delivery', raw: { receipt_state: 'landed', receipt_delivery: 'something_else' }, expected: 'sent' },
    { name: 'accepted proof delivery', raw: { receipt_state: 'accepted', receipt_delivery: 'proof_unavailable' }, expected: 'failed' },
    { name: 'missing state proof delivery', raw: { receipt_delivery: 'proof_unavailable' }, expected: 'failed' },
    { name: 'legacy no fields', raw: undefined, expected: 'sent' },
    { name: 'present but partial', raw: { receipt_state: '' }, expected: 'sending' },
  ] as const;

  for (const [index, entry] of cases.entries()) {
    const reconciled = reconcileOptimisticSendWithServerEvent(
      createOptimistic(),
      OPTIMISTIC_ID,
      serverUserEvent({ optimistic_id: OPTIMISTIC_ID, raw: entry.raw }),
    );
    assert.deepEqual(reconciled.events[0]?.raw, entry.raw, `${entry.name} raw preservation`);
    // Each fixture is an independent state, so give the memoized selector an
    // independent render window key rather than reusing the first fixture's
    // cached detail.
    const row = selectSessionDetail(reconciled, STREAM_ID, { visibleCount: 80 + index })?.transcriptItems[0];
    assert.equal(row?.receiptCaption, entry.expected, entry.name);
  }
});

test('issue #5: direct-ID committed-pending vs partial receipt caption matrix', () => {
  // A busy claude send commits the paste but cannot confirm the pane receipt
  // marker, so the daemon records the receipt in its committed-pending
  // vocabulary — state not_landed/accepted, delivery not_landed/
  // committed_pending_proof/proof_pending (confirmation pending, NOT a failure;
  // comms.py _attempt_send_delivery). The durable USER echo still correlates
  // back to the optimistic row (direct id match, finite correlatedDaemonSeq),
  // and no daemon echo->landed upgrade exists, so those committed states must
  // read Sent instead of sticking at Sending. A blank / whitespace / unknown /
  // both-empty receipt is an unresolved partial and stays Sending; an explicit
  // failure stays Failed. Regression for HJK6/pentacle issue #5 (narrowed per
  // spec-QA: only the daemon's committed vocabulary, not any non-empty value).
  const cases = [
    { name: 'not_landed committed', raw: { receipt_state: 'not_landed', receipt_delivery: 'not_landed' }, expected: 'sent' },
    { name: 'accepted committed_pending_proof', raw: { receipt_state: 'accepted', receipt_delivery: 'committed_pending_proof' }, expected: 'sent' },
    { name: 'accepted proof_pending', raw: { receipt_state: 'accepted', receipt_delivery: 'proof_pending' }, expected: 'sent' },
    { name: 'accepted state, no delivery field', raw: { receipt_state: 'accepted' }, expected: 'sent' },
    { name: 'empty state, committed_pending_proof delivery', raw: { receipt_state: '', receipt_delivery: 'committed_pending_proof' }, expected: 'sent' },
    { name: 'accepted state + proof_unavailable stays Failed', raw: { receipt_state: 'accepted', receipt_delivery: 'proof_unavailable' }, expected: 'failed' },
    { name: 'whitespace state is partial', raw: { receipt_state: '   ' }, expected: 'sending' },
    { name: 'unrecognized state is partial', raw: { receipt_state: 'weird_unknown' }, expected: 'sending' },
    { name: 'both fields empty is partial', raw: { receipt_state: '', receipt_delivery: '' }, expected: 'sending' },
  ] as const;
  for (const [index, entry] of cases.entries()) {
    const reconciled = reconcileOptimisticSendWithServerEvent(
      createOptimistic(),
      OPTIMISTIC_ID,
      serverUserEvent({ optimistic_id: OPTIMISTIC_ID, raw: entry.raw }),
    );
    const row = selectSessionDetail(reconciled, STREAM_ID, { visibleCount: 300 + index })?.transcriptItems[0];
    assert.equal(row?.receiptCaption, entry.expected, entry.name);
  }
});

test('issue #5: committed-pending receipt settles Sent through the live applyPentacleEvent path', () => {
  // Exercise the real live matcher/reducer path (not a direct reconcile call):
  // a dispatched+acked send, then the daemon USER echo carrying its optimistic
  // id and a not_landed receipt, applied via applyPentacleEvent.
  const acked = markOptimisticAckedByRequestId(
    markOptimisticDispatchedByRequestId(createOptimistic(), REQUEST_ID, CREATED_AT + 10),
    REQUEST_ID,
    CREATED_AT + 20,
  );
  const live = applyPentacleEvent(acked, serverUserEvent({
    optimistic_id: OPTIMISTIC_ID,
    raw: { receipt_state: 'not_landed', receipt_delivery: 'not_landed' },
  }));
  const row = selectSessionDetail(live, STREAM_ID, { visibleCount: 'all' })?.transcriptItems[0];
  assert.equal(row?.optimisticId, OPTIMISTIC_ID);
  assert.equal(row?.receiptCaption, 'sent');
});

test('issue #5: committed-pending receipt settles Sent through a snapshot replay', () => {
  const acked = markOptimisticAckedByRequestId(
    markOptimisticDispatchedByRequestId(createOptimistic(), REQUEST_ID, CREATED_AT + 10),
    REQUEST_ID,
    CREATED_AT + 20,
  );
  const replayed = applySnapshotWithOptimisticReconciliation(acked, {
    sessions: [session()],
    events: [serverUserEvent({
      optimistic_id: OPTIMISTIC_ID,
      raw: { receipt_state: 'not_landed', receipt_delivery: 'not_landed' },
    })],
  }, CREATED_AT + 1_000);
  const row = selectSessionDetail(replayed, STREAM_ID, { visibleCount: 'all' })?.transcriptItems[0];
  assert.equal(row?.receiptCaption, 'sent');
});

test('issue #5: a committed-pending echo on another stream never captions this send Sent', () => {
  // Narrowed-authority guard: the caption resolves only a row the reducer bound
  // to THIS send. A not_landed USER echo on a different stream must not settle
  // this stream's still-uncorrelated optimistic row — it stays Sending.
  const otherStream = 'host_c:codex:two';
  const withOther = applyPentacleEvent(createOptimistic(), serverUserEvent({
    stream_id: otherStream,
    session_id: otherStream,
    optimistic_id: 'optimistic_host_c_codex_two_9',
    raw: { receipt_state: 'not_landed', receipt_delivery: 'not_landed' },
  }));
  const row = selectSessionDetail(withOther, STREAM_ID, { visibleCount: 'all' })?.transcriptItems[0];
  assert.equal(row?.receiptCaption, 'sending');
});

test('issue #5: a committed-pending echo settles Sent independent of the send request_id (retry-safe)', () => {
  // Direct-ID matching keys on optimistic_id, not request_id: a retry rotates
  // request_id but retains optimistic_id, so the committed-pending echo still
  // settles the single row to Sent. The caption logic is request_id-independent,
  // so asserting the property directly (rather than a rotated dispatch that the
  // reducer keys by request_id) is the faithful check.
  const dispatched = markOptimisticDispatchedByRequestId(createOptimistic(), REQUEST_ID, CREATED_AT + 10);
  const live = applyPentacleEvent(dispatched, serverUserEvent({
    optimistic_id: OPTIMISTIC_ID,
    raw: { receipt_state: 'accepted', receipt_delivery: 'committed_pending_proof' },
  }));
  const detail = selectSessionDetail(live, STREAM_ID, { visibleCount: 'all' });
  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0]?.receiptCaption, 'sent');
});

test('issue #5: a null correlatedDaemonSeq is not treated as correlated (strict numeric)', () => {
  // Number(null) and Number('') both coerce to 0 (finite); the strict check must
  // keep a null/blank sequence uncorrelated so the caption stays Sending.
  const optimistic = createOptimistic();
  const row = optimistic.events[0];
  const withNullSeq = { ...row, correlatedDaemonSeq: null as unknown as number, receiptDirectMatch: true, raw: { receipt_state: 'not_landed' } };
  const patched = { ...optimistic, events: [withNullSeq] };
  const detail = selectSessionDetail(patched, STREAM_ID, { visibleCount: 'all' });
  assert.equal(detail?.transcriptItems[0]?.receiptCaption, 'sending');
});

test('pre-echo failure remains Sending and a no-ID USER echo has no receipt caption', () => {
  const locallyFailed = markOptimisticFailedByRequestId(createOptimistic(), REQUEST_ID, 'send_error', CREATED_AT + 20);
  const failedRow = selectSessionDetail(locallyFailed, STREAM_ID, { visibleCount: 'all' })?.transcriptItems[0];
  assert.equal(failedRow?.sendState, 'failed');
  assert.equal(failedRow?.receiptCaption, 'sending');

  const noIdEcho = applyPentacleEvent(buildState(), serverUserEvent({
    raw: { receipt_state: 'landed', receipt_delivery: 'landed' },
  }));
  const noIdRow = selectSessionDetail(noIdEcho, STREAM_ID, { visibleCount: 'all' })?.transcriptItems[0];
  assert.equal(noIdRow?.receiptCaption, undefined);
});

test('matching missing-ID fallback clears Sending without manufacturing a receipt caption', () => {
  const reconciled = applyPentacleEvent(
    createOptimistic(),
    serverUserEvent({ raw: { receipt_state: 'landed', receipt_delivery: 'landed' } }),
  );

  assert.equal(reconciled.optimisticSends?.[OPTIMISTIC_ID], undefined);
  const row = selectSessionDetail(reconciled, STREAM_ID, { visibleCount: 201 })?.transcriptItems[0];
  assert.equal(row?.sendState, undefined);
  assert.equal(row?.receiptCaption, undefined);
});

test('later landed echo supersedes proof on one direct-ID row', () => {
  const proof = reconcileOptimisticSendWithServerEvent(
    createOptimistic(),
    OPTIMISTIC_ID,
    serverUserEvent({
      optimistic_id: OPTIMISTIC_ID,
      raw: { receipt_state: 'accepted', receipt_delivery: 'proof_unavailable' },
    }),
  );
  assert.equal(selectSessionDetail(proof, STREAM_ID, { visibleCount: 'all' })?.transcriptItems[0]?.receiptCaption, 'failed');

  const landed = applyPentacleEvent(proof, serverUserEvent({
    optimistic_id: OPTIMISTIC_ID,
    raw: { receipt_state: 'landed', receipt_delivery: 'proof_unavailable' },
  }));
  const detail = selectSessionDetail(landed, STREAM_ID, { visibleCount: 'all' });

  assert.equal(landed.events.length, 1);
  assert.equal(landed.events[0]?.client_origin, true);
  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0]?.receiptCaption, 'sent');
});

test('later proof echo does not downgrade a direct landed row', () => {
  const landed = reconcileOptimisticSendWithServerEvent(
    createOptimistic(),
    OPTIMISTIC_ID,
    serverUserEvent({
      optimistic_id: OPTIMISTIC_ID,
      raw: { receipt_state: 'landed', receipt_delivery: 'accepted' },
    }),
  );
  assert.equal(selectSessionDetail(landed, STREAM_ID, { visibleCount: 'all' })?.transcriptItems[0]?.receiptCaption, 'sent');

  const proof = applyPentacleEvent(landed, serverUserEvent({
    optimistic_id: OPTIMISTIC_ID,
    raw: { receipt_state: 'accepted', receipt_delivery: 'proof_unavailable' },
  }));

  assert.equal(proof.events.length, 1);
  assert.equal(selectSessionDetail(proof, STREAM_ID, { visibleCount: 'all' })?.transcriptItems[0]?.receiptCaption, 'sent');
});

test('direct proof remains Failed through a no-fields snapshot replay', () => {
  const proof = reconcileOptimisticSendWithServerEvent(
    createOptimistic(),
    OPTIMISTIC_ID,
    serverUserEvent({
      optimistic_id: OPTIMISTIC_ID,
      raw: { receipt_state: 'accepted', receipt_delivery: 'proof_unavailable' },
    }),
  );
  const replayed = applySnapshotWithOptimisticReconciliation(
    proof,
    { sessions: [session()], events: [serverUserEvent({ raw: { source: 'snapshot' } })] },
    CREATED_AT + 1_000,
  );

  const row = selectSessionDetail(replayed, STREAM_ID, { visibleCount: 202 })?.transcriptItems[0];
  assert.equal(row?.receiptCaption, 'failed');
});

test('a direct landed snapshot invalidates the Failed receipt selector cache', () => {
  const proof = reconcileOptimisticSendWithServerEvent(
    createOptimistic(),
    OPTIMISTIC_ID,
    serverUserEvent({
      optimistic_id: OPTIMISTIC_ID,
      raw: { receipt_state: 'accepted', receipt_delivery: 'proof_unavailable' },
    }),
  );
  assert.equal(selectSessionDetail(proof, STREAM_ID, { visibleCount: 'all' })?.transcriptItems[0]?.receiptCaption, 'failed');
  const landed = applySnapshotWithOptimisticReconciliation(
    proof,
    { sessions: [session()], events: [serverUserEvent({
      optimistic_id: OPTIMISTIC_ID,
      raw: { receipt_state: 'landed', receipt_delivery: 'proof_unavailable' },
    })] },
    CREATED_AT + 1_000,
  );

  assert.equal(selectSessionDetail(landed, STREAM_ID, { visibleCount: 'all' })?.transcriptItems[0]?.receiptCaption, 'sent');
});

test('direct proof remains Failed through a no-fields history backfill', () => {
  const proof = reconcileOptimisticSendWithServerEvent(
    createOptimistic(),
    OPTIMISTIC_ID,
    serverUserEvent({
      optimistic_id: OPTIMISTIC_ID,
      raw: { receipt_state: 'accepted', receipt_delivery: 'proof_unavailable' },
    }),
  );
  const replayed = applyFetchedStreamEvents(
    proof,
    [serverUserEvent({ raw: { source: 'history' } })],
  );

  const row = selectSessionDetail(replayed, STREAM_ID, { visibleCount: 203 })?.transcriptItems[0];
  assert.equal(row?.receiptCaption, 'failed');
});

test('direct legacy echo is Sent only when no receipt state was previously observed', () => {
  const reconciled = reconcileOptimisticSendWithServerEvent(
    createOptimistic(),
    OPTIMISTIC_ID,
    serverUserEvent({ optimistic_id: OPTIMISTIC_ID }),
  );

  const row = selectSessionDetail(reconciled, STREAM_ID, { visibleCount: 204 })?.transcriptItems[0];
  assert.equal(row?.receiptCaption, 'sent');
});

test('direct receipt lattice covers every state and echo kind', () => {
  const proofRaw = { receipt_state: 'accepted', receipt_delivery: 'proof_unavailable' };
  const landedRaw = { receipt_state: 'landed', receipt_delivery: 'accepted' };
  const noFieldsRaw = { source: 'receipt-lattice-table' };
  const states = {
    Sending: () => createOptimistic(),
    LegacySent: () => reconcileOptimisticSendWithServerEvent(
      createOptimistic(),
      OPTIMISTIC_ID,
      serverUserEvent({ optimistic_id: OPTIMISTIC_ID, raw: noFieldsRaw }),
    ),
    Failed: () => reconcileOptimisticSendWithServerEvent(
      createOptimistic(),
      OPTIMISTIC_ID,
      serverUserEvent({ optimistic_id: OPTIMISTIC_ID, raw: proofRaw }),
    ),
    Landed: () => reconcileOptimisticSendWithServerEvent(
      createOptimistic(),
      OPTIMISTIC_ID,
      serverUserEvent({ optimistic_id: OPTIMISTIC_ID, raw: landedRaw }),
    ),
  } as const;
  const echoKinds = [
    { name: 'no fields', raw: noFieldsRaw },
    { name: 'proof unavailable', raw: proofRaw },
    { name: 'landed', raw: landedRaw },
  ] as const;
  const identities = [
    { name: 'id matched', optimisticId: OPTIMISTIC_ID, expectedKey: 'matched' },
    { name: 'no id', optimisticId: undefined, expectedKey: 'unmatched' },
  ] as const;
  const expected = {
    Sending: { matched: ['sent', 'failed', 'sent'], unmatched: [undefined, undefined, undefined] },
    LegacySent: { matched: ['sent', 'failed', 'sent'], unmatched: ['sent', 'sent', 'sent'] },
    Failed: { matched: ['failed', 'failed', 'sent'], unmatched: ['failed', 'failed', 'failed'] },
    Landed: { matched: ['sent', 'sent', 'sent'], unmatched: ['sent', 'sent', 'sent'] },
  } as const;

  for (const [stateName, makeState] of Object.entries(states) as Array<[
    keyof typeof states,
    () => PentacleStreamState,
  ]>) {
    for (const identity of identities) {
      for (const [echoIndex, echo] of echoKinds.entries()) {
        const next = applyPentacleEvent(makeState(), serverUserEvent({
          optimistic_id: identity.optimisticId,
          raw: echo.raw,
        }));
        const row = selectSessionDetail(next, STREAM_ID, { visibleCount: 300 + (Object.keys(states).indexOf(stateName) * 6) + (identities.indexOf(identity) * 3) + echoIndex })?.transcriptItems[0];

        assert.equal(next.events.length, 1, `${stateName} + ${identity.name} + ${echo.name} keeps one row`);
        assert.equal(row?.receiptCaption, expected[stateName][identity.expectedKey][echoIndex], `${stateName} + ${identity.name} + ${echo.name}`);
      }
    }
  }
});

test('no-ID proof and landed replays preserve direct receipt states', () => {
  const proofRaw = { receipt_state: 'accepted', receipt_delivery: 'proof_unavailable' };
  const landedRaw = { receipt_state: 'landed', receipt_delivery: 'accepted' };
  const states = {
    LegacySent: {
      make: () => reconcileOptimisticSendWithServerEvent(
        createOptimistic(),
        OPTIMISTIC_ID,
        serverUserEvent({ optimistic_id: OPTIMISTIC_ID, raw: { source: 'legacy' } }),
      ),
      expected: 'sent',
    },
    Failed: {
      make: () => reconcileOptimisticSendWithServerEvent(
        createOptimistic(),
        OPTIMISTIC_ID,
        serverUserEvent({ optimistic_id: OPTIMISTIC_ID, raw: proofRaw }),
      ),
      expected: 'failed',
    },
    Landed: {
      make: () => reconcileOptimisticSendWithServerEvent(
        createOptimistic(),
        OPTIMISTIC_ID,
        serverUserEvent({ optimistic_id: OPTIMISTIC_ID, raw: landedRaw }),
      ),
      expected: 'sent',
    },
  } as const;
  const replays = [
    {
      name: 'snapshot',
      apply: (state: PentacleStreamState, event: PentacleEvent) => applySnapshotWithOptimisticReconciliation(
        state,
        { sessions: [session()], events: [event] },
        CREATED_AT + 1_000,
      ),
    },
    {
      name: 'history',
      apply: (state: PentacleStreamState, event: PentacleEvent) => applyFetchedStreamEvents(state, [event]),
    },
  ] as const;
  const echoes = [
    { name: 'proof unavailable', raw: proofRaw },
    { name: 'landed', raw: landedRaw },
  ] as const;

  for (const [stateName, state] of Object.entries(states) as Array<[
    keyof typeof states,
    (typeof states)[keyof typeof states],
  ]>) {
    for (const replay of replays) {
      for (const [echoIndex, echo] of echoes.entries()) {
        const next = replay.apply(state.make(), serverUserEvent({
          optimistic_id: undefined,
          raw: echo.raw,
        }));
        const row = selectSessionDetail(next, STREAM_ID, { visibleCount: 400 + (Object.keys(states).indexOf(stateName) * 4) + (replays.indexOf(replay) * 2) + echoIndex })?.transcriptItems[0];

        assert.equal(next.events.length, 1, `${stateName} + ${replay.name} + ${echo.name} keeps one row`);
        assert.equal(row?.receiptCaption, state.expected, `${stateName} + ${replay.name} + ${echo.name}`);
      }
    }
  }
});

test('only the comparator-selected latest user row projects a receipt caption', () => {
  const first = reconcileOptimisticSendWithServerEvent(
    createOptimistic(),
    OPTIMISTIC_ID,
    serverUserEvent({
      daemon_seq: 41,
      optimistic_id: OPTIMISTIC_ID,
      raw: { receipt_state: 'accepted', receipt_delivery: 'proof_unavailable' },
    }),
  );
  const second: PentacleEvent = {
    ...serverUserEvent({ daemon_seq: Number.NaN, timestamp: '2026-05-16T12:00:02.000Z' }),
    client_origin: true,
    optimistic_id: 'optimistic_host_c_codex_one_2',
    correlatedDaemonSeq: 42,
    pending: false,
    raw: { receipt_state: 'landed' },
    receiptDirectMatch: true,
  };
  const state = {
    ...first,
    events: [...first.events, second],
    eventContentVersionByStream: { [STREAM_ID]: 2 },
  };
  const detail = selectSessionDetail(state, STREAM_ID, { visibleCount: 1 });

  assert.deepEqual(detail?.transcriptItems.map((item) => item.receiptCaption), ['sent']);
});

test('acked optimistic row no longer renders as pending before the server echo arrives', () => {
  const queued = createOptimistic();
  const dispatched = markOptimisticDispatchedByRequestId(queued, REQUEST_ID, CREATED_AT + 10);
  const acked = markOptimisticAckedByRequestId(dispatched, REQUEST_ID, CREATED_AT + 20);

  const detail = selectSessionDetail(acked, STREAM_ID, { visibleCount: 'all' });
  const row = detail?.transcriptItems.find((item) => item.optimisticId === OPTIMISTIC_ID);

  assert.equal(row?.pending, false);
  assert.equal(row?.sendState, undefined);
  assert.equal(getSessionSendingState(acked, STREAM_ID).sending, false);
});

test('local stream rows render through a transient missing session summary', () => {
  const queued = createOptimistic();
  const summaryGap = {
    ...queued,
    sessions: [],
  };

  const detail = selectSessionDetail(summaryGap, STREAM_ID, { visibleCount: 'all' });

  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].optimisticId, OPTIMISTIC_ID);
  assert.equal(detail?.transcriptItems[0].sendState, 'sending');
});

test('inventory gaps do not suppress placeholder live rows or queued terminal rows', () => {
  const queued = createOptimistic();
  const firstGap = applyPentacleSessionInventory(queued, [], CREATED_AT + 100);
  const withAssist = applyPentacleEvent(firstGap, serverUserEvent({
    daemon_seq: 73,
    kind: 'ASSIST',
    text: 'assistant live into placeholder',
    optimistic_id: undefined,
  }));
  const withTerminalUser = applyPentacleEvent(withAssist, serverUserEvent({
    daemon_seq: 74,
    timestamp: '2026-05-16T12:00:02.000Z',
    text: 'queued terminal steering message',
    optimistic_id: undefined,
    raw: { source: 'terminal' },
  }));

  const secondGap = applyPentacleSessionInventory(withTerminalUser, [], CREATED_AT + 200);
  const detail = selectSessionDetail(secondGap, STREAM_ID, { visibleCount: 'all' });
  const texts = detail?.transcriptItems.map((item) => item.text) ?? [];

  assert.equal(detail?.transcriptItems.some((item) => item.optimisticId === OPTIMISTIC_ID), true);
  assert.equal(texts.includes('assistant live into placeholder'), true);
  assert.equal(texts.includes('queued terminal steering message'), true);
});

test('summary-mode snapshot gaps keep acked optimistic rows non-pending', () => {
  const dispatched = markOptimisticDispatchedByRequestId(createOptimistic(), REQUEST_ID, CREATED_AT + 10);
  const acked = markOptimisticAckedByRequestId(dispatched, REQUEST_ID, CREATED_AT + 20);

  const gap = applySnapshotWithOptimisticReconciliation(
    acked,
    { sessions: [], events: [] },
    CREATED_AT + 30,
  );
  const row = selectSessionDetail(gap, STREAM_ID, { visibleCount: 'all' })
    ?.transcriptItems.find((item) => item.optimisticId === OPTIMISTIC_ID);

  assert.equal(row?.pending, false);
  assert.equal(row?.sendState, undefined);
});

test('live ASSIST apply preserves the committed row render model', () => {
  const next = applyPentacleEvent(buildState(), serverUserEvent({
    daemon_seq: 70,
    kind: 'ASSIST',
    text: 'assistant live reply',
    optimistic_id: undefined,
  }));

  const detail = selectSessionDetail(next, STREAM_ID, { visibleCount: 'all' });

  assert.equal(next.events.length, 1);
  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].kind, 'ASSIST');
  assert.equal(detail?.transcriptItems[0].text, 'assistant live reply');
  assert.equal(detail?.transcriptItems[0].sendState, undefined);
  assert.equal(detail?.transcriptItems[0].optimisticId, undefined);
});

test('live USER echo with optimistic id reconciles through applyPentacleEvent', () => {
  const dispatched = markOptimisticDispatchedByRequestId(createOptimistic(), REQUEST_ID, CREATED_AT + 10);

  const next = applyPentacleEvent(dispatched, serverUserEvent({
    daemon_seq: 71,
    optimistic_id: OPTIMISTIC_ID,
  }));
  const row = selectSessionDetail(next, STREAM_ID, { visibleCount: 'all' })?.transcriptItems[0];

  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID], undefined);
  assert.equal(next.optimisticByRequestId?.[REQUEST_ID], undefined);
  assert.equal(next.events.length, 1);
  assert.equal(row?.optimisticId, OPTIMISTIC_ID);
  assert.equal(row?.sendState, undefined);
  assert.equal((row as any).correlatedDaemonSeq, 71);
});

test('duplicate live delivery does not duplicate persisted or rendered rows', () => {
  const duplicate = serverUserEvent({
    daemon_seq: 72,
    optimistic_id: undefined,
    text: 'duplicate delivery',
  });
  const once = applyPentacleEvent(buildState(), duplicate);
  const twice = applyPentacleEvent(once, duplicate);

  const detail = selectSessionDetail(twice, STREAM_ID, { visibleCount: 'all' });

  assert.equal(twice.events.filter((event) => Number(event.daemon_seq) === 72).length, 1);
  assert.equal(detail?.transcriptItems.filter((item) => item.text === 'duplicate delivery').length, 1);
});

test('late indeterminate same-text server echo reconciles instead of leaving a NaN optimistic zombie', () => {
  const dispatched = markOptimisticDispatchedByRequestId(createOptimistic(), REQUEST_ID, CREATED_AT + 10);
  const indeterminate = markOptimisticIndeterminateByRequestId(dispatched, REQUEST_ID, CREATED_AT + 61_000);

  const next = applyPentacleEvent(indeterminate, lateServerUserEvent());
  const detail = selectSessionDetail(next, STREAM_ID, { visibleCount: 'all' });

  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID], undefined);
  assert.equal(next.optimisticByRequestId?.[REQUEST_ID], undefined);
  assert.equal(next.events.length, 1);
  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].optimisticId, OPTIMISTIC_ID);
  assert.equal(detail?.transcriptItems[0].sendState, undefined);
  assert.equal((detail?.transcriptItems[0] as any).correlatedDaemonSeq, 42);
  assert.equal(
    next.events.some((event) => (
      event.optimistic_id === OPTIMISTIC_ID &&
      event.client_origin === true &&
      Number.isNaN(event.daemon_seq) &&
      !Number.isFinite(Number(event.correlatedDaemonSeq))
    )),
    false,
  );
});

test('ambiguous late same-text echo does not collapse or absorb a second legitimate send', () => {
  const first = markOptimisticIndeterminateByRequestId(
    markOptimisticDispatchedByRequestId(createOptimistic(), REQUEST_ID, CREATED_AT + 10),
    REQUEST_ID,
    CREATED_AT + 61_000,
  );
  const secondOptimisticId = 'optimistic_host_c_codex_one_2';
  const secondRequestId = 'send-req-2';
  const withSecond = markOptimisticDispatchedByRequestId(
    sendOptimisticMessage(first, {
      streamId: STREAM_ID,
      text: 'hello',
      optimisticId: secondOptimisticId,
      requestId: secondRequestId,
      createdAt: CREATED_AT + 70_000,
      windowStartedAt: CREATED_AT + 70_000,
    }),
    secondRequestId,
    CREATED_AT + 70_010,
  );

  const next = applyPentacleEvent(withSecond, lateServerUserEvent({ daemon_seq: 43 }));

  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID]?.status, 'indeterminate');
  assert.equal(next.optimisticSends?.[secondOptimisticId]?.status, 'dispatched');
  assert.equal(next.events.some((event) => event.optimistic_id === secondOptimisticId && event.client_origin === true), true);
  assert.equal(next.events.some((event) => Number(event.daemon_seq) === 43 && event.optimistic_id === undefined), true);
});

test('dispatched optimistic send keeps sending state until its unambiguous echo arrives', () => {
  const dispatched = markOptimisticDispatchedByRequestId(createOptimistic(), REQUEST_ID, CREATED_AT + 10);

  assert.equal(getSessionSendingState(dispatched, STREAM_ID).sending, true);
  assert.equal(
    selectSessionDetail(dispatched, STREAM_ID, { visibleCount: 'all' })
      ?.transcriptItems.find((item) => item.optimisticId === OPTIMISTIC_ID)?.sendState,
    'sending',
  );

  const next = applyPentacleEvent(dispatched, serverUserEvent({ text: 'different server row', daemon_seq: 44 }));

  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID]?.status, 'dispatched');
  assert.equal(
    selectSessionDetail(next, STREAM_ID, { visibleCount: 'all' })
      ?.transcriptItems.find((item) => item.optimisticId === OPTIMISTIC_ID)?.sendState,
    'sending',
  );
});

test('terminal optimistic sends are not resurrected by late same-text fallback echoes', () => {
  const dispatched = markOptimisticDispatchedByRequestId(createOptimistic(), REQUEST_ID, CREATED_AT + 10);
  const terminalStates = [
    {
      name: 'cancelled',
      state: markOptimisticCancelledByRequestId(dispatched, REQUEST_ID, CREATED_AT + 20),
      expectedStatus: 'cancelled',
    },
    {
      name: 'failed',
      state: markOptimisticFailedByRequestId(dispatched, REQUEST_ID, 'send_error', CREATED_AT + 20),
      expectedStatus: 'failed',
    },
    {
      name: 'returned_to_prompt',
      state: markOptimisticReturnedToPromptByOptimisticId(dispatched, OPTIMISTIC_ID, CREATED_AT + 20),
      expectedStatus: 'returned_to_prompt',
    },
  ] as const;

  for (const entry of terminalStates) {
    const next = applyPentacleEvent(entry.state, lateServerUserEvent({ daemon_seq: 50 }));
    assert.equal(next.optimisticSends?.[OPTIMISTIC_ID]?.status, entry.expectedStatus, entry.name);
    assert.equal(next.events.some((event) => Number(event.daemon_seq) === 50 && event.optimistic_id === undefined), true, entry.name);
  }
});

test('enqueueOptimisticMessage compatibility path remains reconcilable', () => {
  const nativeQueued = enqueueOptimisticMessage(buildState(), {
    streamId: STREAM_ID,
    text: 'hello',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: CREATED_AT,
    queuedAt: CREATED_AT,
  });

  assert.equal(nativeQueued.optimisticSends?.[OPTIMISTIC_ID]?.queued_at, CREATED_AT);
  const queuedRow = selectSessionDetail(nativeQueued, STREAM_ID, { visibleCount: 'all' })
    ?.transcriptItems.find((item) => item.optimisticId === OPTIMISTIC_ID) as
      | { queuedWhileWorking?: boolean }
      | undefined;
  assert.equal(queuedRow?.queuedWhileWorking, true);

  const next = applyPentacleEvent(nativeQueued, lateServerUserEvent({
    daemon_seq: 55,
    optimistic_id: OPTIMISTIC_ID,
  }));

  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID], undefined);
  const reconciledEvent = next.events.find(
    (event) => event.optimistic_id === OPTIMISTIC_ID && event.pending === false,
  ) as (PentacleEvent & { queued_at?: number }) | undefined;
  assert.equal(reconciledEvent?.queued_at, CREATED_AT);
  const reconciledRow = selectSessionDetail(next, STREAM_ID, { visibleCount: 'all' })
    ?.transcriptItems.find((item) => item.optimisticId === OPTIMISTIC_ID) as
      | { queuedWhileWorking?: boolean }
      | undefined;
  assert.equal(reconciledRow?.queuedWhileWorking, true);
});

test('two queued-origin sends keep independent origin when USER echoes reconcile in reverse order', () => {
  const secondOptimisticId = `${OPTIMISTIC_ID}_second`;
  const secondRequestId = `${REQUEST_ID}-second`;
  const first = enqueueOptimisticMessage(buildState(), {
    streamId: STREAM_ID,
    text: 'first',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: CREATED_AT,
    queuedAt: CREATED_AT,
  });
  const both = enqueueOptimisticMessage(first, {
    streamId: STREAM_ID,
    text: 'second',
    optimisticId: secondOptimisticId,
    requestId: secondRequestId,
    createdAt: CREATED_AT + 1,
    queuedAt: CREATED_AT + 1,
  });

  const secondFirst = applyPentacleEvent(both, lateServerUserEvent({
    daemon_seq: 56,
    text: 'second',
    optimistic_id: secondOptimisticId,
  }));
  const reconciled = applyPentacleEvent(secondFirst, lateServerUserEvent({
    daemon_seq: 57,
    text: 'first',
    optimistic_id: OPTIMISTIC_ID,
  }));

  const eventsById = new Map(reconciled.events.map((event) => [event.optimistic_id, event]));
  assert.equal(eventsById.get(OPTIMISTIC_ID)?.queued_at, CREATED_AT);
  assert.equal(eventsById.get(secondOptimisticId)?.queued_at, CREATED_AT + 1);
  const items = selectSessionDetail(reconciled, STREAM_ID, { visibleCount: 'all' })?.transcriptItems ?? [];
  assert.equal(items.find((item) => item.optimisticId === OPTIMISTIC_ID)?.queuedWhileWorking, true);
  assert.equal(items.find((item) => item.optimisticId === secondOptimisticId)?.queuedWhileWorking, true);
});

test('same-text server row from another stream is not absorbed by an optimistic send', () => {
  const dispatched = markOptimisticDispatchedByRequestId(createOptimistic(), REQUEST_ID, CREATED_AT + 10);
  const next = applyPentacleEvent(
    dispatched,
    lateServerUserEvent({
      daemon_seq: 56,
      stream_id: 'host_c:codex:two',
      session_id: 'host_c:codex:two',
    }),
  );

  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID]?.status, 'dispatched');
  assert.equal(next.events.some((event) => Number(event.daemon_seq) === 56 && event.optimistic_id === undefined), true);
});

test('live duplicate of an older same-stream same-text row does not absorb a newer optimistic send', () => {
  const staleRow = serverUserEvent({
    daemon_seq: 57,
    timestamp: '2026-05-16T12:00:01.000Z',
    optimistic_id: undefined,
  });
  const optimistic = sendOptimisticMessage(buildState({ events: [staleRow] }), {
    streamId: STREAM_ID,
    text: 'hello',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: CREATED_AT + 70_000,
    windowStartedAt: CREATED_AT + 70_000,
  });
  const dispatched = markOptimisticDispatchedByRequestId(optimistic, REQUEST_ID, CREATED_AT + 70_010);
  const indeterminate = markOptimisticIndeterminateByRequestId(dispatched, REQUEST_ID, CREATED_AT + 131_000);

  const next = applyPentacleEvent(indeterminate, staleRow);

  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID]?.status, 'indeterminate');
  assert.equal(next.events.some((event) => Number(event.daemon_seq) === 57 && event.optimistic_id === undefined), true);
  assert.equal(next.events.some((event) => event.optimistic_id === OPTIMISTIC_ID && event.client_origin === true), true);
  assert.equal(
    next.events.some((event) => event.optimistic_id === OPTIMISTIC_ID && Number.isFinite(Number(event.correlatedDaemonSeq))),
    false,
  );
});

test('same-text server row without parseable timestamp does not absorb a recent optimistic send through the reconcile window', () => {
  const now = Date.now();
  const optimistic = sendOptimisticMessage(buildState(), {
    streamId: STREAM_ID,
    text: 'hello',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: now,
    windowStartedAt: now,
  });
  const dispatched = markOptimisticDispatchedByRequestId(optimistic, REQUEST_ID, now + 10);

  for (const timestamp of [undefined, 'not-a-real-timestamp'] as const) {
    const next = applyPentacleEvent(
      dispatched,
      serverUserEvent({ daemon_seq: timestamp === undefined ? 59 : 60, optimistic_id: undefined, timestamp }),
    );

    assert.equal(next.optimisticSends?.[OPTIMISTIC_ID]?.status, 'dispatched', String(timestamp));
    assert.equal(next.events.some((event) => event.optimistic_id === OPTIMISTIC_ID && event.client_origin === true), true, String(timestamp));
    assert.equal(
      next.events.some((event) => Number(event.daemon_seq) === (timestamp === undefined ? 59 : 60) && event.optimistic_id === undefined),
      true,
      String(timestamp),
    );
  }
});

test('same-text server row without parseable timestamp does not absorb an out-of-window optimistic send through late collapse', () => {
  const createdAt = Date.now() - 120_000;
  const optimistic = sendOptimisticMessage(buildState(), {
    streamId: STREAM_ID,
    text: 'hello',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt,
    windowStartedAt: createdAt,
  });
  const dispatched = markOptimisticDispatchedByRequestId(optimistic, REQUEST_ID, createdAt + 10);
  const indeterminate = markOptimisticIndeterminateByRequestId(dispatched, REQUEST_ID, createdAt + 61_000);

  for (const timestamp of [undefined, 'not-a-real-timestamp'] as const) {
    const next = applyPentacleEvent(
      indeterminate,
      serverUserEvent({ daemon_seq: timestamp === undefined ? 61 : 62, optimistic_id: undefined, timestamp }),
    );

    assert.equal(next.optimisticSends?.[OPTIMISTIC_ID]?.status, 'indeterminate', String(timestamp));
    assert.equal(next.events.some((event) => event.optimistic_id === OPTIMISTIC_ID && event.client_origin === true), true, String(timestamp));
    assert.equal(
      next.events.some((event) => Number(event.daemon_seq) === (timestamp === undefined ? 61 : 62) && event.optimistic_id === undefined),
      true,
      String(timestamp),
    );
  }
});

test('same-text server row with a real newer timestamp still reconciles an out-of-window optimistic send', () => {
  const createdAt = Date.now() - 120_000;
  const optimistic = sendOptimisticMessage(buildState(), {
    streamId: STREAM_ID,
    text: 'hello',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt,
    windowStartedAt: createdAt,
  });
  const dispatched = markOptimisticDispatchedByRequestId(optimistic, REQUEST_ID, createdAt + 10);
  const indeterminate = markOptimisticIndeterminateByRequestId(dispatched, REQUEST_ID, createdAt + 61_000);

  const next = applyPentacleEvent(
    indeterminate,
    serverUserEvent({
      daemon_seq: 63,
      optimistic_id: undefined,
      timestamp: new Date(createdAt + 90_000).toISOString(),
    }),
  );

  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID], undefined);
  assert.equal(next.events.length, 1);
  assert.equal(next.events[0].optimistic_id, OPTIMISTIC_ID);
  assert.equal((next.events[0] as any).correlatedDaemonSeq, 63);
});

test('exact optimistic-id echo reconciles regardless of server timestamp', () => {
  const createdAt = Date.now() - 120_000;
  const optimistic = sendOptimisticMessage(buildState(), {
    streamId: STREAM_ID,
    text: 'hello',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt,
    windowStartedAt: createdAt,
  });
  const dispatched = markOptimisticDispatchedByRequestId(optimistic, REQUEST_ID, createdAt + 10);

  const next = applyPentacleEvent(
    dispatched,
    serverUserEvent({
      daemon_seq: 64,
      optimistic_id: OPTIMISTIC_ID,
      timestamp: undefined,
    }),
  );

  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID], undefined);
  assert.equal(next.events.length, 1);
  assert.equal(next.events[0].optimistic_id, OPTIMISTIC_ID);
  assert.equal((next.events[0] as any).correlatedDaemonSeq, 64);
});

test('returned-to-prompt server USER retracts the optimistic bubble and preserves recoverable draft text', () => {
  const dispatched = markOptimisticDispatchedByRequestId(createOptimistic(), REQUEST_ID, CREATED_AT + 10);

  const next = reconcileOptimisticSendWithServerEvent(
    dispatched,
    OPTIMISTIC_ID,
    serverUserEvent({
      raw: {
        returned_to_prompt: true,
        user_delivery_state: 'returned_to_prompt',
      },
    }),
  );

  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID]?.status, 'returned_to_prompt');
  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID]?.text, 'hello');
  assert.equal(next.optimisticByRequestId?.[REQUEST_ID], undefined);
  assert.equal(next.events.some((event) => event.optimistic_id === OPTIMISTIC_ID), false);
  assert.equal(selectSessionDetail(next, STREAM_ID, { visibleCount: 'all' })?.transcriptItems.length, 0);
});

test('late returned-to-prompt reclassification retracts an already reconciled optimistic row', () => {
  const dispatched = markOptimisticDispatchedByRequestId(createOptimistic(), REQUEST_ID, CREATED_AT + 10);
  const echoed = reconcileOptimisticSendWithServerEvent(
    dispatched,
    OPTIMISTIC_ID,
    serverUserEvent({
      raw: { jsonl_record_uuid: 'jsonl-user-u' },
    }),
  );

  assert.equal(echoed.optimisticSends?.[OPTIMISTIC_ID], undefined);
  assert.equal(selectSessionDetail(echoed, STREAM_ID, { visibleCount: 'all' })?.transcriptItems.length, 1);

  const reclassified = applyPentacleEvent(
    echoed,
    serverUserEvent({
      daemon_seq: 43,
      timestamp: '2026-05-16T12:00:03.000Z',
      raw: {
        jsonl_record_uuid: 'jsonl-user-u',
        user_delivery_state: 'returned_to_prompt',
        returned_to_prompt: true,
      },
    }),
  );

  assert.equal(reclassified.optimisticSends?.[OPTIMISTIC_ID]?.status, 'returned_to_prompt');
  assert.equal(reclassified.optimisticSends?.[OPTIMISTIC_ID]?.text, 'hello');
  assert.equal(reclassified.events.some((event) => event.optimistic_id === OPTIMISTIC_ID), false);
  assert.equal(selectSessionDetail(reclassified, STREAM_ID, { visibleCount: 'all' })?.transcriptItems.length, 0);
});

test('server returned-to-prompt USER rows are omitted even without optimistic state', () => {
  const next = applyPentacleEvent(
    buildState(),
    serverUserEvent({
      raw: {
        jsonl_record_uuid: 'jsonl-user-u',
        user_delivery_state: 'returned_to_prompt',
        returned_to_prompt: true,
      },
    }),
  );

  assert.equal(next.events.length, 1);
  assert.equal(selectSessionDetail(next, STREAM_ID, { visibleCount: 'all' })?.transcriptItems.length, 0);
});

test('direct optimistic reconcile preserves local attachment render URI while adopting server blob ref', () => {
  const optimistic = sendOptimisticMessage(buildState(), {
    streamId: STREAM_ID,
    text: 'caption',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: CREATED_AT,
    windowStartedAt: CREATED_AT,
    attachments: [optimisticAttachment()],
  });
  const dispatched = markOptimisticDispatchedByRequestId(optimistic, REQUEST_ID, CREATED_AT + 10);

  const next = reconcileOptimisticSendWithServerEvent(
    dispatched,
    OPTIMISTIC_ID,
    serverUserEvent({
      text: 'caption',
      optimistic_id: OPTIMISTIC_ID,
      attachments: [{ key: 'f'.repeat(64), mime: 'image/jpeg' }],
    }),
  );

  const row = selectSessionDetail(next, STREAM_ID, { visibleCount: 'all' })?.transcriptItems[0];
  assert.equal(row?.text, 'caption');
  assert.equal(row?.attachments?.[0].key, 'f'.repeat(64));
  assert.equal((row?.attachments?.[0] as ChatAttachment & { uri?: string }).uri, 'file:///tmp/photo-a.jpg');
  assert.equal(row?.attachments?.[0].width, 120);
  assert.equal(row?.pending, false);
  assert.equal(row?.sendState, undefined);
  assert.equal(JSON.stringify(row?.attachments ?? []).includes('localPath'), false);
});

test('captionless attachment optimistic reconcile keeps the image row renderable', () => {
  const optimistic = sendOptimisticMessage(buildState(), {
    streamId: STREAM_ID,
    text: '',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: CREATED_AT,
    windowStartedAt: CREATED_AT,
    attachments: [optimisticAttachment()],
  });

  const next = reconcileOptimisticSendWithServerEvent(
    optimistic,
    OPTIMISTIC_ID,
    serverUserEvent({
      text: '',
      optimistic_id: OPTIMISTIC_ID,
      attachments: [{ key: 'e'.repeat(64), mime: 'image/jpeg' }],
    }),
  );

  const detail = selectSessionDetail(next, STREAM_ID, { visibleCount: 'all' });
  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].text, '');
  assert.equal((detail?.transcriptItems[0].attachments?.[0] as ChatAttachment & { uri?: string }).uri, 'file:///tmp/photo-a.jpg');
});

test('snapshot optimistic reconcile removes indexes and converts the server echo into the stable optimistic row', () => {
  const dispatched = markOptimisticDispatchedByRequestId(createOptimistic(), REQUEST_ID, CREATED_AT + 10);

  const next = applySnapshotWithOptimisticReconciliation(
    dispatched,
    { sessions: [session()], events: [serverUserEvent()] },
    CREATED_AT + 2_000,
  );

  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID], undefined);
  assert.equal(next.optimisticByRequestId?.[REQUEST_ID], undefined);
  assert.equal(next.events.length, 1);
  assert.equal(next.events[0].optimistic_id, OPTIMISTIC_ID);
  assert.equal(Number.isNaN(next.events[0].daemon_seq), true);
  assert.equal((next.events[0] as any).correlatedDaemonSeq, 42);
  assert.equal(next.events[0].pending, false);
});

test('a cancelled optimistic send is excluded from snapshot reconcile so it never resurrects as a sent row', () => {
  const dispatched = markOptimisticDispatchedByRequestId(createOptimistic(), REQUEST_ID, CREATED_AT + 10);
  const canceled = markOptimisticCancelledByRequestId(dispatched, REQUEST_ID, CREATED_AT + 40);

  const next = applySnapshotWithOptimisticReconciliation(
    canceled,
    { sessions: [session()], events: [serverUserEvent({ daemon_seq: 99 })] },
    CREATED_AT + 2_000,
  );

  // The cancelled send is preserved (terminal), not pruned away by a reconcile.
  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID]?.status, 'cancelled');
  // The same-text server USER echo stays a server-origin row — it was NOT
  // correlated onto the cancelled optimistic id.
  const serverRow = next.events.find((event) => Number(event.daemon_seq) === 99);
  assert.equal(serverRow?.optimistic_id, undefined);
});

test('snapshot optimistic reconcile uses optimistic id when multiline server echo text differs', () => {
  const queued = sendOptimisticMessage(buildState(), {
    streamId: STREAM_ID,
    text: 'first line\nsecond line\nthird line',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: CREATED_AT,
    queuedAt: CREATED_AT,
    windowStartedAt: CREATED_AT,
  });
  const dispatched = markOptimisticDispatchedByRequestId(queued, REQUEST_ID, CREATED_AT + 10);

  const next = applySnapshotWithOptimisticReconciliation(
    dispatched,
    {
      sessions: [session()],
      events: [
        serverUserEvent({
          daemon_seq: 43,
          text: 'first line second line third line',
          optimistic_id: OPTIMISTIC_ID,
          correlatedDaemonSeq: 43,
        } as Partial<PentacleEvent>),
      ],
    },
    CREATED_AT + 2_000,
  );

  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID], undefined);
  assert.equal(next.optimisticByRequestId?.[REQUEST_ID], undefined);
  assert.equal(next.events.length, 1);
  assert.equal(next.events[0].optimistic_id, OPTIMISTIC_ID);
  assert.equal((next.events[0] as any).correlatedDaemonSeq, 43);
  assert.equal(next.events[0].pending, false);
  assert.equal(next.events[0].queued_at, CREATED_AT);
  assert.equal(next.events[0].text, 'first line second line third line');
  assert.equal(
    selectSessionDetail(next, STREAM_ID, { visibleCount: 'all' })?.transcriptItems[0]?.queuedWhileWorking,
    true,
  );
});

test('optimistic matcher refuses text fallback when a mismatched optimistic id is present', () => {
  const send = {
    stream_id: STREAM_ID,
    text: 'hello',
    optimistic_id: OPTIMISTIC_ID,
    created_at: CREATED_AT,
  };

  assert.equal(
    optimisticMatchesServerUser(send, serverUserEvent({ optimistic_id: 'optimistic_stale_send_a' }), 60_000),
    false,
  );
  assert.equal(
    optimisticMatchesServerUser(send, serverUserEvent({ optimistic_id: undefined }), 60_000),
    true,
  );
});

test('snapshot replay keeps a previously reconciled optimistic row correlated to the same daemon sequence', () => {
  const prior = {
    ...buildState(),
    events: [{
      ...serverUserEvent(),
      daemon_seq: Number.NaN,
      correlatedDaemonSeq: 42,
      client_origin: true,
      optimistic_id: OPTIMISTIC_ID,
      pending: false,
      queued_at: CREATED_AT,
    } as any],
  };

  const next = applySnapshotWithOptimisticReconciliation(
    prior,
    { sessions: [session()], events: [serverUserEvent()] },
    CREATED_AT + 3_000,
  );

  assert.equal(next.events.length, 1);
  assert.equal(next.events[0].optimistic_id, OPTIMISTIC_ID);
  assert.equal(Number.isNaN(next.events[0].daemon_seq), true);
  assert.equal((next.events[0] as any).correlatedDaemonSeq, 42);
  assert.equal(next.events[0].pending, false);
  assert.equal(next.events[0].queued_at, CREATED_AT);
});

test('snapshot stale same-stream same-text row already correlated elsewhere does not absorb a newer optimistic send', () => {
  const previousOptimisticId = 'optimistic_host_c_codex_one_previous';
  const staleRow = serverUserEvent({
    daemon_seq: 58,
    timestamp: '2026-05-16T12:00:01.000Z',
    optimistic_id: undefined,
  });
  const previousCorrelatedRow = {
    ...staleRow,
    daemon_seq: Number.NaN,
    correlatedDaemonSeq: 58,
    client_origin: true,
    optimistic_id: previousOptimisticId,
    pending: false,
  } as PentacleEvent;
  const optimistic = sendOptimisticMessage(buildState({ events: [previousCorrelatedRow] }), {
    streamId: STREAM_ID,
    text: 'hello',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: CREATED_AT + 70_000,
    windowStartedAt: CREATED_AT + 70_000,
  });
  const dispatched = markOptimisticDispatchedByRequestId(optimistic, REQUEST_ID, CREATED_AT + 70_010);
  const indeterminate = markOptimisticIndeterminateByRequestId(dispatched, REQUEST_ID, CREATED_AT + 131_000);

  const next = applySnapshotWithOptimisticReconciliation(
    indeterminate,
    { sessions: [session()], events: [staleRow] },
    CREATED_AT + 132_000,
  );

  assert.equal(next.optimisticSends?.[OPTIMISTIC_ID]?.status, 'indeterminate');
  assert.equal(next.events.some((event) => event.optimistic_id === previousOptimisticId), true);
  assert.equal(next.events.some((event) => event.optimistic_id === OPTIMISTIC_ID && event.client_origin === true), true);
  assert.equal(
    next.events.some((event) => event.optimistic_id === OPTIMISTIC_ID && Number.isFinite(Number(event.correlatedDaemonSeq))),
    false,
  );
});

test('snapshot replay keeps local attachment render fields from a previously reconciled row', () => {
  const prior = {
    ...buildState(),
    events: [{
      ...serverUserEvent({
        attachments: [{ key: 'f'.repeat(64), mime: 'image/jpeg', width: 640, height: 480 }],
      }),
      daemon_seq: Number.NaN,
      correlatedDaemonSeq: 42,
      client_origin: true,
      optimistic_id: OPTIMISTIC_ID,
      pending: false,
      attachments: [{
        key: 'f'.repeat(64),
        mime: 'image/jpeg',
        width: 640,
        height: 480,
        uri: 'file:///tmp/photo-a.jpg',
      }],
    } as any],
  };

  const next = applySnapshotWithOptimisticReconciliation(
    prior,
    {
      sessions: [session()],
      events: [serverUserEvent({
        attachments: [{ key: 'f'.repeat(64), mime: 'image/jpeg', width: 640, height: 480 }],
      })],
    },
    CREATED_AT + 3_000,
  );

  const row = selectSessionDetail(next, STREAM_ID, { visibleCount: 'all' })?.transcriptItems[0];
  assert.equal((row?.attachments?.[0] as ChatAttachment & { uri?: string }).uri, 'file:///tmp/photo-a.jpg');
  assert.equal(row?.attachments?.[0].key, 'f'.repeat(64));
});

test('session detail rows use optimistic id after reconcile and surface correlatedDaemonSeq', () => {
  const reconciled = reconcileOptimisticSendWithServerEvent(
    createOptimistic(),
    OPTIMISTIC_ID,
    serverUserEvent(),
  );

  const detail = selectSessionDetail(reconciled, STREAM_ID, { visibleCount: 'all' });
  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].id, OPTIMISTIC_ID);
  assert.equal(detail?.transcriptItems[0].optimisticId, OPTIMISTIC_ID);
  assert.equal((detail?.transcriptItems[0] as any).correlatedDaemonSeq, 42);
});

test('render-stability telemetry exports the transcript-mounted harness event and ref', () => {
  assert.equal(TELEMETRY_EVENTS.HARNESS_TRANSCRIPT_ITEM_MOUNTED, 'harness:transcript_item_mounted');
  assert.equal(CHAT_RENDER_STABILITY_REF, 'chat-render-stability');
  assert.equal(
    TELEMETRY_EVENT_BUG_REFS[TELEMETRY_EVENTS.HARNESS_TRANSCRIPT_ITEM_MOUNTED],
    CHAT_RENDER_STABILITY_REF,
  );
});
