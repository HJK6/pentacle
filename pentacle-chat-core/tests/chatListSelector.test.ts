import assert from 'node:assert/strict';
import test from 'node:test';
import { spawnSync } from 'node:child_process';

import {
  __getInterpretMissCountForTests,
  __resetInterpretMissCountForTests,
  appendLiveEventsProjection,
  buildPentacleChatEventIndex,
  createChatListSelector,
  initialPentacleStreamState,
  invalidateSessionDetailCache,
  latestDisplayedSessionPreview,
  normalizePentacleEventBuckets,
  selectChatList,
  selectPentacleDerivedEventIndex,
  selectSessionDetail,
  type PentacleEvent,
  type PentacleSessionSummary,
  type PentacleStreamState,
} from '../src/index.ts';

function session(index: number, overrides: Partial<PentacleSessionSummary> = {}): PentacleSessionSummary {
  const streamId = `host_c:codex:stream-${index}`;
  return {
    stream_id: streamId,
    host: 'host_c',
    provider: 'codex',
    session_name: `stream-${index}`,
    last_event_at: new Date(Date.UTC(2026, 6, 12, 12, 0, index)).toISOString(),
    last_text: '',
    last_kind: '',
    draft: '',
    pending: false,
    working: false,
    online: true,
    ...overrides,
  };
}

function event(index: number, sessionCount: number): PentacleEvent {
  const streamIndex = index % sessionCount;
  return {
    daemon_seq: index + 1,
    host: 'host_c',
    provider: 'codex',
    session_id: `stream-${streamIndex}`,
    session_name: `stream-${streamIndex}`,
    stream_id: `host_c:codex:stream-${streamIndex}`,
    timestamp: new Date(Date.UTC(2026, 6, 12, 11, 0, 0, index)).toISOString(),
    kind: index % 3 === 0 ? 'USER' : 'ASSIST',
    text: `message ${index}`,
  };
}

function state(sessionCount: number, eventCount: number): PentacleStreamState {
  return {
    ...initialPentacleStreamState,
    connected: true,
    hasHydrated: true,
    sessions: Array.from({ length: sessionCount }, (_, index) => session(index)),
    events: Array.from({ length: eventCount }, (_, index) => event(index, sessionCount)),
  };
}

function countArrayReads(events: PentacleEvent[]) {
  let reads = 0;
  const value = new Proxy(events, {
    get(target, property, receiver) {
      if (typeof property === 'string' && /^\d+$/.test(property)) reads += 1;
      return Reflect.get(target, property, receiver);
    },
  });
  return { value, reads: () => reads };
}

test('pure event index groups one immutable event array by stream', () => {
  const events = [event(0, 2), event(1, 2), event(2, 2)];
  const index = buildPentacleChatEventIndex(events);
  assert.deepEqual(index.get('host_c:codex:stream-0'), [events[0], events[2]]);
  assert.deepEqual(index.get('host_c:codex:stream-1'), [events[1]]);
  assert.equal(events.length, 3);
});

test('selector indexes the global event array once and does zero event reads for unrelated emissions', () => {
  const base = state(64, 1_200);
  const counted = countArrayReads(base.events);
  const instrumented = { ...base, events: counted.value };
  const selector = createChatListSelector();

  assert.equal(selector(instrumented).length, 64);
  const firstReads = counted.reads();
  assert.ok(firstReads <= 1_200, `expected one global pass, observed ${firstReads} indexed reads`);

  for (let index = 0; index < 32; index += 1) {
    selector({ ...instrumented, connected: index % 2 === 0 });
  }
  assert.equal(counted.reads(), firstReads);
});

test('selector event-index work scales linearly and invalidates only on immutable replacement', () => {
  const readsFor = (eventCount: number, sessionCount: number) => {
    const base = state(sessionCount, eventCount);
    const counted = countArrayReads(base.events);
    createChatListSelector()({ ...base, events: counted.value });
    return counted.reads();
  };
  const reads300 = readsFor(300, 16);
  const reads600 = readsFor(600, 32);
  const reads1200 = readsFor(1_200, 64);
  assert.ok(reads600 / reads300 <= 2.25);
  assert.ok(reads1200 / reads600 <= 2.25);

  const base = state(4, 20);
  const first = countArrayReads(base.events);
  const selector = createChatListSelector();
  const firstRows = selector({ ...base, events: first.value });
  const replacement = countArrayReads([...base.events]);
  const replacementRows = selector({ ...base, events: replacement.value });
  assert.equal(replacement.reads(), base.events.length);
  assert.equal(replacementRows, firstRows);
});

test('factory instances isolate filters and structural sharing while the wrapper remains pure', () => {
  const base = state(3, 12);
  base.sessions[2] = session(2, { host: 'hosta' });
  const firstSelector = createChatListSelector();
  const secondSelector = createChatListSelector();

  const first = firstSelector(base);
  assert.equal(firstSelector(base), first);
  assert.equal(firstSelector(base, 'host_c').length, 2);
  assert.equal(firstSelector(base), first);
  assert.notEqual(secondSelector(base), first);
  assert.deepEqual(secondSelector(base), first);

  const wrapperFirst = selectChatList(base);
  const wrapperSecond = selectChatList(base);
  assert.deepEqual(wrapperSecond, wrapperFirst);
  assert.notEqual(wrapperSecond, wrapperFirst);
  assert.notEqual(wrapperSecond[0], wrapperFirst[0]);
});

test('relative labels age without rebuilding the event index or replacing unchanged rows', () => {
  const realNow = Date.now;
  let now = Date.UTC(2026, 6, 12, 12, 0, 0);
  Date.now = () => now;
  try {
    const recent = session(0, { last_event_at: new Date(now - 30_000).toISOString() });
    const stable = session(1, { last_event_at: '2026-01-01T00:00:00.000Z' });
    const events = countArrayReads([event(0, 2), event(1, 2)]);
    const base = { ...state(0, 0), sessions: [recent, stable], events: events.value };
    const selector = createChatListSelector();
    const first = selector(base);
    const indexedReads = events.reads();
    assert.equal(first[0]?.updatedLabel, 'Just now');

    now += 40_000;
    const second = selector(base);
    assert.equal(second[0]?.updatedLabel, '1m ago');
    assert.notEqual(second[0], first[0]);
    assert.equal(second[1], first[1]);
    assert.equal(events.reads(), indexedReads);
    assert.deepEqual(selectChatList(base), second);
  } finally {
    Date.now = realNow;
  }
});

test('relative date formatting is shared across a 64-row selection without changing output', () => {
  const selector = createChatListSelector();
  const snapshot = normalizePentacleEventBuckets(state(64, 64));
  const expected = selector(snapshot);
  const originalFormat = Date.prototype.toLocaleDateString;
  const OriginalFormatter = Intl.DateTimeFormat;
  let legacyCalls = 0;
  let constructions = 0;
  Date.prototype.toLocaleDateString = function (...args: [Intl.LocalesArgument?, Intl.DateTimeFormatOptions?]) {
    legacyCalls += 1;
    return originalFormat.apply(this, args);
  };
  Intl.DateTimeFormat = new Proxy(OriginalFormatter, {
    construct(target, args) {
      constructions += 1;
      return Reflect.construct(target, args);
    },
  });
  try {
    assert.deepEqual(selector(snapshot), expected);
    assert.equal(legacyCalls, 0, 'warm selection must not repeat locale-date setup for every row');
    assert.equal(constructions, 1, 'one formatter serves both preview signatures and updated labels');
  } finally {
    Date.prototype.toLocaleDateString = originalFormat;
    Intl.DateTimeFormat = OriginalFormatter;
  }
});

test('relative date formatting refreshes the default time zone between selections', () => {
  const previousZone = process.env.TZ;
  const selector = createChatListSelector();
  const timestamps = ['2020-01-01T00:30:00Z', '2020-02-29T23:30:00Z', '2020-03-08T09:30:00Z'];
  const snapshot = { ...state(0, 0), sessions: timestamps.map((last_event_at, i) => session(i, { last_event_at })) };
  try {
    for (const zone of ['UTC', 'America/Los_Angeles', 'Asia/Tokyo', 'UTC']) {
      process.env.TZ = zone;
      assert.deepEqual(selector(snapshot).map(row => row.updatedLabel), timestamps.map(timestamp =>
        new Date(timestamp).toLocaleDateString([], { month: 'short', day: 'numeric' })));
    }
  } finally {
    if (previousZone === undefined) delete process.env.TZ;
    else process.env.TZ = previousZone;
  }
});

test('relative date formatting preserves fresh-runtime default locales and calendars', () => {
  const modulePath = require.resolve('../src/index.ts');
  for (const locale of ['en_US.UTF-8', 'fr_FR.UTF-8', 'ar_EG.UTF-8']) {
    const child = spawnSync(process.execPath, ['--import', 'tsx', '-e', `
      const core = require(${JSON.stringify(modulePath)});
      const session = ${JSON.stringify(session(0, { last_event_at: '2020-02-29T23:30:00Z' }))};
      const snapshot = { ...core.initialPentacleStreamState, sessions: [session] };
      const expected = new Date(session.last_event_at).toLocaleDateString([], { month: 'short', day: 'numeric' });
      const actual = core.createChatListSelector()(snapshot)[0].updatedLabel;
      console.log(JSON.stringify({ expected, actual, locale: new Intl.DateTimeFormat().resolvedOptions().locale }));
    `], { encoding: 'utf8', env: { ...process.env, LANG: locale, LC_ALL: locale, TZ: 'Asia/Tokyo' } });
    assert.equal(child.status, 0, child.stderr);
    const result = JSON.parse(child.stdout);
    assert.equal(result.actual, result.expected);
    assert.ok(result.locale.startsWith(locale.slice(0, 2)), `default locale was not exercised: ${result.locale}`);
  }
});

test('relative date formatting preserves invalid, future and minute/hour/day boundary labels', () => {
  const originalNow = Date.now;
  const now = Date.UTC(2026, 8, 6, 12);
  Date.now = () => now;
  try {
    const cases: [string, string][] = [
      ['', 'No activity yet'], ['invalid', 'No activity yet'],
      [new Date(now + 1).toISOString(), 'Just now'],
      [new Date(now - 59_999).toISOString(), 'Just now'],
      [new Date(now - 60_000).toISOString(), '1m ago'],
      [new Date(now - 3_599_999).toISOString(), '60m ago'],
      [new Date(now - 3_600_000).toISOString(), '1h ago'],
      [new Date(now - 86_399_999).toISOString(), '24h ago'],
      [new Date(now - 86_400_000).toISOString(), new Date(now - 86_400_000).toLocaleDateString([], { month: 'short', day: 'numeric' })],
    ];
    const snapshot = { ...state(0, 0), sessions: cases.map(([last_event_at], i) => session(i, { last_event_at })) };
    assert.deepEqual(createChatListSelector()(snapshot).map(row => row.updatedLabel), cases.map(([, expected]) => expected));
  } finally {
    Date.now = originalNow;
  }
});

test('bounded transcript windows retain conversation bubbles through a tool-heavy tail', () => {
  const streamId = 'host_c:claude:tool-heavy';
  let sequence = 1_000;
  const events: PentacleEvent[] = [];
  const add = (kind: string, text: string, raw: Record<string, unknown> = { source: 'claude-jsonl' }) => {
    events.push({
      ...event(sequence, 1),
      daemon_seq: sequence,
      stream_id: streamId,
      provider: 'claude',
      kind,
      text,
      raw,
    });
    sequence += 1;
  };
  for (let turn = 0; turn < 12; turn += 1) {
    add('USER', `user question ${turn}`);
    add('ASSIST_TEXT', `assistant reply ${turn}`);
    for (let tool = 0; tool < 8; tool += 1) {
      const toolUseId = `tool-${turn}-${tool}`;
      add('TOOL_USE', `Read ${tool}`, { source: 'claude-jsonl', tool_use_id: toolUseId, tool_name: 'Read' });
      add('TOOL_RESULT', `result ${tool}`, { source: 'claude-jsonl', tool_use_id: toolUseId });
    }
  }
  for (let tool = 0; tool < 40; tool += 1) {
    const toolUseId = `tail-${tool}`;
    add('TOOL_USE', `Bash ${tool}`, { source: 'claude-jsonl', tool_use_id: toolUseId, tool_name: 'Bash' });
    add('TOOL_RESULT', `output ${tool}`, { source: 'claude-jsonl', tool_use_id: toolUseId });
  }
  const detail = selectSessionDetail(
    {
      ...state(0, 0),
      sessions: [{ ...session(0), stream_id: streamId, provider: 'claude' }],
      events,
      eventContentVersionByStream: { [streamId]: 1 },
    },
    streamId,
    { visibleCount: 120, includeDraft: false },
  );
  assert.ok((detail?.transcriptItems.filter((item) => item.isUser).length || 0) >= 10);
  assert.ok((detail?.transcriptItems.filter((item) => item.tone === 'assistant').length || 0) >= 10);
});

test('exact remaining counts interpret the held set once and reuse the cache while widening', () => {
  const streamId = 'host_c:codex:load-earlier';
  const events = Array.from({ length: 500 }, (_, index) => ({
    ...event(index, 1),
    daemon_seq: 2_000 + index,
    stream_id: streamId,
    kind: index % 2 === 0 ? 'USER' : 'ASSIST',
    text: `load earlier message ${index}`,
  }));
  const snapshot = {
    ...state(0, 0),
    sessions: [{ ...session(0), stream_id: streamId }],
    events,
    eventContentVersionByStream: { [streamId]: 1 },
  };

  __resetInterpretMissCountForTests();
  selectSessionDetail(snapshot, streamId, { visibleCount: 8 });
  const initialMisses = __getInterpretMissCountForTests();
  selectSessionDetail(snapshot, streamId, { visibleCount: 32 });
  const addedMisses = __getInterpretMissCountForTests() - initialMisses;

  assert.equal(initialMisses, 500);
  assert.equal(addedMisses, 0);
});

test('plain retained assistant previews interpret only the latest ordered event', () => {
  const streamId = 'host_c:codex:plain-preview';
  const events = Array.from({ length: 1200 }, (_, index): PentacleEvent => ({
    daemon_seq: index + 1,
    host: 'host_c',
    provider: 'codex',
    session_id: 'plain-preview',
    session_name: 'plain-preview',
    stream_id: streamId,
    timestamp: new Date(Date.UTC(2026, 6, 12, 12, 0, 0, index)).toISOString(),
    kind: 'ASSIST',
    text: `plain row ${index + 1}`,
  }));
  const latest = events.at(-1)!;
  const snapshot: PentacleStreamState = {
    ...initialPentacleStreamState,
    sessions: [session(0, {
      stream_id: streamId,
      session_name: 'plain-preview',
      last_event_at: latest.timestamp,
      last_text: latest.text,
      last_kind: latest.kind,
    })],
    events,
  };

  __resetInterpretMissCountForTests();
  const rows = createChatListSelector()(snapshot);

  assert.equal(rows[0]?.previewText, 'plain row 1200');
  assert.equal(__getInterpretMissCountForTests(), 1);
});

test('plain preview guard preserves ordering, hidden, foreign, replay, progressive, and summary invalidation', () => {
  const streamId = 'host_c:codex:guarded-preview';
  const makeEvent = (daemonSeq: number, text: string, overrides: Partial<PentacleEvent> = {}): PentacleEvent => ({
    daemon_seq: daemonSeq,
    host: 'host_c',
    provider: 'codex',
    session_id: 'guarded-preview',
    session_name: 'guarded-preview',
    stream_id: streamId,
    timestamp: new Date(Date.UTC(2026, 6, 12, 12, 0, 0, daemonSeq)).toISOString(),
    kind: 'ASSIST',
    text,
    ...overrides,
  });
  const preview = (events: PentacleEvent[], current: PentacleSessionSummary) => latestDisplayedSessionPreview(
    { ...initialPentacleStreamState, sessions: [current], events },
    current,
    'fallback',
    events,
  );

  const orderedLatest = makeEvent(3, 'ordered latest');
  const orderedSession = session(0, {
    stream_id: streamId,
    last_event_at: orderedLatest.timestamp,
    last_text: orderedLatest.text,
    last_kind: orderedLatest.kind,
  });
  const foreign = makeEvent(99, 'foreign latest', { stream_id: 'host_c:codex:foreign' });
  assert.equal(preview([orderedLatest, foreign, makeEvent(1, 'ordered older')], orderedSession), 'ordered latest');

  const hiddenTool = makeEvent(5, 'Bash hidden command', {
    kind: 'TOOL_USE',
    raw: { source: 'claude-jsonl', tool_use_id: 'tool-hidden', tool_name: 'Bash' },
  });
  assert.equal(preview([makeEvent(4, 'visible before tool'), hiddenTool], {
    ...orderedSession,
    last_event_at: hiddenTool.timestamp,
    last_text: hiddenTool.text,
    last_kind: hiddenTool.kind,
  }), 'visible before tool');

  const replayLeft = makeEvent(6, 'durable replay');
  const replayRight = makeEvent(6, 'durable replay');
  __resetInterpretMissCountForTests();
  assert.equal(preview([replayLeft, replayRight], {
    ...orderedSession,
    last_event_at: replayRight.timestamp,
    last_text: replayRight.text,
  }), 'durable replay');
  assert.equal(__getInterpretMissCountForTests(), 2);

  const progressiveStart = makeEvent(7, 'Deploying the exact immutable candidate to production');
  const intervening = makeEvent(8, 'intervening newest row');
  const progressiveLatest = makeEvent(9, 'Deploying the exact immutable candidate to production now');
  assert.equal(preview([progressiveStart, intervening, progressiveLatest], {
    ...orderedSession,
    last_event_at: progressiveLatest.timestamp,
    last_text: progressiveLatest.text,
  }), 'intervening newest row');

  assert.equal(preview([orderedLatest], {
    ...orderedSession,
    last_event_at: makeEvent(10, '').timestamp,
    last_text: 'newer summary fallback',
  }), 'newer summary fallback');
});

function characterizeStressContinuation(withCanonicalRaw: boolean) {
  const sessionCount = 64;
  const setupCount = 19_120;
  const burstCount = 16;
  const origin = Date.UTC(2026, 6, 12, 12, 0, 0);
  const sessions = Array.from({ length: sessionCount }, (_, index) => ({
    ...session(index),
    stream_id: `mock-host:freeze-${String(index).padStart(2, '0')}`,
    host: 'mock-host',
    session_name: `freeze-${String(index).padStart(2, '0')}`,
    title: `Freeze fixture ${String(index).padStart(2, '0')}`,
    last_event_at: new Date(origin).toISOString(),
    last_text: '',
    last_kind: '',
    working: index % 8 === 0,
  }));
  const makeStressEvent = (offset: number): PentacleEvent => ({
    daemon_seq: offset + 1,
    host: 'mock-host',
    provider: 'codex',
    session_id: '',
    session_name: 'freeze-00',
    stream_id: `mock-host:freeze-${String(offset % sessionCount).padStart(2, '0')}`,
    timestamp: new Date(origin + offset + 1).toISOString().replace('Z', '000Z'),
    kind: 'ASSIST',
    text: `freeze event ${offset + 1}`,
    // scripted_replay.event_payload_for_frame supplies envelope metadata even
    // when the frozen fixture frame has no raw field (burst fixture3862106).
    ...(withCanonicalRaw ? { raw: { source: 'scripted-daemon', session_name: 'freeze-00' } } : {}),
  });
  const setupEvents = Array.from({ length: setupCount }, (_, index) => makeStressEvent(index));
  const setupState = normalizePentacleEventBuckets({
    ...initialPentacleStreamState,
    connected: true,
    hasHydrated: true,
    sessions,
    events: setupEvents,
  });
  assert.equal(setupState.events.length, setupCount, 'stress setup must retain all 19,120 events');
  let countRetainedReads = false;
  let retainedReads = 0;
  let fullProjectionCalls = 0;
  const retainedReadsByStream = new Map<string, number>();
  const observeEvents = (streamId: string, events: PentacleEvent[]) => new Proxy(events, {
    get(target, property, receiver) {
      if (countRetainedReads && property === 'filter') fullProjectionCalls += 1;
      if (countRetainedReads && typeof property === 'string' && /^\d+$/.test(property)) {
        retainedReads += 1;
        retainedReadsByStream.set(streamId, (retainedReadsByStream.get(streamId) || 0) + 1);
      }
      return Reflect.get(target, property, receiver);
    },
  });
  const observedSetup = {
    ...setupState,
    eventBucketsByStream: Object.fromEntries(Object.entries(setupState.eventBucketsByStream || {}).map(([streamId, bucket]) => [
      streamId,
      { ...bucket, events: observeEvents(streamId, bucket.events) },
    ])),
  };
  const selector = createChatListSelector();
  for (const item of sessions) invalidateSessionDetailCache(item.stream_id);
  const setupRows = selector(observedSetup);

  const burst = Array.from({ length: burstCount }, (_, index) => makeStressEvent(setupCount + index));
  const burstByStream = new Map<string, PentacleEvent[]>();
  for (const item of burst) burstByStream.set(item.stream_id, [item]);
  const continued = appendLiveEventsProjection(
    observedSetup,
    burstByStream,
    [...observedSetup.events, ...burst],
    { perStreamMaxEvents: 1_000, totalEventCostMax: 1_000_000_000 },
  );
  let eventSideEligible = 0;
  let exactSummaryParity = 0;
  for (const item of sessions) {
    const latest = continued.eventBucketsByStream?.[item.stream_id]?.events.at(-1);
    if (!latest) continue;
    if (
      latest.kind === 'ASSIST' && latest.text.trim().length < 40 &&
      !latest.raw && !latest.attachments?.length && !latest.client_origin &&
      !latest.optimistic_id && !latest.jsonl_record_uuid &&
      !latest.jsonl_resolution_for_record_uuid && latest.correlatedDaemonSeq == null
    ) eventSideEligible += 1;
    if (
      item.last_kind === latest.kind && item.last_text === latest.text &&
      Date.parse(item.last_event_at || '') === Date.parse(latest.timestamp || '')
    ) exactSummaryParity += 1;
  }
  assert.equal(eventSideEligible, withCanonicalRaw ? 0 : sessionCount);
  assert.equal(exactSummaryParity, 0, 'frozen summaries cannot use the direct summary-preview shortcut');
  assert.equal(burst.filter(item => Boolean(item.raw && Object.keys(item.raw).length)).length, withCanonicalRaw ? burstCount : 0);
  const observedBuckets = Object.fromEntries(Object.entries(continued.eventBucketsByStream || {}).map(([streamId, bucket]) => [
    streamId,
    {
      ...bucket,
      events: burstByStream.has(streamId) ? observeEvents(streamId, bucket.events) : bucket.events,
    },
  ]));
  const observed = { ...continued, eventBucketsByStream: observedBuckets };
  const firstIndex = selectPentacleDerivedEventIndex(observed);
  const copiedStateIndex = selectPentacleDerivedEventIndex({ ...observed, sessions: [...observed.sessions] });
  assert.equal(copiedStateIndex, firstIndex, 'copying state/sessions must reuse the derived event index');

  __resetInterpretMissCountForTests();
  countRetainedReads = true;
  const rows = selector(observed);
  assert.equal(rows.length, sessionCount);
  assert.equal(__getInterpretMissCountForTests(), burstCount, 'only the 16 new event objects should miss interpretation cache');
  assert.equal(fullProjectionCalls, 0, 'plain canonical envelope metadata must retain append-preview admission');
  if (!withCanonicalRaw) {
    assert.equal(retainedReads, 9_568, 'strict prefix proof reads both prior and current touched-stream views');
    for (const streamId of burstByStream.keys()) assert.equal(retainedReadsByStream.get(streamId), 598);
  }
  assert.equal(retainedReadsByStream.size, burstCount);
  const verifyRows = (snapshot: PentacleStreamState, nextRows: typeof rows, previousRows: typeof rows) => {
    const previousById = new Map(previousRows.map(row => [row.streamId, row]));
    assert.equal(nextRows.filter(row => row === previousById.get(row.streamId)).length, sessionCount - burstCount);
    assert.deepEqual(nextRows.map(row => row.streamId), sessions.map(item => item.stream_id));
    fullProjectionCalls = 0;
    __resetInterpretMissCountForTests();
    assert.equal(selector(snapshot), nextRows, 'repeat selection reuses the entire ordered row array');
    assert.equal(fullProjectionCalls, 0, 'repeat selection does not rebuild a full transcript');
    assert.equal(__getInterpretMissCountForTests(), 0);
    countRetainedReads = false;
    for (const row of nextRows) {
      const item = sessions.find(candidate => candidate.stream_id === row.streamId)!;
      const events = snapshot.eventBucketsByStream![row.streamId].events;
      assert.equal(row.previewText, latestDisplayedSessionPreview(snapshot, item, 'Waiting for first message…', events),
        'explicit retained view supplies an uncached full-projector preview oracle');
    }
  };
  verifyRows(observed, rows, setupRows);
  const results = [rows];
  let current: PentacleStreamState = observed;
  for (let burstIndex = 1; burstIndex < 5; burstIndex += 1) {
    const nextBurst = Array.from({ length: burstCount }, (_, index) => makeStressEvent(setupCount + burstIndex * burstCount + index));
    const nextByStream = new Map(nextBurst.map(item => [item.stream_id, [item]]));
    const nextState = appendLiveEventsProjection(current, nextByStream, [...current.events, ...nextBurst],
      { perStreamMaxEvents: 1_000, totalEventCostMax: 1_000_000_000 });
    current = {
      ...nextState,
      eventBucketsByStream: Object.fromEntries(Object.entries(nextState.eventBucketsByStream || {}).map(([streamId, bucket]) => [
        streamId, { ...bucket, events: nextByStream.has(streamId) ? observeEvents(streamId, bucket.events) : bucket.events },
      ])),
    };
    fullProjectionCalls = 0;
    retainedReadsByStream.clear();
    __resetInterpretMissCountForTests();
    countRetainedReads = true;
    const nextRows = selector(current);
    assert.equal(fullProjectionCalls, 0);
    assert.equal(__getInterpretMissCountForTests(), burstCount);
    for (const streamId of nextByStream.keys()) assert.ok(retainedReadsByStream.has(streamId));
    assert.equal(current.events.length, setupCount + (burstIndex + 1) * burstCount);
    verifyRows(current, nextRows, results.at(-1)!);
    results.push(nextRows);
  }
  assert.equal(current.events.length, 19_200);
  return results;
}

test('stress continuation admits canonical raw with full output parity against the raw-free control', () => {
  const rawFreeControl = characterizeStressContinuation(false);
  const canonicalRaw = characterizeStressContinuation(true);
  assert.deepEqual(canonicalRaw, rawFreeControl, 'all row fields and order match across all five bursts');
});

test('cached append preview admits plain envelope metadata and preserves semantic or unknown raw fallback', () => {
  const streamId = 'host_c:codex:raw-admission';
  const current = session(0, { stream_id: streamId, last_event_at: '', last_text: '', last_kind: '' });
  const make = (seq: number, raw?: PentacleEvent['raw']): PentacleEvent => ({
    ...event(seq - 1, 1), stream_id: streamId, kind: 'ASSIST', text: `assistant ${seq}`, raw,
  });
  const cases: [string, PentacleEvent['raw'], boolean][] = [
    ['canonical envelope', { source: 'scripted-daemon', session_name: 'freeze-00' }, true],
    ['generic default-route envelope', { source: 'terminal-feed', session_name: 'one', sessionId: 'session-one' }, true],
    ['session metadata without source', { session_name: 'one', sessionId: 'session-one' }, true],
    ['structured source', { source: 'structured' }, false],
    ['JSONL source', { source: 'claude-jsonl' }, false],
    ['scrollback source', { source: 'scrollback_fallback' }, false],
    ['structured transport', { transport: 'codex-rollout' }, false],
    ['JSONL transport', { transport: 'claude-jsonl' }, false],
    ['replay uuid', { uuid: 'shared-replay' }, false],
    ['JSONL identity', { jsonl_record_uuid: 'shared-replay' }, false],
    ['resolution identity', { jsonl_resolution_for_record_uuid: 'shared-replay' }, false],
    ['sidechain', { is_sidechain: true }, false],
    ['tool provenance', { tool_use_id: 'tool-one' }, false],
    ['unknown key', { source: 'terminal-feed', future_semantics: false }, false],
    ['malformed source', { source: 1 }, false],
    ['malformed session metadata', { session_name: null }, false],
    ['inherited metadata', Object.create({ source: 'scrollback_fallback' }), false],
    ['non-enumerable semantic key', Object.defineProperty({}, 'uuid', { value: 'shared-replay' }), false],
    ['accessor metadata', Object.defineProperty({}, 'source', { get: () => 'terminal-feed', enumerable: true }), false],
  ];
  for (const [name, raw, expectFast] of cases) {
    for (const position of ['prior-terminal', 'new-tail'] as const) {
      invalidateSessionDetailCache(streamId);
      const selector = createChatListSelector();
      const prior = [make(1), make(2, position === 'prior-terminal' ? raw : undefined)];
      const before = normalizePentacleEventBuckets({ ...initialPentacleStreamState, sessions: [current], events: prior });
      selector(before);
      const tail = make(3, position === 'new-tail' ? raw : undefined);
      const next = appendLiveEventsProjection(before, new Map([[streamId, [tail]]]), [...before.events, tail]);
      const bucket = next.eventBucketsByStream![streamId];
      let fullProjections = 0;
      const events = new Proxy(bucket.events, {
        get(target, property, receiver) {
          if (property === 'filter') fullProjections += 1;
          return Reflect.get(target, property, receiver);
        },
      });
      const observed = { ...next, eventBucketsByStream: { ...next.eventBucketsByStream, [streamId]: { ...bucket, events } } };
      const rows = selector(observed);
      const calls = fullProjections;
      const expected = latestDisplayedSessionPreview(observed, current, 'Waiting for first message…', events);
      assert.equal(rows[0].previewText, expected, `${name}/${position} output parity`);
      assert.equal(calls, expectFast ? 0 : 1, `${name}/${position} admission`);
      assert.equal(tail.raw, position === 'new-tail' ? raw : undefined, 'raw metadata is never rewritten');
    }
  }
});

test('cached append preview falls back on changed history, retention, replay, progressive, provenance, turn, and eviction ambiguity', () => {
  const streamId = 'host_c:codex:append-admission';
  const make = (seq: number, text: string, overrides: Partial<PentacleEvent> = {}): PentacleEvent => ({
    daemon_seq: seq,
    host: 'host_c',
    provider: 'codex',
    session_id: 'append-admission',
    session_name: 'append-admission',
    stream_id: streamId,
    timestamp: new Date(Date.UTC(2026, 6, 12, 14, 0, 0, seq)).toISOString(),
    kind: 'ASSIST',
    text,
    ...overrides,
  });
  const assist1 = make(1, 'first assistant');
  const assist2 = make(2, 'second assistant');
  const assist3 = make(3, 'third assistant');
  const current = session(0, {
    stream_id: streamId,
    session_name: 'append-admission',
    last_event_at: '',
    last_text: '',
    last_kind: '',
  });

  const check = (
    name: string,
    baseEvents: PentacleEvent[],
    nextEvents: PentacleEvent[],
    expectFast: boolean,
    evict = false,
  ) => {
    invalidateSessionDetailCache(streamId);
    const selector = createChatListSelector();
    const base = normalizePentacleEventBuckets({
      ...initialPentacleStreamState,
      sessions: [current],
      events: baseEvents,
    });
    selector(base);
    if (evict) invalidateSessionDetailCache(streamId);
    const next = normalizePentacleEventBuckets({
      ...initialPentacleStreamState,
      sessions: [current],
      events: nextEvents,
    });
    let fullProjectionCalls = 0;
    const bucket = next.eventBucketsByStream?.[streamId];
    assert.ok(bucket, name);
    const observedEvents = new Proxy(bucket.events, {
      get(target, property, receiver) {
        if (property === 'filter') fullProjectionCalls += 1;
        return Reflect.get(target, property, receiver);
      },
    });
    const observed = {
      ...next,
      eventBucketsByStream: {
        ...next.eventBucketsByStream,
        [streamId]: { ...bucket, events: observedEvents },
      },
    };
    const actual = selector(observed)[0]?.previewText;
    const callsAfterSelector = fullProjectionCalls;
    const expected = latestDisplayedSessionPreview(observed, current, 'fallback', observedEvents);
    assert.equal(actual, expected, name);
    assert.equal(callsAfterSelector, expectFast ? 0 : 1, name);
  };

  check('strict copied-array append', [assist1, assist2], [assist1, assist2, assist3], true);
  check('changed retained reference', [assist1, assist2], [{ ...assist1 }, assist2, assist3], false);
  check('retention/cap shift', [assist1, assist2], [assist2, assist3], false);
  check('reconnect reconstruction', [assist1, assist2], [{ ...assist1 }, { ...assist2 }, assist3], false);
  check('replay sequence', [assist1, assist2], [assist1, assist2, { ...assist3, daemon_seq: 2 }], false);
  check('progressive ambiguity', [assist1, assist2], [assist1, assist2, make(3, 'third assistant progressively extended beyond the conservative preview bound')], false);
  check('provenance ambiguity', [assist1, assist2], [assist1, assist2, make(3, 'third assistant', { raw: { source: 'claude-jsonl' } })], false);
  check('older terminal and newer turn', [assist1, make(2, 'user turn', { kind: 'USER' })], [assist1, make(2, 'user turn', { kind: 'USER' }), assist3], false);
  check('cache eviction', [assist1, assist2], [assist1, assist2, assist3], false, true);
});

test('cached list fallback separates options and invalidates metadata and same-count content transitions', () => {
  const streamId = 'host_c:codex:preview-cache-invalidation';
  const timestamp = new Date(Date.UTC(2026, 6, 12, 13)).toISOString();
  const nextTimestamp = new Date(Date.UTC(2026, 6, 12, 13, 0, 1)).toISOString();
  const userEvent: PentacleEvent = {
    daemon_seq: 1,
    host: 'host_c',
    provider: 'codex',
    session_id: 'preview-cache-invalidation',
    session_name: 'preview-cache-invalidation',
    stream_id: streamId,
    timestamp,
    kind: 'USER',
    text: 'held user preview',
  };
  const systemEvent: PentacleEvent = {
    ...userEvent,
    daemon_seq: 2,
    timestamp: nextTimestamp,
    kind: 'SYSTEM',
    text: 'system-only row',
  };
  const currentSession = session(0, {
    stream_id: streamId,
    session_name: 'preview-cache-invalidation',
    last_event_at: systemEvent.timestamp,
    last_text: systemEvent.text,
    last_kind: systemEvent.kind,
  });
  const draftEvent: PentacleEvent = {
    ...userEvent,
    daemon_seq: Number.NaN,
    text: 'local draft must not become a list preview',
    pending: true,
    raw: { working: true, working_label: 'Drafting' },
  };
  const base = normalizePentacleEventBuckets({
    ...initialPentacleStreamState,
    sessions: [currentSession],
    events: [userEvent, systemEvent],
    drafts: { [streamId]: draftEvent },
  });

  const otherOptions = selectSessionDetail(base, streamId, {
    includeSystem: true,
    includeDraft: true,
    visibleCount: 1,
  });
  assert.equal(otherOptions?.draftText, draftEvent.text);
  assert.equal(otherOptions?.transcriptItems.at(-1)?.text, systemEvent.text);

  const selector = createChatListSelector();
  const coldFallback = selector(base)[0];
  const cleanDetail = selectSessionDetail(base, streamId, {
    includeTools: false,
    includeSystem: false,
    includeDraft: false,
    visibleCount: 'all',
  });
  assert.equal(coldFallback?.previewText, userEvent.text);
  assert.notEqual(cleanDetail, otherOptions, 'list fallback options must use a separate cache entry');
  assert.equal(cleanDetail?.draftText, '');
  assert.deepEqual(cleanDetail?.transcriptItems.map((item) => item.text), [userEvent.text]);

  const metadataOnly = {
    ...base,
    sessions: [{
      ...currentSession,
      title: 'Renamed without content',
      working: true,
      last_event_at: new Date(Date.UTC(2026, 6, 12, 13, 0, 2)).toISOString(),
      last_text: '[tell:notification-answer-summary-only] summary without retained content',
      last_kind: 'USER',
    }],
    drafts: { [streamId]: { ...draftEvent, text: 'updated draft without content' } },
  };
  const metadataRow = selector(metadataOnly)[0];
  const metadataDetail = selectSessionDetail(metadataOnly, streamId, {
    includeTools: false,
    includeSystem: false,
    includeDraft: false,
    visibleCount: 'all',
  });
  assert.equal(metadataRow?.previewText, userEvent.text);
  assert.equal(metadataRow?.title, 'Renamed without content');
  assert.equal(metadataRow?.status, 'working');
  assert.notEqual(metadataDetail, cleanDetail, 'session/working/draft signature changes must invalidate metadata');
  assert.equal(metadataDetail?.draftText, '');

  const failedReceiptEvent: PentacleEvent = {
    ...userEvent,
    client_origin: true,
    optimistic_id: 'preview-cache-receipt',
    correlatedDaemonSeq: 1,
    receiptDirectMatch: true,
    raw: { receipt_state: 'accepted', receipt_delivery: 'proof_unavailable' },
  };
  const landedReceiptEvent: PentacleEvent = {
    ...failedReceiptEvent,
    raw: { receipt_state: 'landed', receipt_delivery: 'accepted' },
  };
  const receiptSession = { ...currentSession, last_event_at: timestamp, last_text: userEvent.text, last_kind: 'USER' };
  const receiptBefore = normalizePentacleEventBuckets({
    ...initialPentacleStreamState,
    sessions: [receiptSession],
    events: [failedReceiptEvent],
  });
  const receiptSelector = createChatListSelector();
  assert.equal(receiptSelector(receiptBefore)[0]?.previewText, userEvent.text);
  const failedReceiptDetail = selectSessionDetail(receiptBefore, streamId, { includeDraft: false, visibleCount: 'all' });
  assert.equal(failedReceiptDetail?.transcriptItems[0]?.receiptCaption, 'failed');
  const receiptAfter = normalizePentacleEventBuckets({
    ...initialPentacleStreamState,
    sessions: [receiptSession],
    events: [landedReceiptEvent],
  });
  assert.equal(receiptAfter.events.length, receiptBefore.events.length, 'receipt reconciliation must not add content');
  assert.equal(receiptSelector(receiptAfter)[0]?.previewText, userEvent.text);
  const landedReceiptDetail = selectSessionDetail(receiptAfter, streamId, { includeDraft: false, visibleCount: 'all' });
  assert.notEqual(landedReceiptDetail, failedReceiptDetail, 'same-count content change must invalidate the cache');
  assert.equal(landedReceiptDetail?.transcriptItems[0]?.receiptCaption, 'sent');

  const answerPayload = JSON.stringify({
    type: 'notification.answer',
    answer: { notification_id: 'preview-cache-answer', action_kind: 'select', label: 'host_c', selections: ['host_c'] },
  });
  const notificationEvent: PentacleEvent = {
    ...userEvent,
    text: `[from daemon:notifications]\n[tell:notification-answer-preview-cache-answer]${answerPayload}`,
  };
  const notificationState = normalizePentacleEventBuckets({
    ...initialPentacleStreamState,
    sessions: [{ ...receiptSession, last_text: notificationEvent.text }],
    events: [notificationEvent],
  });
  assert.equal(notificationState.events.length, receiptBefore.events.length, 'notification reconciliation must not add content');
  assert.equal(receiptSelector(notificationState)[0]?.previewText, 'Operator answered: host_c');
});

test('fallback preserves an explicit stream event projection that differs from state', () => {
  const current = session(0, { last_kind: 'USER', last_text: 'override row' });
  const held = event(0, 1);
  const override = { ...held, daemon_seq: 2, kind: 'USER', text: 'override row' } satisfies PentacleEvent;
  const state = { ...initialPentacleStreamState, sessions: [current], events: [held] };

  assert.equal(latestDisplayedSessionPreview(state, current, 'fallback', [override]), 'override row');
});
