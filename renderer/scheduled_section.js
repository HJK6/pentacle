'use strict';

// Scheduled sidebar section: recurring rules first (pause/resume/edit), then
// pending one-shot rows (read-only). Pure HTML builders; app.js owns the DOM.

const EVERY_OPTIONS = Object.freeze(['daily', 'weekdays', 'weekly']);
const DAY_NAMES = Object.freeze(['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']);
const PENDING_STATES = new Set(['pending', 'retry_pending', 'firing']);

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

function formatInZone(iso, tz) {
  const millis = Date.parse(iso || '');
  if (!Number.isFinite(millis)) return '';
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: tz || undefined, weekday: 'short', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short',
    }).format(new Date(millis));
  } catch (_) {
    return new Date(millis).toISOString();
  }
}

function formatRelative(iso, nowMs = Date.now()) {
  const millis = Date.parse(iso || '');
  if (!Number.isFinite(millis)) return '';
  const delta = millis - nowMs;
  const minutes = Math.round(Math.abs(delta) / 60000);
  const span = minutes < 1 ? 'now'
    : minutes < 60 ? `${minutes}m`
      : minutes < 48 * 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m`
        : `${Math.round(minutes / 1440)}d`;
  if (span === 'now') return 'now';
  return delta >= 0 ? `in ${span}` : `${span} ago`;
}

function lastRunText(rule) {
  if (rule.last_result === 'missed') return `missed: ${rule.last_result_reason || 'unknown'}`;
  if (rule.last_result === 'fired') return 'fired';
  return 'never run';
}

function editFormHtml(rule) {
  const recurrence = rule.recurrence || {};
  const days = new Set(recurrence.days || []);
  const everyOptions = EVERY_OPTIONS.map((value) => (
    `<option value="${value}"${recurrence.every === value ? ' selected' : ''}>${value}</option>`
  )).join('');
  const dayBoxes = DAY_NAMES.map((day) => (
    `<label class="scheduled-day"><input type="checkbox" name="day" value="${day}"${days.has(day) ? ' checked' : ''}>${day}</label>`
  )).join('');
  return `<form class="scheduled-edit" data-recurring-edit="${escapeHtml(rule.recurring_id)}">`
    + `<label>Name <input name="name" type="text" maxlength="120" value="${escapeHtml(rule.name)}" required></label>`
    + `<label>Every <select name="every">${everyOptions}</select></label>`
    + `<label>At <input name="at_time" type="time" value="${escapeHtml(recurrence.at_time || '')}" required></label>`
    + `<div class="scheduled-days">${dayBoxes}</div>`
    + `<label>Zone <input name="tz" type="text" value="${escapeHtml(recurrence.tz || '')}" required></label>`
    + '<div class="scheduled-edit-actions">'
    + '<button type="button" data-scheduled-action="save">Save</button>'
    + '<button type="button" data-scheduled-action="cancel-edit">Cancel</button></div>'
    + `<div class="scheduled-edit-error" role="alert" hidden></div></form>`;
}

function recurringRowHtml(rule, { nowMs, editing }) {
  const paused = rule.state === 'paused';
  const id = escapeHtml(rule.recurring_id);
  const tz = rule.tz || (rule.recurrence && rule.recurrence.tz) || '';
  const next = paused ? 'paused' : `${formatInZone(rule.next_fire_at_utc, tz)} (${formatRelative(rule.next_fire_at_utc, nowMs)})`;
  return `<div class="scheduled-row scheduled-rule" data-recurring-id="${id}" data-state="${escapeHtml(rule.state)}">`
    + `<div class="scheduled-title"><span class="scheduled-name">${escapeHtml(rule.name)}</span>`
    + `<span class="scheduled-badge scheduled-badge-${escapeHtml(rule.state)}">${escapeHtml(rule.state)}</span></div>`
    + `<div class="scheduled-recurrence">${escapeHtml(rule.recurrence_text || '')}</div>`
    + `<div class="scheduled-next">Next: ${escapeHtml(next)}</div>`
    + `<div class="scheduled-last">Last: ${escapeHtml(lastRunText(rule))}</div>`
    + '<div class="scheduled-actions">'
    + `<button type="button" data-scheduled-action="${paused ? 'resume' : 'pause'}">${paused ? 'Resume' : 'Pause'}</button>`
    + '<button type="button" data-scheduled-action="edit">Edit</button></div>'
    + (editing ? editFormHtml(rule) : '')
    + '</div>';
}

function oneShotRowHtml(schedule, tz, nowMs) {
  return `<div class="scheduled-row scheduled-oneshot" data-schedule-id="${escapeHtml(schedule.schedule_id)}">`
    + `<div class="scheduled-title"><span class="scheduled-name">${escapeHtml(schedule.prompt_preview || schedule.schedule_id)}</span>`
    + '<span class="scheduled-badge scheduled-badge-oneshot">one-shot</span></div>'
    + `<div class="scheduled-next">Fires: ${escapeHtml(formatInZone(schedule.fires_at_utc, tz))} (${escapeHtml(formatRelative(schedule.fires_at_utc, nowMs))})</div>`
    + '</div>';
}

function pendingOneShots(schedules) {
  return (schedules || []).filter((schedule) => PENDING_STATES.has(String(schedule.state || '')));
}

function hasScheduledContent({ recurring, schedules } = {}) {
  return (recurring || []).length > 0 || pendingOneShots(schedules).length > 0;
}

function scheduledSectionHtml({ recurring, schedules, nowMs = Date.now(), editingId = null, localTz } = {}) {
  const rules = (recurring || []).map((rule) => recurringRowHtml(rule, { nowMs, editing: rule.recurring_id === editingId }));
  const oneShots = pendingOneShots(schedules).map((schedule) => oneShotRowHtml(schedule, localTz, nowMs));
  return [...rules, ...oneShots].join('');
}

// Collect the Edit form into the recurring.update fields (days only for weekly).
function editFieldsFromForm(form) {
  const field = (name) => String(form.elements[name]?.value ?? '').trim();
  const every = field('every');
  const days = every === 'weekly'
    ? Array.from(form.querySelectorAll('input[name="day"]:checked')).map((box) => box.value)
    : [];
  return { name: field('name'), every, at_time: field('at_time'), tz: field('tz'), days };
}

module.exports = {
  escapeHtml,
  formatInZone,
  formatRelative,
  hasScheduledContent,
  scheduledSectionHtml,
  editFieldsFromForm,
};
