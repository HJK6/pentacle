import type {
  ChatAttachment,
  PentacleEvent,
  PentacleHostStatus,
  PentacleMachineStats,
  PentacleSendState,
  PentacleSessionSummary,
  PentacleStreamState,
  OptimisticSendStatus,
} from '../types/pentacle';
import { getHostOrder, getHostTheme } from './hostConfig';
import { logTelemetry } from '../utils/telemetry';
import { TELEMETRY_EVENTS } from '../utils/telemetryEvents';
import { normalizePentacleHost } from './pentacleHosts';
import {
  coalesceInterpretedEvents,
  collapseCodeBlocks,
  findCoalescibleTerminalDividerIndex,
  interpretPentacleEvent,
  isTransientTranscriptNoise,
  normalizeWorkingLabel,
  normalizedEventText,
  type PentacleDisplayRule,
  type PentacleDisclosurePresentation,
  type PentacleInterpretedEvent,
} from './pentacleEventInterpreter';
import { isSystemEndOfTurnEvent } from './pentacleStreamReducer';
import {
  eventsForStream,
  pentacleEventContentVersion,
  peekEventsForStream,
  selectPentacleDerivedEventIndex,
} from './pentacleEventBuckets';

export type PentacleSessionStatus = 'unresponsive' | 'working' | 'sending' | 'live' | 'idle' | 'offline';
export type PentacleTranscriptTone = 'user' | 'agent' | 'assistant' | 'tool' | 'thinking' | 'system';
export type PentacleReceiptCaption = 'sending' | 'sent' | 'failed';

export type PentacleMachineCard = {
  host: string;
  title: string;
  online: boolean;
  sessionCount: number;
  statusLabel: string;
  error?: string;
  hostStatusReason?: string;
  hostStatusSince?: string;
};

export type PentacleMachineStatsCard = PentacleMachineCard & {
  stats?: PentacleMachineStats;
};

export type PentacleChatOverview = {
  connectionLabel: string;
  connectionTone: 'live' | 'connecting' | 'error';
  liveCount: number;
  machineCount: number;
  sessionCount: number;
};

export type PentacleChatListItem = {
  streamId: string;
  host: string;
  hostTitle: string;
  provider: string;
  sessionName: string;
  title: string;
  previewText: string;
  status: PentacleSessionStatus;
  statusLabel: string;
  workingElapsedSeconds?: number | null;
  sending: boolean;
  sendingImmediate: boolean;
  updatedLabel: string;
  draft: string;
  hostStatus?: string;
  hostStatusReason?: string;
  hostStatusSince?: string;
};

export type PentacleUnifiedFeedItem = {
  id: string;
  streamId: string;
  host: string;
  hostTitle: string;
  provider: string;
  sessionName: string;
  chatTitle: string;
  accent: string;
  timestampLabel: string;
  text: string;
  kind: string;
  event: PentacleEvent;
};

export type PentacleOpenQuestionItem = {
  streamId: string;
  host: string;
  hostTitle: string;
  provider: string;
  sessionName: string;
  chatTitle: string;
  accent: string;
  timestampLabel: string;
  question: NonNullable<PentacleSessionSummary['question']>;
};

export type PentacleTranscriptItem = {
  id: string;
  timestampLabel: string;
  label: string;
  tone: PentacleTranscriptTone;
  provider: string;
  source: string;
  text: string;
  kind: string;
  isUser: boolean;
  eventCase: string;
  displayRule: PentacleDisplayRule;
  disclosure?: PentacleDisclosurePresentation;
  pending?: boolean;
  receiptCaption?: PentacleReceiptCaption;
  // B1 (chat_send_turn_lifecycle_batch2): the optimistic send's user-visible
  // lifecycle for a client-origin row — 'sending' while unconfirmed, 'failed'/
  // 'cancelled' on terminal non-delivery, undefined once confirmed (normal
  // bubble). Absent for server-origin rows.
  sendState?: PentacleSendState;
  queuedWhileWorking?: boolean;
  eventKey?: string;
  optimisticId?: string;
  correlatedDaemonSeq?: number | null;
  // Present on `agent-question-answer` rows: the durable notification this row echoes, so a
  // client holding the same answer as a resolved-notification projection can render one of them
  // rather than both.
  notificationId?: string;
  // Image attachments for a client-origin USER bubble (spec ## Attachment
  // model). Copied from the underlying PentacleEvent so every view (desktop +
  // mobile) renders the media bubble from the same transcript shape. FIFO order.
  attachments?: ChatAttachment[];
};

/** A summary-only row that is safe to paint before transcript derivation. */
export type PentacleSafeSummaryPreview = {
  key: string;
  text: string;
  label: string;
  tone: PentacleTranscriptTone;
  kind: string;
};

export type PentacleSessionDetail = {
  streamId: string;
  title: string;
  hostTitle: string;
  providerLabel: string;
  status: PentacleSessionStatus;
  statusLabel: string;
  summaryLabel: string;
  workingLabel: string;
  draftText: string;
  transcriptItems: PentacleTranscriptItem[];
  hiddenCount: number;
  remainingCount: number;
  /**
   * ISO timestamp of the newest event we actually hold for this stream, across
   * ALL kinds (including hidden/tool/system rows) — i.e. "how far our local
   * transcript reaches". Undefined when we hold no events yet. The session
   * screen compares this against the resilient `session.last_event_at` summary
   * to detect a live `chat.event` frame that was dropped on a lossy link (the
   * summary advances even when the one-shot event frame is lost) and trigger a
   * recovery refetch. Derived from the focused bucket, so it changes only when
   * stream content changes (already gated by `eventContentVersionByStream`).
   */
  latestEventAt?: string;
};

type SessionDetailCacheEntry = {
  detail: PentacleSessionDetail;
  sourceEvents: readonly PentacleEvent[];
};

const sessionDetailCache = new Map<string, SessionDetailCacheEntry>();
// Stage 5c — track the per-stream content-version that the cached detail was
// computed against. When the live `state.eventContentVersionByStream[streamId]`
// advances, the cache hit short-circuit is skipped and a new detail object is
// returned even if every other field happens to match (Bug C: in-place
// progressive updates used to slip past the equality check and leave the
// previous render reference returned, blocking re-render).
const sessionDetailVersionByKey = new Map<string, number>();
const sessionDetailInputSignatureByKey = new Map<string, string>();
const interpretedCache = new WeakMap<PentacleEvent, { label: string; value: PentacleInterpretedEvent }>();
const SLICE_CUSHION = 24;

let interpretMissCount = 0;

function getInterpreted(event: PentacleEvent, label: string): PentacleInterpretedEvent {
  const hit = interpretedCache.get(event);
  if (hit && hit.label === label) return hit.value;

  // interpretPentacleEvent is pure for a given event object and assistant label.
  interpretMissCount += 1;
  const value = interpretPentacleEvent(event, label);
  interpretedCache.set(event, { label, value });
  return value;
}

export function __getInterpretMissCountForTests() {
  return interpretMissCount;
}

export function __resetInterpretMissCountForTests() {
  interpretMissCount = 0;
}

function resolveLiveFlags(
  session: PentacleSessionSummary | undefined,
  draftEvent?: PentacleEvent,
): { working: boolean; workingLabel: string } {
  const working = Boolean(session?.working || draftEvent?.raw?.working);
  const rawWorkingLabel = String(session?.working_label || draftEvent?.raw?.working_label || '').trim();
  const workingLabel = working
    ? normalizeWorkingLabel(rawWorkingLabel)
    : '';
  return { working, workingLabel };
}

export { getHostOrder };

function resolveHostTitle(host: string) {
  return getHostTheme(host).label;
}

function trimPreview(text: string, fallback: string) {
  const value = String(text || '').replace(/\s+/g, ' ').trim();
  return value || fallback;
}

function displayTitleForSession(session: PentacleSessionSummary) {
  const title = String(session.title || session.display_name || '').trim();
  return title || session.session_name;
}

function formatClock(timestamp?: string) {
  if (!timestamp) return '';
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function hostAccent(host: string) {
  const theme = getHostTheme(host);
  return theme.accent || theme.color || '';
}

function timestampMinuteKey(timestamp?: string) {
  if (!timestamp) return '';
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return '';
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}-${date.getHours()}-${date.getMinutes()}`;
}

function eventTimestampMs(event: PentacleEvent): number {
  const ms = Date.parse(String(event.timestamp || ''));
  return Number.isFinite(ms) ? ms : Number.NaN;
}

function authoritativeEventSeq(event: PentacleEvent): number {
  if (event.correlatedDaemonSeq !== null && event.correlatedDaemonSeq !== undefined) {
    const correlated = Number(event.correlatedDaemonSeq);
    if (Number.isFinite(correlated)) return correlated;
  }
  const seq = Number(event.daemon_seq);
  return Number.isFinite(seq) ? seq : Number.NaN;
}

function hasReceiptField(event: PentacleEvent, field: 'receipt_state' | 'receipt_delivery'): boolean {
  return Boolean(event.raw) && Object.prototype.hasOwnProperty.call(event.raw, field);
}

function normalizedReceiptField(event: PentacleEvent, field: 'receipt_state' | 'receipt_delivery'): string {
  return String(event.raw?.[field] ?? '').trim().toLowerCase();
}

function isDirectMatchedUserEcho(event: PentacleEvent): boolean {
  return event.client_origin === true && Boolean(event.optimistic_id) &&
    event.receiptDirectMatch === true &&
    Number.isFinite(Number(event.correlatedDaemonSeq));
}

function receiptCaptionForLatestUserEvent(event: PentacleEvent): PentacleReceiptCaption | undefined {
  if (!event.client_origin || !event.optimistic_id) return undefined;
  if (!Number.isFinite(Number(event.correlatedDaemonSeq))) return 'sending';
  if (!isDirectMatchedUserEcho(event)) return undefined;
  if (normalizedReceiptField(event, 'receipt_state') === 'landed') return 'sent';
  if (normalizedReceiptField(event, 'receipt_delivery') === 'proof_unavailable') return 'failed';
  if (!hasReceiptField(event, 'receipt_state') && !hasReceiptField(event, 'receipt_delivery')) return 'sent';
  return 'sending';
}

type TranscriptEventSortKey = {
  seq: number;
  hasSeq: boolean;
  time: number;
  hasTime: boolean;
  pairRank: number;
  deterministicTie: string;
};

function transcriptEventDeterministicTie(event: PentacleEvent): string {
  return [
    String(event.stream_id || ''),
    String(event.raw?.tool_use_id || ''),
    String(event.kind || ''),
    String(event.raw?.uuid || event.jsonl_record_uuid || ''),
    String(event.daemon_seq),
    String(event.timestamp || ''),
    String(event.text || ''),
  ].join('\u0000');
}

function shouldPreferClaudeToolUseCandidate(candidate: PentacleEvent, current: PentacleEvent): boolean {
  const candidateSeq = authoritativeEventSeq(candidate);
  const currentSeq = authoritativeEventSeq(current);
  const candidateHasSeq = Number.isFinite(candidateSeq);
  const currentHasSeq = Number.isFinite(currentSeq);
  if (candidateHasSeq && currentHasSeq && candidateSeq !== currentSeq) return candidateSeq < currentSeq;
  if (candidateHasSeq !== currentHasSeq) return candidateHasSeq;

  const candidateTime = eventTimestampMs(candidate);
  const currentTime = eventTimestampMs(current);
  const candidateHasTime = Number.isFinite(candidateTime);
  const currentHasTime = Number.isFinite(currentTime);
  if (candidateHasTime && currentHasTime && candidateTime !== currentTime) return candidateTime < currentTime;
  if (candidateHasTime !== currentHasTime) return candidateHasTime;

  return transcriptEventDeterministicTie(candidate).localeCompare(transcriptEventDeterministicTie(current)) < 0;
}

function buildTranscriptEventSortKeys(events: readonly PentacleEvent[]): Map<PentacleEvent, TranscriptEventSortKey> {
  const claudeToolUseById = new Map<string, PentacleEvent>();
  for (const event of events) {
    if (event.raw?.source !== 'claude-jsonl') continue;
    if (event.kind !== 'TOOL_USE') continue;
    const toolUseId = String(event.raw?.tool_use_id || '').trim();
    if (!toolUseId) continue;
    const current = claudeToolUseById.get(toolUseId);
    if (!current || shouldPreferClaudeToolUseCandidate(event, current)) {
      claudeToolUseById.set(toolUseId, event);
    }
  }

  const keys = new Map<PentacleEvent, TranscriptEventSortKey>();
  for (const event of events) {
    const toolUseId = String(event.raw?.tool_use_id || '').trim();
    const matchedToolUse = event.raw?.source === 'claude-jsonl' && event.kind === 'TOOL_RESULT' && toolUseId
      ? claudeToolUseById.get(toolUseId)
      : undefined;
    const primary = matchedToolUse || event;
    const seq = authoritativeEventSeq(primary);
    const time = eventTimestampMs(primary);
    keys.set(event, {
      seq,
      hasSeq: Number.isFinite(seq),
      time,
      hasTime: Number.isFinite(time),
      pairRank: matchedToolUse ? 1 : 0,
      deterministicTie: transcriptEventDeterministicTie(event),
    });
  }
  return keys;
}

function compareTranscriptEventSortKeys(left: TranscriptEventSortKey, right: TranscriptEventSortKey): number {
  if (left.hasSeq && right.hasSeq && left.seq !== right.seq) return left.seq - right.seq;
  if (left.hasTime && right.hasTime && left.time !== right.time) return left.time - right.time;
  if (left.hasTime !== right.hasTime) return left.hasTime ? -1 : 1;
  if (left.hasSeq !== right.hasSeq) return left.hasSeq ? -1 : 1;
  if (left.pairRank !== right.pairRank) return left.pairRank - right.pairRank;
  return left.deterministicTie.localeCompare(right.deterministicTie);
}

function sortTranscriptEventsForDisplay(events: PentacleEvent[]): PentacleEvent[] {
  const sortKeys = buildTranscriptEventSortKeys(events);
  return events
    .map((event, index) => ({ event, index }))
    .sort((left, right) => {
      const leftKey = sortKeys.get(left.event);
      const rightKey = sortKeys.get(right.event);
      if (!leftKey || !rightKey) return left.index - right.index;
      return compareTranscriptEventSortKeys(leftKey, rightKey) || left.index - right.index;
    })
    .map((item) => item.event);
}

type TranscriptEventCandidate = {
  event: PentacleEvent;
  index: number;
  key: TranscriptEventSortKey;
};

function compareTranscriptEventCandidates(left: TranscriptEventCandidate, right: TranscriptEventCandidate): number {
  return compareTranscriptEventSortKeys(left.key, right.key) || left.index - right.index;
}

function insertTranscriptEventCandidate(candidates: TranscriptEventCandidate[], candidate: TranscriptEventCandidate) {
  let insertAt = candidates.length;
  while (insertAt > 0 && compareTranscriptEventCandidates(candidate, candidates[insertAt - 1]) < 0) {
    insertAt -= 1;
  }
  candidates.splice(insertAt, 0, candidate);
}

function transcriptBaseEventsForDisplay(
  events: readonly PentacleEvent[],
  streamId: string,
  visibleCount: number | 'all',
  shouldKeepEvent: (event: PentacleEvent) => boolean = () => true,
  shouldForceIncludeEvent: (event: PentacleEvent) => boolean = () => false,
  shouldCountTowardWindow: (event: PentacleEvent) => boolean = () => true,
) {
  if (visibleCount === 'all') {
    return {
      baseEvents: sortTranscriptEventsForDisplay(
        events.filter((item) => item.stream_id === streamId && item.kind !== 'DRAFT' && shouldKeepEvent(item)),
      ),
      remainingOutsideWindow: 0,
    };
  }

  const baseMaxCandidates = Math.max(0, visibleCount + Math.min(SLICE_CUSHION, visibleCount));
  if (baseMaxCandidates === 0) {
    let remainingOutsideWindow = 0;
    for (const event of events) {
      if (event.stream_id === streamId && event.kind !== 'DRAFT' && shouldKeepEvent(event)) remainingOutsideWindow += 1;
    }
    return { baseEvents: [] as PentacleEvent[], remainingOutsideWindow };
  }

  const eligible: Array<{ event: PentacleEvent; index: number }> = [];
  for (let index = 0; index < events.length; index += 1) {
    const event = events[index];
    if (event.stream_id !== streamId || event.kind === 'DRAFT') continue;
    if (!shouldKeepEvent(event)) continue;
    eligible.push({ event, index });
  }

  const likelyFilteredTailCount = eligible
    .slice(-baseMaxCandidates)
    .reduce((count, item) => count + (shouldCountTowardWindow(item.event) ? 0 : 1), 0);
  const maxCandidates = baseMaxCandidates + Math.min(baseMaxCandidates, likelyFilteredTailCount);

  const sortKeys = buildTranscriptEventSortKeys(eligible.map((item) => item.event));
  const selected: TranscriptEventCandidate[] = [];
  const forced: TranscriptEventCandidate[] = [];
  let normalEligibleCount = 0;

  for (const item of eligible) {
    const key = sortKeys.get(item.event);
    if (!key) continue;
    const candidate = { event: item.event, index: item.index, key };
    if (shouldForceIncludeEvent(item.event)) {
      forced.push(candidate);
      continue;
    }
    normalEligibleCount += 1;
    if (selected.length < maxCandidates) {
      insertTranscriptEventCandidate(selected, candidate);
      continue;
    }
    if (selected.length > 0 && compareTranscriptEventCandidates(candidate, selected[0]) > 0) {
      selected.shift();
      insertTranscriptEventCandidate(selected, candidate);
    }
  }

  const selectedEvents = [...selected, ...forced]
    .sort(compareTranscriptEventCandidates)
    .map((item) => item.event);
  return {
    baseEvents: selectedEvents,
    remainingOutsideWindow: Math.max(0, normalEligibleCount - selected.length),
  };
}

function formatRelative(timestamp?: string, dateFormatter?: Intl.DateTimeFormat) {
  if (!timestamp) return 'No activity yet';
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return 'No activity yet';

  const deltaMs = Date.now() - date.getTime();
  const minute = 60_000;
  const hour = 60 * minute;
  const day = 24 * hour;

  if (deltaMs < minute) return 'Just now';
  if (deltaMs < hour) return `${Math.max(1, Math.round(deltaMs / minute))}m ago`;
  if (deltaMs < day) return `${Math.max(1, Math.round(deltaMs / hour))}h ago`;
  return dateFormatter
    ? dateFormatter.format(date)
    : date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function resolveMachineStatus(
  host: string,
  hosts: Record<string, PentacleHostStatus>,
  sessions: PentacleSessionSummary[],
): PentacleMachineCard {
  const normalizedHost = normalizePentacleHost(host);
  const matchingHosts = Object.values(hosts).filter((status) => normalizePentacleHost(status.host) === normalizedHost);
  const base = matchingHosts.reduce<PentacleHostStatus>(
    (current, status) => ({
      host: normalizedHost,
      online: current.online || status.online,
      checked_at: status.checked_at || current.checked_at,
      session_count: Math.max(current.session_count || 0, status.session_count || 0),
      error: current.online || status.online ? undefined : status.error || current.error,
      host_status_reason: status.host_status_reason || current.host_status_reason,
      host_status_since: status.host_status_since || current.host_status_since,
    }),
    { host: normalizedHost, online: false, checked_at: '', session_count: 0 },
  );
  const sessionCount = sessions.filter((session) => normalizePentacleHost(session.host) === normalizedHost).length;
  const online = Boolean(base.online || sessionCount > 0);
  return {
    host: normalizedHost,
    title: resolveHostTitle(normalizedHost),
    online,
    sessionCount: Math.max(base.session_count || 0, sessionCount),
    statusLabel: online ? 'Online' : 'Offline',
    error: online ? undefined : base.error,
    hostStatusReason: base.host_status_reason,
    hostStatusSince: base.host_status_since,
  };
}

export function getPentacleSessionStatus(session?: PentacleSessionSummary): PentacleSessionStatus {
  if (!session) return 'offline';
  if (!session.online) return 'offline';
  if (session.pane_status === 'pane_unresponsive') return 'unresponsive';
  if (session.working) return 'working';
  if (session.last_event_at) return 'live';
  return 'idle';
}

export function getPentacleSessionStatusLabel(status: PentacleSessionStatus) {
  if (status === 'unresponsive') return 'Unresponsive';
  if (status === 'working') return 'Working';
  if (status === 'sending') return 'Sending';
  if (status === 'live') return 'Live';
  if (status === 'idle') return 'Idle';
  return 'Offline';
}

function groupClaudeJsonlSidechains(events: PentacleEvent[]): PentacleEvent[] {
  const uuidToEvent = new Map<string, PentacleEvent>();
  const agentByToolUseId = new Map<string, PentacleEvent>();
  const toolNameByToolUseId = new Map<string, string>();
  const toolInputByToolUseId = new Map<string, unknown>();
  const childCounts = new Map<string, number>();
  const hidden = new Set<PentacleEvent>();

  for (const event of events) {
    const uuid = String(event.raw?.uuid || '');
    if (uuid) uuidToEvent.set(uuid, event);
    if (
      event.raw?.source === 'claude-jsonl' &&
      event.kind === 'TOOL_USE'
    ) {
      const toolUseId = String(event.raw?.tool_use_id || '');
      if (toolUseId) {
        toolNameByToolUseId.set(toolUseId, String(event.raw?.tool_name || ''));
        if (event.raw?.tool_input && typeof event.raw.tool_input === 'object') {
          toolInputByToolUseId.set(toolUseId, event.raw.tool_input);
        }
        if (event.raw?.tool_name === 'Agent') agentByToolUseId.set(toolUseId, event);
      }
    }
  }

  const findAgentParent = (event: PentacleEvent): PentacleEvent | undefined => {
    const directToolUseId = String(event.raw?.parent_tool_use_id || event.raw?.tool_use_id || '');
    if (directToolUseId && agentByToolUseId.has(directToolUseId)) return agentByToolUseId.get(directToolUseId);

    const visited = new Set<string>();
    let parentUuid = String(event.raw?.parent_uuid || '');
    while (parentUuid && !visited.has(parentUuid)) {
      visited.add(parentUuid);
      const parent = uuidToEvent.get(parentUuid);
      if (!parent) return undefined;
      if (parent.kind === 'TOOL_USE' && parent.raw?.tool_name === 'Agent') return parent;
      parentUuid = String(parent.raw?.parent_uuid || '');
    }
    return undefined;
  };

  for (const event of events) {
    if (event.raw?.source !== 'claude-jsonl') continue;
    if (event.kind === 'TOOL_RESULT' && agentByToolUseId.has(String(event.raw?.tool_use_id || ''))) {
      const parent = agentByToolUseId.get(String(event.raw?.tool_use_id || ''));
      if (parent) {
        const parentKey = String(parent.raw?.tool_use_id || '');
        childCounts.set(parentKey, (childCounts.get(parentKey) || 0) + 1);
        hidden.add(event);
      }
      continue;
    }
    if (!event.raw?.is_sidechain) continue;
    const parent = findAgentParent(event);
    const parentKey = String(parent?.raw?.tool_use_id || '');
    if (!parentKey) continue;
    childCounts.set(parentKey, (childCounts.get(parentKey) || 0) + 1);
    hidden.add(event);
  }

  return events
    .filter((event) => !hidden.has(event))
    .map((event) => {
      const toolUseId = String(event.raw?.tool_use_id || '');
      const childCount = childCounts.get(toolUseId) || 0;
      const inferredToolName = event.kind === 'TOOL_RESULT' && !event.raw?.tool_name
        ? toolNameByToolUseId.get(toolUseId)
        : undefined;
      const pairedToolInput = event.kind === 'TOOL_RESULT' && !event.raw?.tool_input
        ? toolInputByToolUseId.get(toolUseId)
        : undefined;
      if (!childCount && !inferredToolName && !pairedToolInput) return event;
      return {
        ...event,
        raw: {
          ...event.raw,
          ...(childCount ? { child_count: childCount } : {}),
          ...(inferredToolName ? { tool_name: inferredToolName } : {}),
          ...(pairedToolInput ? { tool_input: pairedToolInput } : {}),
        },
      };
    });
}

function sameChatListItem(a: PentacleChatListItem, b: PentacleChatListItem) {
  return (
    a.streamId === b.streamId &&
    a.host === b.host &&
    a.hostTitle === b.hostTitle &&
    a.provider === b.provider &&
    a.sessionName === b.sessionName &&
    a.title === b.title &&
    a.previewText === b.previewText &&
    a.status === b.status &&
    a.statusLabel === b.statusLabel &&
    a.workingElapsedSeconds === b.workingElapsedSeconds &&
    a.sending === b.sending &&
    a.sendingImmediate === b.sendingImmediate &&
    a.updatedLabel === b.updatedLabel &&
    a.draft === b.draft &&
    a.hostStatus === b.hostStatus &&
    a.hostStatusReason === b.hostStatusReason &&
    a.hostStatusSince === b.hostStatusSince
  );
}

function sameTranscriptItem(a: PentacleTranscriptItem, b: PentacleTranscriptItem) {
  return (
    a.id === b.id &&
    a.timestampLabel === b.timestampLabel &&
    a.label === b.label &&
    a.tone === b.tone &&
    a.provider === b.provider &&
    a.source === b.source &&
    a.text === b.text &&
    a.kind === b.kind &&
    a.isUser === b.isUser &&
    a.eventKey === b.eventKey &&
    a.optimisticId === b.optimisticId &&
    a.correlatedDaemonSeq === b.correlatedDaemonSeq &&
    a.eventCase === b.eventCase &&
    a.displayRule === b.displayRule &&
    a.disclosure?.previewText === b.disclosure?.previewText &&
    a.disclosure?.previewTail === b.disclosure?.previewTail &&
    a.disclosure?.expandable === b.disclosure?.expandable &&
    a.disclosure?.expandedText === b.disclosure?.expandedText &&
    a.pending === b.pending &&
    a.receiptCaption === b.receiptCaption &&
    a.sendState === b.sendState &&
    a.queuedWhileWorking === b.queuedWhileWorking &&
    a.attachments === b.attachments
  );
}

// B1/B4 (chat_send_turn_lifecycle_batch2): map an optimistic send to the
// user-visible row lifecycle. A held send (B4 turn_queued — typed behind an
// in-flight turn, not yet dispatched) shows 'queued'. Otherwise: 'sending' until
// the daemon confirms delivery (acked) or echoes the message back
// (echoed/reconciled → normal bubble); 'failed'/'cancelled' on terminal
// non-delivery. Returns undefined for a confirmed/unknown status so the row
// renders as an ordinary bubble.
function deriveSendState(
  send: { status?: OptimisticSendStatus; turn_queued?: boolean } | undefined,
): PentacleSendState | undefined {
  if (!send) return undefined;
  if (send.turn_queued === true) return 'queued';
  switch (send.status) {
    case 'queued':
    case 'dispatched':
    case 'indeterminate':
      return 'sending';
    case 'failed':
      return 'failed';
    case 'cancelled':
      return 'cancelled';
    default:
      // acked / echoed / reconciled / undefined → confirmed, normal bubble.
      return undefined;
  }
}

export const SESSION_SENDING_VISIBLE_AFTER_MS = 275;

export type SessionSendingState = {
  sending: boolean;
  immediate: boolean;
};

export function getSessionSendingState(
  state: Pick<PentacleStreamState, 'optimisticSends' | 'workingByStream'>,
  streamId: string,
): SessionSendingState {
  const turn = state.workingByStream?.[streamId];
  if (turn?.phase === 'working') return { sending: false, immediate: false };
  let delayed = false;
  for (const send of Object.values(state.optimisticSends ?? {})) {
    if (send.stream_id !== streamId || deriveSendState(send) !== 'sending') continue;
    if ((send.attachments?.length ?? 0) > 0) {
      return { sending: true, immediate: true };
    }
    delayed = true;
  }
  return { sending: delayed, immediate: false };
}

export function isSessionSending(
  state: Pick<PentacleStreamState, 'optimisticSends' | 'workingByStream'>,
  streamId: string,
) {
  return getSessionSendingState(state, streamId).sending;
}

function reuseTranscriptItem(
  previousItems: Map<string, PentacleTranscriptItem>,
  nextItem: PentacleTranscriptItem,
) {
  const previous = previousItems.get(nextItem.id);
  return previous && sameTranscriptItem(previous, nextItem) ? previous : nextItem;
}

export function selectMachineStatusList(state: PentacleStreamState) {
  return getHostOrder(state).map((host) => resolveMachineStatus(host, state.hosts, state.sessions));
}

export function selectMachineStatsTabs(state: PentacleStreamState): PentacleMachineStatsCard[] {
  return selectMachineStatusList(state).map((machine) => ({
    ...machine,
    stats: state.machineStats[normalizePentacleHost(machine.host)],
  }));
}

export function selectChatOverview(state: PentacleStreamState): PentacleChatOverview {
  const liveCount = selectMachineStatusList(state).filter((item) => item.online).length;
  const machineCount = getHostOrder(state).length;
  if (state.connected) {
    return {
      connectionLabel: 'Realtime stream live',
      connectionTone: 'live',
      liveCount,
      machineCount,
      sessionCount: state.sessions.length,
    };
  }
  if (state.connecting) {
    return {
      connectionLabel: 'Reconnecting to Pentacle',
      connectionTone: 'connecting',
      liveCount,
      machineCount,
      sessionCount: state.sessions.length,
    };
  }
  return {
    connectionLabel: state.lastError || 'Pentacle stream offline',
    connectionTone: 'error',
    liveCount,
    machineCount,
    sessionCount: state.sessions.length,
  };
}

export type PentacleChatEventIndex = ReadonlyMap<string, readonly PentacleEvent[]>;

export function buildPentacleChatEventIndex(events: readonly PentacleEvent[]): PentacleChatEventIndex {
  const eventsByStream = new Map<string, PentacleEvent[]>();
  for (const event of events) {
    const streamEvents = eventsByStream.get(event.stream_id);
    if (streamEvents) streamEvents.push(event);
    else eventsByStream.set(event.stream_id, [event]);
  }
  return eventsByStream;
}

export type PentacleChatListSelector = (
  state: PentacleStreamState,
  filter?: 'all' | string,
) => PentacleChatListItem[];

export function createChatListSelector(): PentacleChatListSelector {
  const previousByFilter = new Map<string, PentacleChatListItem[]>();

  return (state: PentacleStreamState, filter: 'all' | string = 'all') => {
    // Share locale setup across this synchronous selection, refreshing defaults
    // on the next selection so locale/time-zone changes cannot leave stale labels.
    const dateFormatter = new Intl.DateTimeFormat([], { month: 'short', day: 'numeric' });
    const eventIndex = selectPentacleDerivedEventIndex(state).byStream;

    const previous = previousByFilter.get(filter) || [];
    const previousItems = new Map(previous.map((item) => [item.streamId, item]));
    const next = state.sessions
      .filter((session) => filter === 'all' || normalizePentacleHost(session.host) === filter)
      .map((session) => {
        const liveFlags = resolveLiveFlags(session, state.drafts[session.stream_id]);
        const host = normalizePentacleHost(session.host);
        const sendingState = getSessionSendingState(state, session.stream_id);
        const status = !session.online
          ? 'offline'
          : session.pane_status === 'pane_unresponsive'
            ? 'unresponsive'
            : liveFlags.working
              ? 'working'
              : sendingState.sending
                ? 'sending'
                : session.last_event_at
                  ? 'live'
                  : 'idle';
        const workingElapsedSeconds = liveFlags.working
          ? Math.floor(Math.max(0, state.workingStates?.[session.stream_id]?.elapsed_ms ?? 0) / 1000)
          : null;
        const candidate: PentacleChatListItem = {
          streamId: session.stream_id,
          host,
          hostTitle: resolveHostTitle(host),
          provider: session.provider.toUpperCase(),
          sessionName: session.session_name,
          title: displayTitleForSession(session),
          previewText: latestDisplayedSessionPreviewFromEvents(
            state,
            session,
            'Waiting for first message…',
            eventIndex.get(session.stream_id),
            Boolean(state.eventBucketsByStream?.[session.stream_id]),
            dateFormatter,
          ),
          status,
          statusLabel: getPentacleSessionStatusLabel(status),
          workingElapsedSeconds,
          sending: status === 'sending',
          sendingImmediate: status === 'sending' && sendingState.immediate,
          updatedLabel: formatRelative(session.last_event_at, dateFormatter),
          draft: '',
          hostStatus: session.host_status,
          hostStatusReason: session.host_status_reason,
          hostStatusSince: session.host_status_since,
        };
        const existing = previousItems.get(candidate.streamId);
        return existing && sameChatListItem(existing, candidate) ? existing : candidate;
      });

    if (previous.length === next.length && previous.every((item, index) => item === next[index])) {
      return previous;
    }

    previousByFilter.set(filter, next);
    return next;
  };
}

export function selectChatList(
  state: PentacleStreamState,
  filter: 'all' | string = 'all',
): PentacleChatListItem[] {
  return createChatListSelector()(state, filter);
}

export function latestDisplayedSessionPreview(
  state: PentacleStreamState,
  session: PentacleSessionSummary,
  fallback = 'Waiting for first message…',
  streamEvents?: readonly PentacleEvent[],
): string {
  return latestDisplayedSessionPreviewFromEvents(
    state,
    session,
    fallback,
    streamEvents ?? peekEventsForStream(state, session.stream_id),
    streamEvents === undefined,
  );
}

function latestDisplayedSessionPreviewFromEvents(
  state: PentacleStreamState,
  session: PentacleSessionSummary,
  fallback: string,
  resolvedStreamEvents: readonly PentacleEvent[] = [],
  useSessionDetailCache = false,
  dateFormatter?: Intl.DateTimeFormat,
): string {
  const directPreview = directLatestAssistantPreview(session, resolvedStreamEvents);
  if (directPreview !== null) return trimPreview(directPreview, fallback);
  const cachedAppendPreview = useSessionDetailCache
    ? cachedSessionDetailAppendPreview(state, session, resolvedStreamEvents, dateFormatter)
    : null;
  if (cachedAppendPreview !== null) return trimPreview(cachedAppendPreview, fallback);
  const transcriptItems = useSessionDetailCache
    ? selectSessionDetailFromStreamEvents(state, session, session.stream_id, {
      includeTools: false,
      includeSystem: false,
      includeDraft: false,
      visibleCount: 'all',
    }, resolvedStreamEvents, dateFormatter)?.transcriptItems ?? []
    : buildSessionTranscriptRows(
      state,
      session,
      { includeTools: false, includeSystem: false, visibleCount: 'all' },
      new Map(),
      false,
      resolvedStreamEvents,
    ).transcriptItems;
  const latestDisplayedText = transcriptItems.at(-1)?.text || '';
  return trimPreview(latestDisplayedText, fallback);
}

function hasOnlyPlainAssistantEnvelopeMetadata(raw: PentacleEvent['raw']): boolean {
  if (!raw) return true;
  const prototype = Object.getPrototypeOf(raw);
  if (prototype !== Object.prototype && prototype !== null) return false;
  for (const key of Reflect.ownKeys(raw)) {
    if (key !== 'source' && key !== 'session_name' && key !== 'sessionId') return false;
    const descriptor = Object.getOwnPropertyDescriptor(raw, key);
    if (!descriptor || !('value' in descriptor) || typeof descriptor.value !== 'string') return false;
  }
  // These sources change interpretation or transcript provenance. Other source
  // strings use the interpreter's default route; session labels are inert there.
  return raw.source !== 'structured' && raw.source !== 'claude-jsonl' && raw.source !== 'scrollback_fallback';
}

function cachedSessionDetailAppendPreview(
  state: PentacleStreamState,
  session: PentacleSessionSummary,
  streamEvents: readonly PentacleEvent[],
  dateFormatter?: Intl.DateTimeFormat,
): string | null {
  const cacheKey = sessionDetailCacheKey(session.stream_id, false, 'none', false, 'all');
  const entry = sessionDetailCache.get(cacheKey);
  const cachedContentVersion = sessionDetailVersionByKey.get(cacheKey);
  const cachedInputSignature = sessionDetailInputSignatureByKey.get(cacheKey);
  const currentContentVersion = pentacleEventContentVersion(state, session.stream_id);
  if (
    !entry || cachedContentVersion === undefined || !cachedInputSignature ||
    currentContentVersion === cachedContentVersion
  ) return null;

  const draftEvent = state.drafts[session.stream_id];
  const statusFlags = resolveLiveFlags(session, draftEvent);
  const status: PentacleSessionStatus = !session.online
    ? 'offline'
    : session.pane_status === 'pane_unresponsive'
      ? 'unresponsive'
      : statusFlags.working
        ? 'working'
        : session.last_event_at
          ? 'live'
          : 'idle';
  const unchangedContentSignature = sessionDetailInputSignature(
    state,
    session,
    session.stream_id,
    cachedContentVersion,
    {
      title: displayTitleForSession(session),
      hostTitle: resolveHostTitle(session.host),
      providerLabel: session.provider.toUpperCase(),
      status,
      statusLabel: getPentacleSessionStatusLabel(status),
      summaryLabel: session.last_event_at ? `Updated ${formatRelative(session.last_event_at, dateFormatter)}` : 'No activity yet',
      workingLabel: statusFlags.workingLabel,
      draftText: '',
    },
    draftEvent,
  );
  if (unchangedContentSignature !== cachedInputSignature) return null;

  const previousEvents = entry.sourceEvents;
  const appendedCount = streamEvents.length - previousEvents.length;
  if (previousEvents.length === 0 || appendedCount <= 0 || appendedCount > SLICE_CUSHION) return null;
  for (let index = 0; index < previousEvents.length; index += 1) {
    if (streamEvents[index] !== previousEvents[index]) return null;
  }

  const terminalEvents = streamEvents.slice(previousEvents.length - 1);
  const interpreted = [];
  let previousSeq = -Infinity;
  for (const event of terminalEvents) {
    const seq = Number(event.daemon_seq);
    const text = normalizedEventText(event.text || '');
    if (
      event.stream_id !== session.stream_id || event.kind !== 'ASSIST' ||
      !Number.isFinite(seq) || seq <= previousSeq || !text || text.length >= 40 ||
      event.client_origin === true || event.pending === true || event.optimistic_id ||
      event.jsonl_record_uuid || event.jsonl_resolution_for_record_uuid ||
      event.correlatedDaemonSeq !== null && event.correlatedDaemonSeq !== undefined ||
      event.attachments?.length || !hasOnlyPlainAssistantEnvelopeMetadata(event.raw)
    ) return null;
    const value = getInterpreted(event, resolveHostTitle(session.host));
    if (value.hidden || value.displayRule !== 'bubble:assistant') return null;
    interpreted.push(value);
    previousSeq = seq;
  }
  const coalesced = coalesceInterpretedEvents(interpreted);
  if (
    coalesced.length !== interpreted.length ||
    coalesced.some((item, index) => item.event !== terminalEvents[index])
  ) return null;
  const cachedTerminal = entry.detail.transcriptItems.at(-1);
  const priorTerminal = interpreted[0];
  if (
    !cachedTerminal ||
    cachedTerminal.id !== String(authoritativeEventSeq(priorTerminal.event)) ||
    cachedTerminal.text !== priorTerminal.text.trim()
  ) return null;
  return interpreted.at(-1)?.text.trim() || null;
}

function directLatestAssistantPreview(
  session: PentacleSessionSummary,
  streamEvents: readonly PentacleEvent[],
): string | null {
  const summaryText = normalizedEventText(session.last_text || '');
  const summaryTimestamp = Date.parse(String(session.last_event_at || ''));
  if (
    session.last_kind !== 'ASSIST' ||
    !summaryText ||
    summaryText.length >= 40 ||
    !Number.isFinite(summaryTimestamp) ||
    splitProvenancedNotificationAnswerSummaryText(session)
  ) return null;

  const { baseEvents } = transcriptBaseEventsForDisplay(streamEvents, session.stream_id, 1);
  const latest = baseEvents.at(-1);
  if (!latest || latest.kind !== 'ASSIST' || latest.stream_id !== session.stream_id) return null;
  if (
    latest.client_origin === true ||
    latest.optimistic_id ||
    latest.jsonl_record_uuid ||
    latest.jsonl_resolution_for_record_uuid ||
    latest.correlatedDaemonSeq !== null && latest.correlatedDaemonSeq !== undefined ||
    latest.attachments?.length ||
    latest.raw && Object.keys(latest.raw).length > 0
  ) return null;

  const text = normalizedEventText(latest.text);
  if (!text || text.length >= 40) return null;
  if (
    summaryText !== text ||
    summaryTimestamp !== Date.parse(String(latest.timestamp || ''))
  ) return null;

  const latestSeq = Number(latest.daemon_seq);
  if (!Number.isFinite(latestSeq)) return null;
  let skippedLatest = false;
  for (const event of streamEvents) {
    if (event.stream_id !== session.stream_id) continue;
    if (event === latest && !skippedLatest) {
      skippedLatest = true;
      continue;
    }
    if (Number(event.daemon_seq) === latestSeq) return null;
    const correlated = event.correlatedDaemonSeq;
    if (correlated !== null && correlated !== undefined && Number(correlated) === latestSeq) return null;
  }

  const interpreted = getInterpreted(latest, resolveHostTitle(session.host));
  if (interpreted.hidden || interpreted.displayRule !== 'bubble:assistant') return null;
  return interpreted.text.trim();
}

function unifiedFeedId(event: PentacleEvent) {
  return [
    event.stream_id,
    event.jsonl_record_uuid || event.raw?.uuid || '',
    event.daemon_seq,
    event.timestamp,
    event.kind,
  ].join(':');
}

function unifiedEventAllowsFeed(event: PentacleEvent, session: PentacleSessionSummary | undefined) {
  if (!session) return null;
  if (event.client_origin) return null;
  if (event.kind !== 'ASSIST' && event.kind !== 'ASSIST_TEXT') return null;
  const interpreted = getInterpreted(event, resolveHostTitle(session.host));
  if (interpreted.hidden) return null;
  if (interpreted.tone !== 'assistant') return null;
  if (interpreted.displayRule !== 'bubble:assistant') return null;
  const text = String(interpreted.text || '').trim();
  if (!text) return null;
  return { interpreted, text };
}

export function selectUnifiedFeed(state: PentacleStreamState): PentacleUnifiedFeedItem[] {
  const sessionsByStream = new Map(state.sessions.map((session) => [session.stream_id, session]));
  const globalEvents = selectPentacleDerivedEventIndex(state).chronological;
  const sortKeys = buildTranscriptEventSortKeys(globalEvents);
  return globalEvents
    .flatMap((event) => {
      const session = sessionsByStream.get(event.stream_id);
      const allowed = unifiedEventAllowsFeed(event, session);
      if (!session || !allowed) return [];
      const host = normalizePentacleHost(session.host || event.host);
      return [{
        id: unifiedFeedId(event),
        streamId: event.stream_id,
        host,
        hostTitle: resolveHostTitle(host),
        provider: (session.provider || event.provider || '').toUpperCase(),
        sessionName: session.session_name || event.session_name,
        chatTitle: displayTitleForSession(session),
        accent: hostAccent(host),
        timestampLabel: formatClock(event.timestamp),
        text: allowed.text,
        kind: event.kind,
        event,
      }];
    })
    .sort((left, right) => {
      const leftKey = sortKeys.get(left.event);
      const rightKey = sortKeys.get(right.event);
      if (leftKey && rightKey) {
        if (leftKey.hasSeq && rightKey.hasSeq && leftKey.seq !== rightKey.seq) return leftKey.seq - rightKey.seq;
        if (leftKey.hasTime && rightKey.hasTime && leftKey.time !== rightKey.time) return leftKey.time - rightKey.time;
        return leftKey.deterministicTie.localeCompare(rightKey.deterministicTie);
      }
      return unifiedFeedId(left.event).localeCompare(unifiedFeedId(right.event));
    });
}

export function selectOpenQuestions(state: PentacleStreamState): PentacleOpenQuestionItem[] {
  return state.sessions
    .filter((session): session is PentacleSessionSummary & { question: NonNullable<PentacleSessionSummary['question']> } => Boolean(session.question))
    .map((session) => {
      const host = normalizePentacleHost(session.host);
      return {
        streamId: session.stream_id,
        host,
        hostTitle: resolveHostTitle(host),
        provider: session.provider.toUpperCase(),
        sessionName: session.session_name,
        chatTitle: displayTitleForSession(session),
        accent: hostAccent(host),
        timestampLabel: formatClock(session.last_event_at),
        question: session.question,
      };
    })
    .sort((left, right) => {
      const leftMs = Date.parse(state.sessions.find((session) => session.stream_id === left.streamId)?.last_event_at || '');
      const rightMs = Date.parse(state.sessions.find((session) => session.stream_id === right.streamId)?.last_event_at || '');
      if (Number.isFinite(leftMs) && Number.isFinite(rightMs) && leftMs !== rightMs) return leftMs - rightMs;
      return left.streamId.localeCompare(right.streamId);
    });
}

export function splitProvenancedNotificationAnswerSummaryText(
  summary: Pick<PentacleSessionSummary, 'last_text' | 'last_text_provenance'>,
) {
  const provenance = summary.last_text_provenance;
  if (
    provenance?.schema_version !== 1 ||
    provenance.kind !== 'notification.answer' ||
    !provenance.tell_id ||
    !provenance.injected_text ||
    !summary.last_text.startsWith(provenance.injected_text)
  ) {
    return null;
  }
  try {
    const payload = JSON.parse(provenance.injected_text) as {
      type?: unknown;
      notification_id?: unknown;
      answer?: { notification_id?: unknown };
    };
    const notificationId = String(
      payload.answer?.notification_id || payload.notification_id || '',
    );
    if (
      payload.type !== 'notification.answer' ||
      !notificationId ||
      provenance.tell_id !== `notification-answer-${notificationId}`
    ) {
      return null;
    }
  } catch {
    return null;
  }
  return {
    answerText: provenance.injected_text,
    suffixText: summary.last_text.slice(provenance.injected_text.length),
  };
}

function safeSummaryPreviewVersion(summary: PentacleSessionSummary) {
  const source = [summary.last_event_at || '', summary.last_kind || '', summary.last_text || ''].join('\u0000');
  let hash = 2166136261;
  for (let index = 0; index < source.length; index += 1) {
    hash ^= source.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `${summary.last_event_at || '0'}-${(hash >>> 0).toString(36)}`;
}

/**
 * The shell calls this before transcript selection. Its parser and rejection
 * checks mirror the transcript fallback so first paint cannot leak a working,
 * tool, transient, hidden, or returned-to-prompt summary.
 */
export function selectSafeSessionSummaryPreview(
  state: PentacleStreamState,
  streamId: string,
): PentacleSafeSummaryPreview | null {
  const session = state.sessions.find((item) => item.stream_id === streamId);
  if (!session) return null;

  const fallbackSegments = splitProvenancedNotificationAnswerSummaryText(session);
  const fallbackText = normalizedEventText(fallbackSegments?.answerText ?? session.last_text ?? '');
  const kind = session.last_kind || 'ASSIST';
  const event = {
    daemon_seq: -1,
    host: session.host,
    provider: session.provider,
    session_id: streamId,
    session_name: session.session_name,
    stream_id: streamId,
    timestamp: session.last_event_at,
    kind,
    text: fallbackText,
  };
  const interpretation = interpretPentacleEvent(event, resolveHostTitle(session.host));
  const displayText = (interpretation.text || fallbackText).trim();
  const compareText = normalizedEventText(displayText || fallbackText);
  const matchesReturnedToPrompt = String(kind).toUpperCase() === 'USER' && Object.values(state.optimisticSends ?? {}).some((send) => (
    send.stream_id === streamId &&
    send.status === 'returned_to_prompt' &&
    normalizedEventText(send.text) === compareText
  ));
  if (
    !compareText ||
    matchesReturnedToPrompt ||
    interpretation.hidden ||
    isTransientTranscriptNoise(fallbackText) ||
    session.working ||
    isToolActionRow(interpretation)
  ) return null;

  return {
    key: `preview:${streamId}:${safeSummaryPreviewVersion(session)}`,
    text: collapseCodeBlocks(displayText || fallbackText),
    label: interpretation.label,
    tone: interpretation.tone,
    kind,
  };
}

export function selectSessionDetail(
  state: PentacleStreamState,
  streamId: string,
  options?: SessionDetailOptions,
): PentacleSessionDetail | null {
  const session = state.sessions.find((item) => item.stream_id === streamId) ??
    synthesizeSessionForLocalStreamRows(state, streamId);
  if (!session) return null;
  const streamEvents = eventsForStream(state, streamId);
  return selectSessionDetailFromStreamEvents(state, session, streamId, options, streamEvents);
}

function sessionDetailCacheKey(
  streamId: string,
  includeTools: boolean,
  systemRowsKey: string,
  includeDraft: boolean,
  visibleCount: number | 'all',
) {
  return `session:${streamId}:${includeTools ? 'tools' : 'clean'}:${systemRowsKey}:${includeDraft ? 'draft' : 'nodraft'}:${visibleCount}`;
}

function selectSessionDetailFromStreamEvents(
  state: PentacleStreamState,
  session: PentacleSessionSummary,
  streamId: string,
  options: SessionDetailOptions | undefined,
  streamEvents: readonly PentacleEvent[],
  dateFormatter?: Intl.DateTimeFormat,
): PentacleSessionDetail {
  const draftEvent = state.drafts[streamId];

  const showToolActions = Boolean(options?.showToolActions);
  const includeTools = Boolean(options?.includeTools || showToolActions);
  const includeSystem = Boolean(options?.includeSystem || showToolActions);
  const includeDraft = options?.includeDraft !== false;
  const visibleCount = options?.visibleCount === 'all'
    ? 'all'
    : Math.max(0, options?.visibleCount ?? 80);
  const systemRows = includeSystem ? (options?.systemRows ?? 'all') : undefined;
  const systemRowsKey = systemRows ?? 'none';
  const cacheKey = sessionDetailCacheKey(streamId, includeTools, systemRowsKey, includeDraft, visibleCount);
  const currentContentVersion = pentacleEventContentVersion(state, streamId);
  const cachedContentVersion = sessionDetailVersionByKey.get(cacheKey);
  const previousEntry = sessionDetailCache.get(cacheKey);
  const previousDetail = previousEntry?.detail;
  const statusFlags = resolveLiveFlags(session, draftEvent);
  const status = !session.online
    ? 'offline'
    : session.pane_status === 'pane_unresponsive'
      ? 'unresponsive'
      : statusFlags.working
        ? 'working'
        : session.last_event_at
          ? 'live'
          : 'idle';
  const workingLabel = statusFlags.workingLabel;
  const title = displayTitleForSession(session);
  const hostTitle = resolveHostTitle(session.host);
  const providerLabel = session.provider.toUpperCase();
  const statusLabel = getPentacleSessionStatusLabel(status);
  const summaryLabel = session.last_event_at ? `Updated ${formatRelative(session.last_event_at, dateFormatter)}` : 'No activity yet';
  const draftText = includeDraft ? draftEvent?.text || session.draft || '' : '';
  const inputSignature = sessionDetailInputSignature(
    state,
    session,
    streamId,
    currentContentVersion,
    {
      title,
      hostTitle,
      providerLabel,
      status,
      statusLabel,
      summaryLabel,
      workingLabel,
      draftText,
    },
    draftEvent,
  );
  if (
    previousDetail &&
    cachedContentVersion === currentContentVersion &&
    sessionDetailInputSignatureByKey.get(cacheKey) === inputSignature
  ) {
    sessionDetailCache.set(cacheKey, { detail: previousDetail, sourceEvents: streamEvents });
    return previousDetail;
  }
  const previousItems = new Map(previousDetail?.transcriptItems.map((item) => [item.id, item]) || []);

  const { transcriptItems, hiddenCount, remainingCount } = buildSessionTranscriptRows(
    state,
    session,
    {
      includeTools,
      includeSystem,
      systemRows,
      visibleCount,
    },
    previousItems,
    // Default FALSE — only a real render path opts in (see emitRenderTelemetry
    // doc on SessionDetailOptions). This kills the self-fulfilling beacon for
    // every non-render caller (harness probes, terminal-view frames, diag).
    Boolean(options?.emitRenderTelemetry),
    streamEvents,
  );

  // Newest event we actually hold for this stream, across all kinds (hidden/
  // tool/system included): "how far our local transcript reaches". Compared by
  // the session screen against the resilient `session.last_event_at` summary to
  // detect (and refetch-recover) a live frame dropped on a lossy link. Computed
  // only on the build path — cache hits reuse the prior detail's value.
  let latestEventAt = state.eventBucketsByStream?.[streamId]?.latestEventAt;
  if (!latestEventAt) {
    let latestEventAtMs = -Infinity;
    for (const event of streamEvents) {
      const ms = Date.parse(String(event.timestamp || ''));
      if (Number.isFinite(ms) && ms > latestEventAtMs) {
        latestEventAtMs = ms;
        latestEventAt = event.timestamp;
      }
    }
  }

  const nextDetail: PentacleSessionDetail = {
    streamId,
    title,
    hostTitle,
    providerLabel,
    status,
    statusLabel,
    summaryLabel,
    workingLabel,
    draftText,
    transcriptItems,
    hiddenCount,
    remainingCount,
    latestEventAt,
  };

  if (
    previousDetail &&
    cachedContentVersion === currentContentVersion &&
    previousDetail.streamId === nextDetail.streamId &&
    previousDetail.title === nextDetail.title &&
    previousDetail.hostTitle === nextDetail.hostTitle &&
    previousDetail.providerLabel === nextDetail.providerLabel &&
    previousDetail.status === nextDetail.status &&
    previousDetail.statusLabel === nextDetail.statusLabel &&
    previousDetail.summaryLabel === nextDetail.summaryLabel &&
    previousDetail.workingLabel === nextDetail.workingLabel &&
    previousDetail.draftText === nextDetail.draftText &&
    previousDetail.hiddenCount === nextDetail.hiddenCount &&
    previousDetail.remainingCount === nextDetail.remainingCount &&
    previousDetail.latestEventAt === nextDetail.latestEventAt &&
    previousDetail.transcriptItems.length === nextDetail.transcriptItems.length &&
    previousDetail.transcriptItems.every((item, index) => item === nextDetail.transcriptItems[index])
  ) {
    sessionDetailCache.set(cacheKey, { detail: previousDetail, sourceEvents: streamEvents });
    sessionDetailVersionByKey.set(cacheKey, currentContentVersion);
    sessionDetailInputSignatureByKey.set(cacheKey, inputSignature);
    return previousDetail;
  }

  sessionDetailCache.set(cacheKey, { detail: nextDetail, sourceEvents: streamEvents });
  sessionDetailVersionByKey.set(cacheKey, currentContentVersion);
  sessionDetailInputSignatureByKey.set(cacheKey, inputSignature);
  return nextDetail;
}

type SessionDetailOptions = {
  includeTools?: boolean;
  includeSystem?: boolean;
  systemRows?: 'all' | 'timing-only';
  /**
   * User-facing session-screen toggle. When true it has the same visible
   * effect as includeTools=true and includeSystem=true.
   */
  showToolActions?: boolean;
  includeDraft?: boolean;
  visibleCount?: number | 'all';
  /**
   * Emit per-row `chat:event_rendered` telemetry while building the transcript.
   * Defaults to FALSE: the selector is invoked by many non-render callers
   * (harness probes, terminal-view per-frame calls, diagnostics) and emitting
   * for them makes `chat:event_rendered` self-fulfilling — a polling harness can
   * satisfy its own "rendered" await with zero new content or paint
   * (render-telemetry QA layer, defect 1; audit §1.1/§4.3). ONLY a real
   * render path (the desktop renderer's post-`chatMode` chat-render call) sets
   * this true, so `chat:event_rendered` is emitted exclusively at real render.
   */
  emitRenderTelemetry?: boolean;
};

export function invalidateSessionDetailCache(streamId: string) {
  const prefix = `session:${streamId}:`;
  for (const key of sessionDetailCache.keys()) {
    if (key.startsWith(prefix)) {
      sessionDetailCache.delete(key);
      sessionDetailVersionByKey.delete(key);
      sessionDetailInputSignatureByKey.delete(key);
    }
  }
}

function sessionDetailInputSignature(
  state: PentacleStreamState,
  session: PentacleSessionSummary,
  streamId: string,
  currentContentVersion: number,
  resolved: {
    title: string;
    hostTitle: string;
    providerLabel: string;
    status: PentacleSessionStatus;
    statusLabel: string;
    summaryLabel: string;
    workingLabel: string;
    draftText: string;
  },
  draftEvent?: PentacleEvent,
) {
  const optimisticSendSignature = Object.values(state.optimisticSends ?? {})
    .filter((send) => send.stream_id === streamId)
    .sort((a, b) => a.optimistic_id.localeCompare(b.optimistic_id))
    .map((send) => [
      send.optimistic_id,
      send.status,
      send.failure_reason || '',
      send.turn_queued === true ? 'queued' : '',
    ].join(':'))
    .join('|');
  return JSON.stringify({
    contentVersion: currentContentVersion,
    session: {
      stream_id: session.stream_id,
      host: session.host,
      provider: session.provider,
      session_name: session.session_name,
      display_name: session.display_name || '',
      title: session.title || '',
      last_event_at: session.last_event_at || '',
      last_text: session.last_text || '',
      last_kind: session.last_kind || '',
      draft: session.draft || '',
      pending: Boolean(session.pending),
      working: Boolean(session.working),
      working_label: session.working_label || '',
      online: Boolean(session.online),
      question: session.question ?? null,
    },
    resolved,
    draftEvent: draftEvent
      ? {
        daemon_seq: draftEvent.daemon_seq,
        timestamp: draftEvent.timestamp,
        kind: draftEvent.kind,
        text: draftEvent.text,
        working: draftEvent.raw?.working ?? null,
        working_label: draftEvent.raw?.working_label ?? '',
      }
      : null,
    optimisticSendSignature,
  });
}

function buildSessionTranscriptRows(
  state: PentacleStreamState,
  session: PentacleSessionSummary,
  options: Pick<SessionDetailOptions, 'includeTools' | 'includeSystem' | 'showToolActions' | 'systemRows' | 'visibleCount'>,
  previousItems: Map<string, PentacleTranscriptItem>,
  emitRenderTelemetry: boolean,
  streamEvents: readonly PentacleEvent[] = peekEventsForStream(state, session.stream_id),
) {
  const streamId = session.stream_id;
  const showToolActions = Boolean(options.showToolActions);
  const includeTools = Boolean(options.includeTools || showToolActions);
  const includeSystem = Boolean(options.includeSystem || showToolActions);
  const systemRows = includeSystem ? (options.systemRows ?? 'all') : 'none';
  const visibleCount = options.visibleCount === 'all'
    ? 'all'
    : Math.max(0, options.visibleCount ?? 80);
  const shouldKeepBaseEvent = (event: PentacleEvent) => {
    if (systemRows !== 'timing-only') return true;
    if (String(event.kind || '').toUpperCase() !== 'SYSTEM') return true;
    return isSystemEndOfTurnEvent(event);
  };
  const shouldForceIncludeBaseEvent = (event: PentacleEvent) => {
    if (visibleCount === 'all') return false;
    const optimisticId = String(event.optimistic_id || '');
    return Boolean(
      event.client_origin === true &&
      optimisticId &&
      deriveSendState(state.optimisticSends?.[optimisticId]),
    );
  };
  const shouldCountBaseEventTowardWindow = (event: PentacleEvent) => {
    if (includeTools) return true;
    const kind = String(event.kind || '').toUpperCase();
    return kind !== 'TOOL_USE' && kind !== 'TOOL_RESULT' && kind !== 'TOOL_BATCH_SUMMARY' && kind !== 'THINKING';
  };
  const { baseEvents } = transcriptBaseEventsForDisplay(
    streamEvents,
    streamId,
    'all',
    shouldKeepBaseEvent,
    shouldForceIncludeBaseEvent,
    shouldCountBaseEventTowardWindow,
  );
  const assistantLabel = resolveHostTitle(session.host);
  const windowedBaseEvents = groupClaudeJsonlSidechains(
    baseEvents,
  );
  const interpretedEvents = windowedBaseEvents.map((event) => getInterpreted(event, assistantLabel));
  const filteredEvents = interpretedEvents.filter((item) => {
    if (item.hidden) return false;
    if (!includeTools && isToolActionRow(item)) return false;
    if (!includeSystem && item.tone === 'system' && !isAlwaysVisibleSystemRow(item)) return false;
    if (includeSystem && systemRows === 'timing-only' && item.tone === 'system' && !isTimingSystemRow(item)) return false;
    return true;
  });
  let trailingBlankDroppedCount = 0;
  const dedupedEvents = suppressToolRowsCoveredByBatch(coalesceInterpretedEvents(filteredEvents)).filter((item) => {
    if (isReturnedToPromptUserEvent(item.event)) return false;
    if (shouldDropEmptyTranscriptRow(item)) {
      trailingBlankDroppedCount += 1;
      return false;
    }
    return true;
  });
  const latestUserEvent = dedupedEvents.reduce<PentacleEvent | undefined>((latest, item) => (
    item.tone === 'user' && isBubbleRow(item) ? item.event : latest
  ), undefined);
  const visibleEvents = visibleCount === 'all'
    ? dedupedEvents
    : visibleCount > 0
      ? dedupedEvents.slice(-visibleCount)
      : [];
  if (visibleCount !== 'all') {
    const visibleEventKeys = new Set(visibleEvents.map((item) => (
      item.event.optimistic_id || `${item.event.stream_id}:${item.event.daemon_seq}:${item.event.timestamp}`
    )));
    for (const item of dedupedEvents) {
      const optimisticId = item.event.optimistic_id;
      if (!item.event.client_origin || !optimisticId) continue;
      const sendState = deriveSendState(state.optimisticSends?.[optimisticId]);
      if (!sendState) continue;
      const key = optimisticId;
      if (visibleEventKeys.has(key)) continue;
      visibleEventKeys.add(key);
      visibleEvents.push(item);
    }
  }
  const transcriptItems: PentacleTranscriptItem[] = [];
  let lastTimestampMinute = '';

  for (const item of visibleEvents) {
    const event = item.event;
    if (isReturnedToPromptUserEvent(event)) continue;
    const text = item.text.trim();
    if (!isBubbleRow(item) && !text) {
      trailingBlankDroppedCount += 1;
      continue;
    }
    const minuteKey = timestampMinuteKey(event.timestamp);
    const timestampLabel = minuteKey && minuteKey !== lastTimestampMinute
      ? formatClock(event.timestamp)
      : '';
    if (minuteKey) lastTimestampMinute = minuteKey;

    const renderOrderIndex = transcriptItems.length;
    const send = event.client_origin === true && event.optimistic_id
      ? state.optimisticSends?.[event.optimistic_id]
      : undefined;
    if (emitRenderTelemetry) {
      logTelemetry(TELEMETRY_EVENTS.CHAT_EVENT_RENDERED, {
        stream_id: event.stream_id,
        kind: event.kind,
        seq: event.daemon_seq,
        optimistic_id: event.optimistic_id,
        correlated_daemon_seq: event.correlatedDaemonSeq,
        render_order_index: renderOrderIndex,
        display_rule: item.displayRule,
        event_case: item.caseId,
        rendered_text: item.text,
        source: String(event.raw?.source || ''),
        attachment_count: event.attachments?.length ?? 0,
        send_state: event.client_origin === true ? deriveSendState(send) : undefined,
      });
    }

    const authoritativeSeq = authoritativeEventSeq(event);
    const nextItem: PentacleTranscriptItem = {
      id: event.optimistic_id || (Number.isFinite(authoritativeSeq) ? String(authoritativeSeq) : String(event.daemon_seq)),
      timestampLabel,
      label: item.label,
      tone: item.tone,
      provider: String(event.provider || ''),
      source: String(event.raw?.source || ''),
      text,
      kind: event.kind,
      isUser: item.tone === 'user',
      eventCase: item.caseId,
      displayRule: item.displayRule,
      ...(item.disclosure ? { disclosure: item.disclosure } : {}),
      eventKey: renderEventKey(event.stream_id, authoritativeSeq, event.optimistic_id),
      optimisticId: event.optimistic_id,
      correlatedDaemonSeq: event.correlatedDaemonSeq,
      ...(item.notificationId ? { notificationId: item.notificationId } : {}),
    };
    if (event.attachments && event.attachments.length > 0) {
      nextItem.attachments = event.attachments;
    }
    if (send?.queued_at !== undefined || event.queued_at !== undefined) {
      nextItem.queuedWhileWorking = true;
    }
    if (event.client_origin === true) {
      if (send?.status === 'returned_to_prompt') continue;
      const sendState = deriveSendState(send);
      nextItem.pending = send ? sendState === 'queued' || sendState === 'sending' : event.pending !== false;
      // B1/B4: retain the optimistic lifecycle for queue and cancellation
      // affordances. Receipt captions are derived separately, and only from
      // the latest matching durable USER echo.
      nextItem.sendState = sendState;
    }
    if (event === latestUserEvent && send?.status !== 'cancelled') {
      const receiptCaption = receiptCaptionForLatestUserEvent(event);
      if (receiptCaption) nextItem.receiptCaption = receiptCaption;
    }
    transcriptItems.push(reuseTranscriptItem(previousItems, nextItem));
  }

  if (trailingBlankDroppedCount > 0) {
    logTelemetry(TELEMETRY_EVENTS.CHAT_SURFACE_TRAILING_BLANK_DROPPED, {
      stream_id: streamId,
      count: trailingBlankDroppedCount,
    });
  }

  const hiddenCount = Math.max(0, windowedBaseEvents.length - filteredEvents.length);
  // Count the same filtered/coalesced rows that can actually render. The
  // interpreter cache keeps widening the visible window from repeating work.
  const remainingInWindow = Math.max(0, dedupedEvents.length - visibleEvents.length);
  const remainingCount = remainingInWindow;
  const fallbackSegments = splitProvenancedNotificationAnswerSummaryText(session);
  const fallbackText = normalizedEventText(fallbackSegments?.answerText ?? session.last_text ?? '');
  const fallbackKind = session.last_kind || 'ASSIST';
  const fallbackEvent = {
    daemon_seq: -1,
    host: session.host,
    provider: session.provider,
    session_id: streamId,
    session_name: session.session_name,
    stream_id: streamId,
    timestamp: session.last_event_at,
    kind: fallbackKind,
    text: fallbackText,
  };
  const fallbackInterpretation = interpretPentacleEvent(fallbackEvent, resolveHostTitle(session.host));
  const fallbackDisplayText = (fallbackInterpretation.text || fallbackText).trim();
  const fallbackCompareText = normalizedEventText(fallbackDisplayText || fallbackText);
  const fallbackMatchesReturnedToPrompt = String(fallbackKind || '').toUpperCase() === 'USER' &&
    (
      Object.values(state.optimisticSends ?? {}).some((send) => (
        send.stream_id === streamId &&
        send.status === 'returned_to_prompt' &&
        normalizedEventText(send.text) === fallbackCompareText
      )) ||
      baseEvents.some((event) => (
        event.stream_id === streamId &&
        isReturnedToPromptUserEvent(event) &&
        normalizedEventText(event.text) === fallbackCompareText
      ))
    );
  const fallbackAt = Date.parse(String(session.last_event_at || ''));
  const fallbackMatchesHeldEvent = Number.isFinite(fallbackAt) && baseEvents.some((event) => {
    if (event.stream_id !== streamId) return false;
    if (isReturnedToPromptUserEvent(event)) return false;
    if (event.kind !== fallbackKind) return false;
    if (normalizedEventText(event.text) !== fallbackCompareText) return false;
    if (!includeTools) {
      const eventInterpretation = interpretPentacleEvent(event, resolveHostTitle(session.host));
      if (isToolActionRow(eventInterpretation) && !isToolActionRow(fallbackInterpretation)) {
        return false;
      }
    }
    const eventAt = eventTimestampMs(event);
    return Number.isFinite(eventAt) && eventAt >= fallbackAt;
  });
  const fallbackAlreadyRendered = dedupedEvents.some((item) => {
    if (isReturnedToPromptUserEvent(item.event)) return false;
    const renderedText = normalizedEventText(item.text.trim());
    if (
      renderedText !== fallbackCompareText &&
      !renderedText.startsWith(fallbackCompareText)
    ) {
      return false;
    }
    if (item.event.kind !== fallbackKind) return false;
    if (!includeTools && isToolActionRow(item) && !isToolActionRow(fallbackInterpretation)) {
      return false;
    }
    if (isToolActionRow(fallbackInterpretation) && renderedText.startsWith(fallbackCompareText)) {
      return true;
    }
    if (item.event.client_origin === true && item.event.optimistic_id) {
      return true;
    }
    const eventAt = eventTimestampMs(item.event);
    if (Number.isFinite(fallbackAt) && Number.isFinite(eventAt)) {
      return eventAt >= fallbackAt;
    }
    return true;
  });
  const fallbackSuffixEvent = fallbackSegments?.suffixText
    ? { ...fallbackEvent, daemon_seq: -2, text: fallbackSegments.suffixText }
    : null;
  const fallbackSuffixInterpretation = fallbackSuffixEvent
    ? interpretPentacleEvent(fallbackSuffixEvent, resolveHostTitle(session.host))
    : null;
  const fallbackSuffixDisplayText = fallbackSuffixInterpretation
    ? (fallbackSuffixInterpretation.text || fallbackSegments?.suffixText || '').trim()
    : '';
  const fallbackSuffixCompareText = normalizedEventText(fallbackSuffixDisplayText);
  const fallbackSuffixAlreadyRendered = Boolean(fallbackSuffixCompareText) && dedupedEvents.some(
    (item) => normalizedEventText(item.text.trim()) === fallbackSuffixCompareText,
  );
  const shouldAppendFallbackSuffix = Boolean(
    fallbackSuffixEvent &&
    fallbackSuffixInterpretation &&
    fallbackSuffixCompareText &&
    !fallbackSuffixInterpretation.hidden &&
    !isTransientTranscriptNoise(fallbackSuffixEvent.text) &&
    (includeTools || !session.working) &&
    (includeTools || !isToolActionRow(fallbackSuffixInterpretation)) &&
    !fallbackSuffixAlreadyRendered,
  );
  const shouldAppendFallback = Boolean(
    fallbackCompareText &&
    !fallbackMatchesReturnedToPrompt &&
    !fallbackInterpretation.hidden &&
    !isTransientTranscriptNoise(fallbackText) &&
    !fallbackMatchesHeldEvent &&
    // Never surface the session-summary fallback while the agent is WORKING.
    // session.last_text churns through transient states during a turn
    // ("Thinking" -> "Ran 1 shell command" -> "Worked for 4s · 8 msgs" -> a
    // running Bash command, …), and appending it re-mounts a live-updating row
    // at the bottom of the transcript every tick — the operator-reported flicker
    // and the noisy "Worked for Ns · N msgs" / leaked tool row. The working dock
    // already conveys live status; the transcript shows only committed content
    // while working and surfaces the fallback only once the turn settles to idle.
    // Scoped to the tools-hidden (default) view: a tools-on/verbose view opted
    // into live activity, so it still shows the fallback while working.
    (includeTools || !session.working) &&
    // Belt-and-suspenders for the working->idle transition: keep applying the
    // tool-action filter (a tool-kind last_text must not leak when tools hidden).
    (includeTools || !isToolActionRow(fallbackInterpretation)) &&
    !fallbackAlreadyRendered,
  );

  if (shouldAppendFallback) {
    const minuteKey = timestampMinuteKey(session.last_event_at);
    const timestampLabel = minuteKey && minuteKey !== lastTimestampMinute
      ? formatClock(session.last_event_at)
      : '';
    const renderOrderIndex = transcriptItems.length;
    if (emitRenderTelemetry) {
      logTelemetry(TELEMETRY_EVENTS.CHAT_EVENT_RENDERED, {
        stream_id: streamId,
        kind: fallbackKind,
        seq: -1,
        render_order_index: renderOrderIndex,
        display_rule: fallbackInterpretation.displayRule,
        event_case: fallbackInterpretation.caseId,
        rendered_text: fallbackInterpretation.text,
        source: 'session_summary_fallback',
      });
    }

    const fallbackItem = reuseTranscriptItem(previousItems, {
      id: `fallback:${streamId}`,
      timestampLabel,
      label: fallbackInterpretation.label,
      tone: fallbackInterpretation.tone,
      provider: String(session.provider || ''),
      source: '',
      text: collapseCodeBlocks(fallbackDisplayText || fallbackText),
      kind: fallbackKind,
      isUser: fallbackInterpretation.tone === 'user',
      eventCase: fallbackInterpretation.caseId,
      displayRule: fallbackInterpretation.displayRule,
      ...(fallbackInterpretation.disclosure ? { disclosure: fallbackInterpretation.disclosure } : {}),
      eventKey: 'uncorrelated_fallback',
    });
    const existingDividerIndex = findCoalescibleTerminalDividerIndex(transcriptItems, fallbackItem);
    if (existingDividerIndex !== -1) {
      transcriptItems.splice(existingDividerIndex, transcriptItems.length - existingDividerIndex, fallbackItem);
    } else {
      transcriptItems.push(fallbackItem);
    }
  }

  if (
    shouldAppendFallbackSuffix &&
    fallbackSuffixEvent &&
    fallbackSuffixInterpretation
  ) {
    if (emitRenderTelemetry) {
      logTelemetry(TELEMETRY_EVENTS.CHAT_EVENT_RENDERED, {
        stream_id: streamId,
        kind: fallbackKind,
        seq: -2,
        render_order_index: transcriptItems.length,
        display_rule: fallbackSuffixInterpretation.displayRule,
        event_case: fallbackSuffixInterpretation.caseId,
        rendered_text: fallbackSuffixInterpretation.text,
        source: 'session_summary_provenance_suffix',
      });
    }
    transcriptItems.push(reuseTranscriptItem(previousItems, {
      id: `fallback:${streamId}:notification-answer-suffix`,
      timestampLabel: '',
      label: fallbackSuffixInterpretation.label,
      tone: fallbackSuffixInterpretation.tone,
      provider: String(session.provider || ''),
      source: '',
      text: collapseCodeBlocks(fallbackSuffixDisplayText || fallbackSuffixEvent.text),
      kind: fallbackKind,
      isUser: fallbackSuffixInterpretation.tone === 'user',
      eventCase: fallbackSuffixInterpretation.caseId,
      displayRule: fallbackSuffixInterpretation.displayRule,
      ...(fallbackSuffixInterpretation.disclosure
        ? { disclosure: fallbackSuffixInterpretation.disclosure }
        : {}),
      eventKey: 'uncorrelated_fallback:notification-answer-suffix',
    }));
  }

  return { transcriptItems, hiddenCount, remainingCount };
}

function synthesizeSessionForLocalStreamRows(
  state: PentacleStreamState,
  streamId: string,
): PentacleSessionSummary | null {
  const events = peekEventsForStream(state, streamId);
  const optimistic = Object.values(state.optimisticSends ?? {})
    .filter((send) => send.stream_id === streamId)
    .sort((a, b) => b.created_at - a.created_at)[0];
  if (events.length === 0 && !optimistic) return null;

  const latestEvent = events
    .map((event, index) => ({ event, index, time: eventTimestampMs(event) }))
    .sort((a, b) => {
      const aFinite = Number.isFinite(a.time);
      const bFinite = Number.isFinite(b.time);
      if (aFinite && bFinite && a.time !== b.time) return b.time - a.time;
      if (aFinite !== bFinite) return aFinite ? -1 : 1;
      return b.index - a.index;
    })[0]?.event;
  const optimisticSendState = optimistic ? deriveSendState(optimistic) : undefined;
  const host = latestEvent?.host || streamId.split(':', 1)[0] || '';
  const sessionName = latestEvent?.session_name || streamId.split(':').slice(1).join(':') || streamId;
  return {
    stream_id: streamId,
    host,
    provider: latestEvent?.provider || '',
    session_name: sessionName,
    last_event_at: latestEvent?.timestamp || (optimistic ? new Date(optimistic.created_at).toISOString() : ''),
    last_text: latestEvent?.text || optimistic?.text || '',
    last_kind: latestEvent?.kind || (optimistic ? 'USER' : ''),
    draft: '',
    pending: optimisticSendState === 'queued' || optimisticSendState === 'sending',
    working: false,
    working_label: '',
    online: true,
  };
}

function renderEventKey(streamId: string, daemonSeq: number, optimisticId?: string) {
  if (Number.isFinite(daemonSeq) && daemonSeq >= 0) {
    return `${streamId}:${daemonSeq}`;
  }
  if (optimisticId) {
    return `${streamId}:optimistic:${optimisticId}`;
  }
  return 'uncorrelated_fallback';
}

function isBubbleRow(item: PentacleInterpretedEvent) {
  return item.displayRule === 'bubble:user' || item.displayRule === 'bubble:agent' || item.displayRule === 'bubble:assistant';
}

function isToolActionRow(item: PentacleInterpretedEvent) {
  if (item.displayRule === 'activity:question') return false;
  if (item.tone === 'tool' || item.tone === 'thinking') return true;
  // Structured tool kinds are tool-action rows regardless of how the text was
  // interpreted. The session-summary fallback builds a synthetic event from
  // session.last_text/last_kind with NO `raw`, so the interpreter's structured
  // path does not engage and a TOOL_USE/TOOL_RESULT fallback is mislabeled as a
  // 'bubble:assistant' (tone 'assistant') — which would otherwise slip past the
  // tone/displayRule checks below and leak the in-progress Bash/tool row while
  // the agent works (mobile renders any TOOL_USE item as a ToolInvocationCard).
  const kind = item.event?.kind;
  if (
    kind === 'TOOL' ||
    kind === 'TOOL-OUT' ||
    kind === 'TOOL_USE' ||
    kind === 'TOOL_RESULT' ||
    kind === 'TOOL_BATCH_SUMMARY'
  ) {
    return true;
  }
  return (
    item.displayRule === 'activity:command' ||
    item.displayRule === 'activity:tool-output' ||
    item.displayRule === 'activity:tool-batch' ||
    item.displayRule === 'activity:collapsed-tool' ||
    item.displayRule === 'activity:explored' ||
    item.displayRule === 'activity:file-change' ||
    item.displayRule === 'activity:code-block'
  );
}

function isAlwaysVisibleSystemRow(item: PentacleInterpretedEvent) {
  return item.displayRule === 'terminal:divider' ||
    item.displayRule === 'system:compacted' ||
    item.displayRule === 'activity:question';
}

function isTimingSystemRow(item: PentacleInterpretedEvent) {
  return (
    item.displayRule === 'activity:turn-summary' ||
    item.displayRule === 'terminal:divider'
  );
}

function shouldDropEmptyTranscriptRow(item: PentacleInterpretedEvent) {
  const text = item.text.trim();
  if (item.displayRule === 'terminal:divider' || item.displayRule === 'activity:turn-summary') {
    return !text;
  }
  return !isBubbleRow(item) && !text;
}

function isReturnedToPromptUserEvent(event: PentacleEvent | undefined): boolean {
  if (!event || String(event.kind || '').toUpperCase() !== 'USER') return false;
  const raw = event.raw || {};
  return raw.returned_to_prompt === true || raw.user_delivery_state === 'returned_to_prompt';
}

export function diagSessionDetailCounts(
  state: PentacleStreamState,
  streamId: string,
): { rendered_full_count: number; rendered_visible_count: number } {
  const session = state.sessions.find((item) => item.stream_id === streamId);
  if (!session) {
    return { rendered_full_count: 0, rendered_visible_count: 0 };
  }
  const renderedFull = buildSessionTranscriptRows(
    state,
    session,
    { includeTools: true, includeSystem: true, visibleCount: 'all' },
    new Map(),
    false,
  );
  const renderedVisible = buildSessionTranscriptRows(
    state,
    session,
    { includeTools: false, includeSystem: false, visibleCount: 'all' },
    new Map(),
    false,
  );
  return {
    rendered_full_count: renderedFull.transcriptItems.length,
    rendered_visible_count: renderedVisible.transcriptItems.length,
  };
}

function suppressToolRowsCoveredByBatch(items: PentacleInterpretedEvent[]) {
  const coveredToolUseIds = new Set<string>();
  for (const item of items) {
    if (item.displayRule !== 'activity:tool-batch') continue;
    const ids = item.event.raw?.tool_use_ids;
    if (!Array.isArray(ids)) continue;
    for (const id of ids) {
      const value = String(id || '').trim();
      if (value) coveredToolUseIds.add(value);
    }
  }
  if (coveredToolUseIds.size === 0) return items;
  return items.filter((item) => {
    if (item.event.kind !== 'TOOL_USE' && item.event.kind !== 'TOOL_RESULT') return true;
    const toolUseId = String(item.event.raw?.tool_use_id || '').trim();
    return !toolUseId || !coveredToolUseIds.has(toolUseId);
  });
}
