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
};

export type WorkLanesCounts = { open: number; active: number; paused: number; blocked: number };

export type WorkLanesInventory = {
  lanes: WorkLane[];
  counts: WorkLanesCounts;
  truncated: boolean;
  generated_at: string | null;
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
  return { lanes: [], counts: { open: 0, active: 0, paused: 0, blocked: 0 }, truncated: false, generated_at: null };
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
  return { lanes, counts, truncated: frame.truncated === true, generated_at: str(frame.generated_at) };
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

/** Where a lane tap goes. Never Bart, a hidden worker or a new generation as a fallback. */
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
