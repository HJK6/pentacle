import assert from 'node:assert/strict';
import test from 'node:test';

import {
  TELEMETRY_EVENTS,
  applyFetchedStreamEvents,
  applyPentacleSnapshotMessage,
  initialPentacleStreamState,
  invalidateSessionDetailCache,
  getSessionSendingState,
  isSessionSending,
  selectChatList,
  selectSessionDetail,
  selectSafeSessionSummaryPreview,
  setTelemetrySink,
  type OptimisticSendState,
  type PentacleEvent,
  type PentacleQuestion,
  type PentacleSessionSummary,
  type PentacleStreamState,
  type TelemetryPayload,
} from '../src/index.ts';

const STREAM_A = 'host_c:codex:a';
const STREAM_B = 'host_c:codex:b';
const STREAM_C = 'host_c:codex:c';

setTelemetrySink(() => {});

function session(streamId: string, overrides: Partial<PentacleSessionSummary> = {}): PentacleSessionSummary {
  const name = streamId.split(':').at(-1) || streamId;
  return {
    stream_id: streamId,
    host: 'host_c',
    provider: 'codex',
    session_name: name,
    last_event_at: '2026-06-10T12:00:00.000Z',
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
    timestamp: `2026-06-10T12:00:${String(seq).padStart(2, '0')}.000Z`,
    kind: 'ASSIST',
    text: `message ${seq}`,
    ...overrides,
  };
}

function state(overrides: Partial<PentacleStreamState> = {}): PentacleStreamState {
  return {
    ...initialPentacleStreamState,
    connected: true,
    hasHydrated: true,
    ...overrides,
  };
}

function countRenderedRows() {
  let count = 0;
  setTelemetrySink((payload: TelemetryPayload) => {
    if (payload.message === TELEMETRY_EVENTS.CHAT_EVENT_RENDERED) {
      count += 1;
    }
  });
  return () => count;
}

test('event-less snapshots preserve events for surviving streams and drop removed streams', () => {
  const prior = state({
    sessions: [session(STREAM_A), session(STREAM_B), session(STREAM_C)],
    events: [event(STREAM_A, 1), event(STREAM_B, 2), event(STREAM_C, 3)],
    eventContentVersionByStream: {
      [STREAM_A]: 7,
      [STREAM_B]: 8,
      [STREAM_C]: 9,
    },
  });

  const next = applyPentacleSnapshotMessage(prior, {
    sessions: [session(STREAM_A), session(STREAM_B)],
  });

  assert.deepEqual(next.events.map((item) => item.stream_id), [STREAM_A, STREAM_B]);
  assert.deepEqual(next.events.map((item) => item.daemon_seq), [1, 2]);
  assert.equal(next.events[0], prior.events[0]);
  assert.equal(next.events[1], prior.events[1]);
  assert.deepEqual(next.eventContentVersionByStream, {
    [STREAM_A]: 7,
    [STREAM_B]: 8,
  });
});

test('snapshots that carry events still replace the event ring', () => {
  const prior = state({
    sessions: [session(STREAM_A), session(STREAM_B)],
    events: [event(STREAM_A, 1), event(STREAM_B, 2)],
    eventContentVersionByStream: {
      [STREAM_A]: 1,
      [STREAM_B]: 1,
    },
  });

  const next = applyPentacleSnapshotMessage(prior, {
    sessions: [session(STREAM_A), session(STREAM_B)],
    events: [event(STREAM_A, 10)],
  });

  assert.deepEqual(next.events.map((item) => `${item.stream_id}:${item.daemon_seq}`), [`${STREAM_A}:10`]);
  assert.equal(next.eventContentVersionByStream?.[STREAM_A], 2);
  assert.equal(next.eventContentVersionByStream?.[STREAM_B], 2);
});

test('selectSessionDetail early-exits before row building when selector inputs are unchanged', () => {
  invalidateSessionDetailCache(STREAM_A);
  const renderedRows = countRenderedRows();
  const base = state({
    sessions: [session(STREAM_A), session(STREAM_B)],
    events: [event(STREAM_A, 1), event(STREAM_B, 20)],
    eventContentVersionByStream: {
      [STREAM_A]: 1,
      [STREAM_B]: 1,
    },
  });

  const first = selectSessionDetail(base, STREAM_A, { visibleCount: 'all', emitRenderTelemetry: true });
  const afterFirst = renderedRows();
  const second = selectSessionDetail(base, STREAM_A, { visibleCount: 'all', emitRenderTelemetry: true });
  assert.equal(second, first);
  assert.equal(renderedRows(), afterFirst);

  const unrelatedStreamUpdate = {
    ...base,
    events: [...base.events, event(STREAM_B, 21)],
    eventContentVersionByStream: {
      ...base.eventContentVersionByStream,
      [STREAM_B]: 2,
    },
  };
  const third = selectSessionDetail(unrelatedStreamUpdate, STREAM_A, { visibleCount: 'all', emitRenderTelemetry: true });
  assert.equal(third, first);
  assert.equal(renderedRows(), afterFirst);
});

test('selectSessionDetail rebuilds when content version changes', () => {
  invalidateSessionDetailCache(STREAM_A);
  const renderedRows = countRenderedRows();
  const base = state({
    sessions: [session(STREAM_A)],
    events: [event(STREAM_A, 1)],
    eventContentVersionByStream: { [STREAM_A]: 1 },
  });

  const first = selectSessionDetail(base, STREAM_A, { visibleCount: 'all', emitRenderTelemetry: true });
  const nextState = applyFetchedStreamEvents(base, [event(STREAM_A, 2)]);
  const second = selectSessionDetail(nextState, STREAM_A, { visibleCount: 'all', emitRenderTelemetry: true });

  assert.notEqual(second, first);
  assert.equal(second?.transcriptItems.length, 2);
  assert.equal(renderedRows(), 3);
});

test('selectSessionDetail rebuilds when optimistic send status changes without content version change', () => {
  invalidateSessionDetailCache(STREAM_A);
  const optimisticId = 'optimistic-send-a';
  const requestId = 'request-a';
  const optimisticSend: OptimisticSendState = {
    optimistic_id: optimisticId,
    request_id: requestId,
    stream_id: STREAM_A,
    text: 'hello from the client',
    status: 'dispatched',
    created_at: 1_786_449_600_000,
    dispatched_at: 1_786_449_600_010,
    reconnect_count: 0,
  };
  const base = state({
    sessions: [session(STREAM_A)],
    events: [event(STREAM_A, Number.NaN, {
      kind: 'USER',
      text: optimisticSend.text,
      client_origin: true,
      optimistic_id: optimisticId,
      pending: true,
      created_at: optimisticSend.created_at,
    })],
    optimisticSends: {
      [optimisticId]: optimisticSend,
    },
    optimisticByRequestId: {
      [requestId]: optimisticId,
    },
    eventContentVersionByStream: { [STREAM_A]: 1 },
  });

  const first = selectSessionDetail(base, STREAM_A, { visibleCount: 'all' });
  const optimisticRow = first?.transcriptItems.find((item) => item.optimisticId === optimisticId);
  assert.equal(optimisticRow?.sendState, 'sending');

  const failed = {
    ...base,
    optimisticSends: {
      ...base.optimisticSends,
      [optimisticId]: {
        ...optimisticSend,
        status: 'failed' as const,
      },
    },
  };
  assert.equal(failed.eventContentVersionByStream?.[STREAM_A], base.eventContentVersionByStream?.[STREAM_A]);

  const second = selectSessionDetail(failed, STREAM_A, { visibleCount: 'all' });
  const failedRow = second?.transcriptItems.find((item) => item.optimisticId === optimisticId);
  assert.notEqual(second, first);
  assert.equal(failedRow?.sendState, 'failed');

  const third = selectSessionDetail(failed, STREAM_A, { visibleCount: 'all' });
  assert.equal(third, second);
});

test('selectSessionDetail keeps pending optimistic rows visible beyond bounded raw window', () => {
  invalidateSessionDetailCache(STREAM_A);
  const optimisticId = 'optimistic-windowed-send';
  const optimisticSend: OptimisticSendState = {
    optimistic_id: optimisticId,
    request_id: 'request-windowed-send',
    stream_id: STREAM_A,
    text: 'queued behind a busy transcript',
    status: 'queued',
    created_at: 1_786_449_600_000,
    turn_queued: true,
    reconnect_count: 0,
  };
  const base = state({
    sessions: [session(STREAM_A)],
    events: [
      event(STREAM_A, Number.NaN, {
        kind: 'USER',
        text: optimisticSend.text,
        client_origin: true,
        optimistic_id: optimisticId,
        pending: true,
        created_at: optimisticSend.created_at,
      }),
      ...Array.from({ length: 40 }, (_, index) => event(STREAM_A, index + 1, {
        text: `later assistant row ${index + 1}`,
      })),
    ],
    optimisticSends: {
      [optimisticId]: optimisticSend,
    },
    optimisticByRequestId: {
      [optimisticSend.request_id]: optimisticId,
    },
    eventContentVersionByStream: { [STREAM_A]: 1 },
  });

  const detail = selectSessionDetail(base, STREAM_A, { visibleCount: 16 });
  const optimisticRow = detail?.transcriptItems.find((item) => item.optimisticId === optimisticId);
  assert.equal(optimisticRow?.text, optimisticSend.text);
  assert.equal(optimisticRow?.sendState, 'queued');
  assert.equal(optimisticRow?.pending, true);
});

test('selectChatList uses daemon working elapsed and optimistic sending status', () => {
  const now = 10_000;
  const textOptimistic = 'optimistic_text';
  const imageOptimistic = 'optimistic_image';
  const queuedOptimistic = 'optimistic_held';
  const working = state({
    sessions: [session(STREAM_A, { working: true })],
    workingStates: {
      [STREAM_A]: {
        stream_id: STREAM_A,
        timestamp: '2026-06-10T12:00:00.000Z',
        tokens_input: 0,
        tokens_output: 0,
        tokens_cache_read: 0,
        tokens_cache_creation: 0,
        tokens_phase: 'idle',
        shell_count_started: 0,
        tasks: [],
        task_summary: { total: 0, done: 0, in_progress: 0, open: 0 },
        elapsed_ms: 65_400,
      },
    },
  });
  assert.equal(selectChatList(working)[0]?.workingElapsedSeconds, 65);

  const sending = state({
    sessions: [session(STREAM_A)],
    optimisticSends: {
      [textOptimistic]: {
        optimistic_id: textOptimistic,
        request_id: 'req_text',
        stream_id: STREAM_A,
        text: 'hello',
        status: 'dispatched',
        created_at: now - 1_000,
        dispatched_at: now - 500,
        window_started_at: now - 500,
        reconnect_count: 0,
      },
    },
  });
  assert.deepEqual(getSessionSendingState(sending, STREAM_A), { sending: true, immediate: false });
  assert.equal(selectChatList(sending)[0]?.status, 'sending');

  const fastText = state({
    sessions: [session(STREAM_A)],
    optimisticSends: {
      [textOptimistic]: {
        optimistic_id: textOptimistic,
        request_id: 'req_text',
        stream_id: STREAM_A,
        text: 'hello',
        status: 'dispatched',
        created_at: now - 100,
        dispatched_at: now - 100,
        window_started_at: now - 100,
        reconnect_count: 0,
      },
    },
  });
  assert.deepEqual(getSessionSendingState(fastText, STREAM_A), { sending: true, immediate: false });

  const queuedText = state({
    sessions: [session(STREAM_A)],
    optimisticSends: {
      [queuedOptimistic]: {
        optimistic_id: queuedOptimistic,
        request_id: 'req_queued',
        stream_id: STREAM_A,
        text: 'queued but active',
        status: 'queued',
        created_at: now - 100,
        window_started_at: null,
        reconnect_count: 0,
      },
    },
  });
  assert.deepEqual(getSessionSendingState(queuedText, STREAM_A), { sending: true, immediate: false });

  const uploadingImage = state({
    sessions: [session(STREAM_A)],
    optimisticSends: {
      [imageOptimistic]: {
        optimistic_id: imageOptimistic,
        request_id: 'req_image',
        stream_id: STREAM_A,
        text: '',
        status: 'queued',
        created_at: now,
        window_started_at: null,
        reconnect_count: 0,
        attachments: [{ key: 'pending-local', mime: 'image/jpeg' }],
      },
    },
  });
  assert.deepEqual(getSessionSendingState(uploadingImage, STREAM_A), { sending: true, immediate: true });

  const heldQueue = state({
    sessions: [session(STREAM_A)],
    optimisticSends: {
      [queuedOptimistic]: {
        optimistic_id: queuedOptimistic,
        request_id: 'req_held',
        stream_id: STREAM_A,
        text: 'later',
        status: 'queued',
        created_at: now - 2_000,
        window_started_at: null,
        reconnect_count: 0,
        turn_queued: true,
      },
    },
  });
  assert.equal(isSessionSending(heldQueue, STREAM_A), false);

  const workingWins = state({
    sessions: [session(STREAM_A, { working: true })],
    workingByStream: { [STREAM_A]: { phase: 'working', optimisticId: textOptimistic, sentAt: now - 2_000 } },
    optimisticSends: sending.optimisticSends,
  });
  assert.equal(isSessionSending(workingWins, STREAM_A), false);
});

test('getSessionSendingState ignores failed and cancelled sends (falls back to non-sending)', () => {
  const now = 10_000;
  for (const terminal of ['failed', 'cancelled'] as const) {
    const snapshot = state({
      sessions: [session(STREAM_A)],
      optimisticSends: {
        [`optimistic_${terminal}`]: {
          optimistic_id: `optimistic_${terminal}`,
          request_id: `req_${terminal}`,
          stream_id: STREAM_A,
          text: 'oops',
          status: terminal,
          created_at: now - 1_000,
          dispatched_at: now - 800,
          window_started_at: now - 800,
          failed_at: terminal === 'failed' ? now - 200 : undefined,
          reconnect_count: 0,
        },
      },
    });
    assert.deepEqual(
      getSessionSendingState(snapshot, STREAM_A),
      { sending: false, immediate: false },
      `${terminal} send must not drive the sending indicator`,
    );
    assert.equal(isSessionSending(snapshot, STREAM_A), false);
    // The session indicator falls back to its resting state, never 'sending'.
    assert.notEqual(selectChatList(snapshot)[0]?.status, 'sending');
  }
});

test('selectSessionDetail rebuilds when visibleCount or includeDraft changes', () => {
  invalidateSessionDetailCache(STREAM_A);
  const renderedRows = countRenderedRows();
  const base = state({
    sessions: [session(STREAM_A, { draft: 'summary draft' })],
    drafts: {
      [STREAM_A]: event(STREAM_A, 99, { kind: 'DRAFT', text: 'typed draft' }),
    },
    events: [event(STREAM_A, 1), event(STREAM_A, 2)],
    eventContentVersionByStream: { [STREAM_A]: 1 },
  });

  const allRows = selectSessionDetail(base, STREAM_A, { visibleCount: 'all', includeDraft: true, emitRenderTelemetry: true });
  const afterAllRows = renderedRows();
  const oneRow = selectSessionDetail(base, STREAM_A, { visibleCount: 1, includeDraft: true, emitRenderTelemetry: true });
  const noDraft = selectSessionDetail(base, STREAM_A, { visibleCount: 1, includeDraft: false, emitRenderTelemetry: true });

  assert.equal(allRows?.transcriptItems.length, 2);
  assert.equal(oneRow?.transcriptItems.length, 1);
  assert.equal(noDraft?.draftText, '');
  assert.ok(renderedRows() > afterAllRows);
});

test('selectSessionDetail timing-only system rows filter before visible windowing', () => {
  invalidateSessionDetailCache(STREAM_A);
  const noisySystemRows = Array.from({ length: 12 }, (_, index) => event(STREAM_A, index + 3, {
    kind: 'SYSTEM',
    text: `runtime notice ${index + 1}`,
  }));
  const base = state({
    sessions: [session(STREAM_A)],
    events: [
      event(STREAM_A, 1, { text: 'important answer before duration' }),
      event(STREAM_A, 2, {
        kind: 'SYSTEM',
        text: 'Turn complete',
        raw: { source: 'claude-jsonl', subtype: 'turn-summary' },
      }),
      ...noisySystemRows,
      event(STREAM_A, 15, { text: 'Context Compacted to save tokens' }),
      event(STREAM_A, 20, { text: 'latest answer' }),
    ],
    eventContentVersionByStream: { [STREAM_A]: 1 },
  });

  const allSystemRows = selectSessionDetail(base, STREAM_A, {
    includeSystem: true,
    visibleCount: 3,
  });
  assert.deepEqual(
    allSystemRows?.transcriptItems.map((item) => item.displayRule),
    ['activity:system', 'system:compacted', 'bubble:assistant'],
    'unfiltered includeSystem rows still admit compacted markers and consume the visible window before app-side filtering',
  );

  const timingOnly = selectSessionDetail(base, STREAM_A, {
    includeSystem: true,
    systemRows: 'timing-only',
    visibleCount: 3,
  });
  assert.deepEqual(
    timingOnly?.transcriptItems.map((item) => item.displayRule),
    ['bubble:assistant', 'activity:turn-summary', 'bubble:assistant'],
    'timing-only admits exactly duration dividers and turn summaries, not compacted markers',
  );
  assert.deepEqual(
    timingOnly?.transcriptItems.map((item) => item.text),
    ['important answer before duration', 'Turn complete', 'latest answer'],
  );
});

test('selectSessionDetail rebuilds when session summary fallback fields change', () => {
  invalidateSessionDetailCache(STREAM_A);
  const renderedRows = countRenderedRows();
  const base = state({
    sessions: [session(STREAM_A, {
      title: 'Before',
      last_text: 'old fallback',
      last_kind: 'ASSIST',
    })],
    events: [],
    eventContentVersionByStream: { [STREAM_A]: 1 },
  });

  const first = selectSessionDetail(base, STREAM_A, { visibleCount: 'all', emitRenderTelemetry: true });
  const afterFirst = renderedRows();
  const changed = {
    ...base,
    sessions: [session(STREAM_A, {
      title: 'After',
      last_text: 'new fallback',
      last_kind: 'ASSIST',
    })],
  };
  const second = selectSessionDetail(changed, STREAM_A, { visibleCount: 'all', emitRenderTelemetry: true });

  assert.notEqual(second, first);
  assert.equal(second?.title, 'After');
  assert.equal(second?.transcriptItems.at(-1)?.text, 'new fallback');
  assert.ok(renderedRows() > afterFirst);
});

test('safe session summary preview is parser-safe and suppresses provisional summaries', () => {
  const previewState = state({
    sessions: [session(STREAM_A, {
      last_event_at: '2026-08-28T12:00:00.000Z',
      last_kind: 'ASSIST',
      last_text: 'A concise authoritative-looking summary.',
    })],
    events: [],
  });
  const preview = selectSafeSessionSummaryPreview(previewState, STREAM_A);
  assert.equal(preview?.text, 'A concise authoritative-looking summary.');
  assert.match(preview?.key ?? '', new RegExp(`^preview:${STREAM_A}:`));

  assert.equal(selectSafeSessionSummaryPreview({
    ...previewState,
    sessions: [session(STREAM_A, { last_text: 'still working', working: true })],
  }, STREAM_A), null);
  assert.equal(selectSafeSessionSummaryPreview({
    ...previewState,
    sessions: [session(STREAM_A, { last_kind: 'TOOL_RESULT', last_text: 'hidden tool output' })],
  }, STREAM_A), null);
});

test('selectSessionDetail rebuilds when working or question state changes', () => {
  invalidateSessionDetailCache(STREAM_A);
  const renderedRows = countRenderedRows();
  const base = state({
    sessions: [session(STREAM_A)],
    events: [event(STREAM_A, 1)],
    eventContentVersionByStream: { [STREAM_A]: 1 },
  });

  const first = selectSessionDetail(base, STREAM_A, { visibleCount: 'all', emitRenderTelemetry: true });
  const afterFirst = renderedRows();
  const working = {
    ...base,
    sessions: [session(STREAM_A, { working: true, working_label: 'Working' })],
  };
  const second = selectSessionDetail(working, STREAM_A, { visibleCount: 'all', emitRenderTelemetry: true });
  assert.notEqual(second, first);
  assert.equal(second?.status, 'working');
  assert.ok(renderedRows() > afterFirst);

  const question: PentacleQuestion = {
    header: 'Choose',
    prompt: 'Pick one',
    options: [{ index: 1, label: 'One' }],
  };
  const afterWorking = renderedRows();
  const withQuestion = {
    ...working,
    sessions: [session(STREAM_A, { working: true, working_label: 'Working', question })],
  };
  const third = selectSessionDetail(withQuestion, STREAM_A, { visibleCount: 'all', emitRenderTelemetry: true });
  assert.equal(third?.status, 'working');
  assert.ok(renderedRows() > afterWorking);

  const afterQuestion = renderedRows();
  const fourth = selectSessionDetail(withQuestion, STREAM_A, { visibleCount: 'all', emitRenderTelemetry: true });
  assert.equal(fourth, third);
  assert.equal(renderedRows(), afterQuestion);
});

// ── Negative proof — render-telemetry QA layer, DEFECT 1 (beacon-window) ──────
// A self-fulfilling `chat:event_rendered`: pre-fix `selectSessionDetail` forced
// `emitRenderTelemetry=true`, so EVERY caller — including a harness's own polling
// probes (default options, distinct cacheKey from the render path) and
// terminal-view per-frame calls — re-emitted one `chat:event_rendered` per
// visible row for ALREADY-rendered content. A walk could then satisfy its own
// "a row rendered" await with ZERO new daemon content and ZERO paints.
//
// This proof seeds a transcript, then fires a STORM of default-option probes
// (each forced through a fresh row build via invalidateSessionDetailCache, i.e.
// a genuine cache MISS) with no new content, and asserts the selector emits NO
// `chat:event_rendered`. It FAILS pre-fix (every probe emits) and PASSES once
// the param defaults false. The trailing real-render call (emitRenderTelemetry
// true) keeps the proof non-vacuous: the SAME selector still emits on the real
// render path, so the assertion proves gating — not a globally-muted beacon.
test('DEFECT 1 negative proof: default-option selectSessionDetail probes emit NO chat:event_rendered even on cache miss', () => {
  invalidateSessionDetailCache(STREAM_A);
  const renderedRows = countRenderedRows();
  const base = state({
    sessions: [session(STREAM_A)],
    events: [event(STREAM_A, 1), event(STREAM_A, 2), event(STREAM_A, 3)],
    eventContentVersionByStream: { [STREAM_A]: 1 },
  });

  // Harness-probe storm: repeated cache-miss reads with zero new daemon content.
  for (let probe = 0; probe < 5; probe += 1) {
    invalidateSessionDetailCache(STREAM_A);
    const detail = selectSessionDetail(base, STREAM_A);
    assert.ok((detail?.transcriptItems.length ?? 0) > 0, 'probe still returns the transcript');
  }
  assert.equal(
    renderedRows(),
    0,
    'default-option probes must NOT emit chat:event_rendered (self-fulfilling beacon closed)',
  );

  // Non-vacuous: the real render path (opt-in) still emits, proving the proof
  // measures GATING, not an unconditionally-muted beacon.
  invalidateSessionDetailCache(STREAM_A);
  selectSessionDetail(base, STREAM_A, { emitRenderTelemetry: true });
  assert.ok(renderedRows() > 0, 'a real render path (emitRenderTelemetry:true) still emits');
});
