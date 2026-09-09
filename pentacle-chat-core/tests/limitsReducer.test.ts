import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyPentacleLimits,
  applyPentacleSnapshotMessage,
  initialPentacleStreamState,
  type PentacleLimit,
  type PentacleStreamState,
} from '../src/index.ts';

const LIVE_LIMITS: PentacleLimit[] = [
  { id: 'claude', label: 'Claude', pct: 31, resets_at_iso: null, resets_text: 'Mar 16 at 7pm (America/Chicago)' },
  { id: 'fable', label: 'Fable', pct: 37, resets_at_iso: null, resets_text: 'Mar 16 at 7pm (America/Chicago)' },
  { id: 'codex', label: 'Codex', pct: 42, resets_at_iso: '2026-08-23T00:00:00Z', resets_text: 'Aug 22 at 7pm (America/Chicago)' },
];

const NULL_LIMITS: PentacleLimit[] = [
  { id: 'claude', label: 'Claude', pct: null, resets_at_iso: null, resets_text: null },
  { id: 'fable', label: 'Fable', pct: null, resets_at_iso: null, resets_text: null },
  { id: 'codex', label: 'Codex', pct: null, resets_at_iso: null, resets_text: null },
];

function state(overrides: Partial<PentacleStreamState> = {}): PentacleStreamState {
  return { ...initialPentacleStreamState, ...overrides };
}

test('initial state exposes the fixed ordered all-null limits list', () => {
  assert.deepEqual(initialPentacleStreamState.limits, NULL_LIMITS);
});

test('a valid snapshot atomically replaces the complete limits list', () => {
  const next = applyPentacleSnapshotMessage(state(), { limits: LIVE_LIMITS });
  assert.deepEqual(next.limits, LIVE_LIMITS);
  assert.notEqual(next.limits, LIVE_LIMITS);
});

test('a snapshot that omits limits preserves the prior cache through reconnect', () => {
  const prior = state({ limits: LIVE_LIMITS });
  const next = applyPentacleSnapshotMessage(prior, { sessions: [] });
  assert.equal(next.limits, prior.limits);
});

test('an explicit complete all-null snapshot replaces populated limits', () => {
  const prior = state({ limits: LIVE_LIMITS });
  const next = applyPentacleSnapshotMessage(prior, { limits: NULL_LIMITS });
  assert.deepEqual(next.limits, NULL_LIMITS);
  assert.notEqual(next.limits, prior.limits);
});

test('partial, reordered, and malformed lists preserve last known good', () => {
  const malformed: unknown[] = [
    LIVE_LIMITS.slice(0, 2),
    [LIVE_LIMITS[1], LIVE_LIMITS[0], LIVE_LIMITS[2]],
    LIVE_LIMITS.map((entry, index) => index === 2 ? { ...entry, pct: 101 } : entry),
  ];
  for (const limits of malformed) {
    const prior = state({ limits: LIVE_LIMITS });
    const next = applyPentacleLimits(prior, limits);
    assert.equal(next, prior);
    assert.equal(next.limits, prior.limits);
  }
});

test('extra daemon fields are tolerated while rendered fields are preserved', () => {
  const withExtras = LIVE_LIMITS.map((entry) => ({
    ...entry,
    upstream_reported_at: '2026-08-23T00:00:00Z',
    probed_at: '2026-08-23T00:00:01Z',
  }));
  const prior = state({ limits: LIVE_LIMITS });
  const next = applyPentacleLimits(prior, withExtras);

  assert.deepEqual(next.limits, LIVE_LIMITS);
  assert.notEqual(next, prior);
});

test('incremental updates replace the whole list, including reset-only changes', () => {
  const prior = state({ limits: LIVE_LIMITS });
  const changed = LIVE_LIMITS.map((entry) => (
    entry.id === 'codex' ? { ...entry, resets_text: 'Aug 29 at 7pm (America/Chicago)' } : entry
  ));
  const next = applyPentacleLimits(prior, changed);
  assert.deepEqual(next.limits, changed);
  assert.notEqual(next.limits, prior.limits);
});
