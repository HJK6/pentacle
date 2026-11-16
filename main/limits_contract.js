'use strict';

// Shared by the socket cache and renderer: limits and health are an atomic pair.
const LIMIT_IDENTITIES = Object.freeze([
  Object.freeze(['claude', 'Claude']),
  Object.freeze(['fable', 'Fable']),
  Object.freeze(['codex', 'Codex']),
]);
const LIMIT_KEYS = 'id,label,pct,probed_at,resets_at_iso,resets_text,upstream_reported_at';
const LIMITS_HEALTH_KEYS = 'attempted_at,error,outcome,probed_at,stale_after_seconds,upstream_reported_at';
const LIMITS_HEALTH_TOP_KEYS = 'claude,schema_version';
const LIMITS_HEALTH_OUTCOMES = new Set([
  'never', 'ok', 'auth_error', 'parser_error', 'provider_error',
  'timeout', 'transport_error', 'internal_error', 'store_error',
]);
const LIMITS_HEALTH_ERRORS = Object.freeze({
  auth_error: Object.freeze({ code: 'claude_not_authenticated', message: 'Claude is not authenticated' }),
  provider_error: Object.freeze({ code: 'claude_subscription_unavailable', message: 'Claude subscription usage is unavailable' }),
  parser_error: Object.freeze({ code: 'claude_usage_parse_failed', message: 'Claude usage could not be parsed' }),
  timeout: Object.freeze({ code: 'claude_usage_timeout', message: 'Claude usage probe timed out' }),
  transport_error: Object.freeze({ code: 'claude_usage_transport_failed', message: 'Claude usage transport failed' }),
  internal_error: Object.freeze({ code: 'claude_usage_internal_error', message: 'Claude usage probe failed internally' }),
  store_error: Object.freeze({ code: 'usage_state_write_failed', message: 'Claude usage state could not be saved' }),
});

function utcRfc3339Millis(value) {
  if (typeof value !== 'string') return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$/.exec(value);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (month < 1 || month > 12 || day < 1 || day > daysInMonth[month - 1]) return null;
  const millis = Date.parse(value);
  return Number.isFinite(millis) ? millis : null;
}

function nullLimits() {
  return LIMIT_IDENTITIES.map(([id, label]) => ({
    id,
    label,
    pct: null,
    resets_at_iso: null,
    resets_text: null,
    upstream_reported_at: null,
    probed_at: null,
  }));
}

function validatedLimitsHealth(value) {
  if (value === null) return null;
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).sort().join(',') !== LIMITS_HEALTH_TOP_KEYS
    || value.schema_version !== 1) return undefined;
  const health = value.claude;
  if (!health || typeof health !== 'object' || Array.isArray(health)
    || Object.keys(health).sort().join(',') !== LIMITS_HEALTH_KEYS
    || !LIMITS_HEALTH_OUTCOMES.has(health.outcome)
    || !Number.isInteger(health.stale_after_seconds)
    || health.stale_after_seconds < 60 || health.stale_after_seconds > 86400) return undefined;
  const attemptedMillis = health.attempted_at === null ? null : utcRfc3339Millis(health.attempted_at);
  const upstreamMillis = health.upstream_reported_at === null ? null : utcRfc3339Millis(health.upstream_reported_at);
  const probedMillis = health.probed_at === null ? null : utcRfc3339Millis(health.probed_at);
  const outcome = health.outcome;
  if ((health.attempted_at !== null && attemptedMillis === null)
    || (health.upstream_reported_at !== null && upstreamMillis === null)
    || (health.probed_at !== null && probedMillis === null)
    || ((upstreamMillis === null) !== (probedMillis === null))) return undefined;
  if (outcome === 'never') {
    if (health.attempted_at !== null || health.upstream_reported_at !== null
      || health.probed_at !== null || health.error !== null) return undefined;
  } else {
    if (attemptedMillis === null) return undefined;
    if (outcome === 'ok') {
      if (health.error !== null || upstreamMillis === null || probedMillis === null) return undefined;
    } else {
      const expected = LIMITS_HEALTH_ERRORS[outcome];
      if (!expected || !health.error || typeof health.error !== 'object' || Array.isArray(health.error)
        || Object.keys(health.error).sort().join(',') !== 'code,message'
        || (outcome === 'provider_error'
          ? !['usage_provider_error', 'claude_usage_provider_error', expected.code].includes(health.error.code)
            || typeof health.error.message !== 'string' || !health.error.message.trim()
          : health.error.code !== expected.code || health.error.message !== expected.message)) return undefined;
    }
  }
  if (upstreamMillis !== null && ((outcome === 'ok' && attemptedMillis > upstreamMillis)
    || upstreamMillis > probedMillis)) return undefined;
  return {
    schema_version: 1,
    claude: {
      attempted_at: health.attempted_at,
      outcome,
      error: health.error === null ? null : { code: health.error.code, message: health.error.message },
      upstream_reported_at: health.upstream_reported_at,
      probed_at: health.probed_at,
      stale_after_seconds: health.stale_after_seconds,
    },
  };
}

function limitsHealthFromFrame(frame) {
  if (!Object.prototype.hasOwnProperty.call(frame || {}, 'limits_health')) {
    return { valid: true, value: null };
  }
  const value = validatedLimitsHealth(frame.limits_health);
  return { valid: value !== undefined, value };
}

function validatedLimits(value) {
  if (!Array.isArray(value) || value.length !== LIMIT_IDENTITIES.length) return null;
  const result = [];
  for (let index = 0; index < LIMIT_IDENTITIES.length; index += 1) {
    const row = value[index];
    const [id, label] = LIMIT_IDENTITIES[index];
    if (!row || typeof row !== 'object' || Array.isArray(row)
      || Object.keys(row).sort().join(',') !== LIMIT_KEYS
      || row.id !== id || row.label !== label
      || (row.pct !== null && (!Number.isInteger(row.pct) || row.pct < 0 || row.pct > 100))
      || (row.resets_at_iso !== null && typeof row.resets_at_iso !== 'string')
      || (row.resets_text !== null && typeof row.resets_text !== 'string')) {
      return null;
    }
    const upstreamMillis = row.upstream_reported_at === null
      ? null : utcRfc3339Millis(row.upstream_reported_at);
    const probedMillis = row.probed_at === null ? null : utcRfc3339Millis(row.probed_at);
    const hasUpstream = row.upstream_reported_at !== null;
    const hasProbed = row.probed_at !== null;
    if ((id !== 'codex' && (hasUpstream || hasProbed))
      || hasUpstream !== hasProbed
      || (hasUpstream && (upstreamMillis === null || probedMillis === null
        // Same tolerance as usage_state.py's LKG check: order at whole-second
        // granularity so sub-second stamp precision cannot invert the pair.
        || Math.floor(upstreamMillis / 1000) > Math.floor(probedMillis / 1000)))) {
      return null;
    }
    result.push({
      id: row.id,
      label: row.label,
      pct: row.pct,
      resets_at_iso: row.resets_at_iso,
      resets_text: row.resets_text,
      upstream_reported_at: row.upstream_reported_at,
      probed_at: row.probed_at,
    });
  }
  return result;
}

module.exports = { nullLimits, validatedLimits, validatedLimitsHealth, limitsHealthFromFrame };
