/**
 * Foreclosure dashboard envelope helpers.
 *
 * Resolves the per-batch snapshot from a `bart.foreclosure` envelope so the
 * IPC handler in main.js stays a thin wrapper around `hubClient.get(...)` and
 * the resolution logic stays unit-testable.
 *
 * The producer publishes a multi-snapshot envelope:
 *   {
 *     snapshots: { '<batch>': { ...full pipeline_stats payload... }, ... },
 *     default_batch: '<batch>',
 *     all_batches: [...],
 *     all_batches_meta: [...],
 *     ...legacy default-batch fields embedded at the top level for back-compat
 *   }
 *
 * The legacy producer (pre-multi-snapshot rollout) publishes only the legacy
 * top-level fields. We must keep working through the rollout window.
 */

'use strict';

/**
 * Build the IPC response for `dashboard:pipeline-stats`.
 *
 * @param {object|null} env             — full hub envelope or null
 * @param {string|undefined} batch      — caller-requested batch (may be empty)
 * @param {boolean} hubConnected        — hubClient.connected
 * @param {number} nowMs                — Date.now() (injected for tests)
 * @returns {object}                    — payload returned to renderer via IPC
 */
function resolveForeclosureSnapshot(env, batch, hubConnected, nowMs) {
  const connected = !!hubConnected;
  if (!env) {
    return { error: 'no data yet from hub', _transport_stale: !connected };
  }
  const ageSec = (nowMs - new Date(env.server_received_at).getTime()) / 1000;
  const ttl = env.freshness_ttl_sec != null ? env.freshness_ttl_sec : 120;
  const base = {
    _updated_at: env.updated_at,
    _server_received_at: env.server_received_at,
    _age_sec: ageSec,
    _transport_stale: !connected,
    _data_stale: ageSec > ttl,
  };
  const data = env.data || {};
  const snapshots = data.snapshots;
  const defaultBatch = data.default_batch;
  const wantedBatch = batch || '';

  if (snapshots && typeof snapshots === 'object') {
    const want = wantedBatch || defaultBatch || '';
    const snap = snapshots[want] || (defaultBatch ? snapshots[defaultBatch] : null);
    const missing = wantedBatch && !snapshots[wantedBatch] ? wantedBatch : null;
    if (!snap) {
      // Snapshots dict present but neither requested nor default batch
      // resolved — surface the legacy top-level body so the renderer at
      // least gets stage boxes.
      return {
        ...data,
        all_batches: data.all_batches,
        all_batches_meta: data.all_batches_meta,
        default_batch: defaultBatch,
        _missing_batch: missing,
        ...base,
      };
    }
    return {
      ...snap,
      all_batches: data.all_batches,
      all_batches_meta: data.all_batches_meta,
      default_batch: defaultBatch,
      _missing_batch: missing,
      ...base,
    };
  }

  // Legacy single-snapshot envelope (pre-multi-snapshot producer rollout).
  return { ...data, ...base };
}

module.exports = { resolveForeclosureSnapshot };
