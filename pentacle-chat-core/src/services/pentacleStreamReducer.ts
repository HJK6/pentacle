import { decodeChildAgents } from "./daemonUpdates";
import { dedupeRecentEventsByStream, mergeProgressiveUpdate } from './pentacleEventUtils';
import {
  appendLiveEventProjection,
  appendLiveEventsProjection,
  mutatePentacleEventBuckets,
  PENTACLE_PER_STREAM_MAX_EVENTS,
  replacePentacleEventProjection,
  selectPentacleDerivedEventIndex,
} from './pentacleEventBuckets';
import { optimisticMatchesServerUser, parseServerEventTimeStrict } from './optimisticMatch';
import { isCodexHelperSuggestion, isTerminalDividerText, isTransientTranscriptNoise } from './pentacleEventInterpreter';
import { normalizePentacleHost } from './pentacleHosts';
import { logTelemetry } from '../utils/telemetry';
import { TELEMETRY_EVENTS } from '../utils/telemetryEvents';
import type {
  ChatAttachment,
  EndReason,
  OptimisticSendState,
  PentacleEvent,
  PentacleHostStatus,
  PentacleLimit,
  PentacleLimitsHealth,
  PentacleProviderHealth,
  PentacleMachineStats,
  PentacleNotification,
  PentacleQuestion,
  PentacleSessionSummary,
  PentacleStreamState,
  PentacleUpdateMessage,
  TurnPhase,
  TurnState,
  WorkingTaskData,
  WorkingTaskSummary,
  WorkingStateData,
} from '../types/pentacle';

export const PENTACLE_RECENT_EVENT_LIMIT = PENTACLE_PER_STREAM_MAX_EVENTS;
export const OPTIMISTIC_SNAPSHOT_RECONCILE_WINDOW_MS = 60_000;
// Grace window for keeping a freshly-added optimistic alive even when the
// next daemon-broadcast session.inventory or snapshot doesn't yet include
// the stream. Spawn-then-send races a periodic inventory rebroadcast: the
// inventory is generated on daemon before our newly-spawned session lands
// in session_summaries, so the broadcast omits our stream. Without the
// grace, applyPentacleSessionInventory + applyPentacleSnapshotMessage drop
// the optimistic and the subsequent USER chat.event arrives to an empty
// optimisticSends map — reconcile silently misses. Spec:
// public-inventory-wipes-recent-optimistic.
export const OPTIMISTIC_INVENTORY_GRACE_MS = 60_000;

// Event immutability contract (Stage 5a).
//
// Once an event is appended to `state.events` it is immutable. Any update is
// either a fresh event reference (a new object replacing the prior at its array
// position) or a wholesale array replacement. No code path is permitted to
// mutate an event's properties in place — selectors, telemetry, and the
// per-stream content-version counter all assume that an event reference
// surviving across reducer transitions is byte-equal to its prior self.
//
// In dev/test builds (`NODE_ENV !== 'production'`) this invariant is enforced
// by `Object.freeze` on insertion so accidental mutation throws TypeError under
// strict mode. Production runtime does NOT freeze (it would impose a per-event
// allocation/freeze cost) — the test bar relies on the dev/test freeze to
// catch any in-place mutation before code ships.
const IS_PRODUCTION = process.env.NODE_ENV === 'production';

function freezeEventInDev(event: PentacleEvent): PentacleEvent {
  if (IS_PRODUCTION) return event;
  if (Object.isFrozen(event)) return event;
  return Object.freeze(event);
}

export const IDLE_TURN: TurnState = Object.freeze({ phase: 'idle' });

export const INITIAL_PENTACLE_LIMITS: PentacleLimit[] = [
  { id: 'claude', label: 'Claude', pct: null, resets_at_iso: null, resets_text: null },
  { id: 'fable', label: 'Fable', pct: null, resets_at_iso: null, resets_text: null },
  { id: 'codex', label: 'Codex', pct: null, resets_at_iso: null, resets_text: null },
];

export const initialPentacleStreamState: PentacleStreamState = {
  connected: false,
  connecting: false,
  hasHydrated: false,
  events: [],
  eventBucketsByStream: {},
  eventBucketMutationRevision: 0,
  drafts: {},
  hosts: {},
  machineStats: {},
  sessions: [],
  specStatuses: [],
  limits: INITIAL_PENTACLE_LIMITS,
  limitsHealth: null,
  updates: [],
  notifications: [],
  workingStates: {},
  workingByStream: {},
  optimisticSends: {},
  optimisticByRequestId: {},
  eventContentVersionByStream: {},
};

// Centralized end-of-turn predicate. Single source of truth for "did the
// provider just end a turn?" Used by the reducer to transition workingByStream
// to `phase: 'idle'`. Covers:
//   - Claude jsonl: SYSTEM event with raw.subtype='turn-summary' (from
//     daemon transport turn_duration record).
//   - Codex pane parse: SYSTEM event with text '─ Worked for ...' or '────────'
//     (from daemon transport _parse_pane_events).
//   - Any provider emitting subtype/displayRule='terminal-divider' or
//     'activity:turn-summary' in raw.
export function isSystemEndOfTurnEvent(event: PentacleEvent | null | undefined) {
  if (String(event?.kind || '').toUpperCase() !== 'SYSTEM') return false;
  const raw = event?.raw || {};
  const subtype = String(raw.subtype || '');
  const displayRule = String(raw.displayRule || raw.display_rule || '');
  return (
    subtype === 'turn-summary' ||
    subtype === 'terminal-divider' ||
    displayRule === 'activity:turn-summary' ||
    displayRule === 'terminal:divider' ||
    isTerminalDividerText(String(event?.text || ''))
  );
}

function endReasonForEvent(event: PentacleEvent): EndReason {
  const raw = event.raw || {};
  const subtype = String(raw.subtype || '');
  const displayRule = String(raw.displayRule || raw.display_rule || '');
  if (subtype === 'turn-summary' || displayRule === 'activity:turn-summary') {
    return 'turn_summary';
  }
  return 'terminal_divider';
}

function streamEventKey(event: PentacleEvent) {
  return `${event.correlatedDaemonSeq ?? event.daemon_seq}:${event.timestamp}:${event.kind}`;
}

// Where a workingByStream phase transition was derived from. 'live' = a live
// chat.event / session.summary frame; 'fetch' = the detail-open
// request_stream_events backfill derivation; 'resync' = the snapshot/inventory
// reconcile. Emitted as `chat:turn_phase_derived` so the otherwise-invisible
// turn-phase derivation is observable for diagnosis + validation
// (turn-phase-derived).
type TurnPhaseSource = 'fetch' | 'live' | 'resync';

interface TurnPhaseDerivation {
  source: TurnPhaseSource;
  drivingEventKey: string | null;
}

type OptimisticOrphanTelemetrySource = 'fetch' | 'live' | 'snapshot';

type OptimisticOrphanTelemetryRecord = {
  streamId: string;
  status: OptimisticSendState['status'];
  suspectedAt: number;
  source: OptimisticOrphanTelemetrySource;
};

const optimisticOrphanTelemetryById = new Map<string, OptimisticOrphanTelemetryRecord>();

export function resetOptimisticOrphanTelemetryForTests() {
  optimisticOrphanTelemetryById.clear();
}

function emitTurnPhaseDerived(
  streamId: string,
  phase: TurnPhase,
  source: TurnPhaseSource,
  drivingEventKey: string | null,
) {
  logTelemetry(TELEMETRY_EVENTS.CHAT_TURN_PHASE_DERIVED, {
    streamId,
    phase,
    source,
    drivingEventKey: drivingEventKey ?? null,
  });
}

function emitOptimisticOrphanSuspected(
  send: OptimisticSendState,
  source: OptimisticOrphanTelemetrySource,
  now: number,
) {
  if (optimisticOrphanTelemetryById.has(send.optimistic_id)) return;
  optimisticOrphanTelemetryById.set(send.optimistic_id, {
    streamId: send.stream_id,
    status: send.status,
    suspectedAt: now,
    source,
  });
  logTelemetry(TELEMETRY_EVENTS.CHAT_COMPOSE_OPTIMISTIC_ORPHAN_SUSPECTED, {
    phase: 'suspected',
    optimisticId: send.optimistic_id,
    requestId: send.request_id,
    streamId: send.stream_id,
    sendStatus: send.status,
    suspectedAt: new Date(now).toISOString(),
    source,
  });
}

function emitOptimisticOrphanResolvedIfNeeded(
  state: PentacleStreamState,
  optimisticId: string,
  serverEvent: PentacleEvent,
  source: OptimisticOrphanTelemetrySource,
  now = Date.now(),
) {
  const suspected = optimisticOrphanTelemetryById.get(optimisticId);
  if (!suspected) return;
  optimisticOrphanTelemetryById.delete(optimisticId);
  const send = state.optimisticSends?.[optimisticId];
  logTelemetry(TELEMETRY_EVENTS.CHAT_COMPOSE_OPTIMISTIC_ORPHAN_SUSPECTED, {
    phase: 'resolved_after_suspected',
    optimisticId,
    requestId: send?.request_id ?? null,
    streamId: send?.stream_id ?? suspected.streamId,
    sendStatus: send?.status ?? suspected.status,
    suspectedAt: new Date(suspected.suspectedAt).toISOString(),
    resolvedAt: new Date(now).toISOString(),
    source,
    correlatedDaemonSeq: Number.isFinite(Number(serverEvent.daemon_seq)) ? Number(serverEvent.daemon_seq) : null,
  });
}

function hasCommittedEchoForOptimisticId(
  state: PentacleStreamState,
  send: OptimisticSendState,
) {
  return state.events.some((event) => {
    if (event.stream_id !== send.stream_id || String(event.kind || '').toUpperCase() !== 'USER') {
      return false;
    }
    if (
      event.optimistic_id === send.optimistic_id &&
      (
        event.client_origin !== true ||
        event.pending === false ||
        Number.isFinite(Number(event.correlatedDaemonSeq))
      )
    ) {
      return true;
    }
    return findUnambiguousOptimisticEchoMatch(
      state.optimisticSends,
      event,
      OPTIMISTIC_SNAPSHOT_RECONCILE_WINDOW_MS,
    ) === send.optimistic_id;
  });
}

function isStrictOrphanCandidateAfterFreshFetch(
  state: PentacleStreamState,
  send: OptimisticSendState,
) {
  if (!state.connected) return false;
  if (send.turn_queued === true) return false;
  if (send.status !== 'acked' && send.status !== 'indeterminate') return false;
  const session = state.sessions.find((item) => item.stream_id === send.stream_id);
  if (!session?.online) return false;
  if (session.pending || session.working) return false;
  const turn = state.workingByStream?.[send.stream_id];
  if (turn && turn.phase !== 'idle') return false;
  return !hasCommittedEchoForOptimisticId(state, send);
}

function emitOptimisticOrphanSuspicionsAfterFreshFetch(
  state: PentacleStreamState,
  touchedStreamIds: Set<string>,
  now = Date.now(),
) {
  for (const send of Object.values(state.optimisticSends ?? {})) {
    if (!touchedStreamIds.has(send.stream_id)) continue;
    if (isStrictOrphanCandidateAfterFreshFetch(state, send)) {
      emitOptimisticOrphanSuspected(send, 'fetch', now);
    }
  }
}

function emitOptimisticOrphanResolutionsFromFetchedEvents(
  state: PentacleStreamState,
  incoming: PentacleEvent[],
  now = Date.now(),
) {
  const resolvedOptimisticIds = new Set<string>();
  for (const event of incoming) {
    const optimisticId = findUnambiguousOptimisticEchoMatch(
      state.optimisticSends,
      event,
      OPTIMISTIC_SNAPSHOT_RECONCILE_WINDOW_MS,
      resolvedOptimisticIds,
    );
    if (!optimisticId) continue;
    resolvedOptimisticIds.add(optimisticId);
    emitOptimisticOrphanResolvedIfNeeded(state, optimisticId, event, 'fetch', now);
  }
}

function nextTurnStateForEvent(
  current: TurnState | undefined,
  event: PentacleEvent,
  now: number,
): TurnState | undefined {
  if (event.client_origin === true) return current;

  // SEED working from an in-band working root (raw.working===true) even when no
  // turn is open yet: opening a working session whose first live/root event is a
  // working root (e.g. the USER root of a working turn) must render Working
  // before the composer's pre-dispatch working-check — the reducer previously
  // left workingByStream untouched here, so working only derived ~4.9s later once
  // the ASSIST flood accumulated. Only SEEDS (never closes); end-of-turn markers
  // and WORKING(raw.working===false) below still own the falling edge.
  if ((!current || current.phase === 'idle') && isWorkingRootEvent(event)) {
    return {
      ...(current ?? {}),
      phase: 'working',
      firstServerEventAt: current?.firstServerEventAt ?? now,
      lastServerEventKey: streamEventKey(event),
    };
  }
  if (!current || current.phase === 'idle') return current;

  if (isSystemEndOfTurnEvent(event)) {
    return {
      ...current,
      phase: 'idle',
      endedAt: now,
      endReason: endReasonForEvent(event),
      lastServerEventKey: streamEventKey(event),
    };
  }

  if (String(event.kind || '').toUpperCase() === 'WORKING' && event.raw?.working === false) {
    return {
      ...current,
      phase: 'idle',
      endedAt: now,
      endReason: 'working_false',
      lastServerEventKey: streamEventKey(event),
    };
  }

  if (current.phase === 'pending') {
    return {
      ...current,
      phase: 'working',
      firstServerEventAt: now,
      lastServerEventKey: streamEventKey(event),
    };
  }

  return { ...current, lastServerEventKey: streamEventKey(event) };
}

function withTurnTransition(
  state: PentacleStreamState,
  streamId: string,
  next: TurnState | undefined,
  derivation?: TurnPhaseDerivation,
): PentacleStreamState {
  const existing = state.workingByStream ?? {};
  const current = existing[streamId];
  if (current === next) return state;
  const workingByStream = { ...existing };
  if (next === undefined) {
    delete workingByStream[streamId];
  } else {
    workingByStream[streamId] = next;
  }
  // Emit only on a real phase change and only for event-derived transitions
  // (callers that pass a derivation). The optimistic-send lifecycle
  // (begin/clear/activate a 'pending' turn) omits it — it is not an
  // event-derived phase and has no source bucket.
  if (derivation && current?.phase !== next?.phase) {
    emitTurnPhaseDerived(streamId, next?.phase ?? 'idle', derivation.source, derivation.drivingEventKey);
  }
  return { ...state, workingByStream };
}

// Reconcile carried-forward per-stream turns against an authoritative resync
// (snapshot or session inventory). The daemon's per-session `working` flag is
// ground truth: the all-chats LIST derives its row status from that flag, but
// the chat-DETAIL spinner + composer lock read workingByStream. Without this a
// resync updates the list to idle while the detail stays "working" with the
// composer locked (public protocol contract).
//
// Only a 'working' turn (server-acknowledged: an event already advanced it past
// 'pending') is closed here, mirroring the falling-edge close in
// applyPentacleSessionSummary but keyed on the resync flag rather than a
// true→false edge so a long-stale turn is recovered too. A 'pending' turn is an
// optimistic send the daemon may not have observed yet (so working:false is
// expected) — it is owned by the optimistic-send lifecycle (grace window +
// failure sweep) and is left untouched here.
function reconcileWorkingByStreamForResync(
  previous: Record<string, TurnState> | undefined,
  sessions: PentacleSessionSummary[],
  survivingStreamIds: Set<string>,
  now: number,
): Record<string, TurnState> {
  const daemonWorking = new Map(sessions.map((session) => [session.stream_id, Boolean(session.working)]));
  const next: Record<string, TurnState> = {};
  for (const [streamId, turn] of Object.entries(previous ?? {})) {
    if (!survivingStreamIds.has(streamId)) continue;
    if (turn.phase === 'working' && daemonWorking.get(streamId) === false) {
      next[streamId] = { ...turn, phase: 'idle', endReason: 'working_false', endedAt: now };
      emitTurnPhaseDerived(streamId, 'idle', 'resync', null);
      continue;
    }
    next[streamId] = turn;
  }
  return next;
}

export function beginPentacleTurn(
  state: PentacleStreamState,
  streamId: string,
  optimisticId: string,
  sentAt: number,
): PentacleStreamState {
  return withTurnTransition(state, streamId, {
    phase: 'pending',
    optimisticId,
    sentAt,
  });
}

export function clearPentacleTurn(
  state: PentacleStreamState,
  streamId: string,
): PentacleStreamState {
  if (!state.workingByStream?.[streamId]) return state;
  return withTurnTransition(state, streamId, undefined);
}

function sessionForStream(state: PentacleStreamState, streamId: string) {
  return state.sessions.find((item) => item.stream_id === streamId);
}

function optimisticEventFromSend(
  state: PentacleStreamState,
  send: OptimisticSendState,
): PentacleEvent {
  const session = sessionForStream(state, send.stream_id);
  return {
    daemon_seq: Number.NaN,
    host: session?.host || '',
    provider: session?.provider || '',
    session_id: send.stream_id,
    session_name: session?.session_name || send.stream_id,
    stream_id: send.stream_id,
    timestamp: new Date(send.created_at).toISOString(),
    kind: 'USER',
    text: send.text,
    client_origin: true,
    optimistic_id: send.optimistic_id,
    pending: isOptimisticSendPending(send),
    created_at: send.created_at,
    ...(send.queued_at !== undefined ? { queued_at: send.queued_at } : {}),
    ...(send.attachments ? { attachments: send.attachments } : {}),
  };
}

function isOptimisticSendPending(send: Pick<OptimisticSendState, 'status'>) {
  // 'acked' means the daemon acknowledged the send RPC but the USER echo has not
  // yet rendered — the row is still in-flight and must show its pending styling
  // until the echo reconciles (FEAT-SEND-NO-FALSE-FAILED: an un-echoed send
  // stays "sending"). Every other in-flight predicate here already groups
  // 'acked' with queued/dispatched/indeterminate (see lines ~936/950/987/994);
  // omitting it only here dropped the pending row styling on an acked send.
  return send.status === 'queued' ||
    send.status === 'dispatched' ||
    send.status === 'acked' ||
    send.status === 'indeterminate';
}

function visualOptimisticEventForSend(
  state: PentacleStreamState,
  send: OptimisticSendState,
): PentacleEvent {
  const existing = state.events.find((event) => event.optimistic_id === send.optimistic_id);
  if (existing) {
    return freezeEventInDev({
      ...existing,
      pending: isOptimisticSendPending(send),
    });
  }
  return freezeEventInDev(optimisticEventFromSend(state, send));
}

function rebuildOptimisticByRequestId(optimisticSends: Record<string, OptimisticSendState>) {
  const optimisticByRequestId: Record<string, string> = {};
  for (const send of Object.values(optimisticSends)) {
    if (send.status === 'returned_to_prompt') continue;
    optimisticByRequestId[send.request_id] = send.optimistic_id;
  }
  return optimisticByRequestId;
}

export function isReturnedToPromptUserEvent(event: PentacleEvent | undefined): boolean {
  if (!event || String(event.kind || '').toUpperCase() !== 'USER') return false;
  const raw = event.raw || {};
  return raw.returned_to_prompt === true || raw.user_delivery_state === 'returned_to_prompt';
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function jsonlRecordUuid(event: PentacleEvent | undefined): string | null {
  if (!event) return null;
  return nonEmptyString(event.jsonl_record_uuid) ?? nonEmptyString(event.raw?.jsonl_record_uuid);
}

function jsonlResolutionForRecordUuid(event: PentacleEvent | undefined): string | null {
  if (!event) return null;
  return nonEmptyString(event.jsonl_resolution_for_record_uuid) ??
    nonEmptyString(event.raw?.jsonl_resolution_for_record_uuid);
}

function returnedToPromptCorrelationKeys(event: PentacleEvent): Set<string> {
  return new Set(
    [jsonlRecordUuid(event), jsonlResolutionForRecordUuid(event)]
      .filter((value): value is string => Boolean(value)),
  );
}

function eventJsonlCorrelationKeys(event: PentacleEvent): Set<string> {
  return new Set(
    [jsonlRecordUuid(event), jsonlResolutionForRecordUuid(event)]
      .filter((value): value is string => Boolean(value)),
  );
}

function eventSharesReturnedToPromptJsonlRecord(prior: PentacleEvent, returnedEvent: PentacleEvent) {
  const returnedKeys = returnedToPromptCorrelationKeys(returnedEvent);
  if (returnedKeys.size === 0) return false;
  for (const key of eventJsonlCorrelationKeys(prior)) {
    if (returnedKeys.has(key)) return true;
  }
  return false;
}

function findPriorCorrelatedOptimisticForReturnedEvent(
  events: PentacleEvent[],
  returnedEvent: PentacleEvent,
): PentacleEvent | undefined {
  if (!isReturnedToPromptUserEvent(returnedEvent)) return undefined;
  return events.find((event) => (
    event.stream_id === returnedEvent.stream_id &&
    String(event.kind || '').toUpperCase() === 'USER' &&
    event.client_origin === true &&
    Boolean(event.optimistic_id) &&
    eventSharesReturnedToPromptJsonlRecord(event, returnedEvent)
  ));
}

function synthesizedReturnedSendFromEvent(
  event: PentacleEvent,
  returnedAt: number,
): OptimisticSendState {
  const createdAt = typeof event.created_at === 'number'
    ? event.created_at
    : Date.parse(String(event.timestamp || ''));
  return {
    optimistic_id: String(event.optimistic_id),
    request_id: `returned:${event.optimistic_id}`,
    stream_id: event.stream_id,
    text: event.text,
    status: 'returned_to_prompt',
    created_at: Number.isFinite(createdAt) ? createdAt : returnedAt,
    returned_at: returnedAt,
    reconnect_count: 0,
    turn_queued: false,
    ...(event.attachments ? { attachments: event.attachments } : {}),
  };
}

function markPriorCorrelatedOptimisticReturnedToPrompt(
  state: PentacleStreamState,
  prior: PentacleEvent,
  returnedAt: number,
): PentacleStreamState {
  const optimisticId = prior.optimistic_id;
  if (!optimisticId) return state;
  const current = state.optimisticSends?.[optimisticId];
  const returnedSend = {
    ...(current ?? synthesizedReturnedSendFromEvent(prior, returnedAt)),
    status: 'returned_to_prompt' as const,
    returned_at: returnedAt,
    turn_queued: false,
  };
  const optimisticSends = {
    ...(state.optimisticSends ?? {}),
    [optimisticId]: returnedSend,
  };
  const events = state.events.filter((event) => event.optimistic_id !== optimisticId);
  return replacePentacleEventProjection({
    ...state,
    optimisticSends,
    optimisticByRequestId: rebuildOptimisticByRequestId(optimisticSends),
  }, events);
}

function withOptimisticSend(
  state: PentacleStreamState,
  send: OptimisticSendState,
): PentacleStreamState {
  return {
    ...state,
    optimisticSends: {
      ...(state.optimisticSends ?? {}),
      [send.optimistic_id]: send,
    },
    optimisticByRequestId: {
      ...(state.optimisticByRequestId ?? {}),
      [send.request_id]: send.optimistic_id,
    },
  };
}

function updateOptimisticSend(
  state: PentacleStreamState,
  optimisticId: string | undefined,
  update: (send: OptimisticSendState) => OptimisticSendState | null,
): PentacleStreamState {
  if (!optimisticId) return state;
  const current = state.optimisticSends?.[optimisticId];
  if (!current) return state;
  const next = update(current);
  if (!next) return state;
  return withOptimisticSend(state, next);
}

function updateOptimisticSendByRequestId(
  state: PentacleStreamState,
  requestId: string,
  update: (send: OptimisticSendState) => OptimisticSendState | null,
): PentacleStreamState {
  return updateOptimisticSend(state, state.optimisticByRequestId?.[requestId], update);
}

export function sendOptimisticMessage(
  state: PentacleStreamState,
  args: {
    streamId: string;
    text: string;
    optimisticId: string;
    requestId: string;
    createdAt: number;
    queuedAt?: number;
    windowStartedAt?: number | null;
    socketGeneration?: number;
    attachments?: ChatAttachment[];
    beginTurn?: boolean;
  },
): PentacleStreamState {
  const send: OptimisticSendState = {
    optimistic_id: args.optimisticId,
    request_id: args.requestId,
    stream_id: args.streamId,
    text: args.text,
    status: 'queued',
    created_at: args.createdAt,
    ...(args.queuedAt !== undefined ? { queued_at: args.queuedAt } : {}),
    window_started_at: args.windowStartedAt ?? null,
    socket_generation: args.socketGeneration,
    reconnect_count: 0,
    ...(args.attachments ? { attachments: args.attachments } : {}),
  };
  const event = optimisticEventFromSend(state, send);
  const withSend = withOptimisticSend(state, send);
  const withEvent = applyPentacleEvent(withSend, event);
  return args.beginTurn === false
    ? withEvent
    : beginPentacleTurn(withEvent, args.streamId, args.optimisticId, args.createdAt);
}

// Compatibility shim for the retired B4 client-side hold. Mid-turn sends now
// dispatch immediately through the provider CLI's native queue; callers that
// still reach this reducer path get an optimistic row without a local
// turn_queued hold and without replacing the active local turn.
export function enqueueOptimisticMessage(
  state: PentacleStreamState,
  args: {
    streamId: string;
    text: string;
    optimisticId: string;
    requestId: string;
    createdAt: number;
    queuedAt: number;
    attachments?: ChatAttachment[];
  },
): PentacleStreamState {
  return sendOptimisticMessage(state, {
    streamId: args.streamId,
    text: args.text,
    optimisticId: args.optimisticId,
    requestId: args.requestId,
    createdAt: args.createdAt,
    queuedAt: args.queuedAt,
    windowStartedAt: null,
    beginTurn: false,
    ...(args.attachments ? { attachments: args.attachments } : {}),
  });
}

// Compatibility no-op for the retired client-side hold.
export function activateQueuedSend(
  state: PentacleStreamState,
  optimisticId: string,
  sentAt: number,
): PentacleStreamState {
  void optimisticId;
  void sentAt;
  return state;
}

export function activateQueuedSends(
  state: PentacleStreamState,
  optimisticIds: string[],
): PentacleStreamState {
  void optimisticIds;
  return state;
}

export function markOptimisticDispatchedByRequestId(
  state: PentacleStreamState,
  requestId: string,
  dispatchedAt: number,
  socketGeneration?: number,
): PentacleStreamState {
  return updateOptimisticSendByRequestId(state, requestId, (send) => ({
    ...send,
    status: 'dispatched',
    dispatched_at: dispatchedAt,
    window_started_at: send.window_started_at ?? dispatchedAt,
    socket_generation: socketGeneration ?? send.socket_generation,
  }));
}

export function markOptimisticAckedByRequestId(
  state: PentacleStreamState,
  requestId: string,
  ackedAt: number,
): PentacleStreamState {
  return updateOptimisticSendByRequestId(state, requestId, (send) => ({
    ...send,
    status: 'acked',
    acked_at: ackedAt,
  }));
}

export function markOptimisticIndeterminateByRequestId(
  state: PentacleStreamState,
  requestId: string,
  indeterminateAt: number,
): PentacleStreamState {
  return updateOptimisticSendByRequestId(state, requestId, (send) => ({
    ...send,
    status: 'indeterminate',
    indeterminate_at: indeterminateAt,
  }));
}

export function markOptimisticFailedByOptimisticId(
  state: PentacleStreamState,
  optimisticId: string,
  reason = 'send_error',
  failedAt = Date.now(),
): PentacleStreamState {
  const withFailedStatus = updateOptimisticSend(state, optimisticId, (send) => {
    if (send.status === 'failed') return null;
    return {
      ...send,
      status: 'failed',
      failed_at: failedAt,
      failure_reason: reason,
    };
  });
  if (withFailedStatus === state) return state;
  return replacePentacleEventProjection(
    withFailedStatus,
    withFailedStatus.events.map((event) => (
      event.optimistic_id === optimisticId
        ? { ...event, pending: false }
        : event
    )),
  );
}

export function markOptimisticFailedByRequestId(
  state: PentacleStreamState,
  requestId: string,
  reason = 'send_error',
  failedAt = Date.now(),
): PentacleStreamState {
  const optimisticId = state.optimisticByRequestId?.[requestId];
  return optimisticId
    ? markOptimisticFailedByOptimisticId(state, optimisticId, reason, failedAt)
    : state;
}

// FEAT-SEND-RETRY: re-arm a "failed sending" overlay back to "sending" so the
// caller can re-transmit it. The mirror image of markOptimisticFailedByOptimisticId:
// clears the failure, re-arms the reconcile window, and flips the visual row's
// `pending` flag back on. No-op unless the row is currently failed. The actual
// re-transmit is the caller's job (it dispatches by optimistic_id).
export function retryOptimisticSend(
  state: PentacleStreamState,
  optimisticId: string,
  now = Date.now(),
): PentacleStreamState {
  const reArmed = updateOptimisticSend(state, optimisticId, (send) => {
    if (send.status !== 'failed') return null;
    return {
      ...send,
      status: 'dispatched',
      dispatched_at: now,
      window_started_at: now,
      turn_queued: false,
      failed_at: undefined,
      failure_reason: undefined,
    };
  });
  if (reArmed === state) return state;
  return replacePentacleEventProjection(
    reArmed,
    reArmed.events.map((event) => (
      event.optimistic_id === optimisticId
        ? { ...event, pending: true }
        : event
    )),
  );
}

// B3 (chat_send_turn_lifecycle_batch2): mark an optimistic send CANCELLED (the
// user cancelled/ESC'd it). Distinct from 'failed' (a delivery error) so the UI
// shows a "cancelled" affordance. Also clears the event's pending flag and drops
// turn_queued so a queued-then-cancelled row stops showing as queued. No-op if
// already cancelled.
export function markOptimisticCancelledByOptimisticId(
  state: PentacleStreamState,
  optimisticId: string,
  cancelledAt = Date.now(),
): PentacleStreamState {
  const withCancelledStatus = updateOptimisticSend(state, optimisticId, (send) => {
    if (send.status === 'cancelled') return null;
    return {
      ...send,
      status: 'cancelled',
      failed_at: cancelledAt,
      failure_reason: 'cancelled',
      turn_queued: false,
    };
  });
  if (withCancelledStatus === state) return state;
  return replacePentacleEventProjection(
    withCancelledStatus,
    withCancelledStatus.events.map((event) => (
      event.optimistic_id === optimisticId
        ? { ...event, pending: false }
        : event
    )),
  );
}

// B3 (chat_send_turn_lifecycle_batch2): request-id wrapper for the cancel path —
// the send.result `not_landed` frame carries a request_id (the daemon's
// `caller_cancelled` reason), so the stream client resolves it to the optimistic
// id and marks that send CANCELLED. Mirrors markOptimisticFailedByRequestId.
export function markOptimisticCancelledByRequestId(
  state: PentacleStreamState,
  requestId: string,
  cancelledAt = Date.now(),
): PentacleStreamState {
  const optimisticId = state.optimisticByRequestId?.[requestId];
  return optimisticId
    ? markOptimisticCancelledByOptimisticId(state, optimisticId, cancelledAt)
    : state;
}

export function markOptimisticReturnedToPromptByOptimisticId(
  state: PentacleStreamState,
  optimisticId: string,
  returnedAt = Date.now(),
): PentacleStreamState {
  const withReturnedStatus = updateOptimisticSend(state, optimisticId, (send) => {
    if (send.status === 'returned_to_prompt') return null;
    return {
      ...send,
      status: 'returned_to_prompt',
      returned_at: returnedAt,
      turn_queued: false,
    };
  });
  if (withReturnedStatus === state) return state;
  const send = withReturnedStatus.optimisticSends?.[optimisticId];
  const optimisticByRequestId = { ...(withReturnedStatus.optimisticByRequestId ?? {}) };
  if (send?.request_id) delete optimisticByRequestId[send.request_id];
  return replacePentacleEventProjection(
    { ...withReturnedStatus, optimisticByRequestId },
    withReturnedStatus.events.filter((event) => !(
      event.client_origin === true && event.optimistic_id === optimisticId
    )),
  );
}

export function markOptimisticEchoedByOptimisticId(
  state: PentacleStreamState,
  optimisticId: string,
  echoedAt = Date.now(),
): PentacleStreamState {
  return updateOptimisticSend(state, optimisticId, (send) => ({
    ...send,
    status: 'echoed',
    echoed_at: echoedAt,
  }));
}

export function pruneOptimisticSend(
  state: PentacleStreamState,
  optimisticId: string,
): PentacleStreamState {
  const current = state.optimisticSends?.[optimisticId];
  if (!current) return state;
  const optimisticSends = { ...(state.optimisticSends ?? {}) };
  delete optimisticSends[optimisticId];
  const optimisticByRequestId = { ...(state.optimisticByRequestId ?? {}) };
  delete optimisticByRequestId[current.request_id];
  return { ...state, optimisticSends, optimisticByRequestId };
}

export function reconcileOptimisticSendWithServerEvent(
  state: PentacleStreamState,
  optimisticId: string,
  serverEvent: PentacleEvent,
): PentacleStreamState {
  if (isReturnedToPromptUserEvent(serverEvent)) {
    return markOptimisticReturnedToPromptByOptimisticId(state, optimisticId, Date.now());
  }
  const echoed = markOptimisticEchoedByOptimisticId(state, optimisticId, Date.now());
  const correlatedEvent = reconciledOptimisticEvent(
    optimisticId,
    serverEvent,
    state.optimisticSends?.[optimisticId]?.attachments,
    state.optimisticSends?.[optimisticId]?.queued_at,
    undefined,
    state.optimisticSends?.[optimisticId]?.text,
  );
  const withServerEvent = applyPentacleEvent(echoed, correlatedEvent);
  return pruneOptimisticSend(withServerEvent, optimisticId);
}

export function resetOptimisticSendWindows(
  state: PentacleStreamState,
  now: number,
): PentacleStreamState {
  let changed = false;
  const optimisticSends: Record<string, OptimisticSendState> = {};
  for (const [optimisticId, send] of Object.entries(state.optimisticSends ?? {})) {
    if (send.status === 'queued' || send.status === 'dispatched' || send.status === 'acked' || send.status === 'indeterminate') {
      optimisticSends[optimisticId] = { ...send, window_started_at: now };
      changed = true;
    } else {
      optimisticSends[optimisticId] = send;
    }
  }
  return changed ? { ...state, optimisticSends } : state;
}

function isReconnectSurvivorStatus(status: OptimisticSendState['status']) {
  return (
    status === 'queued' ||
    status === 'dispatched' ||
    status === 'acked' ||
    status === 'indeterminate'
  );
}

export function onReconnect(
  state: PentacleStreamState,
  gen: number,
  now = Date.now(),
  nextGeneration = gen,
): PentacleStreamState {
  let changed = false;
  const optimisticSends: Record<string, OptimisticSendState> = {};
  for (const [optimisticId, send] of Object.entries(state.optimisticSends ?? {})) {
    if (send.socket_generation !== gen || !isReconnectSurvivorStatus(send.status)) {
      optimisticSends[optimisticId] = send;
      continue;
    }
    optimisticSends[optimisticId] = {
      ...send,
      reconnect_count: send.reconnect_count + 1,
      window_started_at: now,
      socket_generation: nextGeneration,
    };
    changed = true;
  }
  return changed ? { ...state, optimisticSends } : state;
}

function isServerUserEcho(event: PentacleEvent) {
  return String(event.kind || '').toUpperCase() === 'USER' && event.client_origin !== true;
}

// The daemon rewrites an attachment send into an agent-facing wrapper line
// before injecting it into the agent ("Look at the image file at
// <attachment-root>/<sha>.<ext>, then respond to the user's message: <caption>",
// or the caption-less "Image(s) at <path>" form). The transcript then re-emits
// that wrapper as a server USER echo carrying no optimistic_id, so its text no
// longer equals the operator's caption and the equality-based dedup misses it.
// The wrapper path is built from the same blob sha the client sent as
// ChatAttachment.key, so we correlate the echo to the send by requiring every
// attachment key to appear in the wrapper text (and, when present, the caption).
// sha256 keys make key-containment an unambiguous correlator.
const ATTACHMENT_WRAPPER_MARKER = /(?:Look at the image files? at |Images? at )/;

export function serverUserEchoMatchesAttachmentWrapper(
  attachments: ChatAttachment[] | undefined,
  caption: string,
  eventText: string,
): boolean {
  const keys = (attachments ?? [])
    .map((attachment) => String(attachment.key || '').toLowerCase())
    .filter(Boolean);
  if (keys.length === 0) return false;
  const raw = String(eventText || '');
  if (!ATTACHMENT_WRAPPER_MARKER.test(raw)) return false;
  const haystack = raw.toLowerCase();
  if (!keys.every((key) => haystack.includes(key))) return false;
  const trimmedCaption = String(caption || '').trim();
  return trimmedCaption ? raw.includes(trimmedCaption) : true;
}

function canUseTextFallbackStatus(status: OptimisticSendState['status']) {
  return (
    status === 'queued' ||
    status === 'dispatched' ||
    status === 'acked' ||
    status === 'indeterminate' ||
    status === 'echoed'
  );
}

function canUseLateTextFallbackStatus(status: OptimisticSendState['status']) {
  return status === 'dispatched' || status === 'acked' || status === 'indeterminate';
}

function canCollapseNonAnimatingStatus(status: OptimisticSendState['status']) {
  return status === 'indeterminate' || status === 'echoed';
}

function confirmedTwinIsNewerThanSend(send: OptimisticSendState, event: PentacleEvent) {
  const eventTime = parseServerEventTimeStrict(event);
  return eventTime !== null && eventTime > send.created_at;
}

function availableOptimisticSends(
  optimisticSends: Record<string, OptimisticSendState> | undefined,
  matchedOptimisticIds?: Set<string>,
) {
  return Object.entries(optimisticSends ?? {}).filter(([optimisticId]) => (
    !matchedOptimisticIds?.has(optimisticId)
  ));
}

function findUnambiguousOptimisticEchoMatch(
  optimisticSends: Record<string, OptimisticSendState> | undefined,
  event: PentacleEvent,
  windowMs: number,
  matchedOptimisticIds?: Set<string>,
): string | null {
  if (!isServerUserEcho(event)) return null;
  if (event.optimistic_id) {
    const send = optimisticSends?.[event.optimistic_id];
    if (
      send &&
      !matchedOptimisticIds?.has(event.optimistic_id) &&
      send.stream_id === event.stream_id &&
      send.turn_queued !== true &&
      send.status !== 'cancelled' &&
      send.status !== 'returned_to_prompt'
    ) {
      return event.optimistic_id;
    }
    return null;
  }

  const sameTextCandidates = availableOptimisticSends(optimisticSends, matchedOptimisticIds)
    .filter(([, send]) => (
      send.turn_queued !== true &&
      canUseTextFallbackStatus(send.status) &&
      send.stream_id === event.stream_id &&
      // An attachment send's echo is the daemon wrapper, not the caption; match
      // it by its embedded attachment keys so it reconciles like a plain echo.
      (send.text === event.text ||
        serverUserEchoMatchesAttachmentWrapper(send.attachments, send.text, event.text))
    ));
  const windowMatches = sameTextCandidates.filter(([, send]) => (
    optimisticMatchesServerUser(send, event, windowMs)
  ));
  if (windowMatches.length === 1) return windowMatches[0][0];
  if (windowMatches.length > 1) return null;

  const lateMatches = sameTextCandidates.filter(([, send]) => (
    canUseLateTextFallbackStatus(send.status) &&
    confirmedTwinIsNewerThanSend(send, event)
  ));
  if (lateMatches.length === 1 && sameTextCandidates.length === 1) {
    return lateMatches[0][0];
  }

  const collapsibleTwins = sameTextCandidates.filter(([, send]) => (
    canCollapseNonAnimatingStatus(send.status) &&
    confirmedTwinIsNewerThanSend(send, event)
  ));
  if (collapsibleTwins.length === 1 && sameTextCandidates.length === 1) {
    return collapsibleTwins[0][0];
  }
  return null;
}

function snapshotEventMatchesPriorCorrelatedOptimistic(prior: PentacleEvent, event: PentacleEvent) {
  const priorDaemonSeq = Number(prior.correlatedDaemonSeq);
  if (!prior.optimistic_id || !Number.isFinite(priorDaemonSeq)) return false;
  return (
    event.stream_id === prior.stream_id &&
    event.kind === prior.kind &&
    // A reconciled attachment row carries the caption as its text while the raw
    // snapshot echo still carries the daemon wrapper, so match those by the
    // wrapper's embedded keys; daemon_seq equality below keeps it unambiguous.
    (event.text === prior.text ||
      serverUserEchoMatchesAttachmentWrapper(prior.attachments, prior.text, event.text)) &&
    Number(event.daemon_seq) === priorDaemonSeq
  );
}

function carryPriorCorrelatedOptimisticEvents(
  previousState: PentacleStreamState,
  snapshotEvents: PentacleEvent[],
): PentacleEvent[] {
  let nextEvents = snapshotEvents;
  const touchedStreamIds = new Set(snapshotEvents.map((event) => String(event.stream_id || '')).filter(Boolean));
  const priorEventsByStream = selectPentacleDerivedEventIndex(previousState).byStream;
  for (const streamId of touchedStreamIds) {
    for (const prior of priorEventsByStream.get(streamId) ?? []) {
      if (!prior.optimistic_id || !Number.isFinite(Number(prior.correlatedDaemonSeq))) continue;
      const echoIndex = nextEvents.findIndex((event) => snapshotEventMatchesPriorCorrelatedOptimistic(prior, event));
      if (echoIndex === -1) continue;
      const echo = nextEvents[echoIndex];
      if (echo.optimistic_id === prior.optimistic_id && echo.correlatedDaemonSeq === prior.correlatedDaemonSeq) {
        continue;
      }
      if (nextEvents === snapshotEvents) {
        nextEvents = snapshotEvents.slice();
      }
      nextEvents[echoIndex] = reconciledOptimisticEvent(
        prior.optimistic_id,
        echo,
        previousState.optimisticSends?.[prior.optimistic_id]?.attachments ?? prior.attachments,
        previousState.optimisticSends?.[prior.optimistic_id]?.queued_at ?? prior.queued_at,
        prior,
        previousState.optimisticSends?.[prior.optimistic_id]?.text ?? prior.text,
      );
    }
  }
  return nextEvents;
}

function retractReturnedToPromptSnapshotRows(
  previousState: PentacleStreamState,
  snapshotEvents: PentacleEvent[],
  now: number,
): { events: PentacleEvent[]; returnedSends: Record<string, OptimisticSendState> } {
  let nextEvents = snapshotEvents;
  const returnedSends: Record<string, OptimisticSendState> = {};
  for (let index = 0; index < nextEvents.length; index += 1) {
    const event = nextEvents[index];
    if (!isReturnedToPromptUserEvent(event)) continue;
    const prior = event.client_origin === true && event.optimistic_id
      ? event
      : findPriorCorrelatedOptimisticForReturnedEvent(previousState.events, event);
    if (!prior?.optimistic_id) continue;
    returnedSends[prior.optimistic_id] = synthesizedReturnedSendFromEvent(prior, now);
    nextEvents = nextEvents.filter((_item, itemIndex) => itemIndex !== index);
    index -= 1;
  }
  return { events: nextEvents, returnedSends };
}

function appendVisualOptimisticEvents(
  state: PentacleStreamState,
  previousState: PentacleStreamState,
  sends: OptimisticSendState[],
): PentacleStreamState {
  if (sends.length === 0) return state;
  const sendByOptimisticId = new Map(sends.map((send) => [send.optimistic_id, send]));

  // Reconcile pending on optimistic events that ALREADY survived into this
  // snapshot. Previously a summary-mode snapshot wiped the transcript, so a
  // failed send's row was re-appended fresh (pending:false). Now that an empty
  // snapshot preserves existing events, the surviving row must be updated in
  // place — otherwise a send that just transitioned to failed would keep
  // pending:true and never drop its in-flight styling. Mirrors
  // visualOptimisticEventForSend's pending derivation.
  let mutated = false;
  const reconciledEvents = state.events.map((event) => {
    const send = event.optimistic_id ? sendByOptimisticId.get(event.optimistic_id) : undefined;
    if (!send) return event;
    const pending = isOptimisticSendPending(send);
    if (event.pending === pending) return event;
    mutated = true;
    return freezeEventInDev({ ...event, pending });
  });

  const existingOptimisticIds = new Set(reconciledEvents.map((event) => event.optimistic_id).filter(Boolean));
  const eventsToAppend = sends
    .filter((send) => !existingOptimisticIds.has(send.optimistic_id))
    .map((send) => visualOptimisticEventForSend(previousState, send));
  if (eventsToAppend.length === 0) {
    return mutated ? replacePentacleEventProjection(state, reconciledEvents) : state;
  }
  const events = [...reconciledEvents, ...eventsToAppend]
    .map((event, index) => ({ event, index }))
    .sort((left, right) => {
      const leftTime = Date.parse(String(left.event.timestamp || ''));
      const rightTime = Date.parse(String(right.event.timestamp || ''));
      const leftFinite = Number.isFinite(leftTime);
      const rightFinite = Number.isFinite(rightTime);
      if (leftFinite && rightFinite && leftTime !== rightTime) return leftTime - rightTime;
      if (leftFinite !== rightFinite) return leftFinite ? -1 : 1;
      return left.index - right.index;
    })
    .map((item) => item.event);
  return replacePentacleEventProjection(state, events);
}

type AttachmentWithLocalRenderFields = ChatAttachment & Record<string, unknown>;

function mergeOptimisticAttachmentRenderFields(
  serverAttachments: ChatAttachment[] | undefined,
  optimisticAttachments: ChatAttachment[] | undefined,
): ChatAttachment[] | undefined {
  if (!serverAttachments?.length) return optimisticAttachments;
  if (!optimisticAttachments?.length) return serverAttachments;

  const optimisticByKey = new Map<string, AttachmentWithLocalRenderFields>();
  optimisticAttachments.forEach((attachment) => {
    const key = String(attachment.key || '');
    if (key) optimisticByKey.set(key, attachment as AttachmentWithLocalRenderFields);
  });

  return serverAttachments.map((serverAttachment, index) => {
    const local = optimisticByKey.get(String(serverAttachment.key || '')) ||
      (optimisticAttachments[index] as AttachmentWithLocalRenderFields | undefined);
    if (!local) return serverAttachment;
    return {
      ...local,
      ...serverAttachment,
      uri: local.uri,
      width: serverAttachment.width ?? local.width,
      height: serverAttachment.height ?? local.height,
    } as ChatAttachment;
  });
}

function reconciledOptimisticEvent(
  optimisticId: string,
  serverEvent: PentacleEvent,
  optimisticAttachments?: ChatAttachment[],
  queuedAt?: number,
  priorEvent?: PentacleEvent,
  optimisticText?: string,
): PentacleEvent {
  const daemonSeq = Number(serverEvent.daemon_seq);
  const directMatch = serverEvent.optimistic_id === optimisticId;
  const priorRaw = priorEvent?.raw;
  const priorDirect = priorEvent?.receiptDirectMatch === true;
  const priorHasState = 'receipt_state' in (priorRaw ?? {});
  const priorHasDelivery = 'receipt_delivery' in (priorRaw ?? {});
  const priorLanded = String(priorRaw?.receipt_state ?? '').trim().toLowerCase() === 'landed';
  const priorProofUnavailable = String(priorRaw?.receipt_delivery ?? '').trim().toLowerCase() === 'proof_unavailable';
  const incomingLanded = directMatch && String(serverEvent.raw?.receipt_state ?? '').trim().toLowerCase() === 'landed';
  const preserveReceipt = priorDirect && (
    // Identity gate: only an echo with this exact optimistic id enters the
    // receipt lattice. A fallback/no-ID replay retains a direct row's current
    // receipt state, while a Sending row remains uncaptioned.
    !directMatch ||
    // Receipt lattice per optimistic_id:
    // Sending --no-fields--> LegacySent --receipt-bearing--> Failed/Landed
    // Failed --landed--> Landed; Failed/Landed otherwise retain their receipt.
    // The legacy no-field state is deliberately provisional, so the first
    // receipt-bearing direct echo must replace it.
    priorLanded ||
    (priorProofUnavailable && !incomingLanded)
  );
  const { receipt_state: _receiptState, receipt_delivery: _receiptDelivery, ...rawWithoutReceipt } = serverEvent.raw ?? {};
  const raw = preserveReceipt
    ? {
      ...rawWithoutReceipt,
      ...(priorHasState ? { receipt_state: priorRaw?.receipt_state } : {}),
      ...(priorHasDelivery ? { receipt_delivery: priorRaw?.receipt_delivery } : {}),
    }
    : serverEvent.raw;
  return freezeEventInDev({
    ...serverEvent,
    raw,
    // For an attachment send the server echo text is the agent-facing wrapper
    // ("Look at the image file at …, then respond…"); keep the operator's
    // caption as the rendered bubble text so only the operator-formatted message
    // shows.
    ...((optimisticAttachments?.length ?? 0) > 0 && optimisticText !== undefined
      ? { text: optimisticText }
      : {}),
    attachments: mergeOptimisticAttachmentRenderFields(serverEvent.attachments, optimisticAttachments),
    daemon_seq: Number.NaN,
    correlatedDaemonSeq: Number.isFinite(daemonSeq) ? daemonSeq : undefined,
    client_origin: true,
    optimistic_id: optimisticId,
    receiptDirectMatch: directMatch || priorDirect,
    pending: false,
    ...(queuedAt !== undefined ? { queued_at: queuedAt } : {}),
  });
}

export function applySnapshotWithOptimisticReconciliation(
  state: PentacleStreamState,
  message: {
    events?: PentacleEvent[];
    drafts?: Record<string, PentacleEvent>;
    hosts?: Record<string, PentacleHostStatus>;
    sessions?: PentacleSessionSummary[];
    limits?: unknown;
    limits_health?: unknown;
    updates?: PentacleUpdateMessage[];
    notifications?: PentacleNotification[];
    working_states?: Record<string, WorkingStateData>;
  },
  now = Date.now(),
  limit = PENTACLE_RECENT_EVENT_LIMIT,
): PentacleStreamState {
  let snapshotState = applyPentacleSnapshotMessage(state, message, limit, now);
  let snapshotEvents = carryPriorCorrelatedOptimisticEvents(state, snapshotState.events);
  const retractedSnapshot = retractReturnedToPromptSnapshotRows(state, snapshotEvents, now);
  snapshotEvents = retractedSnapshot.events;
  const optimisticSends: Record<string, OptimisticSendState> = { ...retractedSnapshot.returnedSends };
  const visualSends: OptimisticSendState[] = [];

  const matchedOptimisticIds = new Set<string>();
  for (const [optimisticId, send] of Object.entries(snapshotState.optimisticSends ?? {})) {
    const echoIndex = snapshotEvents.findIndex((event) => (
      findUnambiguousOptimisticEchoMatch(
        snapshotState.optimisticSends,
        event,
        OPTIMISTIC_SNAPSHOT_RECONCILE_WINDOW_MS,
        matchedOptimisticIds,
      ) === optimisticId
    ));
    const echo = echoIndex === -1 ? undefined : snapshotEvents[echoIndex];
    if (echo) {
      if (isReturnedToPromptUserEvent(echo)) {
        snapshotEvents = snapshotEvents.filter((_event, index) => index !== echoIndex);
        optimisticSends[optimisticId] = {
          ...send,
          status: 'returned_to_prompt',
          returned_at: now,
          turn_queued: false,
        };
        continue;
      }
      emitOptimisticOrphanResolvedIfNeeded(snapshotState, optimisticId, echo, 'snapshot', now);
      const nextEvents = snapshotEvents.slice();
      nextEvents[echoIndex] = reconciledOptimisticEvent(
        optimisticId,
        echo,
        send.attachments,
        send.queued_at,
        undefined,
        send.text,
      );
      snapshotEvents = nextEvents;
      matchedOptimisticIds.add(optimisticId);
      continue;
    }

    // FEAT-SEND-NO-FALSE-FAILED: never timeout-fail an un-echoed send during a
    // snapshot resync. A transmitted send stays "sending" until the daemon
    // echoes it; "failed sending" is reachable only from a hard transport/
    // daemon-reject signal, never from confirmation lag.
    optimisticSends[optimisticId] = send;
    if (send.status !== 'returned_to_prompt') {
      visualSends.push(send);
    }
  }

  if (snapshotEvents !== snapshotState.events) {
    const survivingStreamIds = new Set(snapshotState.sessions.map((item) => item.stream_id));
    snapshotState = replacePentacleEventProjection(
      snapshotState,
      snapshotEvents,
      [...survivingStreamIds],
    );
  }

  return appendVisualOptimisticEvents({
    ...snapshotState,
    optimisticSends,
    optimisticByRequestId: rebuildOptimisticByRequestId(optimisticSends),
  }, state, visualSends);
}

export function removeOptimisticSendsForStream(
  state: PentacleStreamState,
  streamId: string,
): PentacleStreamState {
  const optimisticSends: Record<string, OptimisticSendState> = {};
  for (const [optimisticId, send] of Object.entries(state.optimisticSends ?? {})) {
    if (send.stream_id !== streamId) {
      optimisticSends[optimisticId] = send;
    }
  }
  return {
    ...state,
    optimisticSends,
    optimisticByRequestId: rebuildOptimisticByRequestId(optimisticSends),
  };
}

function sortSessions(sessions: PentacleSessionSummary[]) {
  return [...sessions].sort((a, b) =>
    String(b.last_event_at || '').localeCompare(String(a.last_event_at || '')),
  );
}

function normalizeEvent(event: PentacleEvent): PentacleEvent {
  const host = normalizePentacleHost(event.host);
  if (host === event.host) return event;
  return { ...event, host };
}

function isClaudeJsonlEvent(event: PentacleEvent) {
  return event.raw?.source === 'claude-jsonl';
}

function isHelperSuggestionEvent(event: PentacleEvent) {
  return String(event.kind || '').toUpperCase() === 'USER' && isCodexHelperSuggestion(event.text);
}

function normalizeSession(summary: PentacleSessionSummary): PentacleSessionSummary {
  const host = normalizePentacleHost(summary.host);
  if (!("agents" in summary)) return host === summary.host ? summary : { ...summary, host };
  const agents = decodeChildAgents(summary.agents);
  const { agents: _rawAgents, ...rest } = summary;
  return { ...rest, host, ...(agents ? { agents } : {}) };
}

function normalizeHostStatus(host: PentacleHostStatus): PentacleHostStatus {
  const normalizedHost = normalizePentacleHost(host.host);
  if (normalizedHost === host.host) return host;
  return { ...host, host: normalizedHost };
}

const HOST_STAT_NUMERIC_FIELDS = [
  'cpu_load_1m',
  'memory_used_bytes',
  'memory_total_bytes',
  'disk_used_bytes',
  'disk_total_bytes',
  'uptime_seconds',
] as const;

function toFiniteNonNegative(value: unknown): number | null {
  if (typeof value !== 'number') return null;
  return Number.isFinite(value) && value >= 0 ? value : null;
}

// Validate one host's daemon-owned stats sample from a `hosts.stats` frame.
// The daemon is the sole sampler and timestamp authority; the client only
// rejects a sample that cannot be real — non-finite/negative numerics, a
// non-positive total, used > total, a host key that disagrees with its `host`
// field, or a missing/unparseable `sampled_at`. Invalid hosts are dropped and
// never rendered; the client never invents a sample or an offline row.
function normalizeHostStats(key: string, raw: unknown): PentacleMachineStats | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  const host = normalizePentacleHost(String(record.host ?? ''));
  if (!host || host !== normalizePentacleHost(key)) return null;
  const numbers: Record<string, number> = {};
  for (const field of HOST_STAT_NUMERIC_FIELDS) {
    const value = toFiniteNonNegative(record[field]);
    if (value === null) return null;
    numbers[field] = value;
  }
  if (numbers.memory_total_bytes <= 0 || numbers.disk_total_bytes <= 0) return null;
  if (numbers.memory_used_bytes > numbers.memory_total_bytes) return null;
  if (numbers.disk_used_bytes > numbers.disk_total_bytes) return null;
  const sampledAt = record.sampled_at;
  // Accept any fractional-second precision the daemon emits. The daemon is
  // Python `datetime.isoformat()` + 'Z', which yields SIX fractional digits
  // (microseconds); a rigid `\.\d{3}` (millis-only) here silently dropped every
  // host and blanked Settings → Machines. `Date.parse` remains the semantic
  // gate, so a bare/empty fraction is still rejected.
  if (
    typeof sampledAt !== 'string' ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(sampledAt) ||
    !Number.isFinite(Date.parse(sampledAt))
  ) return null;
  return {
    host,
    cpu_load_1m: numbers.cpu_load_1m,
    memory_used_bytes: numbers.memory_used_bytes,
    memory_total_bytes: numbers.memory_total_bytes,
    disk_used_bytes: numbers.disk_used_bytes,
    disk_total_bytes: numbers.disk_total_bytes,
    uptime_seconds: numbers.uptime_seconds,
    sampled_at: sampledAt,
  };
}

function normalizeHostsStats(
  hosts: Record<string, unknown> | undefined,
): Record<string, PentacleMachineStats> {
  const normalized: Record<string, PentacleMachineStats> = {};
  if (!hosts || typeof hosts !== 'object') return normalized;
  for (const [key, raw] of Object.entries(hosts)) {
    const next = normalizeHostStats(key, raw);
    if (next) normalized[next.host] = next;
  }
  return normalized;
}

function normalizeHosts(hosts: Record<string, PentacleHostStatus>) {
  const normalized: Record<string, PentacleHostStatus> = {};
  for (const status of Object.values(hosts)) {
    const next = normalizeHostStatus(status);
    const current = normalized[next.host];
    normalized[next.host] = current
      ? {
          ...current,
          ...next,
          online: current.online || next.online,
          session_count: Math.max(current.session_count || 0, next.session_count || 0),
          error: current.online || next.online ? undefined : next.error || current.error,
        }
      : next;
  }
  return normalized;
}

function stripTransientSummary(
  summary: PentacleSessionSummary,
  existing?: PentacleSessionSummary,
): PentacleSessionSummary {
  const isHelperSummary = String(summary.last_kind || '').toUpperCase() === 'USER' && isCodexHelperSuggestion(summary.last_text);
  const cleanSummary = (!summary.last_text || (!isHelperSummary && !isTransientTranscriptNoise(summary.last_text)))
    ? summary
    : {
        ...summary,
        last_event_at: existing?.last_event_at || summary.last_event_at,
        last_text: existing?.last_text || '',
        last_kind: existing?.last_kind || '',
      };
  const existingAt = Date.parse(String(existing?.last_event_at || ''));
  const incomingAt = Date.parse(String(cleanSummary.last_event_at || ''));
  if (
    existing &&
    Number.isFinite(existingAt) &&
    Number.isFinite(incomingAt) &&
    incomingAt < existingAt
  ) {
    return {
      ...cleanSummary,
      last_event_at: existing.last_event_at,
      last_text: existing.last_text,
      last_kind: existing.last_kind,
    };
  }
  return cleanSummary;
}

function nextSessionQuestionFromEvent(
  event: PentacleEvent,
  existing?: PentacleSessionSummary,
): PentacleQuestion | null {
  const raw = event.raw ?? {};
  if (Object.prototype.hasOwnProperty.call(raw, 'question')) {
    return (raw.question as PentacleQuestion | null | undefined) ?? null;
  }
  return existing?.question ?? null;
}

export function mergeHostsWithSessions(
  hosts: Record<string, PentacleHostStatus>,
  sessions: PentacleSessionSummary[],
) {
  const merged: Record<string, PentacleHostStatus> = normalizeHosts(hosts);
  const counts = new Map<string, number>();

  for (const rawSession of sessions) {
    const session = normalizeSession(rawSession);
    counts.set(session.host, (counts.get(session.host) || 0) + 1);
  }

  for (const [host, sessionCount] of counts.entries()) {
    const current = merged[host];
    merged[host] = {
      host,
      online: true,
      checked_at: current?.checked_at || new Date().toISOString(),
      session_count: Math.max(current?.session_count || 0, sessionCount),
      error: undefined,
      host_status_reason: current?.host_status_reason,
      host_status_since: current?.host_status_since,
    };
  }

  return merged;
}

export function applyPentacleSessionSummary(
  state: PentacleStreamState,
  summary: PentacleSessionSummary,
  // Key of the event driving a working→idle falling-edge close, when one is in
  // hand (a live WORKING:false frame). Defaults to null for genuine
  // session.summary frames, which carry no single driving event.
  closeDrivingEventKey: string | null = null,
  // Fail closed at materialization (mirror/visibility freshness). Set by the
  // transcript-event path only: a chat.event / WORKING / DRAFT carries no
  // `visibility`, so it must NOT introduce a list row for a session we have not
  // yet seen in inventory — its visibility is unresolved and could be `hidden`,
  // which under degraded/coalesced serving would flash a hidden session into
  // the list before its inventory row (carrying visibility) arrives. The row
  // appears the moment that inventory row lands. Authoritative session frames
  // (spawn.ok / rename.ok / session.summary) carry visibility and pass false,
  // so they still establish the row. Events for an already-known session always
  // update it, preserving the resolved visibility already on the row.
  deferWhenVisibilityUnresolved = false,
): PentacleStreamState {
  const normalizedSummary = normalizeSession(summary);
  const sessions = new Map(state.sessions.map((item) => [item.stream_id, item]));
  const current = sessions.get(normalizedSummary.stream_id);
  if (
    deferWhenVisibilityUnresolved &&
    current === undefined &&
    normalizedSummary.visibility === undefined
  ) {
    return state;
  }
  const cleanSummary = stripTransientSummary(normalizedSummary, current);
  sessions.set(normalizedSummary.stream_id, {
    ...current,
    ...cleanSummary,
  });
  const nextState: PentacleStreamState = {
    ...state,
    sessions: sortSessions(Array.from(sessions.values())),
  };
  // Falling edge: session_summary working flips true → false closes the turn.
  // Catches WORKING events (which set summary.working from raw.working) and
  // any provider that flips working off without an explicit WORKING event.
  if (current?.working && !cleanSummary.working) {
    const existing = nextState.workingByStream?.[normalizedSummary.stream_id];
    if (existing && existing.phase !== 'idle') {
      return withTurnTransition(nextState, normalizedSummary.stream_id, {
        ...existing,
        phase: 'idle',
        endReason: 'working_false',
        endedAt: Date.now(),
      }, { source: 'live', drivingEventKey: closeDrivingEventKey });
    }
  }
  return nextState;
}

export function removePentacleStream(
  state: PentacleStreamState,
  streamId: string,
): PentacleStreamState {
  const nextSessions = state.sessions.filter((session) => session.stream_id !== streamId);
  const nextDrafts = { ...state.drafts };
  delete nextDrafts[streamId];
  const nextWorkingStates = { ...state.workingStates };
  delete nextWorkingStates[streamId];
  const nextWorkingByStream = { ...(state.workingByStream ?? {}) };
  delete nextWorkingByStream[streamId];
  const nextContentVersions = { ...(state.eventContentVersionByStream ?? {}) };
  delete nextContentVersions[streamId];
  const withoutOptimistics = removeOptimisticSendsForStream(state, streamId);

  return mutatePentacleEventBuckets({
    ...withoutOptimistics,
    drafts: nextDrafts,
    workingStates: nextWorkingStates,
    workingByStream: nextWorkingByStream,
    eventContentVersionByStream: nextContentVersions,
    sessions: nextSessions,
    hosts: mergeHostsWithSessions(state.hosts, nextSessions),
  }, { type: 'evict-stream', streamId });
}

export function applyPentacleWorkingState(
  state: PentacleStreamState,
  workingState: WorkingStateData | undefined,
): PentacleStreamState {
  if (!workingState?.stream_id) return state;
  const existing = state.workingStates?.[workingState.stream_id];
  // Value-equality fast path: a redundant working.state frame (the daemon churns
  // these under load) must not produce a new state object, else setState()'s
  // reference check sees a change and notifies every subscriber for nothing.
  // Guarded so a malformed/partial frame falls through to the plain update
  // rather than throwing in the comparator.
  if (
    existing &&
    existing.task_summary && workingState.task_summary &&
    Array.isArray(existing.tasks) && Array.isArray(workingState.tasks) &&
    sameWorkingStateData(existing, workingState)
  ) {
    return state;
  }
  return {
    ...state,
    workingStates: {
      ...state.workingStates,
      [workingState.stream_id]: workingState,
    },
  };
}

function sameStringArray(a: string[], b: string[]) {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  return a.every((item, index) => item === b[index]);
}

function sameWorkingTaskData(a: WorkingTaskData, b: WorkingTaskData) {
  return (
    a.id === b.id &&
    a.subject === b.subject &&
    a.status === b.status &&
    sameStringArray(a.blocked_by, b.blocked_by)
  );
}

function sameWorkingTaskSummary(a: WorkingTaskSummary, b: WorkingTaskSummary) {
  return (
    a.total === b.total &&
    a.done === b.done &&
    a.in_progress === b.in_progress &&
    a.open === b.open
  );
}

function sameWorkingStateData(a: WorkingStateData, b: WorkingStateData) {
  return (
    a.stream_id === b.stream_id &&
    a.timestamp === b.timestamp &&
    a.tokens_input === b.tokens_input &&
    a.tokens_output === b.tokens_output &&
    a.tokens_cache_read === b.tokens_cache_read &&
    a.tokens_cache_creation === b.tokens_cache_creation &&
    a.tokens_phase === b.tokens_phase &&
    a.shell_count_started === b.shell_count_started &&
    a.elapsed_ms === b.elapsed_ms &&
    sameWorkingTaskSummary(a.task_summary, b.task_summary) &&
    a.tasks.length === b.tasks.length &&
    a.tasks.every((task, index) => sameWorkingTaskData(task, b.tasks[index]))
  );
}

function preserveSnapshotWorkingStates(
  previous: Record<string, WorkingStateData> | undefined,
  incoming: Record<string, WorkingStateData>,
) {
  const previousStates = previous ?? {};
  const incomingEntries = Object.entries(incoming);
  const previousKeys = Object.keys(previousStates);
  if (incomingEntries.length === 0) {
    return previousKeys.length === 0 ? previousStates : {};
  }

  let changed = previousKeys.length !== incomingEntries.length;
  const workingStates: Record<string, WorkingStateData> = {};
  for (const [streamId, nextState] of incomingEntries) {
    const priorState = previousStates[streamId];
    if (priorState && sameWorkingStateData(priorState, nextState)) {
      workingStates[streamId] = priorState;
    } else {
      workingStates[streamId] = nextState;
      changed = true;
    }
  }
  return !changed ? previousStates : workingStates;
}

export function applyPentacleEvent(
  state: PentacleStreamState,
  rawEvent: PentacleEvent,
  limit = PENTACLE_RECENT_EVENT_LIMIT,
): PentacleStreamState {
  const event = normalizeEvent(rawEvent);
  const existing = state.sessions.find((item) => item.stream_id === event.stream_id);
  const turnTransitionTarget = nextTurnStateForEvent(
    state.workingByStream?.[event.stream_id],
    event,
    Date.now(),
  );
  const liveDerivation: TurnPhaseDerivation = {
    source: 'live',
    drivingEventKey: streamEventKey(event),
  };

  if (event.kind === 'DRAFT') {
    return applyPentacleSessionSummary(
      {
        ...state,
        drafts: {
          ...state.drafts,
          [event.stream_id]: event,
        },
      },
      {
        stream_id: event.stream_id,
        host: event.host,
        provider: event.provider,
        session_name: event.session_name,
        last_event_at: existing?.last_event_at || event.timestamp,
        last_text: existing?.last_text || '',
        last_kind: existing?.last_kind || '',
        draft: event.text,
        pending: Boolean(event.raw?.pending),
        working: Boolean(event.raw?.working),
        working_label: String(event.raw?.working_label || ''),
        question: nextSessionQuestionFromEvent(event, existing),
        online: true,
      },
      null,
      true,
    );
  }

  if (
    !isClaudeJsonlEvent(event) &&
    (event.kind === 'ASSIST' || event.kind === 'TOOL' || event.kind === 'TOOL-OUT') &&
    isTransientTranscriptNoise(event.text)
  ) {
    return state;
  }

  if (event.kind === 'WORKING') {
    return withTurnTransition(
      applyPentacleSessionSummary(state, {
        stream_id: event.stream_id,
        host: event.host,
        provider: event.provider,
        session_name: event.session_name,
        last_event_at: existing?.last_event_at || event.timestamp,
        last_text: existing?.last_text || '',
        last_kind: existing?.last_kind || '',
        draft: existing?.draft || '',
        pending: existing?.pending || false,
        working: Boolean(event.raw?.working),
        working_label: String(event.raw?.working_label || ''),
        question: nextSessionQuestionFromEvent(event, existing),
        online: true,
      }, liveDerivation.drivingEventKey, true),
      event.stream_id,
      turnTransitionTarget,
      liveDerivation,
    );
  }

  if (isHelperSuggestionEvent(event)) {
    return state;
  }

  const returnedPromptPrior = findPriorCorrelatedOptimisticForReturnedEvent(state.events, event);
  if (returnedPromptPrior) {
    return markPriorCorrelatedOptimisticReturnedToPrompt(state, returnedPromptPrior, Date.now());
  }

  const optimisticEchoMatch = findUnambiguousOptimisticEchoMatch(
    state.optimisticSends,
    event,
    OPTIMISTIC_SNAPSHOT_RECONCILE_WINDOW_MS,
  );
  if (optimisticEchoMatch) {
    emitOptimisticOrphanResolvedIfNeeded(state, optimisticEchoMatch, event, 'live');
    return reconcileOptimisticSendWithServerEvent(state, optimisticEchoMatch, event);
  }

  const eventSeq = Number(event.correlatedDaemonSeq ?? event.daemon_seq);
  const eventStreamId = String(event.stream_id || '');
  const optimisticPriorIndex = event.optimistic_id
    ? state.events.findIndex((item) => item.optimistic_id === event.optimistic_id)
    : -1;
  if (optimisticPriorIndex !== -1 || Number.isFinite(eventSeq)) {
    const priorIndex = optimisticPriorIndex !== -1
      ? optimisticPriorIndex
      : state.events.findIndex((item) => (
        String(item.stream_id || '') === eventStreamId &&
        Number(item.correlatedDaemonSeq ?? item.daemon_seq) === eventSeq
      ));
    if (priorIndex !== -1) {
      const prior = state.events[priorIndex];
      const sameOptimisticClientReplacement = event.client_origin === true &&
        Boolean(event.optimistic_id) && event.optimistic_id === prior.optimistic_id;
      const merged = sameOptimisticClientReplacement
        ? event
        : prior.client_origin === true && prior.optimistic_id && event.client_origin !== true
          // Pass the prior row's text as the optimistic caption so a duplicate
          // live replay of an attachment-wrapper echo (which re-enters this
          // seq-correlated path after the send is pruned) keeps rendering the
          // operator caption, not the daemon wrapper text — matching the live
          // and snapshot reconcile paths and preventing a wrapper double-render.
          ? reconciledOptimisticEvent(prior.optimistic_id, event, prior.attachments, prior.queued_at, prior, prior.text)
          : mergeProgressiveUpdate(prior, event);
      if (!merged || merged === prior) return state;
      const frozenMerged = freezeEventInDev(merged);
      const nextEvents = state.events.slice();
      nextEvents[priorIndex] = frozenMerged;
      if (event.optimistic_id && Number.isFinite(Number(event.correlatedDaemonSeq))) {
        const nextDrafts = { ...state.drafts };
        delete nextDrafts[event.stream_id];
        return withTurnTransition(
          applyPentacleSessionSummary(
            replacePentacleEventProjection(
              { ...state, drafts: nextDrafts },
              nextEvents,
            ),
              {
                stream_id: event.stream_id,
                host: event.host,
                provider: event.provider,
                session_name: event.session_name,
                last_event_at: event.timestamp,
                last_text: event.text,
                last_kind: event.kind,
                draft: '',
                pending: existing?.pending || false,
                working: existing?.working || false,
                working_label: String(existing?.working_label || ''),
                online: true,
              },
          ),
          event.stream_id,
          turnTransitionTarget,
          liveDerivation,
        );
      }
      return replacePentacleEventProjection(state, nextEvents);
    }
  }

  const frozenEvent = freezeEventInDev(event);
  const nextDrafts = { ...state.drafts };
  delete nextDrafts[event.stream_id];

  // This branch handles an event with no optimistic/daemon_seq predecessor in
  // the store, so a per-stream append is equivalent to the global per-stream
  // dedupe. Take the incremental projection (O(1-stream), preserves every other
  // stream's bucket ref) when the event is one the dedupe would KEEP — the
  // well-formed live/optimistic hot path — and fall back to the full dedupe +
  // snapshot rebuild for the rare droppable/malformed event so the whole-list
  // cleanup semantics stay byte-identical. This is the fix for the
  // O(total-per-delivery) build-1155 regression (spec
  // incremental projection contract).
  const keptByDedupe = event.client_origin === true
    ? Boolean(event.optimistic_id)
    : Number.isFinite(Number(event.daemon_seq));
  const projectedState = keptByDedupe
    ? appendLiveEventProjection({ ...state, drafts: nextDrafts }, frozenEvent, { perStreamMaxEvents: limit })
    : replacePentacleEventProjection(
      { ...state, drafts: nextDrafts },
      dedupeRecentEventsByStream([...state.events, frozenEvent], limit),
    );

  return withTurnTransition(
    applyPentacleSessionSummary(
      projectedState,
        {
          stream_id: event.stream_id,
          host: event.host,
          provider: event.provider,
          session_name: event.session_name,
          last_event_at: event.timestamp,
          last_text: event.text,
          last_kind: event.kind,
          draft: '',
          pending: existing?.pending || false,
          working: existing?.working || false,
          working_label: String(existing?.working_label || ''),
          online: true,
        },
        null,
        true,
    ),
    event.stream_id,
    turnTransitionTarget,
    liveDerivation,
  );
}

// Agent-activity events that advance a working turn. WORKING/DRAFT frames are
// filtered out of the fetched batch before they reach the derivation, so the
// in-band working signal available on the fetch path is the agent's transcript
// output itself (assistant text + tool calls), bounded by the end-of-turn
// predicate (turn-summary / terminal divider).
function isWorkingTurnActivityEvent(event: PentacleEvent) {
  const kind = String(event.kind || '').toUpperCase();
  return kind === 'ASSIST' || kind === 'TOOL' || kind === 'TOOL-OUT';
}

// An in-band working ROOT: a server event that carries the daemon's own
// `raw.working === true` assertion for the turn it opened (e.g. the USER root of
// a session that is currently working). This is an EVENT-level signal ordered by
// daemon_seq — NOT the lagging `session.working` summary (the 007d127
// stale-working race is about closing a live turn from that summary; this only
// ever SEEDS working). Opening a working session whose latest in-band event is a
// working root — before any ASSIST/TOOL activity is fetched — must render as
// Working, so a working root counts as working-turn activity for derivation.
// `=== true` only: absent/false never seeds working (no false-working).
function isWorkingRootEvent(event: PentacleEvent) {
  return event.raw?.working === true && !isSystemEndOfTurnEvent(event);
}

// Monotonic ordering key for a fetched event. Prefer the daemon sequence
// (server-authoritative order); fall back to the timestamp so an event without
// a finite seq still orders deterministically.
function fetchedEventOrder(event: PentacleEvent): number {
  const seq = Number(event.correlatedDaemonSeq ?? event.daemon_seq);
  if (Number.isFinite(seq)) return seq;
  const ts = Date.parse(String(event.timestamp || ''));
  return Number.isFinite(ts) ? ts : Number.NEGATIVE_INFINITY;
}

// Recover the monotonic order of the event a `lastServerEventKey` (seq:ts:kind,
// from streamEventKey) was minted from, mirroring fetchedEventOrder: prefer the
// leading daemon-seq token, fall back to the timestamp segment. Used to decide
// whether a fetched end-of-turn marker is genuinely newer than the live event
// that established the current working turn. An absent key has no established
// server event ⇒ NEGATIVE_INFINITY so any real end-marker counts as newer.
function orderFromEventKey(key: string | undefined): number {
  if (!key) return Number.NEGATIVE_INFINITY;
  const firstColon = key.indexOf(':');
  const lastColon = key.lastIndexOf(':');
  const seqToken = firstColon >= 0 ? key.slice(0, firstColon) : key;
  const seq = Number(seqToken);
  if (Number.isFinite(seq)) return seq;
  const tsToken = firstColon >= 0 && lastColon > firstColon
    ? key.slice(firstColon + 1, lastColon)
    : '';
  const ts = Date.parse(tsToken);
  return Number.isFinite(ts) ? ts : Number.NEGATIVE_INFINITY;
}

// Derive a stream's turn phase from fetched (backfilled) history alone — never
// from the lagging session.working summary (the chat_detail_stale_working_flag
// 007d127 race-close hazard). Compares the latest end-of-turn marker against the
// latest agent-activity event OR in-band working root (raw.working===true) by
// daemon-seq order:
//   - agent activity / working root strictly after the last end-of-turn (or no
//     end-of-turn at all) ⇒ an open working turn ⇒ 'working';
//   - the last end-of-turn at/after the last activity (or activity absent) ⇒ the
//     turn ended ⇒ 'idle';
//   - neither present ⇒ no in-band signal ⇒ null (leave workingByStream alone).
// Server (non-client_origin) events only: optimistic echoes never imply a turn.
function deriveFetchedTurnPhase(
  events: PentacleEvent[],
  streamId: string,
): { phase: TurnPhase; drivingEventKey: string; endReason?: EndReason; endOrder?: number } | null {
  let endEvent: PentacleEvent | null = null;
  let endOrder = Number.NEGATIVE_INFINITY;
  let activityEvent: PentacleEvent | null = null;
  let activityOrder = Number.NEGATIVE_INFINITY;
  for (const event of events) {
    if (event.stream_id !== streamId) continue;
    if (event.client_origin === true) continue;
    const order = fetchedEventOrder(event);
    if (isSystemEndOfTurnEvent(event)) {
      if (order >= endOrder) {
        endOrder = order;
        endEvent = event;
      }
    } else if (isWorkingTurnActivityEvent(event) || isWorkingRootEvent(event)) {
      // ASSIST/TOOL transcript activity OR an in-band working root (raw.working
      // === true) both establish an open working turn; the newest of the two,
      // compared against the newest end-of-turn marker, decides the phase.
      if (order >= activityOrder) {
        activityOrder = order;
        activityEvent = event;
      }
    }
  }
  if (activityEvent && activityOrder > endOrder) {
    return { phase: 'working', drivingEventKey: streamEventKey(activityEvent) };
  }
  if (endEvent) {
    return {
      phase: 'idle',
      drivingEventKey: streamEventKey(endEvent),
      endReason: endReasonForEvent(endEvent),
      endOrder,
    };
  }
  return null;
}

// Seed workingByStream for the touched streams from the fetched history derived
// above. Treats an absent entry as effectively 'idle' so opening an already-idle
// chat is a no-op (no spurious entry, no telemetry). A 'pending' turn (an
// in-flight optimistic send on this client) is left to the optimistic-send
// lifecycle and never overwritten here.
function deriveWorkingByStreamFromFetch(
  previous: Record<string, TurnState> | undefined,
  events: PentacleEvent[],
  touchedStreamIds: Set<string>,
  now: number,
): Record<string, TurnState> | undefined {
  let workingByStream = previous;
  let mutated = false;
  for (const streamId of touchedStreamIds) {
    const derived = deriveFetchedTurnPhase(events, streamId);
    if (!derived) continue;
    const current = (workingByStream ?? {})[streamId];
    if (current?.phase === 'pending') continue;
    const currentPhase = current?.phase ?? 'idle';
    if (currentPhase === derived.phase) continue;
    // Race-close guard (007d127 residue): freely SEED working from fetched
    // history, but only CLOSE a working turn to idle when the fetched
    // end-of-turn marker is strictly newer than the server event that
    // established the live turn. Without this, a stale failure-retry backfill
    // landing after the user re-sent — before the new turn's first server
    // ASSIST is fetched — could flip a live working turn to idle, and idle
    // never reopens (nextTurnStateForEvent/resync only close). An absent
    // lastServerEventKey orders as -∞, so a freshly-seeded working turn with
    // no live provenance (e.g. test-107) still closes normally.
    if (currentPhase === 'working' && derived.phase === 'idle') {
      const currentOrder = orderFromEventKey(current?.lastServerEventKey);
      if (!(typeof derived.endOrder === 'number' && derived.endOrder > currentOrder)) {
        continue;
      }
    }
    const next: TurnState = derived.phase === 'working'
      ? {
          ...(current ?? { phase: 'working' }),
          phase: 'working',
          firstServerEventAt: current?.firstServerEventAt ?? now,
          lastServerEventKey: derived.drivingEventKey,
        }
      : {
          ...(current ?? { phase: 'idle' }),
          phase: 'idle',
          endReason: derived.endReason,
          endedAt: now,
          lastServerEventKey: derived.drivingEventKey,
        };
    if (!mutated) {
      workingByStream = { ...(workingByStream ?? {}) };
      mutated = true;
    }
    workingByStream![streamId] = next;
    emitTurnPhaseDerived(streamId, derived.phase, 'fetch', derived.drivingEventKey);
  }
  return mutated ? workingByStream : undefined;
}

function liveEventDedupeKey(event: PentacleEvent): string | null {
  if (event.client_origin === true) {
    const optimisticId = String(event.optimistic_id || '');
    return optimisticId ? `optimistic:${optimisticId}` : null;
  }
  const seq = Number(event.correlatedDaemonSeq ?? event.daemon_seq);
  return Number.isFinite(seq) ? `seq:${seq}` : null;
}

function applyLiveFetchedStreamEvents(
  state: PentacleStreamState,
  incoming: PentacleEvent[],
  touchedStreamIds: Set<string>,
  limit: number,
): PentacleStreamState {
  const seenKeys = new Set<string>();
  const countsByStream = new Map<string, number>();
  const eventsByStream = selectPentacleDerivedEventIndex(state).byStream;
  for (const streamId of touchedStreamIds) {
    const existing = eventsByStream.get(streamId) ?? [];
    countsByStream.set(streamId, existing.length);
    for (const event of existing) {
      const key = liveEventDedupeKey(event);
      if (key) seenKeys.add(key);
    }
  }

  const nextEvents = state.events.slice();
  let appended = 0;
  for (const event of incoming) {
    const streamId = String(event.stream_id || '');
    const key = liveEventDedupeKey(event);
    if (!streamId || !key || seenKeys.has(key)) {
      return applyFetchedStreamEvents(state, incoming, { limit });
    }
    if ((countsByStream.get(streamId) || 0) >= limit) {
      return applyFetchedStreamEvents(state, incoming, { limit });
    }
    seenKeys.add(key);
    countsByStream.set(streamId, (countsByStream.get(streamId) || 0) + 1);
    nextEvents.push(freezeEventInDev(event));
    appended += 1;
  }

  if (appended === 0) return state;

  let eventContentVersionByStream = { ...(state.eventContentVersionByStream ?? {}) };
  for (const streamId of touchedStreamIds) {
    eventContentVersionByStream[streamId] = (eventContentVersionByStream[streamId] ?? 0) + 1;
  }
  const nextWorkingByStream = deriveWorkingByStreamFromFetch(
    state.workingByStream,
    incoming,
    touchedStreamIds,
    Date.now(),
  );
  // Genuine-new appends only (the dedupe/cap loop above returns to the full path
  // on any collision or per-stream cap), so apply them incrementally: per-stream
  // bucket appends + the already-built flat list, instead of a snapshot-replace
  // that re-groups every stream and re-runs compatibilityProjection each batch —
  // the O(total) allocation that starved the 19.2k freeze burst (spec
  // incremental projection contract S3).
  const appendedTail = nextEvents.slice(state.events.length);
  const appendedByStream = new Map<string, PentacleEvent[]>();
  for (const event of appendedTail) {
    const streamId = String(event.stream_id || '');
    if (!streamId) continue;
    const rows = appendedByStream.get(streamId);
    if (rows) rows.push(event);
    else appendedByStream.set(streamId, [event]);
  }
  return appendLiveEventsProjection(
    {
      ...state,
      workingByStream: nextWorkingByStream ?? state.workingByStream,
      eventContentVersionByStream,
    },
    appendedByStream,
    nextEvents,
  );
}

type FetchedStreamEventsOptions = {
  limit?: number;
  requestedStreamId?: string | null | undefined;
  mode?: 'live' | 'fetch' | 'current-tail';
  ingressSource?: string;
};

type EventFreshnessTuple = {
  correlated_daemon_seq: number | null;
  daemon_seq: number | null;
  timestamp: string | null;
};

export type CurrentTailEventsSelection = {
  events: PentacleEvent[];
  rejectedStreamIds: readonly string[];
};

function finiteEventTupleValue(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function eventFreshnessTuple(event: PentacleEvent): EventFreshnessTuple {
  return {
    correlated_daemon_seq: finiteEventTupleValue(event.correlatedDaemonSeq),
    daemon_seq: finiteEventTupleValue(event.daemon_seq),
    timestamp: typeof event.timestamp === 'string' && event.timestamp ? event.timestamp : null,
  };
}

function compareEventFreshnessTuple(left: EventFreshnessTuple, right: EventFreshnessTuple): number {
  const leftCorrelated = left.correlated_daemon_seq ?? Number.NEGATIVE_INFINITY;
  const rightCorrelated = right.correlated_daemon_seq ?? Number.NEGATIVE_INFINITY;
  if (left.correlated_daemon_seq !== null && right.correlated_daemon_seq !== null && leftCorrelated !== rightCorrelated) {
    return leftCorrelated - rightCorrelated;
  }
  const leftDaemon = left.daemon_seq ?? Number.NEGATIVE_INFINITY;
  const rightDaemon = right.daemon_seq ?? Number.NEGATIVE_INFINITY;
  if (leftDaemon !== rightDaemon) return leftDaemon - rightDaemon;
  return (left.timestamp ?? '').localeCompare(right.timestamp ?? '');
}

function latestEventFreshnessTuple(events: PentacleEvent[], streamId: string): EventFreshnessTuple | null {
  let latest: EventFreshnessTuple | null = null;
  for (const event of events) {
    if (String(event.stream_id || '') !== streamId) continue;
    const candidate = eventFreshnessTuple(event);
    if (!latest || compareEventFreshnessTuple(candidate, latest) > 0) latest = candidate;
  }
  return latest;
}

function filterCurrentTailRegression(
  state: PentacleStreamState,
  incoming: PentacleEvent[],
  requestedStreamIds: Set<string>,
  ingressSource: string,
): CurrentTailEventsSelection {
  const streamIds = new Set([...requestedStreamIds, ...incoming.map((event) => String(event.stream_id || '')).filter(Boolean)]);
  const rejectedStreamIds = new Set<string>();
  for (const streamId of streamIds) {
    const localTuple = latestEventFreshnessTuple(state.events, streamId);
    const incomingTuple = latestEventFreshnessTuple(incoming, streamId);
    if (!incomingTuple) continue;
    const decision = localTuple && compareEventFreshnessTuple(incomingTuple, localTuple) < 0 ? 'rejected' : 'accepted';
    logTelemetry(TELEMETRY_EVENTS.CHAT_CURRENT_TAIL_RECOMPOSITION, {
      stream_id: streamId,
      ingress_source: ingressSource,
      local_tuple: localTuple,
      incoming_tuple: incomingTuple,
      decision,
    });
    if (decision === 'rejected') rejectedStreamIds.add(streamId);
  }
  return {
    events: rejectedStreamIds.size
      ? incoming.filter((event) => !rejectedStreamIds.has(String(event.stream_id || '')))
      : incoming,
    rejectedStreamIds: [...rejectedStreamIds],
  };
}

function normalizeFetchedIncomingEvents(rawEvents: PentacleEvent[] | undefined): PentacleEvent[] {
  if (!Array.isArray(rawEvents)) return [];
  return rawEvents.map(normalizeEvent).filter((event) => (
    event.kind !== 'DRAFT' &&
    event.kind !== 'WORKING' &&
    !isHelperSuggestionEvent(event) &&
    (isClaudeJsonlEvent(event) || !isTransientTranscriptNoise(event.text))
  ));
}

export function selectCurrentTailEvents(
  state: PentacleStreamState,
  rawEvents: PentacleEvent[] | undefined,
  requestedStreamId: string | null | undefined,
  ingressSource: string,
): CurrentTailEventsSelection {
  const requestedStreamIds = new Set<string>();
  if (typeof requestedStreamId === 'string' && requestedStreamId) {
    requestedStreamIds.add(requestedStreamId);
  }
  return filterCurrentTailRegression(
    state,
    normalizeFetchedIncomingEvents(rawEvents),
    requestedStreamIds,
    ingressSource,
  );
}

/**
 * Apply a batch of events fetched on demand for a stream (desktop lazy-load).
 *
 * Under `subscribe.events_mode:'summary'` the daemon ships no events in the
 * hello snapshot; a transcript view fetches its stream's recent ring via the
 * `request_stream_events` RPC and the result is delivered as a `stream_events`
 * frame. This merges those events into `state.events` (dedupe by daemon_seq,
 * same content filtering the snapshot reducer applies) and bumps
 * `eventContentVersionByStream` for each stream whose visible content actually
 * changed, so cached selectors (selectSessionDetail) invalidate.
 *
 * Unlike a snapshot it leaves sessions, hosts, working state, drafts, and
 * optimistic sends untouched: this is historical backfill, so it must not
 * regress a live session's summary/working/last_event_at the way per-event
 * application (applyPentacleEvent) would.
 */
export function applyFetchedStreamEvents(
  state: PentacleStreamState,
  rawEvents: PentacleEvent[] | undefined,
  optionsOrLimit: number | FetchedStreamEventsOptions = PENTACLE_RECENT_EVENT_LIMIT,
  requestedStreamIdArg?: string | null,
): PentacleStreamState {
  const limit = typeof optionsOrLimit === 'number'
    ? optionsOrLimit
    : optionsOrLimit.limit ?? PENTACLE_RECENT_EVENT_LIMIT;
  const requestedStreamId = typeof optionsOrLimit === 'number'
    ? requestedStreamIdArg
    : optionsOrLimit.requestedStreamId;
  const requestedStreamIds = new Set<string>();
  if (typeof requestedStreamId === 'string' && requestedStreamId) {
    requestedStreamIds.add(requestedStreamId);
  }
  if (!Array.isArray(rawEvents) || rawEvents.length === 0) {
    if (requestedStreamIds.size > 0) {
      emitOptimisticOrphanSuspicionsAfterFreshFetch(state, requestedStreamIds);
    }
    return state;
  }
  const incoming = carryPriorCorrelatedOptimisticEvents(
    state,
    normalizeFetchedIncomingEvents(rawEvents),
  );
  if (incoming.length === 0) {
    if (requestedStreamIds.size > 0) {
      emitOptimisticOrphanSuspicionsAfterFreshFetch(state, requestedStreamIds);
    }
    return state;
  }

  emitOptimisticOrphanResolutionsFromFetchedEvents(state, incoming);

  const touchedStreamIds = new Set([...requestedStreamIds, ...incoming.map((event) => event.stream_id)]);
  if (typeof optionsOrLimit !== 'number' && optionsOrLimit.mode === 'live') {
    return applyLiveFetchedStreamEvents(state, incoming, touchedStreamIds, limit);
  }
  const acceptedIncoming = typeof optionsOrLimit !== 'number' && optionsOrLimit.mode === 'current-tail'
    ? filterCurrentTailRegression(state, incoming, requestedStreamIds, optionsOrLimit.ingressSource || 'current-tail').events
    : incoming;
  if (acceptedIncoming.length === 0) return state;
  const acceptedTouchedStreamIds = new Set([
    ...requestedStreamIds,
    ...acceptedIncoming.map((event) => event.stream_id),
  ]);
  const beforeFingerprints = new Map<string, string>();
  for (const streamId of acceptedTouchedStreamIds) {
    beforeFingerprints.set(streamId, streamContentFingerprint(state.events, streamId));
  }

  const events = dedupeRecentEventsByStream(
    [...state.events, ...acceptedIncoming.map(freezeEventInDev)],
    limit,
  );

  let eventContentVersionByStream = state.eventContentVersionByStream ?? {};
  let mutated = false;
  for (const streamId of acceptedTouchedStreamIds) {
    if (streamContentFingerprint(events, streamId) !== beforeFingerprints.get(streamId)) {
      if (!mutated) {
        eventContentVersionByStream = { ...eventContentVersionByStream };
        mutated = true;
      }
      eventContentVersionByStream[streamId] = (eventContentVersionByStream[streamId] ?? 0) + 1;
    }
  }
  // Derive the turn phase from the fetched history itself (in-band, event-only)
  // so a turn that started before the detail was opened renders as Working on
  // open (public protocol contract). This NEVER reads
  // the lagging session.working summary: doing so could race-close a live turn
  // that a content event already advanced to 'working' before the daemon's
  // working:true summary landed (the chat_detail_stale_working_flag 007d127
  // MAJOR). The derivation is purely from the in-band events — an open
  // (un-ended) working turn seeds 'working'; a turn whose history ends in a
  // turn-summary / end-of-turn marker resolves to 'idle' (no stale working). A
  // 'pending' optimistic send is left untouched. The authoritative resync paths
  // (snapshot/inventory) still own falling-edge recovery via
  // reconcileWorkingByStreamForResync, where the working flag arrives in-band.
  const nextWorkingByStream = deriveWorkingByStreamFromFetch(
    state.workingByStream,
    events,
    acceptedTouchedStreamIds,
    Date.now(),
  );
  const observedState = mutated
    ? replacePentacleEventProjection({
      ...state,
      workingByStream: nextWorkingByStream ?? state.workingByStream,
    }, events)
    : {
      ...state,
      workingByStream: nextWorkingByStream ?? state.workingByStream,
    };
  emitOptimisticOrphanSuspicionsAfterFreshFetch(observedState, acceptedTouchedStreamIds);

  // All-duplicate batch with no turn-phase transition: nothing observably
  // changed — keep the prior state identity so subscribers don't re-render on a
  // no-op.
  if (!mutated && nextWorkingByStream === undefined) return state;
  return observedState;
}

export function clearPentacleStreamDraft(
  state: PentacleStreamState,
  streamId: string,
): PentacleStreamState {
  const existing = state.sessions.find((item) => item.stream_id === streamId);
  const nextDrafts = { ...state.drafts };
  delete nextDrafts[streamId];
  const nextState = { ...state, drafts: nextDrafts };

  if (!existing || !existing.draft) {
    return nextState;
  }

  return applyPentacleSessionSummary(nextState, {
    ...existing,
    draft: '',
  });
}

export function applyPentacleSnapshotMessage(
  state: PentacleStreamState,
  message: {
    events?: PentacleEvent[];
    drafts?: Record<string, PentacleEvent>;
    hosts?: Record<string, PentacleHostStatus>;
    sessions?: PentacleSessionSummary[];
    limits?: unknown;
    limits_health?: unknown;
    updates?: PentacleUpdateMessage[];
    notifications?: PentacleNotification[];
    working_states?: Record<string, WorkingStateData>;
  },
  limit = PENTACLE_RECENT_EVENT_LIMIT,
  now: number = Date.now(),
): PentacleStreamState {
  const existingSessions = new Map(state.sessions.map((item) => [item.stream_id, item]));
  const sessions = Array.isArray(message.sessions)
    ? sortSessions(message.sessions.map((item) => stripTransientSummary(normalizeSession(item), existingSessions.get(item.stream_id))))
    : [];
  const hosts = mergeHostsWithSessions(message.hosts || {}, sessions);
  const survivingStreamIds = new Set(sessions.map((item) => item.stream_id));
  // An empty events array is NOT authoritative for transcript replacement. Mobile
  // connects with events_mode='summary', so its hello AND reconnect-resync
  // snapshots carry events:[] (the transcript is lazy-fetched via
  // request_stream_events). Treating [] as "replace with nothing" wiped the
  // already-fetched in-memory transcript on every resync — on a lossy link this
  // stranded the chat showing only locally-inserted optimistic rows ("I only see
  // the messages I sent"), unrecoverable until app restart. Only a NON-EMPTY
  // events array (full-events clients, e.g. desktop) replaces authoritatively;
  // otherwise preserve existing events for surviving streams.
  const incomingEvents = Array.isArray(message.events) && message.events.length > 0
    ? message.events
    : null;
  const hasIncomingEvents = incomingEvents !== null;
  const events = incomingEvents
    ? dedupeRecentEventsByStream(incomingEvents.map(normalizeEvent).filter((event) => (
      event.kind !== 'DRAFT' &&
      event.kind !== 'WORKING' &&
      !isHelperSuggestionEvent(event) &&
      (isClaudeJsonlEvent(event) || !isTransientTranscriptNoise(event.text))
    )), limit).map(freezeEventInDev)
    : state.events.filter((event) => survivingStreamIds.has(event.stream_id));
  const drafts: Record<string, PentacleEvent> = {};
  for (const [streamId, draft] of Object.entries(message.drafts || {})) {
    drafts[streamId] = normalizeEvent(draft);
  }

  let workingByStream = reconcileWorkingByStreamForResync(
    state.workingByStream,
    sessions,
    survivingStreamIds,
    now,
  );
  // A full-events snapshot (a working session's hello carrying its in-band USER
  // working root, or a full-events desktop client) can SEED a working turn that
  // reconcileWorkingByStreamForResync — which only CLOSES on working=false —
  // would otherwise miss, leaving an opened working chat idle until the ASSIST
  // flood accumulates. Derive from the in-band snapshot events, but ONLY for
  // streams the daemon authoritatively reports working=true: this is an OPEN-only
  // gate (the 007d127 hazard is about CLOSING from the lagging summary, never
  // opening), and it keeps a working=false stream — whose transcript may still
  // hold stale un-ended activity — from being re-opened against the daemon's own
  // idle summary. The in-band derivation still resolves an ended turn to idle
  // (a strictly-newer end marker wins). Mobile summary snapshots carry events:[]
  // (hasIncomingEvents=false) and skip this entirely, unchanged.
  if (hasIncomingEvents) {
    const workingReportingStreamIds = new Set(
      sessions.filter((item) => item.working === true).map((item) => item.stream_id),
    );
    if (workingReportingStreamIds.size > 0) {
      const seededWorkingByStream = deriveWorkingByStreamFromFetch(
        workingByStream,
        events,
        workingReportingStreamIds,
        now,
      );
      if (seededWorkingByStream) workingByStream = seededWorkingByStream;
    }
  }
  const optimisticSends: Record<string, OptimisticSendState> = {};
  for (const [optimisticId, send] of Object.entries(state.optimisticSends ?? {})) {
    if (survivingStreamIds.has(send.stream_id)) {
      optimisticSends[optimisticId] = send;
      continue;
    }
    // Grace window for spawn-then-send race: see
    // OPTIMISTIC_INVENTORY_GRACE_MS docstring at top of file.
    if (send.status !== 'failed' && now - send.created_at <= OPTIMISTIC_INVENTORY_GRACE_MS) {
      optimisticSends[optimisticId] = send;
    }
  }
  const nextState = replacePentacleEventProjection({
    ...state,
    connected: true,
    connecting: false,
    hasHydrated: true,
    lastError: undefined,
    drafts,
    hosts,
    sessions,
    limits: normalizePentacleLimits(message.limits) ?? state.limits ?? INITIAL_PENTACLE_LIMITS,
    // Health rides only the hello snapshot (never limits.update). Take it when the
    // key is present (even null = "no health"); a reconnect that omits it keeps prior.
    limitsHealth: message.limits_health !== undefined
      ? normalizePentacleLimitsHealth(message.limits_health)
      : (state.limitsHealth ?? null),
    updates: Array.isArray(message.updates) ? message.updates : state.updates,
    workingStates: preserveSnapshotWorkingStates(state.workingStates, message.working_states || {}),
    workingByStream,
    optimisticSends,
    optimisticByRequestId: rebuildOptimisticByRequestId(optimisticSends),
  }, events, hasIncomingEvents ? undefined : [...survivingStreamIds]);
  return Array.isArray(message.notifications)
    ? applyNotificationList(nextState, message.notifications)
    : nextState;
}

// Cheap per-stream content fingerprint for snapshot/inventory comparison.
// Uses (count, last-daemon-seq, last-text length) — enough to detect any
// observable change in the visible transcript without iterating every event.
function streamContentFingerprint(events: PentacleEvent[], streamId: string): string {
  let count = 0;
  let lastSeq: number | undefined;
  let lastTextLen = 0;
  let receiptFingerprint = '';
  for (const item of events) {
    if (item.stream_id !== streamId) continue;
    count += 1;
    lastSeq = Number(item.correlatedDaemonSeq ?? item.daemon_seq);
    lastTextLen = typeof item.text === 'string' ? item.text.length : 0;
    receiptFingerprint += `${item.receiptDirectMatch ? 1 : 0}:${String(item.raw?.receipt_state ?? '')}:${String(item.raw?.receipt_delivery ?? '')}|`;
  }
  return `${count}|${lastSeq ?? ''}|${lastTextLen}|${receiptFingerprint}`;
}

// Project the daemon-owned `hosts.stats` frame. The frame is a complete
// replacement of the fleet map (both in the hello replay and as a live
// broadcast), so hosts absent from it are removed. Malformed hosts are dropped
// by normalizeHostsStats. This is the single ingestion path for machine stats.
export function applyPentacleHostsStats(
  state: PentacleStreamState,
  hosts: Record<string, unknown> | undefined,
): PentacleStreamState {
  return {
    ...state,
    machineStats: normalizeHostsStats(hosts),
  };
}

// --- Desktop compat shims (pre-rename machine-stats API) ---
// The desktop renderer (renderer/src/chat_core_entry.ts barrel +
// chat_store_controller.ts machine.stats/.inventory handlers) still imports the
// machine-stats API from before it became the host-keyed `hosts.stats` frame.
// These are thin wrappers over the current API so the desktop builds and runs at
// this core pin; migrating the desktop consumer to applyPentacleHostsStats is a
// tracked backlog item. Pinned by tests/desktopCompatExports.test.ts.
export function applyPentacleMachineStats(
  state: PentacleStreamState,
  rawStats: PentacleMachineStats,
): PentacleStreamState {
  const host = String((rawStats as { host?: unknown } | null | undefined)?.host ?? '');
  return {
    ...state,
    machineStats: { ...state.machineStats, ...normalizeHostsStats({ [host]: rawStats }) },
  };
}

export function applyPentacleMachineStatsInventory(
  state: PentacleStreamState,
  machineStats: Record<string, unknown> | undefined,
): PentacleStreamState {
  return applyPentacleHostsStats(state, machineStats);
}

// Codex usage is surfaced through `limits` (applyPentacleLimits) now, and no
// consumer reads a codexUsage field; the desktop imports this only through its
// re-export barrel and never invokes it. Kept as a no-op pass-through so the
// barrel resolves until the desktop consumer migrates.
export function applyPentacleCodexUsage(
  state: PentacleStreamState,
  _usage: unknown,
): PentacleStreamState {
  return state;
}

const LIMIT_IDENTITIES: ReadonlyArray<Pick<PentacleLimit, 'id' | 'label'>> = [
  { id: 'claude', label: 'Claude' },
  { id: 'fable', label: 'Fable' },
  { id: 'codex', label: 'Codex' },
];

export function normalizePentacleLimits(value: unknown): PentacleLimit[] | null {
  if (!Array.isArray(value) || value.length !== LIMIT_IDENTITIES.length) return null;
  const normalized: PentacleLimit[] = [];
  for (let index = 0; index < LIMIT_IDENTITIES.length; index += 1) {
    const raw = value[index];
    const identity = LIMIT_IDENTITIES[index];
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
    const record = raw as Record<string, unknown>;
    // Validate only the fields the client renders; tolerate additional daemon
    // fields (upstream_reported_at, probed_at, and any future column). A strict
    // exact field-set match here is what blanked the phone when the daemon rows
    // grew from 5 to 7 fields — the client projects, it does not gate on schema.
    if (record.id !== identity.id || record.label !== identity.label) return null;
    if (record.pct !== null && (
      typeof record.pct !== 'number' || !Number.isInteger(record.pct) || record.pct < 0 || record.pct > 100
    )) return null;
    if (record.resets_at_iso !== null && typeof record.resets_at_iso !== 'string') return null;
    if (record.resets_text !== null && typeof record.resets_text !== 'string') return null;
    normalized.push({
      id: identity.id,
      label: identity.label,
      pct: record.pct as number | null,
      resets_at_iso: record.resets_at_iso as string | null,
      resets_text: record.resets_text as string | null,
    });
  }
  return normalized;
}

export function applyPentacleLimits(
  state: PentacleStreamState,
  limits: unknown,
): PentacleStreamState {
  const normalized = normalizePentacleLimits(limits);
  if (!normalized) return state;
  return {
    ...state,
    limits: normalized,
  };
}

const LIMIT_HEALTH_PROVIDER_IDS = ['claude', 'fable', 'codex'] as const;
// Outcomes the daemon treats as "usage is available" (usage_state.PERSISTED_OUTCOMES
// minus the failure set). Anything else is a probe failure that must render as text.
const LIMIT_HEALTH_OK_OUTCOMES = new Set(['ok', 'never']);

function normalizeProviderHealth(value: unknown): PentacleProviderHealth | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.outcome !== 'string') return null;
  let error: PentacleProviderHealth['error'] = null;
  if (record.error && typeof record.error === 'object' && !Array.isArray(record.error)) {
    const raw = record.error as Record<string, unknown>;
    error = {
      code: typeof raw.code === 'string' ? raw.code : null,
      message: typeof raw.message === 'string' ? raw.message : null,
    };
  }
  return {
    outcome: record.outcome,
    error,
    attempted_at: typeof record.attempted_at === 'string' ? record.attempted_at : null,
    upstream_reported_at: typeof record.upstream_reported_at === 'string' ? record.upstream_reported_at : null,
    probed_at: typeof record.probed_at === 'string' ? record.probed_at : null,
    stale_after_seconds: typeof record.stale_after_seconds === 'number' ? record.stale_after_seconds : null,
  };
}

export function normalizePentacleLimitsHealth(value: unknown): PentacleLimitsHealth | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const health: PentacleLimitsHealth = {
    schema_version: typeof record.schema_version === 'number' ? record.schema_version : 1,
  };
  for (const providerId of LIMIT_HEALTH_PROVIDER_IDS) {
    const provider = normalizeProviderHealth(record[providerId]);
    if (provider) health[providerId] = provider;
  }
  return health;
}

// Projects a provider's probe health to the short line the client renders under
// the limit row. Returns null when there is nothing to say (healthy/fresh), a
// staleness note when the last successful probe aged past its threshold, and the
// upstream error message for a failed probe. The row itself is never hidden.
export function limitHealthText(
  health: PentacleProviderHealth | null | undefined,
  now: number = Date.now(),
): string | null {
  if (!health) return null;
  if (LIMIT_HEALTH_OK_OUTCOMES.has(health.outcome)) {
    if (health.probed_at && typeof health.stale_after_seconds === 'number') {
      const probedMs = Date.parse(health.probed_at);
      if (Number.isFinite(probedMs) && now - probedMs > health.stale_after_seconds * 1000) {
        return 'Usage may be stale';
      }
    }
    return null;
  }
  return health.error?.message || 'Usage unavailable';
}

function notificationTime(record: PentacleNotification): number {
  const parsed = Date.parse(String(record.created_at || ''));
  return Number.isFinite(parsed) ? parsed : 0;
}

function sortNotificationsNewestFirst(records: PentacleNotification[]): PentacleNotification[] {
  return [...records].sort((a, b) => notificationTime(b) - notificationTime(a));
}

// Upsert a single notification record by notification_id, keeping the slice
// sorted newest-first by created_at. A record with a known id replaces the
// existing entry in place (then re-sorts); a new id is added. Mirrors the
// live `notification` broadcast frame from chat_streamd.
export function applyNotificationFrame(
  state: PentacleStreamState,
  record: PentacleNotification | null | undefined,
): PentacleStreamState {
  if (!record || !record.notification_id) return state;
  const existing = state.notifications ?? [];
  const index = existing.findIndex((item) => item.notification_id === record.notification_id);
  let next: PentacleNotification[];
  if (index === -1) {
    next = [record, ...existing];
  } else {
    next = existing.slice();
    next[index] = record;
  }
  return {
    ...state,
    notifications: sortNotificationsNewestFirst(next),
  };
}

// Replace the entire notification slice with the given list (newest-first).
// Used to apply a `notification.list.ok` RPC reply.
export function applyNotificationList(
  state: PentacleStreamState,
  records: PentacleNotification[] | null | undefined,
): PentacleStreamState {
  return {
    ...state,
    notifications: Array.isArray(records) ? sortNotificationsNewestFirst(records) : [],
  };
}

export function applyPentacleUpdates(
  state: PentacleStreamState,
  updates: PentacleUpdateMessage[] | undefined,
): PentacleStreamState {
  return {
    ...state,
    updates: Array.isArray(updates) ? updates : [],
  };
}

export function applyPentacleHostStatus(
  state: PentacleStreamState,
  rawHost: PentacleHostStatus,
): PentacleStreamState {
  const host = normalizeHostStatus(rawHost);
  const liveSessionCount = state.sessions.filter((session) => session.host === host.host && session.online).length;
  return {
    ...state,
    hosts: mergeHostsWithSessions(
      {
        ...state.hosts,
        [host.host]: {
          ...host,
          online: host.online || liveSessionCount > 0,
          session_count: Math.max(host.session_count, liveSessionCount),
          error: liveSessionCount > 0 ? undefined : host.error,
        },
      },
      state.sessions,
    ),
  };
}

export function applyPentacleSessionInventory(
  state: PentacleStreamState,
  sessions: PentacleSessionSummary[],
  now: number = Date.now(),
): PentacleStreamState {
  const existingSessions = new Map(state.sessions.map((item) => [item.stream_id, item]));
  const sorted = sortSessions(sessions.map((item) => stripTransientSummary(
    normalizeSession(item),
    existingSessions.get(item.stream_id),
  )));
  const survivingStreamIds = new Set(sorted.map((item) => item.stream_id));
  const workingByStream = reconcileWorkingByStreamForResync(
    state.workingByStream,
    sorted,
    survivingStreamIds,
    now,
  );
  const eventContentVersionByStream: Record<string, number> = {};
  for (const [streamId, version] of Object.entries(state.eventContentVersionByStream ?? {})) {
    if (survivingStreamIds.has(streamId)) {
      eventContentVersionByStream[streamId] = version;
    }
  }
  const optimisticSends: Record<string, OptimisticSendState> = {};
  for (const [optimisticId, send] of Object.entries(state.optimisticSends ?? {})) {
    if (survivingStreamIds.has(send.stream_id)) {
      optimisticSends[optimisticId] = send;
      continue;
    }
    // Grace window for spawn-then-send race: a daemon-broadcast inventory
    // generated mid-spawn may not yet include our newly-spawned stream,
    // but our optimistic for that stream is valid and should not be
    // dropped. Keep optimistics younger than OPTIMISTIC_INVENTORY_GRACE_MS;
    // they'll be pruned by the normal reconcile-or-timeout flow if the
    // stream really is gone.
    if (send.status !== 'failed' && now - send.created_at <= OPTIMISTIC_INVENTORY_GRACE_MS) {
      optimisticSends[optimisticId] = send;
    }
  }
  return {
    ...state,
    hasHydrated: true,
    sessions: sorted,
    hosts: mergeHostsWithSessions(state.hosts, sorted),
    workingByStream,
    optimisticSends,
    optimisticByRequestId: rebuildOptimisticByRequestId(optimisticSends),
    eventContentVersionByStream,
  };
}
