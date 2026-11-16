import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyPentacleMachineStats,
  applyPentacleMachineStatsInventory,
  applyPentacleCodexUsage,
  applyPentacleHostsStats,
  initialPentacleStreamState,
  type PentacleMachineStats,
  type PentacleStreamState,
} from '../src/index.ts';

// Desktop consumer contract (renderer/src/chat_core_entry.ts +
// chat_store_controller.ts): the desktop still imports and calls the pre-rename
// machine-stats API. These are thin compat wrappers over the current hosts.stats
// API so the desktop renderer builds and runs at this core pin; migrating the
// desktop consumer to applyPentacleHostsStats/applyPentacleLimits is tracked in
// a backlog spec. Removing any of these three re-breaks the desktop build.

function hostStat(host: string): PentacleMachineStats {
  return {
    host,
    cpu_load_1m: 1,
    memory_used_bytes: 4_000_000_000,
    memory_total_bytes: 16_000_000_000,
    disk_used_bytes: 100_000_000_000,
    disk_total_bytes: 500_000_000_000,
    uptime_seconds: 100,
    sampled_at: '2025-01-15T12:00:01.123456Z',
  };
}

function state(overrides: Partial<PentacleStreamState> = {}): PentacleStreamState {
  return { ...initialPentacleStreamState, ...overrides };
}

test('the three pre-rename exports exist as functions (desktop barrel re-export)', () => {
  assert.equal(typeof applyPentacleMachineStats, 'function');
  assert.equal(typeof applyPentacleMachineStatsInventory, 'function');
  assert.equal(typeof applyPentacleCodexUsage, 'function');
});

test('applyPentacleMachineStatsInventory replaces the fleet map (== hosts.stats)', () => {
  const inventory = { host_b: hostStat('host_b'), host_c: hostStat('host_c') };
  const next = applyPentacleMachineStatsInventory(state({
    machineStats: { stale: hostStat('stale') },
  }), inventory);
  assert.deepEqual(Object.keys(next.machineStats).sort(), ['host_b', 'host_c']);
  // Parity with the new API.
  assert.deepEqual(next.machineStats, applyPentacleHostsStats(state(), inventory).machineStats);
});

test('applyPentacleMachineStats merges a single host, keeping existing hosts', () => {
  const next = applyPentacleMachineStats(state({ machineStats: { host_b: hostStat('host_b') } }), hostStat('host_c'));
  assert.deepEqual(Object.keys(next.machineStats).sort(), ['host_b', 'host_c']);
  assert.equal(next.machineStats.host_c.host, 'host_c');
});

test('applyPentacleMachineStats drops an invalid single sample (no throw)', () => {
  const bad = { ...hostStat('host_b'), memory_total_bytes: 0 } as unknown as PentacleMachineStats;
  const next = applyPentacleMachineStats(state(), bad);
  assert.equal(next.machineStats.host_b, undefined);
});

test('applyPentacleCodexUsage is a no-op pass-through (codex usage is surfaced via limits now)', () => {
  const prior = state({ machineStats: { host_b: hostStat('host_b') } });
  const next = applyPentacleCodexUsage(prior, { pct: 42 } as unknown as undefined);
  assert.equal(next, prior);
});
