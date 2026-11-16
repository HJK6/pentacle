import assert from 'node:assert/strict';
import test from 'node:test';

import {
  TELEMETRY_EVENTS,
  applyFetchedStreamEvents,
  applyPentacleEvent,
  initialPentacleStreamState,
  markOptimisticAckedByRequestId,
  markOptimisticDispatchedByRequestId,
  resetOptimisticOrphanTelemetryForTests,
  sendOptimisticMessage,
  setTelemetrySink,
  type PentacleEvent,
  type PentacleSessionSummary,
  type PentacleStreamState,
  type TelemetryPayload,
} from '../src/index.ts';

const STREAM_ID = 'host_c:codex:orphan';
const OPTIMISTIC_ID = 'optimistic_host_c_codex_orphan_1';
const REQUEST_ID = 'send-req-orphan-1';
const CREATED_AT = Date.parse('2026-06-28T12:00:00.000Z');

function session(overrides: Partial<PentacleSessionSummary> = {}): PentacleSessionSummary {
  return {
    stream_id: STREAM_ID,
    host: 'host_c',
    provider: 'codex',
    session_name: 'orphan',
    last_event_at: '2026-06-28T12:00:10.000Z',
    last_text: '',
    last_kind: '',
    draft: '',
    pending: false,
    working: false,
    online: true,
    ...overrides,
  };
}

function event(seq: number, overrides: Partial<PentacleEvent> = {}): PentacleEvent {
  return {
    daemon_seq: seq,
    host: 'host_c',
    provider: 'codex',
    session_id: STREAM_ID,
    session_name: 'orphan',
    stream_id: STREAM_ID,
    timestamp: `2026-06-28T12:00:${String(seq).padStart(2, '0')}.000Z`,
    kind: 'SYSTEM',
    text: 'Worked for 1s',
    raw: { source: 'claude-jsonl', subtype: 'turn-summary' },
    ...overrides,
  };
}

function baseState(overrides: Partial<PentacleStreamState> = {}): PentacleStreamState {
  return {
    ...initialPentacleStreamState,
    connected: true,
    hasHydrated: true,
    sessions: [session()],
    workingByStream: { [STREAM_ID]: { phase: 'idle' } },
    ...overrides,
  };
}

function ackedOptimistic(state = baseState()) {
  const optimistic = sendOptimisticMessage(state, {
    streamId: STREAM_ID,
    text: 'hello orphan',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: CREATED_AT,
    windowStartedAt: CREATED_AT,
  });
  const dispatched = markOptimisticDispatchedByRequestId(optimistic, REQUEST_ID, CREATED_AT + 10);
  return {
    ...markOptimisticAckedByRequestId(dispatched, REQUEST_ID, CREATED_AT + 20),
    workingByStream: { [STREAM_ID]: { phase: 'idle' as const } },
  };
}

function captureOrphanTelemetry(run: (captured: TelemetryPayload[]) => void) {
  const captured: TelemetryPayload[] = [];
  resetOptimisticOrphanTelemetryForTests();
  setTelemetrySink((payload: TelemetryPayload) => {
    if (payload.message === TELEMETRY_EVENTS.CHAT_COMPOSE_OPTIMISTIC_ORPHAN_SUSPECTED) {
      captured.push(payload);
    }
  });
  try {
    run(captured);
  } finally {
    resetOptimisticOrphanTelemetryForTests();
    setTelemetrySink(() => {});
  }
}

test('fresh post-idle fetch emits one suspected-orphan telemetry event for an acked send without an echo', () => {
  captureOrphanTelemetry((captured) => {
    const first = applyFetchedStreamEvents(ackedOptimistic(), [event(10)]);
    const second = applyFetchedStreamEvents(first, [event(10)]);

    assert.equal(captured.length, 1);
    assert.equal(captured[0].data.phase, 'suspected');
    assert.equal(captured[0].data.optimisticId, OPTIMISTIC_ID);
    assert.equal(captured[0].data.sendStatus, 'acked');
    assert.equal(second.optimisticSends?.[OPTIMISTIC_ID]?.status, 'acked');
  });
});

test('orphan telemetry suppresses legitimate sending, held, non-idle, offline, and echoed states', () => {
  captureOrphanTelemetry((captured) => {
    const dispatched = markOptimisticDispatchedByRequestId(
      sendOptimisticMessage(baseState(), {
        streamId: STREAM_ID,
        text: 'still sending',
        optimisticId: 'optimistic_sending',
        requestId: 'req-sending',
        createdAt: CREATED_AT,
        windowStartedAt: CREATED_AT,
      }),
      'req-sending',
      CREATED_AT + 10,
    );
    applyFetchedStreamEvents({ ...dispatched, workingByStream: { [STREAM_ID]: { phase: 'idle' } } }, [event(11)]);

    const held = ackedOptimistic();
    held.optimisticSends![OPTIMISTIC_ID] = { ...held.optimisticSends![OPTIMISTIC_ID], turn_queued: true };
    applyFetchedStreamEvents(held, [event(12)]);

    applyFetchedStreamEvents({
      ...ackedOptimistic(),
      sessions: [session({ working: true })],
      workingByStream: { [STREAM_ID]: { phase: 'working' } },
    }, [event(13)]);

    applyFetchedStreamEvents({
      ...ackedOptimistic(),
      sessions: [session({ online: false })],
    }, [event(14)]);

    applyFetchedStreamEvents(ackedOptimistic(), [
      event(15),
      event(16, {
        kind: 'USER',
        text: 'hello orphan',
        optimistic_id: OPTIMISTIC_ID,
      }),
    ]);

    assert.equal(captured.length, 0);
  });
});

test('late echo after suspected orphan emits a resolved annotation and reconciles normally', () => {
  captureOrphanTelemetry((captured) => {
    const suspected = applyFetchedStreamEvents(ackedOptimistic(), [event(20)]);
    const reconciled = applyPentacleEvent(suspected, event(21, {
      kind: 'USER',
      text: 'hello orphan',
      timestamp: '2026-06-28T12:02:30.000Z',
    }));

    assert.equal(captured.length, 2);
    assert.equal(captured[0].data.phase, 'suspected');
    assert.equal(captured[1].data.phase, 'resolved_after_suspected');
    assert.equal(reconciled.optimisticSends?.[OPTIMISTIC_ID], undefined);
    assert.equal(reconciled.events.some((item) => item.optimistic_id === OPTIMISTIC_ID && item.pending === false), true);
  });
});

test('late echo delivered by a later fresh fetch emits a resolved annotation', () => {
  captureOrphanTelemetry((captured) => {
    const suspected = applyFetchedStreamEvents(ackedOptimistic(), [event(24)], { requestedStreamId: STREAM_ID });
    const afterEchoFetch = applyFetchedStreamEvents(suspected, [
      event(25, {
        kind: 'USER',
        text: 'hello orphan',
        timestamp: '2026-06-28T12:02:30.000Z',
      }),
    ], { requestedStreamId: STREAM_ID });

    assert.equal(captured.length, 2);
    assert.equal(captured[0].data.phase, 'suspected');
    assert.equal(captured[1].data.phase, 'resolved_after_suspected');
    assert.equal(captured[1].data.source, 'fetch');
    assert.equal(afterEchoFetch.optimisticSends?.[OPTIMISTIC_ID]?.status, 'acked');
  });
});

test('empty fresh post-idle fetch emits suspected orphan for the requested stream', () => {
  captureOrphanTelemetry((captured) => {
    const next = applyFetchedStreamEvents(ackedOptimistic(), [], { requestedStreamId: STREAM_ID });

    assert.equal(captured.length, 1);
    assert.equal(captured[0].data.phase, 'suspected');
    assert.equal(captured[0].data.optimisticId, OPTIMISTIC_ID);
    assert.equal(next.optimisticSends?.[OPTIMISTIC_ID]?.status, 'acked');
  });
});

test('fresh post-idle orphan observation does not reintroduce time-based auto-fail', () => {
  captureOrphanTelemetry(() => {
    const next = applyFetchedStreamEvents(ackedOptimistic(), [
      event(30, { timestamp: '2026-06-28T12:10:00.000Z' }),
    ]);

    assert.equal(next.optimisticSends?.[OPTIMISTIC_ID]?.status, 'acked');
    assert.equal(next.events.find((item) => item.optimistic_id === OPTIMISTIC_ID)?.pending, true);
  });
});
