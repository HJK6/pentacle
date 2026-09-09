const test = require('node:test');
const assert = require('node:assert/strict');

// The desktop schedule window plane (sidebar rows + detail panel) was retired
// (public_behavior_spec). Only the
// schedule protocol ingestion remains in the renderer, so this suite covers the
// protocol/state layer: inventory replacement, lifecycle event application,
// normalization, and the terminal display-grace window.
const {
  replaceSchedulesFromInventory,
  applyScheduleEvent,
  normalizeSchedule,
  withinTerminalDisplayGrace,
  TERMINAL_DISPLAY_GRACE_MS,
} = require('../renderer/schedule_ui_state');

const now = Date.parse('2026-05-20T20:00:00Z');

function schedule(overrides = {}) {
  return {
    schedule_id: 'sched-1',
    state: 'pending',
    fires_at_utc: '2026-05-20T23:49:00Z',
    target_host: 'hosta',
    provider: 'codex',
    visibility: 'default',
    prompt_preview: 'Continue the sample task.',
    ...overrides,
  };
}

test('inventory replacement normalizes, keeps in-grace rows, and sorts deterministically', () => {
  const schedules = replaceSchedulesFromInventory([], [
    schedule({ schedule_id: 'b', fires_at_utc: '2026-05-20T23:50:00Z' }),
    schedule({ schedule_id: 'a', fires_at_utc: '2026-05-20T23:40:00Z' }),
    schedule({ schedule_id: 'mid', fires_at_utc: '2026-05-20T23:45:00Z', visibility: 'hidden' }),
  ], now);
  // Active rows sort by fire time; non-default visibility is retained by the
  // protocol layer (visibility filtering was a window-plane concern).
  assert.deepEqual(schedules.map((s) => s.schedule_id), ['a', 'mid', 'b']);
  assert.equal(schedules.find((s) => s.schedule_id === 'mid').visibility, 'hidden');
});

test('all schedule lifecycle events update protocol state', () => {
  const cases = [
    {
      type: 'schedule_created',
      before: [],
      event: schedule({ schedule_id: 'created', state: 'pending' }),
      assertRow(row) {
        assert.equal(row.state, 'pending');
      },
    },
    {
      type: 'schedule_rescheduled',
      before: [schedule({ schedule_id: 'rescheduled', state: 'fired', terminal_at: '2026-05-20T19:59:00Z', fire_result_stream_id: 'hosta:old-fired' })],
      event: { schedule_id: 'rescheduled', state: 'pending', fires_at_utc: '2026-05-21T00:30:00Z' },
      assertRow(row) {
        assert.equal(row.state, 'pending');
        assert.equal(row.fires_at_utc, '2026-05-21T00:30:00Z');
      },
    },
    {
      type: 'schedule_retry_scheduled',
      before: [schedule({ schedule_id: 'retry', state: 'firing', retry_count: 0 })],
      event: { schedule_id: 'retry', state: 'retry_pending', retry_count: 1 },
      assertRow(row) {
        assert.equal(row.state, 'retry_pending');
        assert.equal(row.retry_count, 1);
      },
    },
    {
      type: 'schedule_indeterminate',
      before: [schedule({ schedule_id: 'indeterminate', state: 'firing' })],
      event: { schedule_id: 'indeterminate', state: 'indeterminate', terminal_at: '2026-05-20T20:00:00Z', error_reason: 'indeterminate_restart' },
      assertRow(row) {
        assert.equal(row.state, 'indeterminate');
        assert.equal(row.error_reason, 'indeterminate_restart');
        assert.equal(row.terminal_at, '2026-05-20T20:00:00Z');
      },
    },
    {
      type: 'schedule_run_requested',
      before: [schedule({ schedule_id: 'run-now', state: 'pending', fires_at_utc: '2026-05-20T23:00:00Z' })],
      event: { schedule_id: 'run-now', state: 'pending', fires_at_utc: '2026-05-20T20:00:03Z' },
      assertRow(row) {
        assert.equal(row.state, 'pending');
        assert.equal(row.fires_at_utc, '2026-05-20T20:00:03Z');
      },
    },
    {
      type: 'schedule_fired',
      before: [schedule({ schedule_id: 'fired' })],
      event: { schedule_id: 'fired', state: 'fired', terminal_at: '2026-05-20T20:00:00Z', fire_result_stream_id: 'hosta:codex-fired' },
      assertRow(row) {
        assert.equal(row.state, 'fired');
        assert.equal(row.fire_result_stream_id, 'hosta:codex-fired');
        assert.equal(row.terminal_at, '2026-05-20T20:00:00Z');
      },
    },
    {
      type: 'schedule_cancelled',
      before: [schedule({ schedule_id: 'cancelled' })],
      event: { schedule_id: 'cancelled', state: 'cancelled', terminal_at: '2026-05-20T20:00:00Z' },
      assertRow(row) {
        assert.equal(row.state, 'cancelled');
        assert.equal(row.terminal_at, '2026-05-20T20:00:00Z');
      },
    },
    {
      type: 'schedule_expired',
      before: [schedule({ schedule_id: 'expired' })],
      event: { schedule_id: 'expired', state: 'expired', terminal_at: '2026-05-20T20:00:00Z' },
      assertRow(row) {
        assert.equal(row.state, 'expired');
        assert.equal(row.terminal_at, '2026-05-20T20:00:00Z');
      },
    },
    {
      type: 'schedule_failed',
      before: [schedule({ schedule_id: 'failed' })],
      event: { schedule_id: 'failed', state: 'error', terminal_at: '2026-05-20T20:00:00Z', error_reason: 'host_offline' },
      assertRow(row) {
        assert.equal(row.state, 'error');
        assert.equal(row.error_reason, 'host_offline');
        assert.equal(row.terminal_at, '2026-05-20T20:00:00Z');
      },
    },
  ];

  for (const entry of cases) {
    const next = applyScheduleEvent(entry.before, { type: entry.type, ...entry.event }, now);
    const row = next.find((item) => item.schedule_id === entry.event.schedule_id);
    assert.ok(row, `${entry.type} should leave a row`);
    entry.assertRow(row);
  }
});

test('renderer retains documented legacy retry and expired reasons, then clears compatibility state', () => {
  const retry = applyScheduleEvent([schedule({ schedule_id: 'legacy-retry', state: 'firing' })], {
    type: 'schedule_retry_scheduled',
    schedule_id: 'legacy-retry',
    state: 'retry_pending',
    error_reason: 'host_offline',
    __legacy_error_reason: true,
  }, now);
  assert.equal(retry[0].error_reason, 'host_offline');

  const rearmed = applyScheduleEvent(retry, {
    type: 'schedule_rescheduled',
    schedule_id: 'legacy-retry',
    state: 'pending',
    fires_at_utc: '2026-05-21T00:00:00Z',
  }, now);
  assert.equal(Object.hasOwn(rearmed[0], 'error_reason'), false);
  assert.equal(Object.hasOwn(rearmed[0], '__legacy_error_reason'), false);

  const expired = applyScheduleEvent([schedule({ schedule_id: 'legacy-expired' })], {
    type: 'schedule_expired',
    schedule_id: 'legacy-expired',
    state: 'expired',
    terminal_at: '2026-05-20T20:00:00Z',
    error_reason: 'restart_staleness',
    __legacy_error_reason: true,
  }, now);
  assert.equal(expired[0].error_reason, 'restart_staleness');

  const strictV2Expired = applyScheduleEvent([schedule({ schedule_id: 'v2-expired' })], {
    type: 'schedule_expired',
    schedule_id: 'v2-expired',
    state: 'expired',
    terminal_at: '2026-05-20T20:00:00Z',
    error_reason: 'must_not_survive_native_projection',
  }, now);
  assert.equal(Object.hasOwn(strictV2Expired[0], 'error_reason'), false);
});

test('re-arming a fired schedule keeps its fire_result_stream_id and returns to pending', () => {
  const fired = applyScheduleEvent([schedule({ schedule_id: 'sched-fire' })], {
    type: 'schedule_fired',
    schedule_id: 'sched-fire',
    state: 'fired',
    terminal_at: '2026-05-20T20:00:00Z',
    fire_result_stream_id: 'hosta:codex-fired',
  }, now);
  assert.equal(fired[0].state, 'fired');

  const rearmed = applyScheduleEvent(fired, {
    type: 'schedule_rescheduled',
    schedule_id: 'sched-fire',
    state: 'pending',
    fires_at_utc: '2026-05-21T00:00:00Z',
  }, now + 1000);
  assert.equal(rearmed[0].state, 'pending');
  assert.equal(rearmed[0].fire_result_stream_id, 'hosta:codex-fired');
});

test('terminal display grace uses terminal_at immediately before, at, and after 24h', () => {
  const terminalAt = '2026-05-20T20:00:00Z';
  const boundary = Date.parse(terminalAt) + TERMINAL_DISPLAY_GRACE_MS;
  const terminal = schedule({ schedule_id: 'grace', state: 'expired', terminal_at: terminalAt });
  assert.equal(withinTerminalDisplayGrace(terminal, boundary - 1), true);
  assert.equal(withinTerminalDisplayGrace(terminal, boundary), false);
  assert.equal(withinTerminalDisplayGrace(terminal, boundary + 1), false);
  // Inventory replacement drops out-of-grace terminal rows.
  assert.equal(replaceSchedulesFromInventory([], [terminal], boundary - 1).length, 1);
  assert.equal(replaceSchedulesFromInventory([], [terminal], boundary).length, 0);
});

test('normalization rejects retired and unknown projected states instead of silently dropping them', () => {
  for (const state of ['fired_in_progress', 'pending_retry', 'failed', 'mystery']) {
    assert.throws(
      () => normalizeSchedule(schedule({ state })),
      (error) => error.code === 'schedule_render_error' && error.message.includes(state),
    );
  }
});

