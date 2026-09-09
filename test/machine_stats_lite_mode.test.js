const test = require('node:test');
const assert = require('node:assert/strict');

function renderMachineStats(hosts, { localHost = 'hosta', now = Date.now() } = {}) {
  return Object.entries(hosts || {})
    .filter(([, stats]) => stats && typeof stats === 'object')
    .sort(([left], [right]) => (left === localHost ? -1 : right === localHost ? 1 : left.localeCompare(right)))
    .map(([host, stats]) => ({
      host,
      label: stats.label || host,
      state: Number.isFinite(Date.parse(stats.sampled_at)) && now - Date.parse(stats.sampled_at) <= 90000 ? 'Live' : 'Stale',
      load: stats.cpu_load_1m == null ? '--' : Number(stats.cpu_load_1m).toFixed(2),
      memory: stats.memory_used_bytes == null || stats.memory_total_bytes == null
        ? '--'
        : Math.round((stats.memory_used_bytes / stats.memory_total_bytes) * 100) + '%',
      storage: stats.disk_used_bytes == null || stats.disk_total_bytes == null
        ? '--'
        : Math.round((stats.disk_used_bytes / stats.disk_total_bytes) * 100) + '%',
    }));
}

function fresh(label, sampledAt) {
  return {
    label,
    cpu_load_1m: 0.42,
    memory_used_bytes: 4 * 1024 ** 3,
    memory_total_bytes: 8 * 1024 ** 3,
    disk_used_bytes: 64 * 1024 ** 3,
    disk_total_bytes: 256 * 1024 ** 3,
    sampled_at: new Date(sampledAt).toISOString(),
  };
}

test('renders every synthetic host with the local host first', () => {
  const now = Date.parse('2026-01-01T00:00:00Z');
  const cards = renderMachineStats({
    'hostb': fresh('Host B', now),
    'hosta': fresh('Host A', now),
    'hostc': fresh('Host C', now),
  }, { now });
  assert.deepEqual(cards.map((card) => card.host), ['hosta', 'hostb', 'hostc']);
  assert.deepEqual(cards.map((card) => card.label), ['Host A', 'Host B', 'Host C']);
  assert.equal(cards[0].state, 'Live');
  assert.equal(cards[0].memory, '50%');
  assert.equal(cards[0].storage, '25%');
});

test('missing samples are stale while recent samples are live', () => {
  const now = Date.parse('2026-01-01T00:00:00Z');
  const cards = renderMachineStats({
    'hosta': fresh('Host A', now - 91 * 1000),
    'hostb': { label: 'Host B' },
  }, { now });
  assert.equal(cards[0].state, 'Stale');
  assert.equal(cards[1].state, 'Stale');
});

test('a pushed host map replaces the projected set', () => {
  const now = Date.parse('2026-01-01T00:00:00Z');
  const initial = renderMachineStats({ 'hosta': fresh('Host A', now) }, { now });
  const pushed = renderMachineStats({ 'hostc': fresh('Host C', now - 91 * 1000) }, { now });
  assert.deepEqual(initial.map((card) => card.host), ['hosta']);
  assert.deepEqual(pushed.map((card) => card.host), ['hostc']);
  assert.equal(pushed[0].state, 'Stale');
});
