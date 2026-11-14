// Work-lanes projection v1 (spec_pentacle__first_class_work_lanes_2026_10 D7/D8).
//
// The daemon owns lane identity, presented state, count, order and tap target.
// This slice only validates the wire frame, holds it, and answers presentation
// questions. Clients never reorder `lanes[]` and never derive the header count
// from sessions. Contract fixture: tests/fixtures/work-lanes-inventory.json.

export type WorkLaneState = 'active' | 'paused' | 'blocked';
export type WorkLaneVisibleChatAvailability = 'open' | 'history' | 'unavailable';
export type WorkLaneUpdateKind =
  | 'major_decision' | 'lane_started' | 'lane_completed' | 'lane_blocked' | 'lane_unblocked' | 'milestone';

export const WORK_LANE_OPEN_STATES: readonly WorkLaneState[] = ['active', 'paused', 'blocked'];
/** Keys in lane_icons.json; its ordered keyword table is the resolver authority. */
export const LANE_ICON_KEYS = [
  'pentacle', 'flask', 'cube', 'phone', 'bot', 'bug', 'truck', 'wrench', 'columns', 'rocket',
  'hammer', 'eye', 'camera', 'cursor', 'home', 'book', 'chart', 'shield', 'key', 'leaf',
  'briefcase', 'headset', 'film', 'server',
] as const;
export type LaneIconKey = typeof LANE_ICON_KEYS[number];
/** Ordered keys mirror the canonical lane_hues.json; parity is tested. */
export const LANE_HUE_KEYS = ['violet', 'cyan', 'yellow', 'pink', 'mint', 'purple', 'sky', 'lime', 'magenta', 'aqua', 'sand', 'orchid', 'teal', 'rose', 'lavender', 'chartreuse'] as const;
export type LaneHueKey = typeof LANE_HUE_KEYS[number];
export const WORK_LANE_UPDATE_KINDS: readonly WorkLaneUpdateKind[] = [
  'major_decision', 'lane_started', 'lane_completed', 'lane_blocked', 'lane_unblocked', 'milestone',
];

export type WorkLaneLead = {
  stream_id: string;
  generation: string | null;
  qualifies: boolean;
  status: string | null;
  visibility: string | null;
  presence: { online: boolean; working: boolean; capture_liveness: string | null; last_activity: string | null };
  status_card: {
    goal: string | null; active_step: string | null; update: string | null;
    eta_at: string | null; eta_set_at: string | null; updated_at: string | null;
  };
  eta_stale: boolean;
};

export type WorkLaneVisibleChat = {
  stream_id: string;
  generation: string | null;
  kind: 'composite' | 'session';
  available: WorkLaneVisibleChatAvailability;
};

export type WorkLaneEstimate = { p25: number; p75: number; median: number; as_of: string | null; provisional: boolean };

/** A seat whose work assignment is this member spec. Activity is an observation, never progress. */
export type WorkLaneSeat = {
  stream_id: string;
  role: string | null;
  host: string | null;
  visibility: string | null;
  parent_stream_id: string | null;
  activity: 'working' | 'idle' | 'offline' | 'unknown';
  last_activity: string | null;
};

/** Detail-only (work_lanes.show) goal facts over the goal's full membership. */
export type WorkLaneGoalDetail = {
  moving?: number; blocked?: number; working?: boolean;
  /** Hours left over open, non-exempt members; null when unknown or incomplete. */
  left_h?: number | null;
  progress_at?: string | null;
};
export type WorkLaneGoalProgress = { state: 'not_started' | 'in_progress' | 'done'; done: number; total: number } & WorkLaneGoalDetail;
export type WorkLaneGoal = WorkLaneGoalProgress & {
  goal_id: string; title: string; rank: number; pos?: number; target_date?: string; icon?: LaneIconKey | null;
};
export type WorkLaneGoalClass = 'done' | 'working' | 'moving' | 'blocked' | 'queued';
/** One entry of real progress, newest first; titles come from the member. */
export type WorkLaneProgressEntry =
  | { at: string; spec_id: string; kind: 'note'; text: string | null }
  | { at: string; spec_id: string; kind: 'checks'; count: number }
  | { at: string; spec_id: string; kind: 'done' }
  | { at: string; spec_id: string; kind: 'blocked'; text: string | null };

export type WorkLaneMember = {
  /** Optional released policy fields survive normalization, including retired fields. */
  [key: string]: unknown;
  spec_id: string;
  goal_id?: string | null;
  title: string | null;
  status: string | null;
  terminal: 'completed' | 'deprecated' | null;
  ac_checked: number | null;
  ac_total: number | null;
  estimate: WorkLaneEstimate | null;
  estimate_exempt: boolean;
  status_text: string | null;
  next_action_text: string | null;
  blockers_text?: string | null;
  checkpoint_state?: 'valid' | 'invalid' | 'unverifiable' | null;
  checkpoint_deficiencies?: Array<{ field: string; code: string; fix: string }>;
  checkpoint_revision?: string | null;
  checkpoint_policy_revision?: string | null;
  checkpoint_changed_at?: string | null;
  checkpoint_first_observed_at?: string | null;
  goal_text?: string | null;
  goal_text_truncated?: boolean;
  /** Whole authored label when it passes the shared readable-fields contract, else null. */
  status_line?: string | null;
  next_line?: string | null;
  /** Detail-only (work_lanes.show), same rule for the Blockers label. */
  blocked_line?: string | null;
  /** Detail-only derived facts: last real progress, readable summary, first estimate, what it unblocks, its trail. */
  progress_at?: string | null;
  summary_line?: string | null;
  estimate_first?: { p25: number; p75: number; median: number } | null;
  unblocks?: Array<{ spec_id: string; title: string | null }>;
  recent?: WorkLaneProgressEntry[];
  worked?: {
    elapsed_h: number | null; censored: boolean; active_h: number | null;
    seats: number; qa_rounds: number; as_of: string;
  } | null;
  lead_seat?: {
    stream_id: string; host: string; provider: string | null;
    activity: WorkLaneSeat['activity']; eta_at: string | null; active_step: string | null;
  } | null;
  /** Detail-only facts from work_lanes.show, in source order. */
  criteria?: Array<{ title: string; disposition: 'open' | 'done' | 'waived' | 'tracked' | 'parked' }>;
  transitions?: Array<{ status: string; at: string }>;
  source_changed_at: string | null;
  observation: { quality: string | null; observed_at: string | null; error: string | null } | null;
  /** Additive attribution; empty/zero from a daemon that predates it. */
  seats: WorkLaneSeat[];
  seats_total: number;
  open_questions: { count: number; first_question_id: string | null };
};

/** Additive progress facts (work-lanes.md, file-derived work); null for a v1 lane without them. */
export type WorkLaneProgress = {
  members: WorkLaneMember[];
  members_total: number | null;
  members_next?: string | null;
  no_spec_reason: string | null;
  items_total: number | null;
  items_completed: number | null;
  items_dropped: number | null;
  items_open: number | null;
  items_unresolved: number | null;
  ac_checked: number | null;
  ac_total: number | null;
  ac_members: number | null;
  open_estimate_h: { p25: number; p75: number; median: number } | null;
  open_estimated: number | null;
  estimate_complete: boolean | null;
  freshness_at: string | null;
};

export type WorkIndexStatus = {
  available: boolean;
  root_configured: boolean;
  snapshot_at: string | null;
  last_sweep_at: string | null;
  error: string | null;
};

export type WorkLane = {
  visibility_counts?: Record<string, unknown>;
  selected_goal?: WorkLaneGoal & Record<string, unknown> & { summary_line?: string | null; recent?: WorkLaneProgressEntry[] };
  lane_id: string;
  goals?: WorkLaneGoal[];
  goals_total?: number;
  /** Complete named-goal classes in rank/id order, or null for absent/invalid input. */
  goal_road: string | null;
  default_goal?: WorkLaneGoalProgress | null;
  /** One class letter per goal, then the default goal; read it only through workLaneGoalClasses. */
  goal_classes?: string | null;
  /** Over every goal and the default goal; working goals count as moving. */
  goal_counts?: { moving: number; blocked: number; queued: number; done: number } | null;
  /** Newest real progress over the lane's full membership. */
  progress_at?: string | null;
  /** When the lane entered its current state. */
  state_at?: string | null;
  /** Detail-only progress trail for the whole lane. */
  recent?: WorkLaneProgressEntry[];
  detail_revision?: string | null;
  title: string;
  icon?: LaneIconKey | null;
  hue?: LaneHueKey | null;
  summary: string;
  state: WorkLaneState;
  parked?: { at: string | null; reason: string | null };
  state_reason: string | null;
  blocker: string | null;
  owner_kind: 'operator' | 'fd' | null;
  version: number | null;
  updated_at: string | null;
  first_admitted_at: string | null;
  done_at: string | null;
  lead: WorkLaneLead | null;
  visible_chat: WorkLaneVisibleChat;
  last_update: { update_id: string; kind: string; event_id: number | null; ts: string | null } | null;
  progress: WorkLaneProgress | null;
};

export type WorkLanesCounts = { open: number; active: number; paused: number; blocked: number };

export type WorkLanesInventory = {
  lanes: WorkLane[];
  counts: WorkLanesCounts;
  truncated: boolean;
  generated_at: string | null;
  work_index: WorkIndexStatus | null;
};

export type WorkLaneTapTarget =
  | { action: 'open_chat'; stream_id: string }
  | { action: 'history'; stream_id: string; generation: string }
  | { action: 'unavailable' };

export type PentacleLaneUpdate = {
  update_id: string;
  lane_id: string;
  kind: WorkLaneUpdateKind;
  summary: string;
  source: { type: string; id: string; grouped_ids?: string[] };
  state: string | null;
  prior_state: string | null;
  owner_kind: string | null;
  title: string | null;
  ts: string | null;
};

type Rec = Record<string, unknown>;
const isRec = (value: unknown): value is Rec => !!value && typeof value === 'object' && !Array.isArray(value);
const str = (value: unknown): string | null => (typeof value === 'string' ? value : null);
const nonEmpty = (value: unknown): string | null => (typeof value === 'string' && value.trim() ? value : null);
const finite = (value: unknown): number | null => (typeof value === 'number' && Number.isFinite(value) ? value : null);

export function emptyWorkLanesInventory(): WorkLanesInventory {
  return { lanes: [], counts: { open: 0, active: 0, paused: 0, blocked: 0 }, truncated: false, generated_at: null, work_index: null };
}

function normalizeLead(raw: unknown): WorkLaneLead | null {
  if (!isRec(raw)) return null;
  const streamId = nonEmpty(raw.stream_id);
  if (!streamId) return null;
  const presence = isRec(raw.presence) ? raw.presence : {};
  const card = isRec(raw.status_card) ? raw.status_card : {};
  return {
    stream_id: streamId,
    generation: str(raw.generation),
    qualifies: raw.qualifies === true,
    status: str(raw.status),
    visibility: str(raw.visibility),
    presence: {
      online: presence.online === true,
      working: presence.working === true,
      capture_liveness: str(presence.capture_liveness),
      last_activity: str(presence.last_activity),
    },
    status_card: {
      goal: str(card.goal), active_step: str(card.active_step), update: str(card.update),
      eta_at: str(card.eta_at), eta_set_at: str(card.eta_set_at), updated_at: str(card.updated_at),
    },
    eta_stale: raw.eta_stale === true,
  };
}

function normalizeVisibleChat(raw: unknown): WorkLaneVisibleChat {
  const rec = isRec(raw) ? raw : {};
  const available = rec.available === 'open' || rec.available === 'history' ? rec.available : 'unavailable';
  return {
    stream_id: str(rec.stream_id) ?? '',
    generation: str(rec.generation),
    kind: rec.kind === 'composite' ? 'composite' : 'session',
    available,
  };
}

const boolOrNull = (value: unknown): boolean | null => (typeof value === 'boolean' ? value : null);

function normalizeRange(raw: unknown): { p25: number; p75: number; median: number } | null {
  if (!isRec(raw)) return null;
  const p25 = finite(raw.p25), p75 = finite(raw.p75), median = finite(raw.median);
  return p25 === null || p75 === null || median === null ? null : { p25, p75, median };
}

const SEAT_ACTIVITY = new Set(['working', 'idle', 'offline', 'unknown']);
const CRITERION_DISPOSITIONS = new Set(['open', 'done', 'waived', 'tracked', 'parked']);

function normalizeWorked(raw: unknown): WorkLaneMember['worked'] {
  if (!isRec(raw) || !nonEmpty(raw.as_of)) return null;
  return {
    elapsed_h: finite(raw.elapsed_h), censored: raw.censored === true, active_h: finite(raw.active_h),
    seats: finite(raw.seats) ?? 0, qa_rounds: finite(raw.qa_rounds) ?? 0, as_of: raw.as_of as string,
  };
}

function normalizeLeadSeat(raw: unknown): WorkLaneMember['lead_seat'] {
  if (!isRec(raw) || !nonEmpty(raw.stream_id) || !nonEmpty(raw.host)) return null;
  return {
    stream_id: raw.stream_id as string, host: raw.host as string, provider: str(raw.provider),
    activity: typeof raw.activity === 'string' && SEAT_ACTIVITY.has(raw.activity)
      ? raw.activity as WorkLaneSeat['activity'] : 'unknown',
    eta_at: str(raw.eta_at), active_step: str(raw.active_step),
  };
}

function normalizeSeat(raw: unknown): WorkLaneSeat | null {
  if (!isRec(raw)) return null;
  const streamId = nonEmpty(raw.stream_id);
  if (!streamId) return null;
  const activity = typeof raw.activity === 'string' && SEAT_ACTIVITY.has(raw.activity)
    ? (raw.activity as WorkLaneSeat['activity']) : 'unknown';
  return {
    stream_id: streamId, role: str(raw.role), host: str(raw.host), visibility: str(raw.visibility),
    parent_stream_id: str(raw.parent_stream_id), activity, last_activity: str(raw.last_activity),
  };
}

const goalId = (value: unknown): string | null =>
  typeof value === 'string' && /^g_[0-9a-f]{24}$/.test(value) ? value : null;
const count = (value: unknown): number | null =>
  typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 2147483647 ? value : null;

function normalizeGoalDetail(raw: Rec): WorkLaneGoalDetail {
  const moving = count(raw.moving), blocked = count(raw.blocked);
  return {
    ...(moving !== null ? { moving } : {}), ...(blocked !== null ? { blocked } : {}),
    ...(typeof raw.working === 'boolean' ? { working: raw.working } : {}),
    ...('left_h' in raw ? { left_h: finite(raw.left_h) !== null && (raw.left_h as number) >= 0 ? raw.left_h as number : null } : {}),
    ...('progress_at' in raw ? { progress_at: str(raw.progress_at) } : {}),
  };
}

function normalizeGoalProgress(raw: unknown): WorkLaneGoalProgress | null {
  if (!isRec(raw) || !['not_started', 'in_progress', 'done'].includes(raw.state as string)) return null;
  const done = count(raw.done), total = count(raw.total);
  if (done === null || total === null || done > total) return null;
  return { state: raw.state as WorkLaneGoalProgress['state'], done, total, ...normalizeGoalDetail(raw) };
}

const TRAIL_KINDS = new Set(['note', 'checks', 'done', 'blocked']);

function normalizeTrail(raw: unknown): WorkLaneProgressEntry[] {
  return (Array.isArray(raw) ? raw : []).flatMap((item): WorkLaneProgressEntry[] => {
    if (!isRec(item) || !nonEmpty(item.at) || !nonEmpty(item.spec_id) || !TRAIL_KINDS.has(item.kind as string)) return [];
    const base = { at: item.at as string, spec_id: item.spec_id as string };
    if (item.kind === 'checks') {
      const rise = count(item.count);
      return rise ? [{ ...base, kind: 'checks', count: rise }] : [];
    }
    if (item.kind === 'done') return [{ ...base, kind: 'done' }];
    return [{ ...base, kind: item.kind as 'note' | 'blocked', text: str(item.text) }];
  }).slice(0, 6);
}

const GOAL_CLASS_NAMES: Record<string, WorkLaneGoalClass> = {
  d: 'done', w: 'working', m: 'moving', b: 'blocked', q: 'queued',
};

/** The one reader of goal_classes: named classes for each sent goal and the default goal. */
export function workLaneGoalClasses(lane: Pick<WorkLane, 'goals' | 'goal_classes'>):
  { goals: Record<string, WorkLaneGoalClass>; default_goal: WorkLaneGoalClass | null } | null {
  const goals = lane.goals ?? [], letters = lane.goal_classes;
  if (typeof letters !== 'string' || letters.length !== goals.length + 1) return null;
  return {
    goals: Object.fromEntries(goals.map((goal, index) => [goal.goal_id, GOAL_CLASS_NAMES[letters[index]]])),
    default_goal: GOAL_CLASS_NAMES[letters[goals.length]] ?? null,
  };
}

function normalizeGoal(raw: unknown): WorkLaneGoal | null {
  if (!isRec(raw)) return null;
  const progress = normalizeGoalProgress(raw), id = goalId(raw.goal_id), rank = count(raw.rank);
  if (!progress || !id || typeof raw.title !== 'string' || rank === null) return null;
  const date = typeof raw.target_date === 'string' && /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(raw.target_date)
    && raw.target_date.slice(0, 4) !== '0000' && Number.isFinite(Date.parse(raw.target_date))
    && new Date(raw.target_date).toISOString().slice(0, 10) === raw.target_date ? raw.target_date : null;
  return { ...progress, goal_id: id, title: raw.title, rank,
    ...(date ? { target_date: date } : {}),
    ...('icon' in raw ? { icon: typeof raw.icon === 'string' && LANE_ICON_KEYS.includes(raw.icon as LaneIconKey)
      ? raw.icon as LaneIconKey : null } : {}) };
}

function normalizeMember(raw: unknown): WorkLaneMember | null {
  if (!isRec(raw)) return null;
  const specId = nonEmpty(raw.spec_id);
  if (!specId) return null;
  const range = normalizeRange(raw.estimate);
  const est = isRec(raw.estimate) ? raw.estimate : {};
  const obs = isRec(raw.observation) ? raw.observation : null;
  const seats = (Array.isArray(raw.seats) ? raw.seats : []).map(normalizeSeat).filter((s): s is WorkLaneSeat => s !== null);
  const questions = isRec(raw.open_questions) ? raw.open_questions : {};
  return {
    // Policy fields are additive and optional; orchestration does not maintain
    // a second executable field list. Known base fields keep their normalizers.
    ...raw,
    spec_id: specId,
    goal_id: goalId(raw.goal_id),
    title: str(raw.title),
    status: str(raw.status),
    terminal: raw.terminal === 'completed' || raw.terminal === 'deprecated' ? raw.terminal : null,
    ac_checked: finite(raw.ac_checked),
    ac_total: finite(raw.ac_total),
    estimate: range ? { ...range, as_of: str(est.as_of), provisional: est.provisional === true } : null,
    estimate_exempt: raw.estimate_exempt === true,
    status_text: str(raw.status_text),
    next_action_text: str(raw.next_action_text),
    status_line: str(raw.status_line), next_line: str(raw.next_line),
    ...('blocked_line' in raw ? { blocked_line: str(raw.blocked_line) } : {}),
    ...('progress_at' in raw ? { progress_at: str(raw.progress_at) } : {}),
    ...('summary_line' in raw ? { summary_line: str(raw.summary_line) } : {}),
    ...('estimate_first' in raw ? { estimate_first: normalizeRange(raw.estimate_first) } : {}),
    ...('unblocks' in raw ? { unblocks: (Array.isArray(raw.unblocks) ? raw.unblocks : []).flatMap((item) =>
      isRec(item) && nonEmpty(item.spec_id) ? [{ spec_id: item.spec_id as string, title: str(item.title) }] : []).slice(0, 4) } : {}),
    ...('recent' in raw ? { recent: normalizeTrail(raw.recent) } : {}),
    worked: normalizeWorked(raw.worked), lead_seat: normalizeLeadSeat(raw.lead_seat),
    ...(Array.isArray(raw.criteria) ? { criteria: raw.criteria.flatMap((item) =>
      isRec(item) && typeof item.title === 'string' && typeof item.disposition === 'string'
        && CRITERION_DISPOSITIONS.has(item.disposition)
        ? [{ title: item.title, disposition: item.disposition as 'open' | 'done' | 'waived' | 'tracked' | 'parked' }] : []) } : {}),
    ...(Array.isArray(raw.transitions) ? { transitions: raw.transitions.flatMap((item) =>
      isRec(item) && typeof item.status === 'string' && typeof item.at === 'string'
        ? [{ status: item.status, at: item.at }] : []) } : {}),
    source_changed_at: str(raw.source_changed_at),
    observation: obs ? { quality: str(obs.quality), observed_at: str(obs.observed_at), error: str(obs.error) } : null,
    seats: seats,
    seats_total: Math.max(finite(raw.seats_total) ?? seats.length, seats.length),
    open_questions: {
      count: Math.max(0, finite(questions.count) ?? 0),
      first_question_id: str(questions.first_question_id),
    },
  };
}

function normalizeProgress(raw: Rec): WorkLaneProgress | null {
  if (!Array.isArray(raw.members) && finite(raw.items_total) === null) return null;
  return {
    members: (Array.isArray(raw.members) ? raw.members : []).map(normalizeMember).filter((m): m is WorkLaneMember => m !== null),
    members_total: finite(raw.members_total),
    members_next: str(raw.members_next),
    no_spec_reason: str(raw.no_spec_reason),
    items_total: finite(raw.items_total),
    items_completed: finite(raw.items_completed),
    items_dropped: finite(raw.items_dropped),
    items_open: finite(raw.items_open),
    items_unresolved: finite(raw.items_unresolved),
    ac_checked: finite(raw.ac_checked),
    ac_total: finite(raw.ac_total),
    ac_members: finite(raw.ac_members),
    open_estimate_h: normalizeRange(raw.open_estimate_h),
    open_estimated: finite(raw.open_estimated),
    estimate_complete: boolOrNull(raw.estimate_complete),
    freshness_at: str(raw.freshness_at),
  };
}

function normalizeWorkIndex(raw: unknown): WorkIndexStatus | null {
  if (!isRec(raw)) return null;
  return {
    available: raw.available === true,
    root_configured: raw.root_configured === true,
    snapshot_at: str(raw.snapshot_at),
    last_sweep_at: str(raw.last_sweep_at),
    error: str(raw.error),
  };
}

function normalizeLane(raw: unknown): WorkLane | null {
  if (!isRec(raw)) return null;
  const laneId = nonEmpty(raw.lane_id);
  const state = raw.state;
  if (!laneId || (state !== 'active' && state !== 'paused' && state !== 'blocked')) return null;
  const last = raw.last_update;
  const rawGoals = Array.isArray(raw.goals) ? raw.goals : [];
  const goals = rawGoals.map(normalizeGoal);
  const road = typeof raw.goal_road === 'string' && !/[^dwmbq]/.test(raw.goal_road)
    && raw.goal_road.length === count(raw.goals_total)
    && rawGoals.every((goal) => isRec(goal) && Number.isInteger(goal.pos)
      && (goal.pos as number) >= 0 && (goal.pos as number) < (raw.goal_road as string).length)
    && new Set(rawGoals.map((goal) => (goal as Record<string, unknown>).pos)).size === rawGoals.length
    ? raw.goal_road : null;
  if (road !== null) goals.forEach((goal, index) => {
    if (goal) goal.pos = (rawGoals[index] as Record<string, unknown>).pos as number;
  });
  // One letter per raw goal plus the default goal; dropped goals drop their letter.
  const letters = typeof raw.goal_classes === 'string' && raw.goal_classes.length === rawGoals.length + 1
    && /^[dwmbq]*[dwmbq-]$/.test(raw.goal_classes) ? raw.goal_classes : null;
  const counts = Array.isArray(raw.goal_counts) && raw.goal_counts.length === 4 ? raw.goal_counts.map(count) : null;
  return {
    lane_id: laneId,
    ...(isRec(raw.visibility_counts) ? { visibility_counts: raw.visibility_counts } : {}),
    ...(isRec(raw.selected_goal) && normalizeGoal(raw.selected_goal)
      ? { selected_goal: { ...raw.selected_goal, ...normalizeGoal(raw.selected_goal)!,
        ...('summary_line' in raw.selected_goal ? { summary_line: str(raw.selected_goal.summary_line) } : {}),
        ...('recent' in raw.selected_goal ? { recent: normalizeTrail(raw.selected_goal.recent) } : {}) } } : {}),
    goals: goals.filter((goal): goal is WorkLaneGoal => goal !== null),
    goals_total: count(raw.goals_total) ?? 0,
    goal_road: road,
    default_goal: normalizeGoalProgress(raw.default_goal),
    ...('goal_classes' in raw ? { goal_classes: letters
      ? goals.flatMap((goal, index) => (goal ? [letters[index]] : [])).join('') + letters[rawGoals.length] : null } : {}),
    ...('goal_counts' in raw ? { goal_counts: counts && counts.every((value) => value !== null)
      ? { moving: counts[0]!, blocked: counts[1]!, queued: counts[2]!, done: counts[3]! } : null } : {}),
    ...('progress_at' in raw ? { progress_at: str(raw.progress_at) } : {}),
    ...('state_at' in raw ? { state_at: str(raw.state_at) } : {}),
    ...('recent' in raw ? { recent: normalizeTrail(raw.recent) } : {}),
    detail_revision: typeof raw.detail_revision === 'string' && /^[0-9a-f]{64}$/.test(raw.detail_revision)
      ? raw.detail_revision : null,
    title: str(raw.title) ?? '',
    icon: typeof raw.icon === 'string' && LANE_ICON_KEYS.includes(raw.icon as LaneIconKey) ? raw.icon as LaneIconKey : null,
    hue: typeof raw.hue === 'string' && LANE_HUE_KEYS.includes(raw.hue as LaneHueKey) ? raw.hue as LaneHueKey : null,
    summary: str(raw.summary) ?? '',
    state,
    ...(isRec(raw.parked) ? { parked: { at: nonEmpty(raw.parked.at), reason: nonEmpty(raw.parked.reason) } } : {}),
    state_reason: str(raw.state_reason),
    blocker: str(raw.blocker),
    owner_kind: raw.owner_kind === 'operator' || raw.owner_kind === 'fd' ? raw.owner_kind : null,
    version: finite(raw.version),
    updated_at: str(raw.updated_at),
    first_admitted_at: str(raw.first_admitted_at),
    done_at: str(raw.done_at),
    lead: normalizeLead(raw.lead),
    visible_chat: normalizeVisibleChat(raw.visible_chat),
    last_update: isRec(last) && nonEmpty(last.update_id)
      ? { update_id: last.update_id as string, kind: str(last.kind) ?? '', event_id: finite(last.event_id), ts: str(last.ts) }
      : null,
    progress: normalizeProgress(raw),
  };
}

/** Decode the complete named-goal road; legacy and invalid input has no road. */
export function workLaneGoalRoad(lane: Pick<WorkLane, 'goal_road'>): WorkLaneGoalClass[] | null {
  if (lane.goal_road == null) return null;
  return [...lane.goal_road].map((letter) => GOAL_CLASS_NAMES[letter]);
}

/** Validate one inventory frame; null when it carries no `lanes` array. Server order is preserved. */
export function normalizeWorkLanesInventory(frame: unknown): WorkLanesInventory | null {
  if (!isRec(frame) || !Array.isArray(frame.lanes)) return null;
  const lanes = frame.lanes.map(normalizeLane).filter((lane): lane is WorkLane => lane !== null);
  const rawCounts = isRec(frame.counts) ? frame.counts : null;
  const tally = (state: WorkLaneState) => lanes.filter((lane) => lane.state === state).length;
  const counts: WorkLanesCounts = {
    active: finite(rawCounts?.active) ?? tally('active'),
    paused: finite(rawCounts?.paused) ?? tally('paused'),
    blocked: finite(rawCounts?.blocked) ?? tally('blocked'),
    open: finite(rawCounts?.open) ?? lanes.length,
  };
  return { lanes, counts, truncated: frame.truncated === true, generated_at: str(frame.generated_at), work_index: normalizeWorkIndex(frame.work_index) };
}

/**
 * Apply a raw daemon frame. Handles the push frame `work_lanes.inventory` and
 * the `work_lanes` field carried by `hello`/snapshot/`list_sessions` replies;
 * every other frame returns `prev` unchanged (same reference).
 */
export function applyWorkLanesFrame(prev: WorkLanesInventory, frame: unknown): WorkLanesInventory {
  if (!isRec(frame)) return prev;
  const source = frame.type === 'work_lanes.inventory' ? frame : frame.work_lanes;
  return normalizeWorkLanesInventory(source) ?? prev;
}

export function selectWorkLanes(inventory: WorkLanesInventory): readonly WorkLane[] {
  return inventory.lanes;
}

/** The header number: open lanes (active + paused + blocked), never a session count. */
export function selectOpenLaneCount(inventory: WorkLanesInventory): number {
  return inventory.counts.open;
}

/** Where a lane tap goes. Never the primary assistant, a hidden worker or a new generation as a fallback. */
export function workLaneTapTarget(lane: WorkLane): WorkLaneTapTarget {
  const chat = lane.visible_chat;
  if (!chat.stream_id) return { action: 'unavailable' };
  if (chat.available === 'open') {
    return { action: 'open_chat', stream_id: chat.stream_id };
  }
  if (chat.available === 'history' && chat.generation) {
    return { action: 'history', stream_id: chat.stream_id, generation: chat.generation };
  }
  return { action: 'unavailable' };
}

/** ETA presentation. A stale ETA reads "ETA stale", never "late Xm". */
export function workLaneEtaLabel(lane: WorkLane, nowMs: number): { text: string; stale: boolean } {
  const lead = lane.lead;
  if (!lead) return { text: '', stale: false };
  if (lead.eta_stale) return { text: 'ETA stale', stale: true };
  const eta = lead.status_card.eta_at ? Date.parse(lead.status_card.eta_at) : NaN;
  if (!Number.isFinite(eta)) return { text: '', stale: false };
  const minutes = Math.max(0, Math.round((eta - nowMs) / 60000));
  return { text: minutes >= 90 ? `~${Math.round(minutes / 60)}h` : `~${minutes}m`, stale: false };
}

/** Typed lane-update card payload of a composite publication, or null for any other event. */
export function laneUpdateFromEvent(event: { publish_kind?: unknown; raw?: unknown } | null | undefined): PentacleLaneUpdate | null {
  if (!event || event.publish_kind !== 'lane_update' || !isRec(event.raw) || !isRec(event.raw.lane_update)) return null;
  const update = event.raw.lane_update;
  const kind = update.kind;
  const updateId = nonEmpty(update.update_id);
  const laneId = nonEmpty(update.lane_id);
  if (!updateId || !laneId || !WORK_LANE_UPDATE_KINDS.includes(kind as WorkLaneUpdateKind)) return null;
  const source = isRec(update.source) ? update.source : {};
  return {
    update_id: updateId,
    lane_id: laneId,
    kind: kind as WorkLaneUpdateKind,
    summary: str(update.summary) ?? '',
    source: {
      type: str(source.type) ?? '',
      id: str(source.id) ?? '',
      ...(Array.isArray(source.grouped_ids) ? { grouped_ids: source.grouped_ids.filter((id): id is string => typeof id === 'string') } : {}),
    },
    state: str(update.state),
    prior_state: str(update.prior_state),
    owner_kind: str(update.owner_kind),
    title: str(update.title),
    ts: str(update.ts),
  };
}
