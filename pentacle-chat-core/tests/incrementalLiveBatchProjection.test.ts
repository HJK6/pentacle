import assert from 'node:assert/strict';
import test from 'node:test';

import { applyFetchedStreamEvents, initialPentacleStreamState } from '../src/services/pentacleStreamReducer.ts';
import {
  replacePentacleEventProjection,
  selectPentacleDerivedEventIndex,
} from '../src/services/pentacleEventBuckets.ts';
import { dedupeRecentEventsByStream } from '../src/services/pentacleEventUtils.ts';
import type { PentacleEvent, PentacleStreamState } from '../src/types/pentacle.ts';

function evt(stream: string, seq: number): PentacleEvent {
  return {
    stream_id: stream,
    daemon_seq: seq,
    correlatedDaemonSeq: seq,
    timestamp: new Date(1788600000000 + seq * 1000).toISOString(),
    kind: 'chat',
    text: `s${stream}-m${seq}`,
  } as PentacleEvent;
}

function live(state: PentacleStreamState, events: PentacleEvent[]): PentacleStreamState {
  return applyFetchedStreamEvents(state, events, { mode: 'live' });
}

// Spec tap_shell_layout_regression_build_1155 S3: under the 19.2k freeze flood the
// live events arrive as BATCHES (background batch limit 1200). The batch path must
// apply incrementally (per-stream bucket append + one flat copy), NOT a
// snapshot-replace that re-groups every stream and re-runs compatibilityProjection
// per batch — the O(total) allocation that made the flood's per-batch wall climb
// 2s→8s and starved the trailing burst. (RED before the incremental batch path:
// snapshot-replace rebuilt every stream's bucket, so an untouched stream's bucket
// ref changed on every batch.)
test('live BATCH delivery preserves untouched streams\' bucket refs (incremental, not snapshot-replace)', () => {
  const streams = ['h:a', 'h:b', 'h:c'];
  let seq = 0;
  let state = live(initialPentacleStreamState, streams.flatMap((s) => [evt(s, seq++), evt(s, seq++)]));
  const bBefore = state.eventBucketsByStream?.['h:b'];
  const cBefore = state.eventBucketsByStream?.['h:c'];
  assert.ok(bBefore && cBefore);

  const eventsBefore = state.events;
  // a live batch touching only stream a (multiple events — the batch case)
  state = live(state, [evt('h:a', seq++), evt('h:a', seq++), evt('h:a', seq++)]);

  assert.strictEqual(state.eventBucketsByStream?.['h:b'], bBefore, 'stream b bucket unchanged');
  assert.strictEqual(state.eventBucketsByStream?.['h:c'], cBefore, 'stream c bucket unchanged');
  assert.strictEqual(state.events.length, eventsBefore.length + 3, 'flat grew by the batch size');
  for (let i = 0; i < eventsBefore.length; i += 1) {
    assert.strictEqual(state.events[i], eventsBefore[i], `flat element ${i} ref preserved`);
  }
});

// A multi-stream burst (the exact freeze shape: one batch hitting many streams).
test('live BATCH across many streams keeps every untouched-in-this-batch stream ref stable', () => {
  const streams = Array.from({ length: 8 }, (_, i) => `h:s${i}`);
  let seq = 0;
  let state = live(initialPentacleStreamState, streams.map((s) => evt(s, seq++)));
  // burst touches only the second half of the streams
  const touched = streams.slice(4);
  const untouched = streams.slice(0, 4);
  const beforeRefs = Object.fromEntries(untouched.map((s) => [s, state.eventBucketsByStream?.[s]]));
  state = live(state, touched.map((s) => evt(s, seq++)));
  for (const s of untouched) {
    assert.strictEqual(state.eventBucketsByStream?.[s], beforeRefs[s], `${s} bucket ref stable`);
  }
});

test('incremental live BATCH is byte-equivalent to the full dedupe+snapshot projection', () => {
  const streams = ['h:a', 'h:b', 'h:c'];
  let seq = 0;
  const script: PentacleEvent[] = [];
  for (let i = 0; i < 60; i += 1) script.push(evt(streams[i % streams.length], seq++));

  // path under test: apply in live batches of 7 (crosses the batch boundary)
  let incremental = initialPentacleStreamState;
  for (let i = 0; i < script.length; i += 7) incremental = live(incremental, script.slice(i, i + 7));

  // reference: pre-fix behaviour — global per-stream dedupe + snapshot-replace
  const referenceEvents = dedupeRecentEventsByStream([...script], 1200);
  const reference = replacePentacleEventProjection(initialPentacleStreamState, referenceEvents);

  const idOf = (e: PentacleEvent) => `${e.stream_id}#${e.daemon_seq}`;
  assert.deepEqual(incremental.events.map(idOf), reference.events.map(idOf), 'flat order identical');
  const incIdx = selectPentacleDerivedEventIndex(incremental).byStream;
  const refIdx = selectPentacleDerivedEventIndex(reference).byStream;
  assert.deepEqual([...incIdx.keys()].sort(), [...refIdx.keys()].sort(), 'same streams');
  for (const streamId of incIdx.keys()) {
    assert.deepEqual(
      (incIdx.get(streamId) ?? []).map(idOf),
      (refIdx.get(streamId) ?? []).map(idOf),
      `stream ${streamId} events identical`,
    );
  }
});

test('a duplicate daemon_seq in a live batch does not double-append (falls back correctly)', () => {
  let state = live(initialPentacleStreamState, [evt('h:a', 1), evt('h:a', 2)]);
  const before = state.events.length;
  state = live(state, [evt('h:a', 2), evt('h:a', 3)]); // seq 2 is a redelivery
  assert.strictEqual(state.events.length, before + 1, 'only the genuinely-new seq 3 appended');
  assert.deepEqual(
    (selectPentacleDerivedEventIndex(state).byStream.get('h:a') ?? []).map((e) => e.daemon_seq),
    [1, 2, 3],
    'no duplicate seq 2',
  );
});

test('live BATCH dedupe does not inspect flat-list events from untouched streams', () => {
  let armed = false;
  const unrelated = new Proxy(evt('h:b', 1), {
    get(target, property, receiver) {
      if (armed) throw new Error('unrelated flat-list event was inspected');
      return Reflect.get(target, property, receiver);
    },
  });
  const state = live(initialPentacleStreamState, [evt('h:a', 1), unrelated]);
  armed = true;

  assert.doesNotThrow(() => live(state, [evt('h:a', 2)]));
});
