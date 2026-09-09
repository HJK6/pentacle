import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyPentacleHostsStats,
  initialPentacleStreamState,
  type PentacleMachineStats,
  type PentacleStreamState,
} from '../src/index.ts';

// A structurally valid daemon sample; `sampled_at` precision varies per case.
function sample(host: string, sampledAt: string): Record<string, unknown> {
  return {
    host,
    cpu_load_1m: 1.5,
    memory_used_bytes: 8_000_000_000,
    memory_total_bytes: 16_000_000_000,
    disk_used_bytes: 200_000_000_000,
    disk_total_bytes: 500_000_000_000,
    uptime_seconds: 123_456,
    sampled_at: sampledAt,
  };
}

function state(overrides: Partial<PentacleStreamState> = {}): PentacleStreamState {
  return { ...initialPentacleStreamState, ...overrides };
}

// The daemon (Python datetime.isoformat + 'Z') emits SIX fractional digits.
// Synthetic protocol examples for 2025-01-15 from ws://example.invalid:7791:
//   host_b      2025-01-15T12:00:01.123456Z
//   host_a 2025-01-15T12:00:02.234567Z
//   host_c    2025-01-15T12:00:03.345678Z
// The client decoder must accept the daemon's ACTUAL precision.
test('microsecond (6-digit) sampled_at with protocol precision is retained', () => {
  const next = applyPentacleHostsStats(state(), {
    host_b: sample('host_b', '2025-01-15T12:00:01.123456Z'),
    host_a: sample('host_a', '2025-01-15T12:00:02.234567Z'),
    host_c: sample('host_c', '2025-01-15T12:00:03.345678Z'),
  });
  assert.deepEqual(Object.keys(next.machineStats).sort(), ['host_a', 'host_b', 'host_c']);
  assert.equal(next.machineStats.host_b.sampled_at, '2025-01-15T12:00:01.123456Z');
});

test('millisecond (3-digit) and no-fraction sampled_at still accepted', () => {
  const next = applyPentacleHostsStats(state(), {
    host_b: sample('host_b', '2025-01-15T13:08:41.475Z'),
    host_c: sample('host_c', '2025-01-15T13:08:54Z'),
  });
  assert.deepEqual(Object.keys(next.machineStats).sort(), ['host_b', 'host_c']);
});

test('nanosecond (9-digit) sampled_at is accepted (future-proof precision)', () => {
  const next = applyPentacleHostsStats(state(), {
    host_b: sample('host_b', '2025-01-15T12:00:01.123456123Z'),
  });
  assert.deepEqual(Object.keys(next.machineStats), ['host_b']);
});

test('invalid samples are still rejected (rejection preserved)', () => {
  const cases: Array<[string, Record<string, unknown>]> = [
    ['unparseable sampled_at', { ...sample('host_b', 'not-a-timestamp') }],
    ['sampled_at missing Z', { ...sample('host_b', '2025-01-15T12:00:01.123456') }],
    ['sampled_at with trailing dot', { ...sample('host_b', '2025-01-15T13:08:41.Z') }],
    ['host key mismatch', { ...sample('host_c', '2025-01-15T12:00:01.123456Z') }],
    ['negative numeric', { ...sample('host_b', '2025-01-15T12:00:01.123456Z'), cpu_load_1m: -1 }],
    ['non-finite numeric', { ...sample('host_b', '2025-01-15T12:00:01.123456Z'), uptime_seconds: Number.POSITIVE_INFINITY }],
    ['used exceeds total', { ...sample('host_b', '2025-01-15T12:00:01.123456Z'), memory_used_bytes: 99_000_000_000 }],
    ['non-positive total', { ...sample('host_b', '2025-01-15T12:00:01.123456Z'), disk_total_bytes: 0 }],
  ];
  for (const [label, raw] of cases) {
    const next = applyPentacleHostsStats(state(), { host_b: raw });
    assert.equal(next.machineStats.host_b, undefined, `expected rejection: ${label}`);
  }
});

test('hosts.stats is a complete replacement of the fleet map', () => {
  const prior = state({
    machineStats: { stale: { host: 'stale' } as PentacleMachineStats },
  });
  const next = applyPentacleHostsStats(prior, {
    host_b: sample('host_b', '2025-01-15T12:00:01.123456Z'),
  });
  assert.deepEqual(Object.keys(next.machineStats), ['host_b']);
});
