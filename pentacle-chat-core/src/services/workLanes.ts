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

export type WorkLaneMember = {
  spec_id: string;
  title: string | null;
  status: string | null;
  terminal: 'completed' | 'deprecated' | null;
  ac_checked: number | null;
  ac_total: number | null;
  estimate: WorkLaneEstimate | null;
  estimate_exempt: boolean;
  status_text: string | null;
  next_action_text: string | null;
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
  lane_id: string;
  title: string;
  summary: string;
  state: WorkLaneState;
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
    spec_id: specId,
    title: str(raw.title),
    status: str(raw.status),
    terminal: raw.terminal === 'completed' || raw.terminal === 'deprecated' ? raw.terminal : null,
    ac_checked: finite(raw.ac_checked),
    ac_total: finite(raw.ac_total),
    estimate: range ? { ...range, as_of: str(est.as_of), provisional: est.provisional === true } : null,
    estimate_exempt: raw.estimate_exempt === true,
    status_text: str(raw.status_text),
    next_action_text: str(raw.next_action_text),
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
  return {
    lane_id: laneId,
    title: str(raw.title) ?? '',
    summary: str(raw.summary) ?? '',
    state,
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
