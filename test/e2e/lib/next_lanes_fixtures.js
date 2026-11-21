'use strict';

const crypto = require('node:crypto');

const LANE_COMPOSITE_ID = 'local:web-gate-assistant';
const ADOPTION_KEYS = new Map();
const L11_ID = fixtureLaneId('l11');
const ORDINARY_A_ID = fixtureLaneId('ordinary-a');
const ORDINARY_B_ID = fixtureLaneId('ordinary-b');
const NO_GOAL_ID = fixtureLaneId('no-goal');
const PARKED_ID = fixtureLaneId('parked');
const QUIET_ID = fixtureLaneId('quiet');
const L65_ID = fixtureLaneId('l65');

const REVISION_A = revision('revision-a');
const REVISION_B = revision('revision-b');
const REVISION_C = revision('revision-c');
const REVISION_D = revision('revision-d');

const INLINE_MEMBERS = 8;
const PAGE_SIZE = 32;
const DETAIL_MEMBER_KEYS = new Set([
  'blocked_line', 'checkpoint_state', 'checkpoint_deficiencies', 'checkpoint_revision',
  'checkpoint_policy_revision', 'checkpoint_changed_at', 'checkpoint_first_observed_at',
  'goal_text', 'goal_text_truncated', 'goal_source', 'progress_at', 'summary_line',
  'estimate_first', 'unblocks', 'recent', 'latest_event', 'status_behind',
  'blockers_text',
]);

function revision(value) {
  return crypto.createHash('sha256').update(String(value)).digest('hex');
}

function fixtureLaneId(key) {
  const adoptionKey = `spec:next_lanes_fixture_${String(key).replace(/[^a-zA-Z0-9_-]/g, '_')}`;
  const laneId = `wl-${crypto.createHash('sha256').update(`${LANE_COMPOSITE_ID}\0${adoptionKey}`).digest('hex').slice(0, 24)}`;
  ADOPTION_KEYS.set(laneId, adoptionKey);
  return laneId;
}

function goalId(epicId) {
  return `g_${crypto.createHash('sha256').update(epicId).digest('hex').slice(0, 24)}`;
}

function lexical(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value));
}

function makeMember(specId, goalIdValue = null, overrides = {}) {
  const member = {
    spec_id: specId,
    goal_id: goalIdValue,
    title: specId,
    status: 'ready_for_dev',
    terminal: null,
    ac_checked: 1,
    ac_total: 3,
    estimate: { p25: 1, p75: 3, median: 2, as_of: '2026-11-20T12:00:00Z', provisional: false },
    estimate_exempt: false,
    status_text: 'Synthetic fixture status.',
    next_action_text: 'Continue the fixture task.',
    status_line: 'Synthetic fixture status.',
    next_line: 'Continue the fixture task.',
    summary_line: 'Synthetic progress summary.',
    progress_at: '2026-11-20T12:00:00Z',
    _progress_at: '2026-11-20T12:00:00Z',
    _latest_event: null,
    worked: { elapsed_h: 1, censored: false, active_h: 1, seats: 0, qa_rounds: 0, as_of: '2026-11-20T12:00:00Z' },
    lead_seat: null,
    seats: [],
    seats_total: 0,
    open_questions: { count: 0, first_question_id: null },
    observation: { quality: 'fresh', observed_at: '2026-11-20T12:00:00Z', error: null },
    source_changed_at: '2026-11-20T12:00:00Z',
    criteria: [
      { title: 'Synthetic open check', disposition: 'open' },
      { title: 'Synthetic completed check', disposition: 'done' },
    ],
    transitions: [],
    recent: [{ at: '2026-11-20T12:00:00Z', spec_id: specId, kind: 'note', text: 'Synthetic fixture progress.' }],
    unblocks: [],
    checkpoint_state: 'unverifiable',
    checkpoint_deficiencies: [],
    checkpoint_policy_revision: revision('synthetic-checkpoint-policy'),
    ...overrides,
  };
  if (member.status === 'completed') member.terminal = 'completed';
  if (member.terminal === 'completed') member.status = 'completed';
  if (!Object.prototype.hasOwnProperty.call(overrides, 'seats_total')) member.seats_total = member.seats.length;
  if (overrides.worked == null && member.seats.length) member.worked.seats = member.seats.length;
  if (!member.open_questions) member.open_questions = { count: 0, first_question_id: null };
  return member;
}

function isDone(member) {
  return member.status === 'completed' || member.terminal === 'completed';
}

function isDeprecated(member) {
  return member.status === 'deprecated' || member.terminal === 'deprecated';
}

function goalClass(members) {
  const counted = members.filter(member => !isDeprecated(member));
  if (counted.length && counted.every(isDone)) return 'd';
  if (counted.some(member => !isDone(member) && (member.seats || []).some(seat => seat.activity === 'working'))) return 'w';
  if (counted.some(member => !isDone(member) && ['analysis', 'in_progress', 'needs_qa'].includes(member.status))) return 'm';
  if (counted.some(member => member.status === 'blocked')) return 'b';
  return counted.length ? 'q' : 'q';
}

function progressFor(members, { certain = true } = {}) {
  const counted = members.filter(member => !isDeprecated(member));
  const done = counted.filter(isDone).length;
  const state = !counted.length ? 'not_started'
    : certain && done === counted.length ? 'done'
      : certain && counted.every(member => member.status === 'backlog') ? 'not_started'
        : 'in_progress';
  return { state, done, total: counted.length };
}

function goalRecord(goal, members, cls, { detail = false } = {}) {
  const { epic_id: _epicId, ...wireGoal } = goal;
  const record = {
    ...wireGoal,
    ...progressFor(members),
  };
  if (detail) {
    record.moving = members.filter(member => ['analysis', 'in_progress', 'needs_qa'].includes(member.status) && !isDone(member)).length;
    record.blocked = members.filter(member => member.status === 'blocked').length;
    record.working = cls === 'w';
    const estimated = members.filter(member => !['completed', 'deprecated', 'missing', 'ambiguous'].includes(member.status)
      && !member.estimate_exempt);
    record.left_h = estimated.length > 0 && estimated.every(member => member.estimate
      && Number.isFinite(member.estimate.median)
      && member.observation?.quality === 'fresh')
      ? Number(estimated.reduce((sum, member) => sum + member.estimate.median, 0).toFixed(1)) : null;
    record.progress_at = members.map(member => member._progress_at).filter(Boolean).sort(lexical).at(-1) || null;
  }
  return record;
}

function inventoryMember(member) {
  const row = {};
  for (const [key, value] of Object.entries(member)) {
    if (!key.startsWith('_') && !DETAIL_MEMBER_KEYS.has(key) && key !== 'criteria' && key !== 'transitions') row[key] = clone(value);
  }
  return row;
}

function defaultGoalProjection(members, cls, { detail = false } = {}) {
  if (!members.length) return null;
  const progress = progressFor(members);
  if (!detail) return progress;
  return {
    ...progress,
    moving: members.filter(member => ['analysis', 'in_progress', 'needs_qa'].includes(member.status) && !isDone(member)).length,
    blocked: members.filter(member => member.status === 'blocked').length,
    working: cls === 'w',
    left_h: (() => {
      const population = members.filter(member => !['completed', 'deprecated', 'missing', 'ambiguous'].includes(member.status)
        && !member.estimate_exempt);
      return population.length > 0 && population.every(member => member.estimate
        && Number.isFinite(member.estimate.median)
        && member.observation?.quality === 'fresh')
        ? Number(population.reduce((sum, member) => sum + member.estimate.median, 0).toFixed(1)) : null;
    })(),
    progress_at: members.map(member => member._progress_at).filter(Boolean).sort(lexical).at(-1) || null,
  };
}

function memberStatusBehind(member, nowIso, indexAvailable) {
  const event = member._latest_event || {};
  const at = event.at;
  let progress = member._progress_at;
  if (member._status_reflected_report && member._status_reflected_report === member._latest_report_id) return false;
  if (member._progress_precedes_report && member._progress_precedes_report === member._latest_report_id) progress = null;
  const eventAt = member._latest_report_epoch ?? (at ? Date.parse(at) / 1000 : 0);
  const progressAt = member._progress_epoch ?? (progress ? Date.parse(progress) / 1000 : 0);
  return Boolean(indexAvailable
    && !['completed', 'deprecated'].includes(member.status)
    && !['completed', 'deprecated'].includes(member.terminal)
    && member.observation?.quality === 'fresh'
    && ['review_passed', 'reported_done'].includes(event.kind)
    && at
    && (!progress || eventAt > progressAt)
    && Date.parse(nowIso) / 1000 - eventAt >= 1800);
}

function detailMember(member, lane, nowIso) {
  const result = {};
  for (const [key, value] of Object.entries(member)) {
    if (!key.startsWith('_')) result[key] = clone(value);
  }
  const line = value => typeof value === 'string' && value.trim() ? value.trim() : null;
  return {
    ...result,
    worked: clone(lane.workedBySpec?.[member.spec_id] ?? null),
    lead_seat: clone(member.lead_seat ?? null),
    status_line: line(member.status_text),
    next_line: line(member.next_action_text),
    blocked_line: line(member.blockers_text),
    progress_at: clone(member._progress_at ?? null),
    latest_event: clone(member._latest_event ?? null),
    status_behind: memberStatusBehind(member, nowIso, lane.workIndex?.available !== false),
    criteria: clone(member.criteria || []),
    transitions: clone(member.transitions || []),
  };
}

function trailEntries(event) {
  const payload = event.payload || {};
  const prior = payload.prior || {};
  const next = payload.next || {};
  const base = { at: `${String(event.created_at || '').slice(0, 19)}Z`, spec_id: payload.spec_id };
  const entries = [];
  if (prior.status !== next.status && next.status === 'completed') entries.push({ ...base, kind: 'done' });
  else if (prior.status !== next.status && next.status === 'blocked') {
    entries.push({ ...base, kind: 'blocked', text: typeof next.blocked_line === 'string' ? next.blocked_line.slice(0, 160) : null });
  }
  if (Number.isInteger(prior.ac_checked) && Number.isInteger(next.ac_checked) && next.ac_checked > prior.ac_checked) {
    entries.push({ ...base, kind: 'checks', count: next.ac_checked - prior.ac_checked });
  }
  if (typeof prior.status_line === 'string' && typeof next.status_line === 'string' && next.status_line !== prior.status_line) {
    entries.push({ ...base, kind: 'note', text: next.status_line.slice(0, 160) });
  }
  return entries;
}

function progressTrail(events, specIds, limit = 6) {
  const selected = new Set(specIds);
  const trail = [];
  for (const event of events || []) {
    if (!selected.has(event.payload?.spec_id)) continue;
    for (const entry of trailEntries(event)) {
      trail.push(entry);
      if (trail.length === limit) return trail;
    }
  }
  return trail;
}

function syntheticTrailEvents(members) {
  return members.flatMap(member => (member.recent || []).map((entry, index) => {
    const createdAt = String(entry.at || '2026-11-20T12:00:00Z');
    const iso = createdAt.includes('.') ? createdAt : createdAt.replace(/Z$/, '.000Z');
    const prior = { status: member.status, status_line: 'Earlier synthetic fixture status.', ac_checked: member.ac_checked };
    const next = { status: member.status, status_line: member.status_text, ac_checked: member.ac_checked };
    if (entry.kind === 'done') { prior.status = 'in_progress'; next.status = 'completed'; }
    else if (entry.kind === 'blocked') { prior.status = 'in_progress'; next.status = 'blocked'; next.blocked_line = entry.text || null; }
    else if (entry.kind === 'checks') {
      prior.ac_checked = Math.max(0, Number(member.ac_checked || 0) - Number(entry.count || 1));
      next.ac_checked = Number(member.ac_checked || 0);
    }
    else if (entry.kind === 'note') {
      prior.status_line = 'Earlier synthetic fixture status.';
      next.status_line = entry.text || member.status_text;
    }
    return {
      event_id: `item:fixture:${member.spec_id}:${String(index).padStart(2, '0')}`,
      created_at: iso,
      payload: { spec_id: member.spec_id, prior, next },
    };
  })).sort((a, b) => lexical(String(b.created_at), String(a.created_at)) || lexical(String(b.event_id), String(a.event_id)));
}

function aggregateProgress(members, laneUpdatedAt) {
  const completed = members.filter(member => member.status === 'completed').length;
  const dropped = members.filter(member => member.status === 'deprecated').length;
  const unresolved = members.filter(member => ['missing', 'ambiguous'].includes(member.status)).length;
  const opened = members.filter(member => !['completed', 'deprecated', 'missing', 'ambiguous'].includes(member.status));
  const ac = members.filter(member => !['missing', 'ambiguous'].includes(member.status) && member.ac_total != null);
  const nonExempt = opened.filter(member => !member.estimate_exempt);
  const estimated = nonExempt.filter(member => member.estimate != null);
  const stamps = members.map(member => member.source_changed_at || member.progress_at).filter(Boolean);
  if (laneUpdatedAt) stamps.push(laneUpdatedAt);
  const openEstimate = estimated.length ? Object.fromEntries(['p25', 'p75', 'median'].map(key =>
    [key, Number(estimated.reduce((sum, member) => sum + Number(member.estimate[key] || 0), 0).toFixed(1))])) : null;
  return {
    members: members.map(inventoryMember),
    members_total: members.length,
    no_spec_reason: null,
    items_total: members.length,
    items_completed: completed,
    items_dropped: dropped,
    items_open: opened.length,
    items_unresolved: unresolved,
    ac_checked: ac.length ? ac.reduce((sum, member) => sum + (Number(member.ac_checked) || 0), 0) : null,
    ac_total: ac.length ? ac.reduce((sum, member) => sum + (Number(member.ac_total) || 0), 0) : null,
    ac_members: ac.length,
    open_estimate_h: openEstimate,
    open_estimated: estimated.length,
    estimate_complete: estimated.length === nonExempt.length,
    freshness_at: stamps.length ? stamps.sort(lexical).at(-1) : null,
    completion_pending: members.length > 0 && members.every(isDone) ? true : false,
    lead_reported_done: null,
    stale: false,
  };
}

function visibilityCounts(members) {
  const working = members.filter(member => !isDone(member) && member.seats?.some(seat => seat.activity === 'working')).length;
  return {
    unfinished: members.filter(member => !isDone(member) && !isDeprecated(member)).length,
    working,
    blocked: members.filter(member => member.status === 'blocked').length,
    needs_answer: members.reduce((sum, member) => sum + (Number(member.open_questions?.count) || 0), 0),
    checkpoint_deficient: members.filter(member => member.checkpoint_state !== 'valid').length,
    complete: { source: true, presence: true, questions: true },
  };
}

function pageLaneDetail(lane, options = {}) {
  const pageSize = 32;
  const nowIso = options.nowIso || '2026-11-20T12:00:00Z';
  const trailEvents = lane.trailEvents || [];
  const cloneValue = value => value == null ? value : JSON.parse(JSON.stringify(value));
  const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
  const isSpecId = value => typeof value === 'string' && /^spec_[A-Za-z0-9_-]+$/.test(value);
  const memberStatusBehind = (member, stamp, indexAvailable) => {
    const event = member._latest_event || {};
    const at = event.at;
    let progress = member._progress_at;
    if (member._status_reflected_report && member._status_reflected_report === member._latest_report_id) return false;
    if (member._progress_precedes_report && member._progress_precedes_report === member._latest_report_id) progress = null;
    const eventAt = member._latest_report_epoch ?? (at ? Date.parse(at) / 1000 : 0);
    const progressAt = member._progress_epoch ?? (progress ? Date.parse(progress) / 1000 : 0);
    return Boolean(indexAvailable
      && !['completed', 'deprecated'].includes(member.status)
      && !['completed', 'deprecated'].includes(member.terminal)
      && member.observation?.quality === 'fresh'
      && ['review_passed', 'reported_done'].includes(event.kind)
      && at
      && (!progress || eventAt > progressAt)
      && Date.parse(stamp) / 1000 - eventAt >= 1800);
  };
  const detailMember = member => {
    const result = {};
    for (const [key, value] of Object.entries(member)) {
      if (!key.startsWith('_')) result[key] = cloneValue(value);
    }
    const line = value => typeof value === 'string' && value.trim() ? value.trim() : null;
    return {
      ...result,
      worked: cloneValue(lane.workedBySpec?.[member.spec_id] ?? null),
      lead_seat: cloneValue(member.lead_seat ?? null),
      status_line: line(member.status_text),
      next_line: line(member.next_action_text),
      blocked_line: line(member.blockers_text),
      progress_at: cloneValue(member._progress_at ?? null),
      latest_event: cloneValue(member._latest_event ?? null),
      status_behind: memberStatusBehind(member, nowIso, lane.workIndex?.available !== false),
      criteria: cloneValue(member.criteria || []),
      transitions: cloneValue(member.transitions || []),
    };
  };
  const trailEntries = event => {
    const payload = event.payload || {};
    const prior = payload.prior || {};
    const next = payload.next || {};
    const base = { at: `${String(event.created_at || '').slice(0, 19)}Z`, spec_id: payload.spec_id };
    const entries = [];
    if (prior.status !== next.status && next.status === 'completed') entries.push({ ...base, kind: 'done' });
    else if (prior.status !== next.status && next.status === 'blocked') {
      entries.push({ ...base, kind: 'blocked', text: typeof next.blocked_line === 'string' ? next.blocked_line.slice(0, 160) : null });
    }
    if (Number.isInteger(prior.ac_checked) && Number.isInteger(next.ac_checked) && next.ac_checked > prior.ac_checked) {
      entries.push({ ...base, kind: 'checks', count: next.ac_checked - prior.ac_checked });
    }
    if (typeof prior.status_line === 'string' && typeof next.status_line === 'string' && next.status_line !== prior.status_line) {
      entries.push({ ...base, kind: 'note', text: next.status_line.slice(0, 160) });
    }
    return entries;
  };
  const progressTrail = (events, specIds, limit = 6) => {
    const selected = new Set(specIds);
    const trail = [];
    for (const event of events || []) {
      if (!selected.has(event.payload?.spec_id)) continue;
      for (const entry of trailEntries(event)) {
        trail.push(entry);
        if (trail.length === limit) return trail;
      }
    }
    return trail;
  };
  // criteria and transitions remain in the legacy detail projection.
  const keys = new Set([
    'blocked_line', 'checkpoint_state', 'checkpoint_deficiencies', 'checkpoint_revision',
    'checkpoint_policy_revision', 'checkpoint_changed_at', 'checkpoint_first_observed_at',
    'goal_text', 'goal_text_truncated', 'goal_source', 'progress_at', 'summary_line',
    'estimate_first', 'unblocks', 'recent', 'latest_event', 'status_behind', 'blockers_text',
  ]);
  const invalid = (code, message) => { const error = new Error(message); error.code = code; throw error; };
  const membersLimit = options.membersLimit ?? pageSize;
  const membersAfter = options.membersAfter ?? null;
  const goalIdFilter = options.goalId ?? null;
  const goals = (lane.goals || []).map(row => cloneValue(row));
  if (!Number.isSafeInteger(membersLimit) || membersLimit < 1 || membersLimit > pageSize) {
    invalid('work_lane_members_limit_invalid', 'Invalid member page limit');
  }
  if (membersAfter !== null && !isSpecId(membersAfter)) {
    invalid('work_lane_members_cursor_invalid', 'Invalid member cursor');
  }
  if (goalIdFilter !== null && !goals.some(goal => goal.goal_id === goalIdFilter)) {
    invalid('work_lane_goal_filter_invalid', 'Invalid goal filter');
  }
  const allMembers = (lane.members || [])
    .filter(member => goalIdFilter === null || member.goal_id === goalIdFilter)
    .slice().sort((a, b) => compare(a.spec_id, b.spec_id));
  const total = allMembers.length;
  const remaining = allMembers.filter(member => membersAfter === null || compare(member.spec_id, membersAfter) > 0);
  const page = remaining.slice(0, membersLimit).map(member => ({
    ...detailMember(member),
    unblocks: cloneValue((member.unblocks || []).slice(0, 4)),
    recent: progressTrail(trailEvents, [member.spec_id]),
  }));
  const membersNext = remaining.length > page.length ? page.at(-1)?.spec_id || null : null;
  const legacyPage = page.map(member => Object.fromEntries(Object.entries(member).filter(([key]) => !keys.has(key))));
  const fullProjection = cloneValue(lane.projection || {});
  const projection = {
    ...fullProjection,
    members: legacyPage,
    members_total: total,
    members_next: membersNext,
    recent: progressTrail(trailEvents, (lane.members || []).map(member => member.spec_id)),
  };
  if (goalIdFilter !== null) {
    const selected = goals.find(goal => goal.goal_id === goalIdFilter);
    const goalMembers = (lane.members || []).filter(member => member.goal_id === goalIdFilter);
    const source = lane.goalSources?.[goalIdFilter] || {};
    projection.selected_goal = selected ? {
      ...selected,
      visibility_counts: {
        unfinished: goalMembers.filter(member => member.status !== 'completed' && member.status !== 'deprecated').length,
        working: goalMembers.filter(member => member.status !== 'completed' && member.seats?.some(seat => seat.activity === 'working')).length,
        blocked: goalMembers.filter(member => member.status === 'blocked').length,
        needs_answer: goalMembers.reduce((sum, member) => sum + (Number(member.open_questions?.count) || 0), 0),
        checkpoint_deficient: goalMembers.filter(member => member.checkpoint_state !== 'valid').length,
        complete: { source: true, presence: true, questions: true },
      },
      ...cloneValue(source.detail || {}),
      summary_line: source.detail?.summary_line || null,
      recent: progressTrail(trailEvents, goalMembers.map(member => member.spec_id)),
      source: { epic_id: source.epicId || null, revision: source.revision || null },
    } : null;
  }
  const { members_json: _membersJson, hue_open_order: _hueOpenOrder, ...storedLane } = lane.storedLane || {};
  return {
    type: 'work_lanes.show.ok',
    lane: { ...cloneValue(storedLane), members: page.map(member => member.spec_id) },
    projection,
    events: cloneValue(lane.events || []),
    updates: cloneValue(lane.updates || []),
    members: page,
    members_total: total,
    members_next: membersNext,
    explicit_member_positions: Object.fromEntries(page.flatMap(member => {
      const index = (lane.storedLane?.members || []).indexOf(member.spec_id);
      return index < 0 ? [] : [[member.spec_id, index]];
    })),
    detail_revision: lane.detailRevision || lane.inventory?.detail_revision || null,
    work_index: cloneValue(lane.workIndex || { available: true, root_configured: true, snapshot_at: null, last_sweep_at: null, error: null }),
  };
}

function makeLane({ laneId, title, revision: revisionValue = REVISION_A, state = 'active',
  members = [], goals = [], activity = 'idle', icon = 'flask', hue = 'violet',
  parked = null, questionPreviewIds = [], inventoryMemberOverride = null, trailEvents: suppliedTrailEvents = null,
  detailRevision = revisionValue, summary = 'Synthetic work lane fixture.',
  progressAt = '2026-11-20T12:00:00Z' } = {}) {
  const sourceMembers = clone(members || []);
  const trailEvents = suppliedTrailEvents == null ? syntheticTrailEvents(sourceMembers) : clone(suppliedTrailEvents);
  const orderedGoals = clone(goals || []).sort((a, b) => a.rank - b.rank || lexical(a.goal_id, b.goal_id));
  orderedGoals.forEach((goal, index) => {
    if (orderedGoals.length <= 64) goal.pos = index;
    else delete goal.pos;
  });
  const unplaced = sourceMembers.filter(member => !member.goal_id);
  const loose = unplaced.filter(member => !isDeprecated(member));
  const groups = orderedGoals.map(goal => ({ goal, members: sourceMembers.filter(member => member.goal_id === goal.goal_id) }));
  const goalClasses = groups.map(group => goalClass(group.members));
  const defaultClass = loose.length ? goalClass(loose) : null;
  const fullClasses = [...goalClasses, ...(defaultClass ? [defaultClass] : [])];
  const moving = fullClasses.filter(letter => letter === 'w' || letter === 'm').length;
  const blocked = fullClasses.filter(letter => letter === 'b').length;
  const queued = fullClasses.filter(letter => letter === 'q').length;
  const done = fullClasses.filter(letter => letter === 'd').length;
  const completeGoals = orderedGoals.map((goal, index) => goalRecord(goal, groups[index].members, goalClasses[index], { detail: true }));
  const selected = orderedGoals.length <= 8 ? orderedGoals : selectGoals(groups, goalClasses);
  const selectedIndexes = selected.map(goal => orderedGoals.findIndex(item => item.goal_id === goal.goal_id));
  const inventoryGoals = selectedIndexes.map(index => goalRecord(orderedGoals[index], groups[index].members, goalClasses[index]));
  const inventoryGoalClasses = selectedIndexes.map(index => goalClasses[index]).join('') + (defaultClass || '-');
  const detailGoalClasses = goalClasses.join('') + (defaultClass || '-');
  const questionIds = new Set([
    ...questionPreviewIds,
    ...sourceMembers.filter(member => Number(member.open_questions?.count) > 0).map(member => member.spec_id),
  ]);
  let preview = sourceMembers.slice(0, INLINE_MEMBERS).map(inventoryMember);
  const questionMember = sourceMembers.find(member => questionIds.has(member.spec_id));
  if (questionMember && !preview.some(member => member.spec_id === questionMember.spec_id) && preview.length) {
    preview[preview.length - 1] = inventoryMember(questionMember);
  }
  preview = preview.map(member => {
    const full = sourceMembers.find(row => row.spec_id === member.spec_id);
    return { ...member, open_questions: questionIds.has(member.spec_id)
      ? clone(full?.open_questions || { count: 0, first_question_id: null })
      : { count: 0, first_question_id: null } };
  });
  if (inventoryMemberOverride) preview = inventoryMemberOverride.map(inventoryMember);

  const road = orderedGoals.length <= 64 ? goalClasses.join('') : null;
  const progress = aggregateProgress(sourceMembers, progressAt);
  progress.members = clone(preview);
  const goalProjection = {
    goals: clone(inventoryGoals),
    goals_total: orderedGoals.length,
    ...(road == null ? {} : { goal_road: road }),
    ...(loose.length ? { default_goal: defaultGoalProjection(unplaced, defaultClass) } : {}),
    goal_classes: inventoryGoalClasses,
    goal_counts: [moving, blocked, queued, done],
    detail_revision: detailRevision,
  };
  const laneMetadata = {
    lane_id: laneId,
    title,
    summary,
    icon,
    hue,
    state,
    state_reason: state === 'paused' ? 'operator_parked' : null,
    blocker: null,
    ...(parked ? { parked: clone(parked) } : {}),
    owner_kind: 'operator',
    version: 1,
    updated_at: progressAt,
    first_admitted_at: progressAt,
    done_at: null,
    lead: null,
    visible_chat: { stream_id: null, generation: null, kind: 'session', available: 'unavailable' },
    last_update: null,
    activity: state === 'paused' ? undefined : activity,
    progress_at: progressAt,
    state_at: state === 'paused' ? progressAt : null,
    detail_revision: detailRevision,
  };
  if (state === 'paused' || activity == null) delete laneMetadata.activity;
  const inventory = { ...laneMetadata, ...clone(progress), ...goalProjection };
  const storedLane = {
    lane_id: laneId,
    stream_id: LANE_COMPOSITE_ID,
    parent_lane_id: null,
    phase: state === 'paused' ? 'waiting' : 'execution',
    work_state: state,
    bound_stream_id: null,
    bound_generation: null,
    bound_backend_kind: null,
    summary,
    version: 1,
    created_at: progressAt,
    updated_at: progressAt,
    parked_at: parked?.at || null,
    parked_reason: parked?.reason || null,
    work_state_reason: state === 'paused' ? 'operator_parked' : null,
    blocker: null,
    owner_kind: 'operator',
    title,
    icon,
    visible_chat_stream_id: null,
    visible_chat_generation: null,
    adoption_key: ADOPTION_KEYS.get(laneId) || null,
    first_admitted_at: progressAt,
    done_at: null,
    last_update_id: null,
    members: sourceMembers.map(member => member.spec_id),
    members_json: JSON.stringify(sourceMembers.map(member => member.spec_id)),
    no_spec_reason: null,
    hue_open_order: 1,
  };
  if (!storedLane.adoption_key) throw new Error(`fixture lane identity was not derived by fixtureLaneId: ${laneId}`);
  const detailProgress = { ...aggregateProgress(sourceMembers, progressAt), members: clone(sourceMembers) };
  const detailProjection = {
    ...laneMetadata,
    ...detailProgress,
    lane_id: laneId,
    goals: clone(completeGoals),
    goals_total: orderedGoals.length,
    ...(road == null ? {} : { goal_road: road }),
    ...(loose.length ? { default_goal: defaultGoalProjection(unplaced, defaultClass, { detail: true }) } : {}),
    goal_classes: detailGoalClasses,
    goal_counts: [moving, blocked, queued, done],
    detail_revision: detailRevision,
    visibility_counts: visibilityCounts(sourceMembers),
    recent: progressTrail(trailEvents, sourceMembers.map(member => member.spec_id)),
  };

  return {
    detailRevision,
    inventory,
    goals: clone(completeGoals),
    goalSources: Object.fromEntries(orderedGoals.map(goal => [goal.goal_id, {
      epicId: goal.epic_id,
      revision: revision(`goal-source:${goal.epic_id}`),
      detail: {
        summary_text: `Synthetic summary for ${goal.title}.`,
        summary_text_truncated: false,
        summary_line: 'Synthetic goal summary.',
        goal_text: 'Synthetic completion condition.',
        goal_text_truncated: false,
      },
    }])),
    members: sourceMembers,
    trailEvents,
    workedBySpec: Object.fromEntries(sourceMembers.map(member => [member.spec_id, clone(member.worked ?? null)])),
    projection: detailProjection,
    storedLane,
    events: [],
    updates: [],
    workIndex: { available: true, root_configured: true, snapshot_at: progressAt, last_sweep_at: progressAt, error: null },
  };
}

function selectGoals(groups, classes) {
  const group = letter => letter === 'w' || letter === 'm' ? 0 : letter === 'b' ? 1 : 2;
  return groups.map((row, index) => ({ ...row.goal, _index: index }))
    .sort((a, b) => group(classes[a._index]) - group(classes[b._index])
      || a.rank - b.rank || lexical(a.goal_id, b.goal_id))
    .slice(0, 8)
    .sort((a, b) => a.rank - b.rank || lexical(a.goal_id, b.goal_id))
    .map(({ _index, ...goal }) => goal);
}

function makeL11({ revision: revisionValue = REVISION_A, previewQuestionIds = [], offPreviewQuestionIds = [],
  completedSpecIds = [], extraMembers = [], includeFinishedEarlier = false, movedSpecId = null,
  swapFirstTwoGoals = false, trailEvents = null } = {}) {
  // This shape matches the daemon's sent positions: all six working/moving
  // goals, then the first two blocked goals. The preview question sits in the
  // queued goal at position 7; goal 8 is working and goal 10 is omitted. E2's
  // completed loose member supplies the default-goal `d` class.
  const classes = ['b', 'b', 'w', 'm', 'q', 'w', 'w', 'b', 'w', 'b', 'w'];
  const assignments = Array.from({ length: 40 }, (_, index) => index % 11);
  assignments[7] = 7;
  assignments[9] = 2;
  assignments[20] = 2;
  const goals = Array.from({ length: 11 }, (_, pos) => {
    const rank = swapFirstTwoGoals ? (pos === 0 ? 1 : pos === 1 ? 0 : pos) : pos;
    const epicId = `epic_fixture_l11_goal_${String(pos + 1).padStart(2, '0')}`;
    return { goal_id: goalId(epicId), title: `Fixture goal ${pos + 1}`, rank, pos, epic_id: epicId };
  }).sort((a, b) => a.rank - b.rank || lexical(a.goal_id, b.goal_id));
  const goalByPosition = new Map(goals.map(goal => [Number(goal.title.match(/(\d+)$/)?.[1]) - 1, goal]));
  const members = Array.from({ length: 40 }, (_, index) => {
    const specId = `spec_gate__next_lanes__${String(index).padStart(2, '0')}`;
    const position = assignments[index];
    const goal = goalByPosition.get(position);
    let status = classes[position] === 'b' ? 'blocked'
      : classes[position] === 'm' ? 'analysis'
        : classes[position] === 'd' ? 'completed'
          : classes[position] === 'q' ? (index % 2 ? 'backlog' : 'ready_for_dev')
            : 'in_progress';
    const seats = classes[position] === 'w' && index === assignments.findIndex(item => item === position)
      ? [{ stream_id: `fixture:worker-l11-${String(position).padStart(2, '0')}`, role: 'worker', host: 'fixture',
        visibility: 'default', parent_stream_id: null, activity: 'working', last_activity: '2026-11-20T12:00:00Z' }]
      : [];
    const openQuestionIds = [];
    if (previewQuestionIds.length && specId === 'spec_gate__next_lanes__07') openQuestionIds.push(...previewQuestionIds);
    if (offPreviewQuestionIds.length && specId === 'spec_gate__next_lanes__20') openQuestionIds.push(...offPreviewQuestionIds);
    return makeMember(specId, goal.goal_id, {
      title: `Fixture spec ${String(index).padStart(2, '0')}`,
      status,
      terminal: status === 'completed' ? 'completed' : null,
      seats,
      open_questions: openQuestionIds.length
        ? { count: openQuestionIds.length, first_question_id: openQuestionIds[0] }
        : { count: 0, first_question_id: null },
      ...(completedSpecIds.includes(specId) ? { status: 'completed', terminal: 'completed', seats: [] } : {}),
    });
  }).filter(member => member.spec_id !== movedSpecId);
  members.push(...clone(extraMembers));
  if (includeFinishedEarlier) members.push(makeMember('spec_gate__next_lanes_finished_earlier', null, {
    title: 'Fixture finished earlier spec', status: 'completed', terminal: 'completed', seats: [],
  }));
  for (const member of members) {
    if (previewQuestionIds.length && member.spec_id === 'spec_gate__next_lanes__07') {
      member.open_questions = { count: previewQuestionIds.length, first_question_id: previewQuestionIds[0] };
    }
    if (offPreviewQuestionIds.length && member.spec_id === 'spec_gate__next_lanes__20') {
      member.open_questions = { count: offPreviewQuestionIds.length, first_question_id: offPreviewQuestionIds[0] };
    }
  }
  const base = makeLane({ laneId: L11_ID, title: 'Fixture lane eleven', revision: revisionValue, detailRevision: revisionValue,
    state: 'active', members, goals, activity: 'working', icon: 'flask', hue: 'violet',
    questionPreviewIds: previewQuestionIds.length ? ['spec_gate__next_lanes__07'] : [], trailEvents });
  base.questionSpecIds = { preview: 'spec_gate__next_lanes__07', offPreview: 'spec_gate__next_lanes__20' };
  return base;
}

function makeL11RevisionB({ movedSpecId = null } = {}) {
  return makeL11({ revision: REVISION_B, swapFirstTwoGoals: true, movedSpecId });
}

function makeOrdinaryLane({ laneId = ORDINARY_A_ID, title = 'Fixture ordinary lane', goalTitle = 'Fixture ordinary goal',
  memberCount = 3, noGoal = false, questionSpecIds = [], goalId: suppliedGoalId = null, revision: revisionValue = REVISION_A,
  members: suppliedMembers = null, state = 'active', activity = 'idle', icon = 'cube', hue = 'mint',
  completedSpecIds = [], questionIdsBySpec = {} } = {}) {
  const goal = noGoal ? null : {
    goal_id: suppliedGoalId || goalId(`epic_fixture_${laneId.replace(/[^a-z0-9_]/gi, '_')}_goal_01`),
    title: goalTitle,
    rank: 0,
    pos: 0,
  };
  const members = suppliedMembers ? clone(suppliedMembers) : Array.from({ length: Math.max(memberCount, questionSpecIds.length) }, (_, index) => {
    const specId = questionSpecIds[index] || `spec_gate__next_lanes_${laneId.replace(/^wl-fixture-/, '').replace(/-/g, '_')}_${String(index).padStart(2, '0')}`;
    return makeMember(specId, noGoal ? null : goal.goal_id, {
      title: `Fixture ${title.toLowerCase()} spec ${String(index).padStart(2, '0')}`,
      ...(completedSpecIds.includes(specId) ? { status: 'completed', terminal: 'completed' } : {}),
    });
  });
  for (const member of members) {
    member.goal_id = noGoal ? null : suppliedGoalId || goal.goal_id;
    const questionIds = questionIdsBySpec[member.spec_id];
    if (Array.isArray(questionIds) && questionIds.length) {
      member.open_questions = { count: questionIds.length, first_question_id: questionIds[0] };
    } else if (typeof questionIds === 'string' && questionIds) {
      member.open_questions = { count: 1, first_question_id: questionIds };
    }
  }
  const goals = goal ? [goal] : [];
  return makeLane({ laneId, title, revision: revisionValue, detailRevision: revisionValue, state, members, goals,
    activity, icon, hue, parked: state === 'paused' ? { at: '2026-11-20T12:00:00Z', reason: 'Synthetic fixture pause.' } : null });
}

function makeParkedLane({ questionIds = [] } = {}) {
  const goal = { goal_id: goalId('epic_fixture_parked_goal_01'), title: 'Fixture parked goal', rank: 0, pos: 0 };
  const members = Array.from({ length: 12 }, (_, index) => makeMember(`spec_gate__next_lanes_parked_${String(index).padStart(2, '0')}`, goal.goal_id, {
    title: `Fixture parked spec ${String(index + 1).padStart(2, '0')}`,
    status: index === 0 ? 'analysis' : 'ready_for_dev',
    open_questions: index === 8 && questionIds.length
      ? { count: questionIds.length, first_question_id: questionIds[0] }
      : { count: 0, first_question_id: null },
  }));
  const lane = makeLane({ laneId: PARKED_ID, title: 'Fixture parked lane', revision: REVISION_A, state: 'paused', members,
    goals: [goal], activity: null, icon: 'leaf', hue: 'lavender', parked: { at: '2026-11-20T12:00:00Z', reason: 'Synthetic fixture pause.' } });
  lane.questionSpecId = 'spec_gate__next_lanes_parked_08';
  return lane;
}

function makeQuietLane() {
  return makeLane({ laneId: QUIET_ID, title: 'Fixture quiet lane', revision: REVISION_A, state: 'active', members: [],
    goals: [], activity: 'idle', icon: 'eye', hue: 'sky' });
}

function makeL65({ questionId = null } = {}) {
  const goals = Array.from({ length: 63 }, (_, index) => {
    const pos = index;
    const epicId = `epic_fixture_l65_goal_${String(index + 1).padStart(2, '0')}`;
    return { goal_id: goalId(epicId), title: `Fixture L65 goal ${String(index + 1).padStart(2, '0')}`,
      rank: index, pos: null, epic_id: epicId };
  });
  let twinEpics = ['epic_fixture_l65_tie_zulu', 'epic_fixture_l65_tie_alpha'];
  if (lexical(goalId(twinEpics[0]), goalId(twinEpics[1])) > 0) twinEpics = twinEpics.reverse();
  goals.push({ goal_id: goalId(twinEpics[0]), title: 'Zulu fixture tie', rank: 63, pos: null, epic_id: twinEpics[0] });
  goals.push({ goal_id: goalId(twinEpics[1]), title: 'Alpha fixture tie', rank: 63, pos: null, epic_id: twinEpics[1] });
  const questionSpecId = 'spec_gate__next_lanes_l65_02';
  const members = goals.map((goal, index) => makeMember(`spec_gate__next_lanes_l65_${String(index).padStart(2, '0')}`, goal.goal_id, {
    title: `Fixture L65 spec ${String(index + 1).padStart(2, '0')}`,
    status: index < 2 ? 'blocked' : 'ready_for_dev',
    ...(questionId && index === 2 ? { open_questions: { count: 1, first_question_id: questionId } } : {}),
  }));
  members.push(makeMember('spec_gate__next_lanes_l65_default', null, {
    title: 'Fixture L65 no-goal spec', status: 'ready_for_dev',
  }));
  const lane = makeLane({ laneId: L65_ID, title: 'Fixture L65 lane', revision: REVISION_A, state: 'active', members, goals,
    activity: 'idle', icon: 'columns', hue: 'sand' });
  lane.questionSpecId = questionSpecId;
  return lane;
}

function inventoryFrame(lanes = [], options = {}) {
  if (!Array.isArray(lanes)) throw new TypeError('inventoryFrame requires complete lane rows');
  const rows = lanes.map(lane => clone(lane.inventory || lane)).filter(row => row.state !== 'done');
  for (const row of rows) {
    if (Object.prototype.hasOwnProperty.call(row, 'progress')) {
      throw new Error(`inventory lane ${row.lane_id} contains normalized nested progress instead of daemon-flat fields`);
    }
    if (!Array.isArray(row.members) || !Number.isSafeInteger(row.members_total)) {
      throw new Error(`inventory lane ${row.lane_id} is missing flat member preview fields`);
    }
  }
  const orderedRows = rows.sort((a, b) => {
    const stateOrder = { blocked: 0, active: 1, paused: 2 };
    return (stateOrder[a.state] ?? 9) - (stateOrder[b.state] ?? 9)
      || lexical(String(b.updated_at || ''), String(a.updated_at || ''))
      || lexical(a.lane_id, b.lane_id);
  });
  const total = orderedRows.length;
  if (options.open != null && options.open !== total) {
    throw new Error(`inventory open count ${options.open} does not match complete lane count ${total}`);
  }
  const counts = orderedRows.reduce((out, row) => {
    if (row.state === 'active') out.active += 1;
    else if (row.state === 'paused') out.paused += 1;
    else if (row.state === 'blocked') out.blocked += 1;
    return out;
  }, { open: total, active: 0, paused: 0, blocked: 0 });
  const truncated = total > 64;
  if (options.truncated != null && options.truncated !== truncated) {
    throw new Error(`inventory truncated flag ${options.truncated} does not match ${total} complete lane rows`);
  }
  return {
    type: 'work_lanes.inventory',
    lanes: orderedRows.slice(0, 64),
    counts,
    truncated,
    generated_at: '2026-11-20T12:00:00Z',
    work_index: { available: true, root_configured: true, snapshot_at: '2026-11-20T12:00:00Z', last_sweep_at: '2026-11-20T12:00:00Z', error: null },
  };
}

function questionFixture(questionId, specId, { producerStreamId = 'local:web-gate-1', title = 'Synthetic fixture question',
  body = 'Choose a synthetic fixture result.', options = null, allowCustom = false } = {}) {
  const choices = Array.isArray(options) && options.length
    ? clone(options)
    : [{ label: 'Keep fixture value', value: 'keep' }, { label: 'Change fixture value', value: 'change' }];
  return {
    question_id: questionId,
    producer_stream_id: producerStreamId,
    ...(specId ? { spec_id: specId } : {}),
    title,
    body,
    response_mode: 'single_choice',
    allow_custom: allowCustom,
    options: choices,
  };
}

module.exports = {
  INLINE_MEMBERS,
  L11_ID,
  L65_ID,
  NO_GOAL_ID,
  ORDINARY_A_ID,
  ORDINARY_B_ID,
  PAGE_SIZE,
  PARKED_ID,
  QUIET_ID,
  REVISION_A,
  REVISION_B,
  REVISION_C,
  REVISION_D,
  fixtureLaneId,
  goalId,
  inventoryFrame,
  inventoryMember,
  pageLaneDetail,
  makeL11,
  makeL11RevisionB,
  makeL65,
  makeMember,
  makeOrdinaryLane,
  makeParkedLane,
  makeQuietLane,
  questionFixture,
};
