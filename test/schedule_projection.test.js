const test = require('node:test');
const assert = require('node:assert/strict');

const {
  SOURCE_STATE_TO_UI,
  TERMINAL_DISPLAY_GRACE_MS,
  projectScheduleRow,
  projectScheduleInventory,
} = require('../main/schedule_projection');

const terminalAt = '2026-08-29T12:00:00Z';
const terminalMs = Date.parse(terminalAt);

function row(state, overrides = {}) {
  const terminal = ['fired', 'cancelled', 'failed', 'indeterminate', 'expired'].includes(state);
  return {
    schedule_id: `schedule-${state}`,
    state,
    fires_at_utc: '2026-08-29T13:00:00Z',
    target_host: 'hosta',
    resolved_provider: 'codex',
    provider: 'codex',
    visibility: 'default',
    prompt_preview: 'Captured prompt preview',
    ...(terminal ? { terminal_at: terminalAt } : {}),
    ...overrides,
  };
}

test('v2 projection is total over all eight durable source states', () => {
  assert.deepEqual(Object.keys(SOURCE_STATE_TO_UI), [
    'pending',
    'firing',
    'retry_pending',
    'fired',
    'cancelled',
    'failed',
    'indeterminate',
    'expired',
  ]);
  const expected = {
    pending: 'pending',
    firing: 'firing',
    retry_pending: 'retry_pending',
    fired: 'fired',
    cancelled: 'cancelled',
    failed: 'error',
    indeterminate: 'indeterminate',
    expired: 'expired',
  };
  for (const [sourceState, uiState] of Object.entries(expected)) {
    const projected = projectScheduleRow(row(sourceState), { nowMs: terminalMs });
    assert.equal(projected.state, uiState, sourceState);
    assert.equal(projected.prompt_preview, 'Captured prompt preview');
    assert.equal(Object.hasOwn(projected, 'terminal_at'), ['fired', 'cancelled', 'failed', 'indeterminate', 'expired'].includes(sourceState));
  }
});

test('projection maps v2 result and error fields without leaking them into unrelated states', () => {
  const failed = projectScheduleRow(row('failed', {
    child_stream_id: 'hosta:child-failed',
    last_error_code: 'spawn_rejected',
  }), { nowMs: terminalMs });
  assert.equal(failed.state, 'error');
  assert.equal(failed.fire_result_stream_id, 'hosta:child-failed');
  assert.equal(failed.error_reason, 'spawn_rejected');
  assert.equal(Object.hasOwn(failed, 'child_stream_id'), false);
  assert.equal(Object.hasOwn(failed, 'last_error_code'), false);

  const fired = projectScheduleRow(row('fired', {
    child_stream_id: 'hosta:child-fired',
    last_error_code: 'stale_error',
  }), { nowMs: terminalMs });
  assert.equal(fired.fire_result_stream_id, 'hosta:child-fired');
  assert.equal(Object.hasOwn(fired, 'error_reason'), false);

  const strictExpired = projectScheduleRow(row('expired', {
    error_reason: 'restart_staleness',
  }), { nowMs: terminalMs });
  assert.equal(Object.hasOwn(strictExpired, 'error_reason'), false);

  const legacyExpired = projectScheduleRow(row('expired', {
    error_reason: 'restart_staleness',
  }), { nowMs: terminalMs, preserveErrorReason: true });
  assert.equal(legacyExpired.error_reason, 'restart_staleness');
});

test('projection removes retired states and rejects every unknown durable state', () => {
  for (const retired of ['fired_in_progress', 'pending_retry', 'unknown']) {
    assert.throws(
      () => projectScheduleRow(row(retired)),
      (error) => error.code === 'schedule_projection_error' && error.message.includes(retired),
    );
  }
  assert.throws(
    () => projectScheduleRow({ ...row('failed'), terminal_at: null }),
    /terminal row is missing terminal_at/,
  );
});

test('projection applies terminal display grace immediately before, at, and after 24h', () => {
  const source = row('expired');
  const boundary = terminalMs + TERMINAL_DISPLAY_GRACE_MS;
  assert.equal(projectScheduleInventory([source], { nowMs: boundary - 1 }).length, 1);
  assert.equal(projectScheduleInventory([source], { nowMs: boundary }).length, 0);
  assert.equal(projectScheduleInventory([source], { nowMs: boundary + 1 }).length, 0);
});

test('inventory never carries full prompt content while get projection preserves prompt modes', () => {
  const inlineB64 = Buffer.from('Full inline prompt', 'utf8').toString('base64');
  const inline = row('pending', { prompt_preview: undefined, prompt_b64: inlineB64 });
  const inventory = projectScheduleRow(inline);
  assert.equal(inventory.prompt_preview, 'Full inline prompt');
  assert.equal(Object.hasOwn(inventory, 'initial_prompt_b64'), false);
  assert.equal(Object.hasOwn(inventory, 'prompt_b64'), false);

  const detail = projectScheduleRow(inline, { includePrompt: true, applyTerminalGrace: false });
  assert.equal(detail.initial_prompt_b64, inlineB64);
  assert.equal(detail.prompt_storage, 'inline');

  const blob = projectScheduleRow(row('pending', { prompt_blob_id: 'a'.repeat(64) }), {
    includePrompt: true,
    applyTerminalGrace: false,
  });
  assert.equal(blob.initial_prompt_blob_sha, 'a'.repeat(64));
  assert.equal(blob.prompt_storage, 'blob');
});

