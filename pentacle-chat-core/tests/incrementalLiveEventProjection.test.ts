import assert from 'node:assert/strict';
import test from 'node:test';

import { applyPentacleEvent, initialPentacleStreamState } from '../src/services/pentacleStreamReducer.ts';
import {
  appendLiveEventProjection,
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

function deliver(seedState: PentacleStreamState, events: PentacleEvent[]): PentacleStreamState {
  return events.reduce((state, event) => applyPentacleEvent(state, event), seedState);
}

// Spec tap_shell_layout_regression_build_1155: the live-delivery hot path must be
// incremental (per-stream), not an O(total) whole-store rebuild, without changing
// observable behaviour.
test('incremental live delivery preserves other streams\' bucket refs (O(1-stream))', () => {
  const streams = ['h:a', 'h:b', 'h:c'];
  let seq = 0;
  // seed several events across all streams
  let state = deliver(initialPentacleStreamState, streams.flatMap((s) => [evt(s, seq++), evt(s, seq++)]));
  const bBefore = state.eventBucketsByStream?.['h:b'];
  const cBefore = state.eventBucketsByStream?.['h:c'];
  assert.ok(bBefore && cBefore);

  // deliver one more event to stream a only
  const eventsBefore = state.events;
  state = applyPentacleEvent(state, evt('h:a', seq++));

  // the untouched streams keep their exact bucket objects (incremental append)
  assert.strictEqual(state.eventBucketsByStream?.['h:b'], bBefore, 'stream b bucket unchanged');
  assert.strictEqual(state.eventBucketsByStream?.['h:c'], cBefore, 'stream c bucket unchanged');
  // flat projection grew by exactly one, preserving prior element refs
  assert.strictEqual(state.events.length, eventsBefore.length + 1);
  for (let i = 0; i < eventsBefore.length; i += 1) {
    assert.strictEqual(state.events[i], eventsBefore[i], `flat element ${i} ref preserved`);
  }
});

test('incremental append is byte-equivalent to the full dedupe+snapshot projection', () => {
  const streams = ['h:a', 'h:b'];
  let seq = 0;
  const script: PentacleEvent[] = [];
  for (let i = 0; i < 40; i += 1) script.push(evt(streams[i % streams.length], seq++));

  // path under test: incremental per-event delivery
  const incremental = deliver(initialPentacleStreamState, script);

  // reference path: the pre-fix behaviour — global per-stream dedupe + snapshot-replace
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

test('a duplicate daemon_seq redelivery does not double-append', () => {
  let state = deliver(initialPentacleStreamState, [evt('h:a', 1), evt('h:a', 2)]);
  const before = state.events.length;
  state = applyPentacleEvent(state, evt('h:a', 2)); // redelivery of seq 2
  assert.strictEqual(state.events.length, before, 'redelivery deduped, no growth');
});

test('appendLiveEventProjection falls back correctly on a fresh stream', () => {
  const seeded = replacePentacleEventProjection(initialPentacleStreamState, [evt('h:a', 1)]);
  const next = appendLiveEventProjection(seeded, evt('h:b', 1));
  assert.deepEqual(next.events.map((e) => e.stream_id).sort(), ['h:a', 'h:b']);
});
