import type {
  EventBucketCoverage,
  EventBucketPins,
  EventBucketRequestState,
  EventBucketRequestWindow,
  PentacleEvent,
  PentacleEventBucket,
  PentacleStreamState,
} from '../types/pentacle';

export const PENTACLE_PER_STREAM_MAX_EVENTS = 1200;
// Interim G5 budget: one unit is one plain event; text, attachments, and
// optimistic rows add deterministic schema-weighted units below. Lane 1B must
// revisit this 20-stream assumption using peak weighted cost + device memory.
export const PENTACLE_TOTAL_EVENT_COST_MAX = 20 * PENTACLE_PER_STREAM_MAX_EVENTS;

export const PENTACLE_EVENT_COST = Object.freeze({
  base: 1,
  textCharsPerUnit: 256,
  attachment: 8,
  optimistic: 1,
});

export type EventBucketPinKind = keyof EventBucketPins;
export type EventBucketAccessSource = 'focus' | 'render' | 'prefetch-complete';

export type PentacleEventBucketMutation =
  | { type: 'append'; events: readonly PentacleEvent[] }
  | { type: 'progressive-replace'; streamId: string; event: PentacleEvent }
  | { type: 'optimistic-replace'; streamId: string; optimisticId: string; event: PentacleEvent }
  | { type: 'optimistic-remove'; streamId: string; optimisticId: string }
  | { type: 'replace-stream'; streamId: string; events: readonly PentacleEvent[] }
  | { type: 'evict-stream'; streamId: string }
  | { type: 'snapshot-replace'; events: readonly PentacleEvent[]; retainedStreamIds?: readonly string[] }
  | { type: 'reset'; reason: 'refresh' | 'credential-change' | 'endpoint-change' }
  | { type: 'touch'; streamId: string; source: EventBucketAccessSource }
  | { type: 'set-pin'; streamId: string; pin: EventBucketPinKind; pinned: boolean }
  | { type: 'set-coverage'; streamId: string; coverage?: EventBucketCoverage }
  | { type: 'set-request'; streamId: string; request?: EventBucketRequestState }
  | {
    type: 'set-request-window';
    streamId: string;
    window: EventBucketRequestWindow;
    request?: EventBucketRequestState;
    coverage?: EventBucketCoverage;
    replaceCoverage?: boolean;
  };

export type PentacleEventRetentionOptions = {
  perStreamMaxEvents?: number;
  totalEventCostMax?: number;
};

type ValidationObserver = {
  visitBucketRows?: (count?: number) => void;
  rebuiltDerivedIndex?: () => void;
};

let validationObserver: ValidationObserver | null = null;
const derivedProjections = new WeakSet<object>();
const eventObjectIds = new WeakMap<object, number>();
const canonicalEventArrayVersions = new WeakMap<object, number>();
const legacyStreamVersions = new Map<string, number>();
let nextEventObjectId = 1;
let nextCanonicalEventArrayVersion = 2_000_000_000;
let nextLegacyStreamVersion = 1_000_000;

/** Test-only G4 hook. Production leaves the observer unset and pays one null check. */
export function setPentacleEventBucketValidationObserver(observer: ValidationObserver | null) {
  validationObserver = observer;
}

export function weightedPentacleEventCost(event: PentacleEvent): number {
  const textUnits = Math.ceil(String(event.text || '').length / PENTACLE_EVENT_COST.textCharsPerUnit);
  const attachmentCount = Array.isArray(event.attachments) ? event.attachments.length : 0;
  return PENTACLE_EVENT_COST.base + textUnits +
    attachmentCount * PENTACLE_EVENT_COST.attachment +
    (event.client_origin === true ? PENTACLE_EVENT_COST.optimistic : 0);
}

function retainedCost(events: readonly PentacleEvent[]) {
  return events.reduce((total, event) => total + weightedPentacleEventCost(event), 0);
}

function sameEventReferences(left: readonly PentacleEvent[], right: readonly PentacleEvent[]) {
  return left.length === right.length && left.every((event, index) => event === right[index]);
}

function latestEventAt(events: readonly PentacleEvent[]) {
  let value: string | undefined;
  let latest = Number.NEGATIVE_INFINITY;
  for (const event of events) {
    const parsed = Date.parse(String(event.timestamp || ''));
    if (Number.isFinite(parsed) && parsed > latest) {
      latest = parsed;
      value = event.timestamp;
    }
  }
  return value;
}

function freezeEvents(events: readonly PentacleEvent[]): PentacleEvent[] {
  const copied = events.map((event) => {
    if (Object.isFrozen(event)) return event;
    return Object.freeze(event);
  });
  return Object.freeze(copied) as unknown as PentacleEvent[];
}

function capStreamEvents(events: readonly PentacleEvent[], limit: number) {
  if (events.length <= limit) return events;
  const removeCount = events.length - limit;
  const removable = events
    .map((event, index) => ({ event, index }))
    .filter(({ event }) => event.client_origin !== true)
    .slice(0, removeCount);
  const removed = new Set(removable.map(({ index }) => index));
  if (removed.size < removeCount) {
    for (let index = 0; index < events.length && removed.size < removeCount; index += 1) {
      removed.add(index);
    }
  }
  return events.filter((_event, index) => !removed.has(index));
}

function activeOptimisticPin(state: PentacleStreamState, streamId: string) {
  return Object.values(state.optimisticSends ?? {}).some((send) => send.stream_id === streamId);
}

function activeInFlightPin(state: PentacleStreamState, streamId: string) {
  return (state.workingByStream?.[streamId]?.phase ?? 'idle') !== 'idle';
}

function effectivePins(
  state: PentacleStreamState,
  streamId: string,
  explicitPins: EventBucketPins | undefined,
  request?: EventBucketRequestState,
  requestsByWindow?: Partial<Record<EventBucketRequestWindow, EventBucketRequestState>>,
): EventBucketPins {
  const hasWindowRequest = Object.values(requestsByWindow ?? {}).some((entry) => (
    entry?.status === 'loading' || entry?.status === 'prefetching'
  ));
  return {
    ...(explicitPins?.focused ? { focused: true } : {}),
    ...((explicitPins?.optimistic || activeOptimisticPin(state, streamId)) ? { optimistic: true } : {}),
    ...((explicitPins?.inFlight || activeInFlightPin(state, streamId) || hasWindowRequest || request?.status === 'loading' || request?.status === 'prefetching') ? { inFlight: true } : {}),
  };
}

function bucketPinned(bucket: PentacleEventBucket) {
  return Boolean(bucket.pins.focused || bucket.pins.optimistic || bucket.pins.inFlight);
}

function groupByStream(events: readonly PentacleEvent[]) {
  const grouped = new Map<string, PentacleEvent[]>();
  for (const event of events) {
    const streamId = String(event.stream_id || '');
    if (!streamId) continue;
    const rows = grouped.get(streamId);
    if (rows) rows.push(event);
    else grouped.set(streamId, [event]);
  }
  return grouped;
}

function eventSortTuple(event: PentacleEvent, ordinal: number) {
  const time = Date.parse(String(event.timestamp || ''));
  const seq = Number(event.correlatedDaemonSeq ?? event.daemon_seq);
  return {
    time: Number.isFinite(time) ? time : Number.POSITIVE_INFINITY,
    seq: Number.isFinite(seq) ? seq : Number.POSITIVE_INFINITY,
    tie: `${event.stream_id}:${event.kind}:${event.optimistic_id || ''}:${ordinal}`,
  };
}

// Memoized on the event ref: events are frozen and their references are reused
// across mutations (an unchanged event keeps its object across deliveries), so
// the 6-field join is computed once per event ever, then O(1) lookup. This is
// the hot path - compatibilityProjection computes identity 2-3x per event on
// every delivery; at 19.2k retained events the un-memoized join was the ingest
// ceiling (spec tap_shell_layout_regression_build_1155). Pure/behaviour-identical.
const eventIdentityCache = new WeakMap<object, string>();
function eventIdentity(event: PentacleEvent) {
  const cached = eventIdentityCache.get(event as unknown as object);
  if (cached !== undefined) return cached;
  const identity = [
    event.stream_id,
    event.optimistic_id || '',
    event.jsonl_record_uuid || '',
    event.correlatedDaemonSeq ?? event.daemon_seq,
    event.timestamp,
    event.kind,
  ].join('\u0000');
  eventIdentityCache.set(event as unknown as object, identity);
  return identity;
}

function compatibilityProjection(
  buckets: Record<string, PentacleEventBucket>,
  preferred: readonly PentacleEvent[],
) {
  const available = new Map<string, PentacleEvent[]>();
  for (const bucket of Object.values(buckets)) {
    for (const event of bucket.events) {
      const key = eventIdentity(event);
      const queue = available.get(key);
      if (queue) queue.push(event);
      else available.set(key, [event]);
    }
  }
  const projection: PentacleEvent[] = [];
  for (const event of preferred) {
    const queue = available.get(eventIdentity(event));
    const current = queue?.shift();
    if (current) projection.push(current);
  }
  for (const streamId of Object.keys(buckets).sort()) {
    for (const event of buckets[streamId].events) {
      const queue = available.get(eventIdentity(event));
      if (queue?.[0] === event) {
        projection.push(event);
        queue.shift();
      }
    }
  }
  return freezeEvents(projection);
}

function chronologicalEvents(buckets: Record<string, PentacleEventBucket>) {
  const rows: Array<{ event: PentacleEvent; ordinal: number }> = [];
  let ordinal = 0;
  for (const streamId of Object.keys(buckets).sort()) {
    for (const event of buckets[streamId].events) rows.push({ event, ordinal: ordinal++ });
  }
  rows.sort((left, right) => {
    const a = eventSortTuple(left.event, left.ordinal);
    const b = eventSortTuple(right.event, right.ordinal);
    if (a.time !== b.time) return a.time - b.time;
    if (a.seq !== b.seq) return a.seq - b.seq;
    return a.tie.localeCompare(b.tie);
  });
  return freezeEvents(rows.map(({ event }) => event));
}

function contentVersions(buckets: Record<string, PentacleEventBucket>) {
  return Object.fromEntries(Object.entries(buckets).map(([streamId, bucket]) => [streamId, bucket.contentVersion]));
}

function withDerivedProjection(
  state: PentacleStreamState,
  buckets: Record<string, PentacleEventBucket>,
  revision: number,
  preferred: readonly PentacleEvent[] = state.events,
): PentacleStreamState {
  const events = compatibilityProjection(buckets, preferred);
  derivedProjections.add(events);
  return {
    ...state,
    eventBucketsByStream: buckets,
    eventBucketMutationRevision: revision,
    eventContentVersionByStream: contentVersions(buckets),
    events,
  };
}

export function normalizePentacleEventBuckets(state: PentacleStreamState): PentacleStreamState {
  if (state.eventBucketsByStream) {
    if (derivedProjections.has(state.events)) return state;
    const bucketRows = Object.values(state.eventBucketsByStream).flatMap((bucket) => bucket.events);
    if (bucketRows.length === 0 && state.events.length === 0) return state;
    if (
      bucketRows.length === state.events.length &&
      bucketRows.every((event) => state.events.includes(event))
    ) {
      return withDerivedProjection(
        state,
        state.eventBucketsByStream,
        state.eventBucketMutationRevision ?? 0,
        state.events,
      );
    }
  }
  const revision = state.eventBucketMutationRevision ?? 0;
  const preferredProjection = Array.from(state.events || []);
  const grouped = groupByStream(preferredProjection);
  const buckets: Record<string, PentacleEventBucket> = {};
  for (const [streamId, rows] of grouped) {
    const events = freezeEvents(rows);
    buckets[streamId] = {
      events,
      contentVersion: state.eventContentVersionByStream?.[streamId] ?? (events.length ? 1 : 0),
      latestEventAt: latestEventAt(events),
      mutationRevision: revision,
      lastAccessRevision: 0,
      retainedCost: retainedCost(events),
      pins: effectivePins(state, streamId, undefined),
    };
  }
  return withDerivedProjection(state, buckets, revision, preferredProjection);
}

export function pentacleEventContentVersion(state: PentacleStreamState, streamId: string) {
  const stored = Math.max(
    state.eventBucketsByStream?.[streamId]?.contentVersion ?? 0,
    state.eventContentVersionByStream?.[streamId] ?? 0,
  );
  if (derivedProjections.has(state.events)) {
    const events = state.eventBucketsByStream?.[streamId]?.events;
    if (!events) return stored;
    let identityVersion = canonicalEventArrayVersions.get(events);
    if (!identityVersion) {
      identityVersion = nextCanonicalEventArrayVersion++;
      canonicalEventArrayVersions.set(events, identityVersion);
    }
    return Math.max(stored, identityVersion);
  }
  const fingerprint = state.events
    .filter((event) => event.stream_id === streamId)
    .map((event) => {
      let id = eventObjectIds.get(event);
      if (!id) {
        id = nextEventObjectId++;
        eventObjectIds.set(event, id);
      }
      return id;
    })
    .join(':');
  let projectionRevision = legacyStreamVersions.get(fingerprint);
  if (!projectionRevision) {
    projectionRevision = nextLegacyStreamVersion++;
    legacyStreamVersions.set(fingerprint, projectionRevision);
  }
  return Math.max(stored, projectionRevision);
}

export function peekEventsForStream(state: PentacleStreamState, streamId: string): readonly PentacleEvent[] {
  const normalized = normalizePentacleEventBuckets(state);
  return normalized.eventBucketsByStream?.[streamId]?.events ?? [];
}

export function eventsForStream(state: PentacleStreamState, streamId: string): readonly PentacleEvent[] {
  const rows = peekEventsForStream(state, streamId);
  validationObserver?.visitBucketRows?.(rows.length);
  return rows;
}

export function retainedPentacleEventCost(state: PentacleStreamState) {
  if (state.eventBucketsByStream) {
    return Object.values(state.eventBucketsByStream).reduce((total, bucket) => total + bucket.retainedCost, 0);
  }
  return retainedCost(state.events);
}

function newEmptyBucket(state: PentacleStreamState, streamId: string, revision: number): PentacleEventBucket {
  return {
    events: freezeEvents([]),
    contentVersion: 0,
    latestEventAt: undefined,
    mutationRevision: revision,
    lastAccessRevision: 0,
    retainedCost: 0,
    pins: effectivePins(state, streamId, undefined),
  };
}

function writeBucketEvents(
  state: PentacleStreamState,
  buckets: Record<string, PentacleEventBucket>,
  streamId: string,
  nextRows: readonly PentacleEvent[],
  revision: number,
  limit: number,
) {
  const previous = buckets[streamId];
  const capped = capStreamEvents(nextRows, limit);
  const didCap = capped.length < nextRows.length;
  const unchanged = previous && sameEventReferences(previous.events, capped);
  if (unchanged) return revision;
  const nextRevision = revision + 1;
  const events = freezeEvents(capped);
  buckets[streamId] = {
    ...(previous ?? newEmptyBucket(state, streamId, nextRevision)),
    events,
    contentVersion: (previous?.contentVersion ?? 0) + 1,
    latestEventAt: latestEventAt(events),
    mutationRevision: nextRevision,
    retainedCost: retainedCost(events),
    pins: effectivePins(state, streamId, previous?.explicitPins, previous?.request, previous?.requestsByWindow),
    ...(didCap ? invalidateBucketCoverage(previous, events) : {}),
  };
  return nextRevision;
}

function invalidateBucketCoverage(previous: PentacleEventBucket | undefined, events: readonly PentacleEvent[]) {
  const stale = (coverage: EventBucketCoverage): EventBucketCoverage => ({
    ...coverage,
    complete: false,
    authoritativeZero: false,
    cursor: Number.isFinite(Number(events[0]?.daemon_seq)) ? Number(events[0]?.daemon_seq) : null,
    freshUntil: undefined,
  });
  const coverageByWindow = Object.fromEntries(
    Object.entries(previous?.coverageByWindow ?? {}).map(([window, coverage]) => [window, stale(coverage)]),
  ) as Partial<Record<EventBucketRequestWindow, EventBucketCoverage>>;
  return {
    ...(previous?.coverage ? { coverage: stale(previous.coverage) } : {}),
    ...(Object.keys(coverageByWindow).length ? { coverageByWindow } : {}),
  };
}

function enforceRetention(
  buckets: Record<string, PentacleEventBucket>,
  maxCost: number,
) {
  let total = Object.values(buckets).reduce((sum, bucket) => sum + bucket.retainedCost, 0);
  const candidates = Object.entries(buckets)
    .filter(([, bucket]) => bucket.retainedCost > 0 && !bucketPinned(bucket))
    .sort(([leftId, left], [rightId, right]) => (
      left.lastAccessRevision - right.lastAccessRevision || leftId.localeCompare(rightId)
    ));
  const evicted: string[] = [];
  for (const [streamId, bucket] of candidates) {
    if (total <= maxCost) break;
    total -= bucket.retainedCost;
    delete buckets[streamId];
    evicted.push(streamId);
  }
  return evicted;
}

/**
 * Sole event-write surface. Lanes 3/4 may also use the serializable request,
 * coverage, access, and pin mutations; request Promises never enter state.
 */
export function mutatePentacleEventBuckets(
  input: PentacleStreamState,
  mutation: PentacleEventBucketMutation,
  retention: PentacleEventRetentionOptions = {},
): PentacleStreamState {
  const state = normalizePentacleEventBuckets(input);
  const perStreamMax = retention.perStreamMaxEvents ?? PENTACLE_PER_STREAM_MAX_EVENTS;
  const maxCost = retention.totalEventCostMax ?? PENTACLE_TOTAL_EVENT_COST_MAX;
  const buckets = { ...(state.eventBucketsByStream ?? {}) };
  let revision = state.eventBucketMutationRevision ?? 0;

  const replace = (streamId: string, rows: readonly PentacleEvent[]) => {
    revision = writeBucketEvents(state, buckets, streamId, rows, revision, perStreamMax);
  };

  switch (mutation.type) {
    case 'append': {
      for (const [streamId, rows] of groupByStream(mutation.events)) {
        replace(streamId, [...(buckets[streamId]?.events ?? []), ...rows]);
      }
      break;
    }
    case 'progressive-replace': {
      const current = buckets[mutation.streamId]?.events ?? [];
      const index = current.findIndex((event) => (
        (mutation.event.jsonl_record_uuid && event.jsonl_record_uuid === mutation.event.jsonl_record_uuid) ||
        (mutation.event.optimistic_id && event.optimistic_id === mutation.event.optimistic_id) ||
        (event.daemon_seq === mutation.event.daemon_seq && event.kind === mutation.event.kind)
      ));
      replace(mutation.streamId, index < 0
        ? [...current, mutation.event]
        : current.map((event, rowIndex) => rowIndex === index ? mutation.event : event));
      break;
    }
    case 'optimistic-replace': {
      const current = buckets[mutation.streamId]?.events ?? [];
      replace(mutation.streamId, current.map((event) => (
        event.client_origin === true && event.optimistic_id === mutation.optimisticId
          ? mutation.event
          : event
      )));
      break;
    }
    case 'optimistic-remove': {
      const current = buckets[mutation.streamId]?.events ?? [];
      replace(mutation.streamId, current.filter((event) => !(
        event.client_origin === true && event.optimistic_id === mutation.optimisticId
      )));
      break;
    }
    case 'replace-stream':
      replace(mutation.streamId, mutation.events);
      break;
    case 'evict-stream':
      if (buckets[mutation.streamId]) {
        delete buckets[mutation.streamId];
        revision += 1;
      }
      break;
    case 'snapshot-replace': {
      const grouped = groupByStream(mutation.events);
      const retained = new Set(mutation.retainedStreamIds ?? [
        ...state.sessions.map((session) => session.stream_id),
        ...grouped.keys(),
        ...Object.entries(buckets).filter(([, bucket]) => bucketPinned(bucket)).map(([streamId]) => streamId),
      ]);
      for (const streamId of new Set([...Object.keys(buckets), ...retained, ...grouped.keys()])) {
        if (!retained.has(streamId)) {
          if (buckets[streamId]) {
            delete buckets[streamId];
            revision += 1;
          }
          continue;
        }
        replace(streamId, grouped.get(streamId) ?? []);
      }
      break;
    }
    case 'reset':
      if (Object.keys(buckets).length) revision += 1;
      for (const streamId of Object.keys(buckets)) delete buckets[streamId];
      break;
    case 'touch': {
      const previous = buckets[mutation.streamId] ?? newEmptyBucket(state, mutation.streamId, revision);
      revision += 1;
      buckets[mutation.streamId] = {
        ...previous,
        lastAccessRevision: revision,
        mutationRevision: revision,
        pins: effectivePins(state, mutation.streamId, previous.explicitPins, previous.request, previous.requestsByWindow),
      };
      break;
    }
    case 'set-pin': {
      const previous = buckets[mutation.streamId] ?? newEmptyBucket(state, mutation.streamId, revision);
      const explicitPins = {
        ...previous.explicitPins,
        [mutation.pin]: mutation.pinned || undefined,
      };
      revision += 1;
      buckets[mutation.streamId] = {
        ...previous,
        mutationRevision: revision,
        explicitPins,
        pins: effectivePins(state, mutation.streamId, explicitPins, previous.request, previous.requestsByWindow),
      };
      break;
    }
    case 'set-coverage':
    case 'set-request': {
      const previous = buckets[mutation.streamId] ?? newEmptyBucket(state, mutation.streamId, revision);
      revision += 1;
      buckets[mutation.streamId] = {
        ...previous,
        mutationRevision: revision,
        ...(mutation.type === 'set-coverage'
          ? { coverage: mutation.coverage }
          : {
            request: mutation.request,
            pins: effectivePins(state, mutation.streamId, previous.explicitPins, mutation.request, previous.requestsByWindow),
          }),
      };
      break;
    }
    case 'set-request-window': {
      const previous = buckets[mutation.streamId] ?? newEmptyBucket(state, mutation.streamId, revision);
      const requestsByWindow = { ...(previous.requestsByWindow ?? {}) };
      const coverageByWindow = { ...(previous.coverageByWindow ?? {}) };
      if (mutation.request) requestsByWindow[mutation.window] = mutation.request;
      else delete requestsByWindow[mutation.window];
      if (mutation.replaceCoverage) {
        if (mutation.coverage) coverageByWindow[mutation.window] = mutation.coverage;
        else delete coverageByWindow[mutation.window];
      }
      revision += 1;
      buckets[mutation.streamId] = {
        ...previous,
        mutationRevision: revision,
        requestsByWindow,
        coverageByWindow,
        coverage: mutation.window === 'history' && mutation.replaceCoverage
          ? mutation.coverage
          : previous.coverage,
        request: mutation.window === 'history' ? mutation.request : previous.request,
        pins: effectivePins(state, mutation.streamId, previous.explicitPins, undefined, requestsByWindow),
      };
      break;
    }
  }

  for (const [streamId, bucket] of Object.entries(buckets)) {
    const pins = effectivePins(state, streamId, bucket.explicitPins, bucket.request, bucket.requestsByWindow);
    if (pins.focused !== bucket.pins.focused || pins.optimistic !== bucket.pins.optimistic || pins.inFlight !== bucket.pins.inFlight) {
      buckets[streamId] = { ...bucket, pins };
    }
  }
  const evicted = enforceRetention(buckets, maxCost);
  revision += evicted.length;
  const preferred = mutation.type === 'snapshot-replace'
    ? mutation.events
    : mutation.type === 'reset'
      ? []
      : state.events;
  return withDerivedProjection(state, buckets, revision, preferred);
}

export function replacePentacleEventProjection(
  state: PentacleStreamState,
  events: readonly PentacleEvent[],
  retainedStreamIds?: readonly string[],
) {
  return mutatePentacleEventBuckets(state, { type: 'snapshot-replace', events, retainedStreamIds });
}

// Incremental single-live-event delivery. The prior path rebuilt the entire flat
// `state.events` on every event — `dedupeRecentEventsByStream([...state.events,
// event])` + a `snapshot-replace` that re-groups every stream + a full
// `compatibilityProjection` — i.e. O(total-retained-events) per delivery, which
// climbs as the store grows and is the shared root of the build-1155 tap_shell /
// all_chats set_state / 19.2k-throughput regressions (spec
// tap_shell_layout_regression_build_1155). Caller guarantees `event` is genuinely
// new to its stream (no optimistic/daemon_seq predecessor — the branch condition
// in applyPentacleEvent), so a plain per-stream append is equivalent to the
// global per-stream dedupe. When the append neither caps the stream nor triggers
// cost eviction, the flat projection is exactly the prior list with this one
// event appended (O(n) copy) instead of a full O(n) map-rebuild+dedupe; on any
// cap/eviction it must drop rows, so fall back to the full projection — byte-for-
// byte the prior behaviour.
export function appendLiveEventProjection(
  input: PentacleStreamState,
  event: PentacleEvent,
  retention: PentacleEventRetentionOptions = {},
): PentacleStreamState {
  const state = normalizePentacleEventBuckets(input);
  const perStreamMax = retention.perStreamMaxEvents ?? PENTACLE_PER_STREAM_MAX_EVENTS;
  const maxCost = retention.totalEventCostMax ?? PENTACLE_TOTAL_EVENT_COST_MAX;
  const streamId = String(event.stream_id || '');
  const buckets = { ...(state.eventBucketsByStream ?? {}) };
  let revision = state.eventBucketMutationRevision ?? 0;
  const previousEvents = buckets[streamId]?.events ?? [];
  revision = writeBucketEvents(state, buckets, streamId, [...previousEvents, event], revision, perStreamMax);
  const appendedStreamEvents = buckets[streamId]?.events ?? [];
  const evicted = enforceRetention(buckets, maxCost);
  revision += evicted.length;
  // Fast path only when the stream grew by exactly this one event (no per-stream
  // cap) and nothing was cost-evicted — then the flat list is the prior list
  // plus this tail. Otherwise rows were dropped: rebuild via the full projection.
  const noRowsDropped = evicted.length === 0
    && appendedStreamEvents.length === previousEvents.length + 1;
  const events = noRowsDropped
    ? freezeEvents([...state.events, appendedStreamEvents[appendedStreamEvents.length - 1]])
    : compatibilityProjection(buckets, state.events);
  derivedProjections.add(events);
  return {
    ...state,
    eventBucketsByStream: buckets,
    eventBucketMutationRevision: revision,
    eventContentVersionByStream: contentVersions(buckets),
    events,
  };
}

// Batched analogue of appendLiveEventProjection: apply a whole live batch with
// per-stream bucket appends and a SINGLE flat-list copy, instead of a
// snapshot-replace that re-groups every stream and re-runs compatibilityProjection
// (an O(total) eventIdentity-string rebuild allocated fresh per batch — the GC
// pressure that made the 19.2k freeze flood's per-batch wall climb 2s→8s and
// starved the trailing burst; spec tap_shell_layout_regression_build_1155 S3 /
// Fix design 2-3). Caller guarantees every event is genuinely new to its stream
// in daemon_seq order (applyLiveFetchedStreamEvents' dedupe/cap loop falls back to
// the full path otherwise). `flatEvents` is the caller's already-built
// `[...state.events, ...appended]`. On any per-stream cap or cost eviction, rows
// were dropped, so rebuild via the full projection — byte-for-byte the prior
// behaviour.
export function appendLiveEventsProjection(
  input: PentacleStreamState,
  eventsByStream: ReadonlyMap<string, readonly PentacleEvent[]>,
  flatEvents: readonly PentacleEvent[],
  retention: PentacleEventRetentionOptions = {},
): PentacleStreamState {
  const state = normalizePentacleEventBuckets(input);
  const perStreamMax = retention.perStreamMaxEvents ?? PENTACLE_PER_STREAM_MAX_EVENTS;
  const maxCost = retention.totalEventCostMax ?? PENTACLE_TOTAL_EVENT_COST_MAX;
  const buckets = { ...(state.eventBucketsByStream ?? {}) };
  let revision = state.eventBucketMutationRevision ?? 0;
  let cappedStream = false;
  for (const [streamId, appended] of eventsByStream) {
    if (!streamId || !appended.length) continue;
    const previousEvents = buckets[streamId]?.events ?? [];
    revision = writeBucketEvents(state, buckets, streamId, [...previousEvents, ...appended], revision, perStreamMax);
    if ((buckets[streamId]?.events.length ?? 0) !== previousEvents.length + appended.length) cappedStream = true;
  }
  const evicted = enforceRetention(buckets, maxCost);
  revision += evicted.length;
  // Fast path only when no stream hit its per-stream cap and nothing was
  // cost-evicted — then the flat list is exactly the caller's append order.
  const events = !cappedStream && evicted.length === 0
    ? freezeEvents(flatEvents)
    : compatibilityProjection(buckets, state.events);
  derivedProjections.add(events);
  return {
    ...state,
    eventBucketsByStream: buckets,
    eventBucketMutationRevision: revision,
    eventContentVersionByStream: contentVersions(buckets),
    events,
  };
}

export type PentacleDerivedEventIndex = {
  byStream: ReadonlyMap<string, readonly PentacleEvent[]>;
  chronological: readonly PentacleEvent[];
  revision: number;
};

let cachedBuckets: Record<string, PentacleEventBucket> | undefined;
let cachedProjection: readonly PentacleEvent[] | undefined;
let cachedIndex: PentacleDerivedEventIndex | undefined;

export function selectPentacleDerivedEventIndex(state: PentacleStreamState): PentacleDerivedEventIndex {
  if (
    cachedProjection === state.events &&
    (!state.eventBucketsByStream || cachedBuckets === state.eventBucketsByStream) &&
    cachedIndex
  ) return cachedIndex;
  const normalized = normalizePentacleEventBuckets(state);
  const buckets = normalized.eventBucketsByStream ?? {};
  // `chronological` is an O(n log n) sort of every retained event, but the
  // hottest consumer (the all-chats list reconcile via `selectSmartChatList`)
  // only reads `byStream`. Compute it lazily/once-per-index so a byStream-only
  // access does not pay the whole-store sort on every delivery as the store
  // grows (spec tap_shell_layout_regression_build_1155). Behaviour-identical:
  // the same frozen array is returned on read, memoised on this index object.
  let chronologicalCache: readonly PentacleEvent[] | undefined;
  const index: PentacleDerivedEventIndex = {
    byStream: new Map(Object.entries(buckets).map(([streamId, bucket]) => [streamId, bucket.events])),
    get chronological() {
      if (chronologicalCache === undefined) chronologicalCache = chronologicalEvents(buckets);
      return chronologicalCache;
    },
    revision: normalized.eventBucketMutationRevision ?? 0,
  };
  validationObserver?.rebuiltDerivedIndex?.();
  cachedBuckets = state.eventBucketsByStream;
  cachedProjection = state.events;
  cachedIndex = index;
  return index;
}
