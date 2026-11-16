// Unit tests for the desktop at-most-once send-recovery helpers
// (public_contract, scope 5).
// Bundled to CJS by scripts/run-tests.js.

import test from 'node:test';
import assert from 'node:assert/strict';

import type { OptimisticSendState, PentacleStreamState } from 'chat-core';
import {
  RECONNECT_REPLAY_MAX_AGE_MS,
  eligibleReconnectReplayOptimisticIds,
  markDaemonRestartSurvivorsIndeterminate,
  rotateOptimisticSendRequestId,
} from '../renderer/src/chat_reconnect_replay';

function send(over: Partial<OptimisticSendState> & { optimistic_id: string }): OptimisticSendState {
  return {
    request_id: `req-${over.optimistic_id}`,
    stream_id: 'hosta:provider_c-hosta-1',
    text: 'hi',
    status: 'dispatched',
    created_at: 1_000_000,
    reconnect_count: 0,
    socket_generation: 7,
    ...over,
  } as OptimisticSendState;
}

function stateOf(sends: OptimisticSendState[]): PentacleStreamState {
  const optimisticSends: Record<string, OptimisticSendState> = {};
  const optimisticByRequestId: Record<string, string> = {};
  for (const s of sends) {
    optimisticSends[s.optimistic_id] = s;
    optimisticByRequestId[s.request_id] = s.optimistic_id;
  }
  return { optimisticSends, optimisticByRequestId } as unknown as PentacleStreamState;
}

const NOW = 2_000_000;

test('eligible replay: queued/dispatched/indeterminate of this generation, within window, oldest-first', () => {
  const st = stateOf([
    send({ optimistic_id: 'o-dispatched', status: 'dispatched', queued_at: 300, socket_generation: 7 }),
    send({ optimistic_id: 'o-queued', status: 'queued', queued_at: 100, socket_generation: 7 }),
    send({ optimistic_id: 'o-acked', status: 'acked', queued_at: 50, socket_generation: 7 }),
    send({ optimistic_id: 'o-indeterminate', status: 'indeterminate', queued_at: 60, socket_generation: 7 }),
    send({ optimistic_id: 'o-failed', status: 'failed', queued_at: 70, socket_generation: 7 }),
    send({ optimistic_id: 'o-othergen', status: 'dispatched', queued_at: 10, socket_generation: 6 }),
  ]);
  const eligible = eligibleReconnectReplayOptimisticIds(st, 7, NOW);
  // Oldest-first by queued_at: indeterminate (60) < queued (100) < dispatched (300).
  // A current-generation indeterminate (an in-flight dispatch cut by a transient
  // drop against a still-live daemon) IS re-driven — request_id reuse keeps it
  // at-most-once. acked/failed/other-generation stay excluded.
  assert.deepEqual(eligible, ['o-indeterminate', 'o-queued', 'o-dispatched']);
});

test('eligible replay: survivors older than the 30-min window are excluded', () => {
  const fresh = send({ optimistic_id: 'o-fresh', status: 'queued', created_at: NOW - 1000, socket_generation: 7 });
  const stale = send({ optimistic_id: 'o-stale', status: 'queued', created_at: NOW - RECONNECT_REPLAY_MAX_AGE_MS - 1, socket_generation: 7 });
  const eligible = eligibleReconnectReplayOptimisticIds(stateOf([fresh, stale]), 7, NOW);
  assert.deepEqual(eligible, ['o-fresh']);
});

test('eligible replay: falls back to created_at when queued_at is absent for ordering', () => {
  // Both in-window; neither has queued_at → order by created_at (older first).
  const a = send({ optimistic_id: 'o-a', status: 'queued', created_at: NOW - 200, socket_generation: 7 });
  const b = send({ optimistic_id: 'o-b', status: 'dispatched', created_at: NOW - 500, socket_generation: 7 });
  const eligible = eligibleReconnectReplayOptimisticIds(stateOf([a, b]), 7, NOW);
  assert.deepEqual(eligible, ['o-b', 'o-a']); // b (older created_at) first
});

test('daemon restart: this-gen queued/dispatched survivors become indeterminate; others untouched', () => {
  const st = stateOf([
    send({ optimistic_id: 'o-q', status: 'queued', socket_generation: 7 }),
    send({ optimistic_id: 'o-d', status: 'dispatched', socket_generation: 7 }),
    send({ optimistic_id: 'o-acked', status: 'acked', socket_generation: 7 }),
    send({ optimistic_id: 'o-oldgen', status: 'dispatched', socket_generation: 6 }),
  ]);
  const next = markDaemonRestartSurvivorsIndeterminate(st, 7);
  assert.equal(next.optimisticSends!['o-q'].status, 'indeterminate');
  assert.equal(next.optimisticSends!['o-d'].status, 'indeterminate');
  assert.equal(next.optimisticSends!['o-acked'].status, 'acked'); // untouched
  assert.equal(next.optimisticSends!['o-oldgen'].status, 'dispatched'); // untouched
});

test('daemon restart: returns the SAME state reference when nothing changed', () => {
  const st = stateOf([send({ optimistic_id: 'o-acked', status: 'acked', socket_generation: 7 })]);
  assert.equal(markDaemonRestartSurvivorsIndeterminate(st, 7), st);
});

test('explicit retry: rotates request_id and re-points the by-request index, dropping the old key', () => {
  const st = stateOf([send({ optimistic_id: 'o1', request_id: 'req-old', status: 'failed', socket_generation: 7 })]);
  const next = rotateOptimisticSendRequestId(st, 'o1', 'req-new');
  assert.equal(next.optimisticSends!['o1'].request_id, 'req-new');
  assert.equal(next.optimisticByRequestId!['req-new'], 'o1');
  assert.equal(next.optimisticByRequestId!['req-old'], undefined); // abandoned key dropped
  // original state object not mutated
  assert.equal(st.optimisticSends!['o1'].request_id, 'req-old');
});

test('explicit retry: no-op when the row is absent or the request_id is unchanged', () => {
  const st = stateOf([send({ optimistic_id: 'o1', request_id: 'req-same', status: 'failed' })]);
  assert.equal(rotateOptimisticSendRequestId(st, 'missing', 'req-x'), st);
  assert.equal(rotateOptimisticSendRequestId(st, 'o1', 'req-same'), st);
});
