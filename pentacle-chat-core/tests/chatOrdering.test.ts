import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyFetchedStreamEvents,
  applyPentacleEvent,
  initialPentacleStreamState,
  selectCurrentTailEvents,
  selectSessionDetail,
  sendOptimisticMessage,
  type PentacleEvent,
  type PentacleSessionSummary,
  type PentacleStreamState,
} from '../src/index.ts';

const STREAM_ID = 'host_a:codex:ordering';
let syntheticContentVersion = 1;

function session(overrides: Partial<PentacleSessionSummary> = {}): PentacleSessionSummary {
  return {
    stream_id: STREAM_ID,
    host: 'host_a',
    provider: 'codex',
    session_name: 'ordering',
    last_event_at: '2026-06-30T12:00:00.000Z',
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
  const state = {
    ...initialPentacleStreamState,
    connected: true,
    sessions: [session()],
    ...overrides,
  };
  return {
    ...state,
    eventContentVersionByStream: {
      ...state.eventContentVersionByStream,
      [STREAM_ID]: syntheticContentVersion++,
    },
  };
}

function event(overrides: Partial<PentacleEvent>): PentacleEvent {
  return {
    daemon_seq: 1,
    host: 'host_a',
    provider: 'codex',
    session_id: STREAM_ID,
    session_name: 'ordering',
    stream_id: STREAM_ID,
    timestamp: '2026-06-30T12:00:00.000Z',
    kind: 'ASSIST',
    text: '',
    ...overrides,
  };
}

function permutations<T>(items: T[]): T[][] {
  if (items.length <= 1) return [items];
  const result: T[][] = [];
  for (let index = 0; index < items.length; index += 1) {
    const remaining = [...items.slice(0, index), ...items.slice(index + 1)];
    for (const tail of permutations(remaining)) {
      result.push([items[index], ...tail]);
    }
  }
  return result;
}

test('session detail renders committed events by daemon order when arrival order differs', () => {
  const newer = event({
    daemon_seq: 3,
    timestamp: '2026-06-30T12:00:03.000Z',
    kind: 'ASSIST',
    text: 'third',
  });
  const older = event({
    daemon_seq: 2,
    timestamp: '2026-06-30T12:00:02.000Z',
    kind: 'USER',
    text: 'second',
  });
  const state = applyPentacleEvent(applyPentacleEvent(buildState(), newer), older);

  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' })?.transcriptItems;

  assert.deepEqual(items?.map((item) => item.text), ['second', 'third']);
});

test('current-tail accepts a newer daemon event when its correlation is absent', () => {
  const local = applyPentacleEvent(buildState(), event({
    daemon_seq: 356586,
    correlatedDaemonSeq: 356586,
    timestamp: '2026-08-02T23:40:13.570Z',
    text: 'stale local tail',
  }));

  const selection = selectCurrentTailEvents(local, [event({
    daemon_seq: 356645,
    correlatedDaemonSeq: null,
    timestamp: '2026-08-02T23:45:07.880Z',
    text: 'new daemon tail',
  })], STREAM_ID, 'request_stream_events:mount-fetch');

  assert.deepEqual(selection.rejectedStreamIds, []);
  assert.equal(selection.events.length, 1);
  assert.equal(selection.events[0]?.daemon_seq, 356645);
});

test('session detail keeps a newer null-correlation tail at the visible daemon-order end', () => {
  const older = event({
    daemon_seq: 356586,
    correlatedDaemonSeq: 356586,
    timestamp: '2026-08-02T23:40:13.570Z',
    kind: 'USER',
    text: 'older tail',
  });
  const newer = event({
    daemon_seq: 356645,
    correlatedDaemonSeq: null,
    timestamp: '2026-08-02T23:45:07.880Z',
    kind: 'ASSIST',
    text: 'newer null-correlation tail',
  });
  const state = applyPentacleEvent(applyPentacleEvent(buildState(), older), newer);

  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' })?.transcriptItems;
  assert.deepEqual(items?.map((item) => item.text), ['older tail', 'newer null-correlation tail']);
});

test('fetched non-overlapping tail appends without duplication or mis-ordering', () => {
  const withHeldWindow = applyPentacleEvent(
    applyPentacleEvent(
      buildState(),
      event({ daemon_seq: 1, timestamp: '2026-06-30T12:00:01.000Z', text: 'held one' }),
    ),
    event({ daemon_seq: 2, timestamp: '2026-06-30T12:00:02.000Z', text: 'held two' }),
  );

  const withTail = applyFetchedStreamEvents(withHeldWindow, [
    event({ daemon_seq: 50, timestamp: '2026-06-30T12:00:50.000Z', text: 'tail fifty' }),
    event({ daemon_seq: 51, timestamp: '2026-06-30T12:00:51.000Z', text: 'tail fifty one' }),
  ]);
  const afterDuplicateTail = applyFetchedStreamEvents(withTail, [
    event({ daemon_seq: 50, timestamp: '2026-06-30T12:00:50.000Z', text: 'tail fifty duplicate' }),
    event({ daemon_seq: 52, timestamp: '2026-06-30T12:00:52.000Z', text: 'tail fifty two' }),
  ]);

  assert.deepEqual(
    afterDuplicateTail.events
      .filter((item) => item.stream_id === STREAM_ID)
      .map((item) => [item.daemon_seq, item.text]),
    [
      [1, 'held one'],
      [2, 'held two'],
      [51, 'tail fifty one'],
      [50, 'tail fifty duplicate'],
      [52, 'tail fifty two'],
    ],
  );
  assert.equal(
    afterDuplicateTail.events.filter((item) => item.stream_id === STREAM_ID && item.daemon_seq === 50).length,
    1,
  );
  const items = selectSessionDetail(afterDuplicateTail, STREAM_ID, { visibleCount: 'all' })?.transcriptItems;
  assert.deepEqual(items?.map((item) => item.text), [
    'held one',
    'held two',
    'tail fifty duplicate',
    'tail fifty one',
    'tail fifty two',
  ]);
});

test('bounded session detail chooses the latest display window, not insertion tail', () => {
  const history = Array.from({ length: 284 }, (_, index) => {
    const seq = 600 - index;
    return event({
      daemon_seq: seq,
      timestamp: `2026-06-30T12:${String(Math.floor(seq / 60)).padStart(2, '0')}:${String(seq % 60).padStart(2, '0')}.000Z`,
      text: `history ${seq}`,
    });
  });
  const state = applyFetchedStreamEvents(buildState(), history);

  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 16 })?.transcriptItems ?? [];

  assert.deepEqual(
    items.map((item) => item.text),
    Array.from({ length: 16 }, (_, index) => `history ${585 + index}`),
  );
});

test('fetched history dedupes daemon sequences per stream, not globally', () => {
  const streamA = STREAM_ID;
  const streamB = 'host_a:codex:ordering-b';
  const sessionB = session({
    stream_id: streamB,
    session_name: 'ordering-b',
    last_text: 'stream b first',
  });
  const base = buildState({ sessions: [session(), sessionB] });

  const state = applyFetchedStreamEvents(base, [
    event({ stream_id: streamA, session_id: streamA, session_name: 'ordering', daemon_seq: 1, text: 'stream a first' }),
    event({ stream_id: streamB, session_id: streamB, session_name: 'ordering-b', daemon_seq: 1, text: 'stream b first' }),
  ]);

  const rowsA = selectSessionDetail(state, streamA, { visibleCount: 'all' })?.transcriptItems ?? [];
  const rowsB = selectSessionDetail(state, streamB, { visibleCount: 'all' })?.transcriptItems ?? [];

  assert.deepEqual(rowsA.map((item) => item.text), ['stream a first']);
  assert.deepEqual(rowsB.map((item) => item.text), ['stream b first']);
});

test('live event apply dedupes daemon sequences per stream, not globally', () => {
  const streamA = STREAM_ID;
  const streamB = 'host_a:codex:ordering-b';
  const sessionB = session({
    stream_id: streamB,
    session_name: 'ordering-b',
    last_text: 'stream b first',
  });
  const base = buildState({ sessions: [session(), sessionB] });

  const state = applyPentacleEvent(
    applyPentacleEvent(
      base,
      event({ stream_id: streamA, session_id: streamA, session_name: 'ordering', daemon_seq: 7, text: 'stream a seven' }),
    ),
    event({ stream_id: streamB, session_id: streamB, session_name: 'ordering-b', daemon_seq: 7, text: 'stream b seven' }),
  );

  const rowsA = selectSessionDetail(state, streamA, { visibleCount: 'all' })?.transcriptItems ?? [];
  const rowsB = selectSessionDetail(state, streamB, { visibleCount: 'all' })?.transcriptItems ?? [];

  assert.deepEqual(rowsA.map((item) => item.text), ['stream a seven']);
  assert.deepEqual(rowsB.map((item) => item.text), ['stream b seven']);
});

test('session detail orders mixed USER ASSIST SYSTEM events by timestamp regardless of arrival order', () => {
  const assist = event({
    daemon_seq: 30,
    timestamp: '2026-06-30T12:00:03.000Z',
    kind: 'ASSIST',
    text: 'assist third',
  });
  const user = event({
    daemon_seq: 10,
    timestamp: '2026-06-30T12:00:01.000Z',
    kind: 'USER',
    text: 'user first',
  });
  const system = event({
    daemon_seq: 20,
    timestamp: '2026-06-30T12:00:02.000Z',
    kind: 'SYSTEM',
    text: 'system second',
  });
  const state = applyPentacleEvent(
    applyPentacleEvent(
      applyPentacleEvent(buildState(), assist),
      system,
    ),
    user,
  );

  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' })?.transcriptItems;

  assert.deepEqual(state.events.map((item) => item.text), ['assist third', 'system second', 'user first']);
  assert.deepEqual(items?.map((item) => item.text), ['user first', 'assist third']);
  assert.deepEqual(items?.map((item) => item.kind), ['USER', 'ASSIST']);
});

test('session detail uses daemon sequence as the same-timestamp tiebreak', () => {
  const laterSeq = event({
    daemon_seq: 42,
    timestamp: '2026-06-30T12:00:01.000Z',
    kind: 'ASSIST',
    text: 'seq 42',
  });
  const earlierSeq = event({
    daemon_seq: 41,
    timestamp: '2026-06-30T12:00:01.000Z',
    kind: 'USER',
    text: 'seq 41',
  });
  const state = applyPentacleEvent(applyPentacleEvent(buildState(), laterSeq), earlierSeq);

  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' })?.transcriptItems;

  assert.deepEqual(items?.map((item) => item.text), ['seq 41', 'seq 42']);
});

test('session detail places uncorrelated optimistic rows by timestamp without losing stable row ids', () => {
  const before = event({
    daemon_seq: 1,
    timestamp: '2026-06-30T12:00:01.000Z',
    kind: 'ASSIST',
    text: 'before',
  });
  const after = event({
    daemon_seq: 2,
    timestamp: '2026-06-30T12:00:03.000Z',
    kind: 'ASSIST',
    text: 'after',
  });
  const withServerEvents = applyPentacleEvent(applyPentacleEvent(buildState(), before), after);
  const withOptimistic = sendOptimisticMessage(withServerEvents, {
    streamId: STREAM_ID,
    text: 'between',
    optimisticId: 'optimistic_host_a_ordering_1',
    requestId: 'request-ordering-1',
    createdAt: Date.parse('2026-06-30T12:00:02.000Z'),
    windowStartedAt: Date.parse('2026-06-30T12:00:02.000Z'),
  });

  const items = selectSessionDetail(withOptimistic, STREAM_ID, { visibleCount: 'all' })?.transcriptItems;

  assert.deepEqual(items?.map((item) => item.text), ['before', 'between', 'after']);
  assert.equal(items?.[1].optimisticId, 'optimistic_host_a_ordering_1');
});

test('session detail keeps a newer same-text session fallback when the matching event is older', () => {
  const state = buildState({
    sessions: [session({
      last_event_at: '2026-06-30T12:00:05.000Z',
      last_kind: 'ASSIST',
      last_text: 'repeat',
    })],
    events: [
      event({
        daemon_seq: 1,
        timestamp: '2026-06-30T12:00:01.000Z',
        kind: 'ASSIST',
        text: 'repeat',
      }),
      event({
        daemon_seq: 2,
        timestamp: '2026-06-30T12:00:02.000Z',
        kind: 'USER',
        text: 'next',
      }),
    ],
  });

  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' })?.transcriptItems;

  assert.deepEqual(items?.map((item) => item.text), ['repeat', 'next', 'repeat']);
  assert.equal(items?.at(-1)?.id, `fallback:${STREAM_ID}`);
});

test('session detail hides structured tool fallback by default while preserving a neighboring assistant row', () => {
  const state = buildState({
    sessions: [session({
      provider: 'claude',
      last_kind: 'TOOL_BATCH_SUMMARY',
      last_text: 'Read 1 file',
    })],
    events: [
      event({
        daemon_seq: 1,
        provider: 'claude',
        kind: 'TOOL_USE',
        text: 'Read a.ts',
        raw: { source: 'claude-jsonl', tool_use_id: 'toolu_1', tool_name: 'Read', tool_input: { file_path: 'a.ts' } },
      }),
      event({
        daemon_seq: 2,
        provider: 'claude',
        kind: 'TOOL_BATCH_SUMMARY',
        text: 'Read 1 file',
        raw: { source: 'claude-jsonl', subtype: 'tool-batch-summary', span_size: 1, tool_use_ids: ['toolu_other'] },
      }),
      event({
        daemon_seq: 3,
        provider: 'claude',
        kind: 'ASSIST',
        text: 'Visible assistant control',
        raw: { source: 'claude-jsonl' },
      }),
    ],
  });

  const detail = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' });
  const verboseDetail = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all', showToolActions: true });

  assert.deepEqual(detail?.transcriptItems.map((item) => item.kind), ['ASSIST']);
  assert.deepEqual(detail?.transcriptItems.map((item) => item.displayRule), ['bubble:assistant']);
  assert.deepEqual(verboseDetail?.transcriptItems.map((item) => item.kind), ['TOOL_USE', 'TOOL_BATCH_SUMMARY', 'ASSIST']);
  assert.deepEqual(verboseDetail?.transcriptItems.map((item) => item.displayRule), ['activity:explored', 'activity:tool-batch', 'bubble:assistant']);
});

test('session detail coalesces same-text adjacent replay events with shared raw JSONL identity', () => {
  const state = buildState({
    sessions: [session({
      last_event_at: '2026-06-30T12:00:02.000Z',
      last_kind: 'ASSIST',
      last_text: 'repeat',
    })],
    events: [
      event({
        daemon_seq: 1,
        timestamp: '2026-06-30T12:00:01.000Z',
        kind: 'ASSIST',
        text: 'repeat',
        raw: { source: 'claude-jsonl', jsonl_record_uuid: 'jsonl-repeat-1' },
      }),
      event({
        daemon_seq: 2,
        timestamp: '2026-06-30T12:00:02.000Z',
        kind: 'ASSIST',
        text: 'repeat   ',
        raw: { source: 'claude-jsonl', jsonl_record_uuid: 'jsonl-repeat-1' },
      }),
    ],
  });

  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' })?.transcriptItems;

  assert.deepEqual(items?.map((item) => item.id), ['1']);
  assert.deepEqual(items?.map((item) => item.text), ['repeat']);
});

test('session detail coalesces same-text replay events with shared raw JSONL identity across intervening rows', () => {
  const state = buildState({
    events: [
      event({
        daemon_seq: 1,
        provider: 'claude',
        timestamp: '2026-06-30T12:00:01.000Z',
        kind: 'USER',
        text: 'status?',
        raw: { source: 'claude-jsonl', jsonl_record_uuid: 'jsonl-status-user-1' },
      }),
      event({
        daemon_seq: 2,
        provider: 'claude',
        timestamp: '2026-06-30T12:00:02.000Z',
        kind: 'ASSIST',
        text: 'No change.',
        raw: { source: 'claude-jsonl', jsonl_record_uuid: 'jsonl-status-assist-1' },
      }),
      event({
        daemon_seq: 3,
        provider: 'claude',
        timestamp: '2026-06-30T12:00:03.000Z',
        kind: 'ASSIST',
        text: 'Unrelated update.',
        raw: { source: 'claude-jsonl', jsonl_record_uuid: 'jsonl-unrelated-1' },
      }),
      event({
        daemon_seq: 4,
        provider: 'claude',
        timestamp: '2026-06-30T12:00:04.000Z',
        kind: 'USER',
        text: 'status?',
        raw: { source: 'claude-jsonl', jsonl_record_uuid: 'jsonl-status-user-1' },
      }),
      event({
        daemon_seq: 5,
        provider: 'claude',
        timestamp: '2026-06-30T12:00:05.000Z',
        kind: 'ASSIST',
        text: 'No change.',
        raw: { source: 'claude-jsonl', jsonl_record_uuid: 'jsonl-status-assist-1' },
      }),
    ],
  });

  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' })?.transcriptItems;

  assert.deepEqual(items?.map((item) => item.text), ['status?', 'No change.', 'Unrelated update.']);
  assert.deepEqual(items?.map((item) => item.id), ['1', '2', '3']);
});

test('session detail preserves legitimate non-adjacent same-text assistant messages', () => {
  const state = buildState({
    events: [
      event({
        daemon_seq: 1,
        timestamp: '2026-06-30T12:00:01.000Z',
        kind: 'ASSIST',
        text: 'repeat',
      }),
      event({
        daemon_seq: 2,
        timestamp: '2026-06-30T12:00:02.000Z',
        kind: 'USER',
        text: 'separator',
      }),
      event({
        daemon_seq: 3,
        timestamp: '2026-06-30T12:00:03.000Z',
        kind: 'ASSIST',
        text: 'repeat',
      }),
    ],
  });

  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' })?.transcriptItems;

  assert.deepEqual(items?.map((item) => item.text), ['repeat', 'separator', 'repeat']);
  assert.deepEqual(items?.map((item) => item.id), ['1', '2', '3']);
});

test('session detail preserves repeated status exchange with distinct JSONL identities', () => {
  const state = buildState({
    events: [
      event({
        daemon_seq: 1,
        provider: 'claude',
        timestamp: '2026-06-30T12:00:01.000Z',
        kind: 'USER',
        text: 'status?',
        raw: { source: 'claude-jsonl', jsonl_record_uuid: 'jsonl-status-user-1' },
      }),
      event({
        daemon_seq: 2,
        provider: 'claude',
        timestamp: '2026-06-30T12:00:02.000Z',
        kind: 'ASSIST',
        text: 'No change.',
        raw: { source: 'claude-jsonl', jsonl_record_uuid: 'jsonl-status-assist-1' },
      }),
      event({
        daemon_seq: 3,
        provider: 'claude',
        timestamp: '2026-06-30T12:00:03.000Z',
        kind: 'USER',
        text: 'different question',
        raw: { source: 'claude-jsonl', jsonl_record_uuid: 'jsonl-other-user-1' },
      }),
      event({
        daemon_seq: 4,
        provider: 'claude',
        timestamp: '2026-06-30T12:00:04.000Z',
        kind: 'ASSIST',
        text: 'Different answer.',
        raw: { source: 'claude-jsonl', jsonl_record_uuid: 'jsonl-other-assist-1' },
      }),
      event({
        daemon_seq: 5,
        provider: 'claude',
        timestamp: '2026-06-30T12:00:05.000Z',
        kind: 'USER',
        text: 'status?',
        raw: { source: 'claude-jsonl', jsonl_record_uuid: 'jsonl-status-user-2' },
      }),
      event({
        daemon_seq: 6,
        provider: 'claude',
        timestamp: '2026-06-30T12:00:06.000Z',
        kind: 'ASSIST',
        text: 'No change.',
        raw: { source: 'claude-jsonl', jsonl_record_uuid: 'jsonl-status-assist-2' },
      }),
    ],
  });

  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' })?.transcriptItems;

  assert.deepEqual(items?.map((item) => item.text), [
    'status?',
    'No change.',
    'different question',
    'Different answer.',
    'status?',
    'No change.',
  ]);
  assert.deepEqual(items?.map((item) => item.id), ['1', '2', '3', '4', '5', '6']);
});

test('session detail preserves same-text rows from distinct origins', () => {
  const state = buildState({
    events: [
      event({
        daemon_seq: 1,
        provider: 'codex',
        timestamp: '2026-06-30T12:00:01.000Z',
        kind: 'ASSIST',
        text: 'repeat',
        raw: { source: 'codex-jsonl' },
      }),
      event({
        daemon_seq: 2,
        provider: 'claude',
        timestamp: '2026-06-30T12:00:01.500Z',
        kind: 'ASSIST',
        text: 'repeat',
        raw: { source: 'claude-jsonl' },
      }),
    ],
  });

  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' })?.transcriptItems;

  assert.deepEqual(items?.map((item) => item.id), ['1', '2']);
  assert.deepEqual(items?.map((item) => item.text), ['repeat', 'repeat']);
});

test('session detail maps Claude JSONL Read tool results to code-block rows', () => {
  const toolUse = event({
    daemon_seq: 1201,
    provider: 'claude',
    timestamp: '2026-06-30T12:00:05.000Z',
    kind: 'TOOL_USE',
    text: 'Read /tmp/example.md',
    raw: {
      source: 'claude-jsonl',
      tool_use_id: 'toolu_read_skew',
      tool_name: 'Read',
    },
  });
  const toolResult = event({
    daemon_seq: 1005,
    provider: 'claude',
    timestamp: '2026-06-30T12:00:04.000Z',
    kind: 'TOOL_RESULT',
    text: '1\t# Example',
    raw: {
      source: 'claude-jsonl',
      tool_use_id: 'toolu_read_skew',
    },
  });
  const state = buildState({
    sessions: [session({ provider: 'claude' })],
    events: [toolResult, toolUse],
  });

  const items = selectSessionDetail(state, STREAM_ID, { includeTools: true, visibleCount: 'all' })?.transcriptItems;

  assert.deepEqual(items?.map((item) => item.id), ['1201', '1005']);
  assert.equal(items?.at(-1)?.displayRule, 'activity:code-block');
});

test('session detail produces stable Claude tool pair order across permutations', () => {
  const toolUse = event({
    daemon_seq: 1201,
    provider: 'claude',
    timestamp: '2026-06-30T12:00:05.000Z',
    kind: 'TOOL_USE',
    text: 'Read /tmp/example.md',
    raw: {
      source: 'claude-jsonl',
      tool_use_id: 'toolu_read_cycle',
      tool_name: 'Read',
    },
  });
  const unrelated = event({
    daemon_seq: 1100,
    provider: 'claude',
    timestamp: '2026-06-30T12:00:04.500Z',
    kind: 'ASSIST_TEXT',
    text: 'middle message',
    raw: { source: 'claude-jsonl' },
  });
  const toolResult = event({
    daemon_seq: 1005,
    provider: 'claude',
    timestamp: '2026-06-30T12:00:04.000Z',
    kind: 'TOOL_RESULT',
    text: '1\t# Example',
    raw: {
      source: 'claude-jsonl',
      tool_use_id: 'toolu_read_cycle',
    },
  });
  const permutations = [
    [toolResult, unrelated, toolUse],
    [toolUse, toolResult, unrelated],
    [unrelated, toolUse, toolResult],
    [toolResult, toolUse, unrelated],
  ];

  for (const events of permutations) {
    const state = buildState({
      sessions: [session({ provider: 'claude' })],
      events,
    });

    const items = selectSessionDetail(state, STREAM_ID, { includeTools: true, visibleCount: 'all' })?.transcriptItems;

    assert.deepEqual(items?.map((item) => item.id), ['1100', '1201', '1005']);
    assert.equal(items?.[2]?.displayRule, 'activity:code-block');
  }
});

test('session detail canonicalizes duplicate Claude tool uses across all input permutations', () => {
  const earlierToolUse = event({
    daemon_seq: 900,
    provider: 'claude',
    timestamp: '2026-06-30T12:00:03.000Z',
    kind: 'TOOL_USE',
    text: 'Read /tmp/example.md',
    raw: {
      source: 'claude-jsonl',
      tool_use_id: 'toolu_read_duplicate',
      tool_name: 'Read',
    },
  });
  const toolResult = event({
    daemon_seq: 1005,
    provider: 'claude',
    timestamp: '2026-06-30T12:00:04.000Z',
    kind: 'TOOL_RESULT',
    text: '1\t# Example',
    raw: {
      source: 'claude-jsonl',
      tool_use_id: 'toolu_read_duplicate',
    },
  });
  const unrelated = event({
    daemon_seq: 1100,
    provider: 'claude',
    timestamp: '2026-06-30T12:00:04.500Z',
    kind: 'ASSIST_TEXT',
    text: 'middle message',
    raw: { source: 'claude-jsonl' },
  });
  const replayedToolUse = event({
    daemon_seq: 1201,
    provider: 'claude',
    timestamp: '2026-06-30T12:00:05.000Z',
    kind: 'TOOL_USE',
    text: 'Read /tmp/example.md',
    raw: {
      source: 'claude-jsonl',
      tool_use_id: 'toolu_read_duplicate',
      tool_name: 'Read',
    },
  });
  const observedOrders = new Set<string>();

  for (const events of permutations([earlierToolUse, toolResult, unrelated, replayedToolUse])) {
    const state = buildState({
      sessions: [session({ provider: 'claude' })],
      events,
    });

    const items = selectSessionDetail(state, STREAM_ID, { includeTools: true, visibleCount: 'all' })?.transcriptItems || [];
    const ids = items.map((item) => item.id);
    observedOrders.add(ids.join(','));

    const useIndex = ids.indexOf('900');
    const resultIndex = ids.indexOf('1005');
    assert.ok(useIndex >= 0, 'canonical tool use should render');
    assert.ok(resultIndex >= 0, 'matched tool result should render');
    assert.ok(useIndex < resultIndex, 'canonical tool use should sort before its result');
    assert.equal(items[resultIndex]?.displayRule, 'activity:code-block');
  }

  assert.equal(observedOrders.size, 1);
  assert.deepEqual([...observedOrders], ['900,1005,1100,1201']);
});

test('session detail collapses Claude JSONL Agent tool rows with child count', () => {
  const state = buildState({
    sessions: [session({
      provider: 'claude',
      last_event_at: '2026-06-30T12:00:10.000Z',
      last_kind: 'TOOL_USE',
      last_text: 'Agent: Audit chat stream',
    })],
    events: [
      event({
        daemon_seq: 10,
        provider: 'claude',
        timestamp: '2026-06-30T12:00:01.000Z',
        kind: 'TOOL_USE',
        text: 'Agent: Audit chat stream',
        raw: {
          source: 'claude-jsonl',
          tool_use_id: 'toolu_agent_1',
          tool_name: 'Agent',
        },
      }),
      event({
        daemon_seq: 11,
        provider: 'claude',
        timestamp: '2026-06-30T12:00:02.000Z',
        kind: 'TOOL_RESULT',
        text: 'Audit complete',
        raw: {
          source: 'claude-jsonl',
          tool_use_id: 'toolu_agent_1',
        },
      }),
    ],
  });

  const items = selectSessionDetail(state, STREAM_ID, { includeTools: true, visibleCount: 'all' })?.transcriptItems;

  assert.deepEqual(items?.map((item) => item.id), ['10']);
  assert.equal(items?.[0]?.displayRule, 'activity:collapsed-tool');
  assert.match(items?.[0]?.text || '', /1 child event/);
});
