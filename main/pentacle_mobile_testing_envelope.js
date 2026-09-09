'use strict';

// Public, provider-free envelope validation for the synthetic client-testing
// dashboard. The function name remains stable for the renderer adapter.
const PANEL_KEYS = [
  'now_running',
  'latest_gate_runs',
  'whats_left',
  'time_estimates',
  'per_test_analytics',
  'recent_closures',
];
const PANEL_STATUSES = new Set(['ok', 'partial', 'unavailable']);

function errorPayload(message, sourceConnected) {
  return {
    error: message,
    _transport_stale: !sourceConnected,
    _data_stale: true,
  };
}

function utcMillis(value) {
  if (typeof value !== 'string' || !value.endsWith('Z')) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function validEnvelope(envelope) {
  if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) return false;
  if (envelope.schema_version !== 1
    || utcMillis(envelope.updated_at) === null
    || utcMillis(envelope.server_received_at) === null) return false;
  if (!Number.isFinite(envelope.freshness_ttl_sec) || envelope.freshness_ttl_sec <= 0) return false;
  const data = envelope.data;
  if (!data || typeof data !== 'object' || Array.isArray(data) || data.schema_version !== 1) return false;
  if (!data.panels || typeof data.panels !== 'object' || Array.isArray(data.panels)) return false;
  return PANEL_KEYS.every((key) => {
    const panel = data.panels[key];
    return panel && typeof panel === 'object' && !Array.isArray(panel) && PANEL_STATUSES.has(panel.status);
  });
}

function resolvePentacleMobileTestingSnapshot(envelope, sourceConnected, nowMs) {
  const connected = !!sourceConnected;
  if (!envelope) return errorPayload('no synthetic data yet', connected);
  if (!validEnvelope(envelope) || !Number.isFinite(nowMs)) {
    return errorPayload('invalid client-testing envelope', connected);
  }
  const receivedMs = utcMillis(envelope.server_received_at);
  const ageSec = Math.max(0, nowMs - receivedMs) / 1000;
  return {
    ...envelope.data,
    _updated_at: envelope.updated_at,
    _server_received_at: envelope.server_received_at,
    _age_sec: ageSec,
    _transport_stale: !connected,
    _data_stale: ageSec > envelope.freshness_ttl_sec,
  };
}

module.exports = { PANEL_KEYS, resolvePentacleMobileTestingSnapshot };
