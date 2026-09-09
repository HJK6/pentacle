'use strict';

const SCHEDULE_EVENT_TYPES = Object.freeze([
  'schedule_created',
  'schedule_firing',
  'schedule_cancelled',
  'schedule_rescheduled',
  'schedule_fired',
  'schedule_retry_scheduled',
  'schedule_failed',
  'schedule_expired',
  'schedule_indeterminate',
  'schedule_run_requested',
]);

const TERMINAL_STATES = Object.freeze([
  'fired',
  'expired',
  'error',
  'cancelled',
  'indeterminate',
]);

const ACTIVE_STATES = Object.freeze([
  'pending',
  'firing',
  'retry_pending',
]);

const ALL_STATES = new Set([...ACTIVE_STATES, ...TERMINAL_STATES]);
const TERMINAL_DISPLAY_GRACE_MS = 24 * 60 * 60 * 1000;

function scheduleKey(schedule) {
  return schedule && typeof schedule.schedule_id === 'string' ? schedule.schedule_id : '';
}

function normalizeSchedule(raw) {
  const schedule = { ...(raw || {}) };
  const state = String(schedule.state || '');
  const preservesLegacyReason = schedule.__legacy_error_reason === true;
  if (!ALL_STATES.has(state)) {
    const error = new Error(`schedule_render_error: unknown projected state ${JSON.stringify(state)}`);
    error.code = 'schedule_render_error';
    throw error;
  }
  if (!schedule.visibility) schedule.visibility = 'default';
  if (TERMINAL_STATES.includes(state)) {
    const terminalAt = Date.parse(schedule.terminal_at || '');
    if (!Number.isFinite(terminalAt)) {
      const error = new Error(`schedule_render_error: terminal row ${schedule.schedule_id || ''} is missing terminal_at`);
      error.code = 'schedule_render_error';
      throw error;
    }
    if (!preservesLegacyReason && !['error', 'indeterminate'].includes(state)) {
      delete schedule.error_reason;
    }
  } else {
    delete schedule.terminal_at;
    if (!preservesLegacyReason) delete schedule.error_reason;
  }
  return schedule;
}

function withinTerminalDisplayGrace(schedule, nowMs = Date.now()) {
  if (!TERMINAL_STATES.includes(String(schedule?.state || ''))) return true;
  const terminalAt = Date.parse(schedule.terminal_at || '');
  return Number.isFinite(terminalAt) && nowMs < terminalAt + TERMINAL_DISPLAY_GRACE_MS;
}

function scheduleMap(schedules) {
  const map = new Map();
  for (const schedule of schedules || []) {
    const id = scheduleKey(schedule);
    if (id) map.set(id, normalizeSchedule(schedule));
  }
  return map;
}

function replaceSchedulesFromInventory(currentSchedules, incomingSchedules, nowMs = Date.now()) {
  void currentSchedules;
  return (incomingSchedules || [])
    .map(normalizeSchedule)
    .filter(scheduleKey)
    .filter((schedule) => withinTerminalDisplayGrace(schedule, nowMs))
    .sort(compareSchedulesForSidebar);
}

function applyScheduleEvent(currentSchedules, event, nowMs = Date.now()) {
  if (!event || !SCHEDULE_EVENT_TYPES.includes(String(event.type || ''))) {
    return currentSchedules || [];
  }
  const id = String(event.schedule_id || '');
  if (!id) return currentSchedules || [];
  const map = scheduleMap(currentSchedules);
  const current = map.get(id) || {};
  const nextRaw = {
    ...current,
    ...Object.fromEntries(Object.entries(event).filter(([key]) => key !== 'type')),
  };
  if (event.__legacy_error_reason !== true) delete nextRaw.__legacy_error_reason;
  const next = normalizeSchedule(nextRaw);
  if (!withinTerminalDisplayGrace(next, nowMs)) {
    map.delete(id);
    return Array.from(map.values()).sort(compareSchedulesForSidebar);
  }
  map.set(id, next);
  return Array.from(map.values()).sort(compareSchedulesForSidebar);
}

function compareSchedulesForSidebar(a, b) {
  const aTerminal = TERMINAL_STATES.includes(String(a.state || ''));
  const bTerminal = TERMINAL_STATES.includes(String(b.state || ''));
  if (aTerminal !== bTerminal) return aTerminal ? 1 : -1;
  const af = Date.parse(a.fires_at_utc || '') || Number.MAX_SAFE_INTEGER;
  const bf = Date.parse(b.fires_at_utc || '') || Number.MAX_SAFE_INTEGER;
  if (af !== bf) return af - bf;
  return String(a.schedule_id || '').localeCompare(String(b.schedule_id || ''));
}

module.exports = {
  SCHEDULE_EVENT_TYPES,
  ACTIVE_STATES,
  TERMINAL_STATES,
  TERMINAL_DISPLAY_GRACE_MS,
  normalizeSchedule,
  withinTerminalDisplayGrace,
  replaceSchedulesFromInventory,
  applyScheduleEvent,
};
