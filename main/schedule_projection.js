'use strict';

const SOURCE_STATE_TO_UI = Object.freeze({
  pending: 'pending',
  firing: 'firing',
  retry_pending: 'retry_pending',
  fired: 'fired',
  cancelled: 'cancelled',
  failed: 'error',
  indeterminate: 'indeterminate',
  expired: 'expired',
});

const TERMINAL_SOURCE_STATES = new Set([
  'fired',
  'cancelled',
  'failed',
  'indeterminate',
  'expired',
]);

const TERMINAL_DISPLAY_GRACE_MS = 24 * 60 * 60 * 1000;

const SCHEDULE_LIFECYCLE_EVENT_SOURCE_STATE = Object.freeze({
  schedule_created: 'pending',
  schedule_rescheduled: 'pending',
  schedule_run_requested: 'pending',
  schedule_firing: 'firing',
  schedule_retry_scheduled: 'retry_pending',
  schedule_fired: 'fired',
  schedule_cancelled: 'cancelled',
  schedule_failed: 'failed',
  schedule_indeterminate: 'indeterminate',
  schedule_expired: 'expired',
});

function scheduleProjectionError(message, scheduleId = '') {
  const suffix = scheduleId ? ` (${scheduleId})` : '';
  const error = new Error(`schedule_projection_error: ${message}${suffix}`);
  error.code = 'schedule_projection_error';
  return error;
}

function decodePromptBase64(value) {
  if (typeof value !== 'string' || !value) return '';
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(Buffer.from(value, 'base64'));
  } catch (_) {
    return '';
  }
}

function terminalAtMillis(value, scheduleId) {
  if (typeof value !== 'string' || !value) {
    throw scheduleProjectionError('terminal row is missing terminal_at', scheduleId);
  }
  const millis = Date.parse(value);
  if (!Number.isFinite(millis)) {
    throw scheduleProjectionError('terminal_at is not a timestamp', scheduleId);
  }
  return millis;
}

function projectedPromptPreview(raw) {
  if (typeof raw.prompt_preview === 'string') return raw.prompt_preview;
  const inline = raw.initial_prompt_b64 || raw.prompt_b64;
  return decodePromptBase64(inline).replace(/\s+/g, ' ').trim().slice(0, 160);
}

function projectScheduleRow(raw, {
  nowMs = Date.now(),
  includePrompt = false,
  applyTerminalGrace = true,
  preserveErrorReason = false,
} = {}) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw scheduleProjectionError('schedule row must be an object');
  }
  const scheduleId = String(raw.schedule_id || '');
  if (!scheduleId) throw scheduleProjectionError('schedule_id is required');

  const sourceState = String(raw.state || '');
  const uiState = SOURCE_STATE_TO_UI[sourceState];
  if (!uiState) {
    throw scheduleProjectionError(`unknown durable state ${JSON.stringify(sourceState)}`, scheduleId);
  }

  const terminal = TERMINAL_SOURCE_STATES.has(sourceState);
  let terminalMillis = null;
  if (terminal) {
    terminalMillis = terminalAtMillis(raw.terminal_at, scheduleId);
    if (applyTerminalGrace && nowMs >= terminalMillis + TERMINAL_DISPLAY_GRACE_MS) return null;
  }

  const projected = { ...raw, state: uiState };
  delete projected.type;
  delete projected.child_stream_id;
  delete projected.last_error_code;
  delete projected.prompt_b64;
  delete projected.prompt_blob_id;
  delete projected.__legacy_error_reason;

  const childStreamId = raw.child_stream_id || raw.fire_result_stream_id;
  if (childStreamId) projected.fire_result_stream_id = String(childStreamId);
  else delete projected.fire_result_stream_id;

  if (terminal) projected.terminal_at = raw.terminal_at;
  else delete projected.terminal_at;

  if (sourceState === 'failed' || sourceState === 'indeterminate' || preserveErrorReason) {
    const reason = raw.last_error_code ?? raw.error_reason;
    if (reason !== undefined && reason !== null && String(reason)) {
      projected.error_reason = String(reason);
    } else {
      delete projected.error_reason;
    }
  } else {
    delete projected.error_reason;
  }

  projected.prompt_preview = projectedPromptPreview(raw);
  if (includePrompt) {
    const promptBase64 = raw.initial_prompt_b64 || raw.prompt_b64;
    const promptBlobSha = raw.initial_prompt_blob_sha || raw.prompt_blob_id;
    if (promptBase64) projected.initial_prompt_b64 = String(promptBase64);
    else delete projected.initial_prompt_b64;
    if (promptBlobSha) projected.initial_prompt_blob_sha = String(promptBlobSha);
    else delete projected.initial_prompt_blob_sha;
    projected.prompt_storage = promptBlobSha ? 'blob' : 'inline';
  } else {
    delete projected.initial_prompt_b64;
    delete projected.initial_prompt_blob_sha;
    delete projected.prompt_storage;
  }

  return projected;
}

function projectScheduleInventory(rows, options = {}) {
  if (!Array.isArray(rows)) return [];
  return rows.map((row) => projectScheduleRow(row, options)).filter(Boolean);
}

function scheduleLifecycleEventType(frame, sourceState) {
  const explicit = [frame?.event_type, frame?.event, frame?.lifecycle, frame?.mutation]
    .find((value) => typeof value === 'string' && value);
  if (explicit) {
    const normalized = explicit.replace(/^schedule[.:_-]?/, '').replace(/[.-]/g, '_');
    const aliases = {
      create: 'schedule_created',
      created: 'schedule_created',
      reschedule: 'schedule_rescheduled',
      rescheduled: 'schedule_rescheduled',
      cancel: 'schedule_cancelled',
      cancelled: 'schedule_cancelled',
      firing: 'schedule_firing',
      fire: 'schedule_firing',
      fired: 'schedule_fired',
      retry: 'schedule_retry_scheduled',
      retry_pending: 'schedule_retry_scheduled',
      retry_scheduled: 'schedule_retry_scheduled',
      failed: 'schedule_failed',
      terminal_failed: 'schedule_failed',
      indeterminate: 'schedule_indeterminate',
      terminal_indeterminate: 'schedule_indeterminate',
      expired: 'schedule_expired',
      terminal_expired: 'schedule_expired',
      run: 'schedule_run_requested',
      run_requested: 'schedule_run_requested',
    };
    const eventType = explicit.startsWith('schedule_') ? explicit : aliases[normalized];
    const expectedState = SCHEDULE_LIFECYCLE_EVENT_SOURCE_STATE[eventType];
    if (!eventType || !expectedState) {
      throw scheduleProjectionError(`unknown lifecycle event ${JSON.stringify(explicit)}`);
    }
    if (expectedState !== sourceState) {
      throw scheduleProjectionError(
        `lifecycle event ${JSON.stringify(eventType)} disagrees with durable state ${JSON.stringify(sourceState)}`,
        frame?.schedule?.schedule_id || frame?.schedule_id || '',
      );
    }
    return eventType;
  }
  return {
    pending: 'schedule_created',
    firing: 'schedule_firing',
    retry_pending: 'schedule_retry_scheduled',
    fired: 'schedule_fired',
    cancelled: 'schedule_cancelled',
    failed: 'schedule_failed',
    indeterminate: 'schedule_indeterminate',
    expired: 'schedule_expired',
  }[sourceState] || '';
}

module.exports = {
  SOURCE_STATE_TO_UI,
  TERMINAL_SOURCE_STATES,
  TERMINAL_DISPLAY_GRACE_MS,
  SCHEDULE_LIFECYCLE_EVENT_SOURCE_STATE,
  projectScheduleRow,
  projectScheduleInventory,
  scheduleLifecycleEventType,
};
