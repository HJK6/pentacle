import assert from 'node:assert/strict';
import test from 'node:test';

import {
  TELEMETRY_EVENTS,
  applyFetchedStreamEvents,
  applyPentacleEvent,
  applyPentacleSessionInventory,
  applySnapshotWithOptimisticReconciliation,
  beginPentacleTurn,
  initialPentacleStreamState,
  setTelemetrySink,
  type PentacleEvent,
  type PentacleSessionSummary,
  type PentacleStreamState,
  type TelemetryPayload,
} from '../src/index.ts';

const STREAM = 'host_c:codex:turn';

function session(streamId: string, overrides: Partial<PentacleSessionSummary> = {}): PentacleSessionSummary {
  const name = streamId.split(':').at(-1) || streamId;
  return {
    stream_id: streamId,
    host: 'host_c',
    provider: 'codex',
    session_name: name,
    last_event_at: '2026-06-16T12:00:00.000Z',
    last_text: '',
    last_kind: '',
    draft: '',
    pending: false,
    working: false,
    online: true,
    ...overrides,
  };
}

function event(streamId: string, seq: number, overrides: Partial<PentacleEvent> = {}): PentacleEvent {
  const name = streamId.split(':').at(-1) || streamId;
  return {
    daemon_seq: seq,
    host: 'host_c',
    provider: 'codex',
    session_id: streamId,
    session_name: name,
    stream_id: streamId,
    timestamp: `2026-06-16T12:00:${String(seq).padStart(2, '0')}.000Z`,
    kind: 'ASSIST',
    text: `message ${seq}`,
    ...overrides,
  };
}

function turnSummary(streamId: string, seq: number): PentacleEvent {
  return event(streamId, seq, {
    kind: 'SYSTEM',
    text: 'Worked for 1m 23s',
    raw: { source: 'claude-jsonl', subtype: 'turn-summary' },
  });
}

function state(overrides: Partial<PentacleStreamState> = {}): PentacleStreamState {
  return {
    ...initialPentacleStreamState,
    connected: true,
    hasHydrated: true,
    ...overrides,
  };
}

// Capture chat:turn_phase_derived payloads while a test runs, restoring the
// noop sink afterwards so tests stay isolated.
function captureTurnPhaseDerived(run: (events: TelemetryPayload[]) => void) {
  const captured: TelemetryPayload[] = [];
  setTelemetrySink((payload: TelemetryPayload) => {
    if (payload.message === TELEMETRY_EVENTS.CHAT_TURN_PHASE_DERIVED) {
      captured.push(payload);
    }
  });
  try {
    run(captured);
  } finally {
    setTelemetrySink(() => {});
  }
}

test('fetch: open (un-ended) working turn seeds workingByStream "working"', () => {
  captureTurnPhaseDerived((captured) => {
    const base = state({ sessions: [session(STREAM, { working: true })] });
    const next = applyFetchedStreamEvents(base, [
      event(STREAM, 1, { kind: 'USER', text: 'do the thing' }),
      event(STREAM, 2, { kind: 'ASSIST', text: 'working on it' }),
      event(STREAM, 3, { kind: 'TOOL', text: 'running tool' }),
    ]);

    assert.equal(next.workingByStream?.[STREAM]?.phase, 'working');
    assert.ok(next.workingByStream?.[STREAM]?.lastServerEventKey);

    const fetchEvents = captured.filter((p) => p.data.source === 'fetch');
    assert.equal(fetchEvents.length, 1);
    assert.equal(fetchEvents[0].data.phase, 'working');
    assert.equal(fetchEvents[0].data.streamId, STREAM);
    assert.ok(fetchEvents[0].data.drivingEventKey);
  });
});

test('fetch: history ending in a turn-summary resolves to idle (no stale working)', () => {
  captureTurnPhaseDerived((captured) => {
    // A live turn is already 'working'; the fetched history shows it ended.
    const base = state({
      sessions: [session(STREAM, { working: true })],
      workingByStream: { [STREAM]: { phase: 'working' } },
    });
    const next = applyFetchedStreamEvents(base, [
      event(STREAM, 1, { kind: 'USER', text: 'do the thing' }),
      event(STREAM, 2, { kind: 'ASSIST', text: 'done' }),
      turnSummary(STREAM, 3),
    ]);

    assert.equal(next.workingByStream?.[STREAM]?.phase, 'idle');
    assert.equal(next.workingByStream?.[STREAM]?.endReason, 'turn_summary');

    const fetchEvents = captured.filter((p) => p.data.source === 'fetch');
    assert.equal(fetchEvents.length, 1);
    assert.equal(fetchEvents[0].data.phase, 'idle');
  });
});

test('fetch: opening an already-idle chat is a no-op (no spurious entry, no telemetry)', () => {
  captureTurnPhaseDerived((captured) => {
    const base = state({ sessions: [session(STREAM)] });
    const next = applyFetchedStreamEvents(base, [
      event(STREAM, 1, { kind: 'USER', text: 'earlier question' }),
      event(STREAM, 2, { kind: 'ASSIST', text: 'earlier answer' }),
      turnSummary(STREAM, 3),
    ]);

    // No prior turn + history ends idle ⇒ effective phase unchanged (idle),
    // so no entry is written and nothing is emitted.
    assert.equal(next.workingByStream?.[STREAM], undefined);
    assert.equal(captured.filter((p) => p.data.source === 'fetch').length, 0);
  });
});

test('fetch: a live working turn with on-going activity is NOT race-closed (007d127 guard)', () => {
  captureTurnPhaseDerived(() => {
    const base = state({
      // session.working lags false, but the fetch path must NEVER read it.
      sessions: [session(STREAM, { working: false })],
      workingByStream: { [STREAM]: { phase: 'working' } },
    });
    const next = applyFetchedStreamEvents(base, [
      event(STREAM, 1, { kind: 'USER', text: 'do the thing' }),
      event(STREAM, 2, { kind: 'ASSIST', text: 'still working' }),
      event(STREAM, 3, { kind: 'TOOL', text: 'still running' }),
    ]);

    // No end-of-turn in the fetched history ⇒ the open turn stays 'working'.
    assert.equal(next.workingByStream?.[STREAM]?.phase, 'working');
  });
});

test('fetch: derivation orders by daemon_seq, not array position', () => {
  captureTurnPhaseDerived((captured) => {
    const base = state({ sessions: [session(STREAM, { working: true })] });
    // The turn-summary appears LATER in the array but carries a LOWER seq than
    // the trailing ASSIST. Seq ordering ⇒ the turn is still open (working); a
    // regression to array-position ordering would wrongly resolve idle.
    const next = applyFetchedStreamEvents(base, [
      event(STREAM, 5, { kind: 'ASSIST', text: 'still working' }),
      turnSummary(STREAM, 2),
    ]);

    assert.equal(next.workingByStream?.[STREAM]?.phase, 'working');
    const fetchEvents = captured.filter((p) => p.data.source === 'fetch');
    assert.equal(fetchEvents.length, 1);
    assert.equal(fetchEvents[0].data.phase, 'working');
  });
});

test('fetch: a live-established working turn is NOT race-closed by an older fetched end-marker (007d127 residue guard)', () => {
  captureTurnPhaseDerived((captured) => {
    // A live turn is established 'working' by a WORKING:true frame at seq 10
    // (WORKING frames are not appended to state.events). A stale failure-retry
    // backfill then delivers only the PRIOR turn's history, ending in a
    // turn-summary at seq 3 — older than the live frame — and with no
    // current-turn server activity. The older end-marker must not flip the
    // newer live working turn to idle (idle never reopens).
    const base0 = beginPentacleTurn(
      state({ sessions: [session(STREAM)] }),
      STREAM,
      'optimistic-1',
      1_786_449_600_000,
    );
    const base = applyPentacleEvent(base0, event(STREAM, 10, { kind: 'WORKING', raw: { working: true } }));
    assert.equal(base.workingByStream?.[STREAM]?.phase, 'working');

    const next = applyFetchedStreamEvents(base, [
      event(STREAM, 1, { kind: 'USER', text: 'prior question' }),
      event(STREAM, 2, { kind: 'ASSIST', text: 'prior answer' }),
      turnSummary(STREAM, 3),
    ]);

    assert.equal(next.workingByStream?.[STREAM]?.phase, 'working');
    // No working→idle fetch transition is derived or emitted.
    assert.equal(captured.filter((p) => p.data.source === 'fetch').length, 0);
  });
});

test('fetch: empty and USER-only history derive no phase (null ⇒ no entry, no emit)', () => {
  captureTurnPhaseDerived((captured) => {
    const base = state({ sessions: [session(STREAM)] });

    // Empty batch: applyFetchedStreamEvents early-returns, nothing derived.
    const afterEmpty = applyFetchedStreamEvents(base, []);
    assert.equal(afterEmpty.workingByStream?.[STREAM], undefined);

    // USER-only history: no activity event, no end-marker ⇒ derivation returns
    // null ⇒ no entry written, nothing emitted.
    const next = applyFetchedStreamEvents(base, [
      event(STREAM, 1, { kind: 'USER', text: 'just a question' }),
    ]);
    assert.equal(next.workingByStream?.[STREAM], undefined);
    assert.equal(captured.filter((p) => p.data.source === 'fetch').length, 0);
  });
});

test('fetch: a pending optimistic turn is left untouched', () => {
  captureTurnPhaseDerived((captured) => {
    const base = beginPentacleTurn(
      state({ sessions: [session(STREAM)] }),
      STREAM,
      'optimistic-1',
      1_786_449_600_000,
    );
    assert.equal(base.workingByStream?.[STREAM]?.phase, 'pending');

    const next = applyFetchedStreamEvents(base, [
      event(STREAM, 2, { kind: 'ASSIST', text: 'server activity' }),
    ]);

    assert.equal(next.workingByStream?.[STREAM]?.phase, 'pending');
    assert.equal(captured.filter((p) => p.data.source === 'fetch').length, 0);
  });
});

test('live: a server event advancing a pending turn emits source "live"', () => {
  captureTurnPhaseDerived((captured) => {
    const base = beginPentacleTurn(
      state({ sessions: [session(STREAM)] }),
      STREAM,
      'optimistic-1',
      1_786_449_600_000,
    );
    const next = applyPentacleEvent(base, event(STREAM, 5, { kind: 'ASSIST', text: 'first reply' }));

    assert.equal(next.workingByStream?.[STREAM]?.phase, 'working');
    const liveEvents = captured.filter((p) => p.data.source === 'live');
    assert.equal(liveEvents.length, 1);
    assert.equal(liveEvents[0].data.phase, 'working');
    assert.ok(liveEvents[0].data.drivingEventKey);
  });
});

test('resync: closing a working turn against working:false emits source "resync"', () => {
  captureTurnPhaseDerived((captured) => {
    const base = state({
      sessions: [session(STREAM, { working: true })],
      workingByStream: { [STREAM]: { phase: 'working' } },
    });
    const next = applyPentacleSessionInventory(base, [session(STREAM, { working: false })]);

    assert.equal(next.workingByStream?.[STREAM]?.phase, 'idle');
    const resyncEvents = captured.filter((p) => p.data.source === 'resync');
    assert.equal(resyncEvents.length, 1);
    assert.equal(resyncEvents[0].data.phase, 'idle');
    assert.equal(resyncEvents[0].data.drivingEventKey, null);
  });
});

// First-open working-seed regressions (spec e2e_fixture_lifecycle_and_mock_first,
// composite queued-send FAIL). Opening a session that is CURRENTLY working — its
// latest in-band event is a working root (USER with raw.working===true) with no
// ASSIST/TOOL activity yet — must render Working immediately, so a send during
// the turn enters the queued path. Before the fix, no seed site honored the
// working root, so working only derived once the ASSIST flood accumulated (~4.9s
// later) and the composer's pre-dispatch working-check missed it. These FAIL on
// 9e61c6d/d1bea96 and PASS after.

test('fetch: a USER working root (raw.working) with no activity seeds working', () => {
  captureTurnPhaseDerived((captured) => {
    // The exact composite-fixture first-open shape: session working, one USER
    // root carrying raw.working===true, and NO ASSIST/TOOL activity yet.
    const base = state({ sessions: [session(STREAM, { working: true })] });
    const next = applyFetchedStreamEvents(base, [
      event(STREAM, 0, {
        kind: 'USER',
        text: 'composite root turn',
        raw: { source: 'terminal', working: true, working_label: 'Working' },
      }),
    ]);

    assert.equal(next.workingByStream?.[STREAM]?.phase, 'working');
    const fetchEvents = captured.filter((p) => p.data.source === 'fetch');
    assert.equal(fetchEvents.length, 1);
    assert.equal(fetchEvents[0].data.phase, 'working');
  });
});

test('fetch: a working root superseded by a strictly-newer end-marker does NOT seed working (no false-working)', () => {
  captureTurnPhaseDerived((captured) => {
    const base = state({ sessions: [session(STREAM, { working: true })] });
    // Working root at seq 1, turn-summary at seq 3 (strictly newer) ⇒ the turn
    // ended. The raw.working seed must NOT outlive its own turn: derivation
    // resolves idle and, since the stream is already effectively idle (absent
    // entry), writes no spurious working entry and emits nothing.
    const next = applyFetchedStreamEvents(base, [
      event(STREAM, 1, { kind: 'USER', text: 'root', raw: { working: true } }),
      turnSummary(STREAM, 3),
    ]);

    assert.equal(next.workingByStream?.[STREAM], undefined);
    assert.equal(captured.filter((p) => p.data.source === 'fetch').length, 0);
  });
});

test('live: a USER working root seeds working from idle and emits source "live"', () => {
  captureTurnPhaseDerived((captured) => {
    // The live seed frame path (composite daemon_seq=1): no prior turn, a USER
    // event carrying raw.working===true must SEED a working turn.
    const base = state({ sessions: [session(STREAM, { working: true })] });
    const next = applyPentacleEvent(base, event(STREAM, 1, {
      kind: 'USER',
      text: 'composite root turn',
      raw: { source: 'terminal', working: true, working_label: 'Working' },
    }));

    assert.equal(next.workingByStream?.[STREAM]?.phase, 'working');
    const liveEvents = captured.filter((p) => p.data.source === 'live');
    assert.equal(liveEvents.length, 1);
    assert.equal(liveEvents[0].data.phase, 'working');
    assert.ok(liveEvents[0].data.drivingEventKey);
  });
});

test('live: a plain USER event (no raw.working) does NOT seed working', () => {
  // Guard: the seed is gated strictly on raw.working===true — an ordinary user
  // message must not spuriously open a working turn.
  const base = state({ sessions: [session(STREAM)] });
  const next = applyPentacleEvent(base, event(STREAM, 1, { kind: 'USER', text: 'just a question' }));
  assert.equal(next.workingByStream?.[STREAM], undefined);
});

test('snapshot: a working-root hello (session.working + USER raw.working) seeds working', () => {
  captureTurnPhaseDerived((captured) => {
    // The composite fixture hello snapshot: one working session + its in-band USER
    // working root (daemon_seq 0). reconcileWorkingByStreamForResync alone never
    // seeds working; the in-band snapshot derivation must.
    const base = state();
    const next = applySnapshotWithOptimisticReconciliation(base, {
      sessions: [session(STREAM, { working: true, working_label: 'Working' })],
      events: [
        event(STREAM, 0, {
          kind: 'USER',
          text: 'composite root turn',
          raw: { source: 'terminal', working: true, working_label: 'Working' },
        }),
      ],
    });

    assert.equal(next.workingByStream?.[STREAM]?.phase, 'working');
    assert.ok(captured.some((p) => p.data.streamId === STREAM && p.data.phase === 'working'));
  });
});

test('snapshot: an empty (summary-mode) hello does NOT seed working', () => {
  // Mobile connects events_mode=summary → hello carries events:[]; the seed pass
  // must be skipped entirely so summary-resync behavior is unchanged.
  const base = state();
  const next = applySnapshotWithOptimisticReconciliation(base, {
    sessions: [session(STREAM, { working: true })],
    events: [],
  });
  assert.equal(next.workingByStream?.[STREAM], undefined);
});
