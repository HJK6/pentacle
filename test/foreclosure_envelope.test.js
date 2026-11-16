const test = require('node:test');
const assert = require('node:assert/strict');

const NOW = Date.parse('2026-01-10T10:00:00Z');
const FRESH = '2026-01-10T09:59:55Z';
const STALE = '2026-01-10T09:55:00Z';

function resolveSnapshotEnvelope(envelope, requestedBatch, connected, now) {
  if (!envelope || typeof envelope !== 'object' || !envelope.data || typeof envelope.data !== 'object') {
    return { error: 'no data yet from hub', _transport_stale: !connected };
  }
  const data = envelope.data;
  const snapshots = data.snapshots;
  const hasSnapshots = snapshots && typeof snapshots === 'object' && !Array.isArray(snapshots);
  const selected = hasSnapshots
    ? (snapshots[requestedBatch] || snapshots[data.default_batch] || null)
    : null;
  const out = selected ? { ...selected } : { ...data };
  if (hasSnapshots && requestedBatch && !snapshots[requestedBatch]) out._missing_batch = requestedBatch;
  const receivedAt = Date.parse(String(envelope.updated_at || envelope.server_received_at || ''));
  const ttl = Number(envelope.freshness_ttl_sec);
  const age = Number.isFinite(receivedAt) && Number.isFinite(now)
    ? Math.max(0, Math.floor((now - receivedAt) / 1000))
    : null;
  out._age_sec = age;
  out._data_stale = age === null || age > (Number.isFinite(ttl) && ttl > 0 ? ttl : 120);
  out._transport_stale = !connected;
  return out;
}

function makeEnvelope({ snapshots, defaultBatch, allBatches, allMeta, freshness = FRESH }) {
  return {
    server_received_at: freshness,
    updated_at: freshness,
    freshness_ttl_sec: 120,
    data: {
      snapshots,
      default_batch: defaultBatch,
      all_batches: allBatches,
      all_batches_meta: allMeta,
      ...((defaultBatch && snapshots[defaultBatch]) || {}),
    },
  };
}

test('missing envelope returns deterministic reader state', () => {
  assert.deepEqual(resolveSnapshotEnvelope(null, undefined, true, NOW), {
    error: 'no data yet from hub', _transport_stale: false,
  });
  assert.equal(resolveSnapshotEnvelope(null, undefined, false, NOW)._transport_stale, true);
});

test('multi-snapshot envelope selects the default and requested sample', () => {
  const env = makeEnvelope({
    snapshots: {
      A: { batch: 'A', collected: 10 },
      B: { batch: 'B', collected: 5 },
    },
    defaultBatch: 'A',
    allBatches: ['A', 'B'],
    allMeta: [{ batch: 'A', source_type: 'sample' }, { batch: 'B', source_type: 'sample' }],
  });
  const defaultOut = resolveSnapshotEnvelope(env, undefined, true, NOW);
  assert.equal(defaultOut.batch, 'A');
  assert.equal(defaultOut.collected, 10);
  assert.equal(defaultOut._missing_batch, undefined);
  const requestedOut = resolveSnapshotEnvelope(env, 'B', true, NOW);
  assert.equal(requestedOut.batch, 'B');
  assert.equal(requestedOut.collected, 5);
});

test('missing requested sample falls back and records the request', () => {
  const env = makeEnvelope({
    snapshots: { A: { batch: 'A', collected: 10 } },
    defaultBatch: 'A',
    allBatches: ['A'],
    allMeta: [{ batch: 'A', source_type: 'sample' }],
  });
  const out = resolveSnapshotEnvelope(env, 'missing', true, NOW);
  assert.equal(out.batch, 'A');
  assert.equal(out._missing_batch, 'missing');
});

test('legacy envelope passes through without a missing-sample marker', () => {
  const env = {
    updated_at: FRESH,
    freshness_ttl_sec: 120,
    data: { batch: 'legacy', collected: 4, all_batches: ['legacy'] },
  };
  const out = resolveSnapshotEnvelope(env, 'ignored', true, NOW);
  assert.equal(out.batch, 'legacy');
  assert.equal(out.collected, 4);
  assert.equal(out._missing_batch, undefined);
});

test('staleness honors the envelope TTL and connection state', () => {
  const env = makeEnvelope({
    snapshots: { A: { batch: 'A', collected: 1 } },
    defaultBatch: 'A',
    allBatches: ['A'],
    allMeta: [],
    freshness: STALE,
  });
  const out = resolveSnapshotEnvelope(env, undefined, false, NOW);
  assert.equal(out._data_stale, true);
  assert.equal(out._transport_stale, true);
  assert.ok(out._age_sec > 120);
  const short = { ...makeEnvelope({ snapshots: { A: { batch: 'A' } }, defaultBatch: 'A', allBatches: ['A'], allMeta: [] }), freshness_ttl_sec: 1 };
  assert.equal(resolveSnapshotEnvelope(short, undefined, true, NOW)._data_stale, true);
});
