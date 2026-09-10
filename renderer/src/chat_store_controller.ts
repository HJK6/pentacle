import {
  initialPentacleStreamState,
  applyPentacleEvent,
  applyFetchedStreamEvents,
  applyPentacleWorkingState,
  applyPentacleHostStatus,
  applyPentacleMachineStats,
  applyPentacleMachineStatsInventory,
  applyPentacleSessionInventory,
  applySnapshotWithOptimisticReconciliation,
  sendOptimisticMessage,
  markOptimisticDispatchedByRequestId,
  markOptimisticAckedByRequestId,
  markOptimisticFailedByRequestId,
  markOptimisticIndeterminateByRequestId,
  markOptimisticCancelledByOptimisticId,
  reconcileOptimisticSendWithServerEvent,
  clearPentacleTurn,
  onReconnect,
  selectChatList,
  selectSessionDetail,
  optimisticMatchesServerUser,
  OPTIMISTIC_RECONCILE_WINDOW_MS,
  logTelemetry,
  TELEMETRY_EVENTS,
} from 'pentacle-chat-core';
import {
  eligibleReconnectReplayOptimisticIds,
  markDaemonRestartSurvivorsIndeterminate,
  rotateOptimisticSendRequestId,
} from './chat_reconnect_replay';
import type {
  PentacleStreamState,
  PentacleEvent,
  PentacleHostStatus,
  PentacleSessionSummary,
  PentacleMachineStats,
  PentacleUpdateMessage,
  PentacleNotification,
  WorkingStateData,
  PentacleChatListItem,
  PentacleSessionDetail,
  PentacleQuestion,
  TurnState,
  ChatAttachment,
} from 'pentacle-chat-core';

type RawFrame = { type?: string; [key: string]: unknown };

export type PentacleLimit = {
  id: 'claude' | 'fable' | 'codex';
  label: 'Claude' | 'Fable' | 'Codex';
  pct: number | null;
  resets_at_iso: string | null;
  resets_text: string | null;
  upstream_reported_at: string | null;
  probed_at: string | null;
};

export type PentacleLimitsHealth = {
  schema_version: 1;
  claude: {
    attempted_at: string | null;
    outcome: 'never' | 'ok' | 'auth_error' | 'parser_error' | 'provider_error'
      | 'timeout' | 'transport_error' | 'internal_error' | 'store_error';
    error: { code: string; message: string } | null;
    upstream_reported_at: string | null;
    probed_at: string | null;
    stale_after_seconds: number;
  };
};

const LIMIT_IDENTITIES = [
  ['claude', 'Claude'],
  ['fable', 'Fable'],
  ['codex', 'Codex'],
] as const;
const LIMIT_KEYS = 'id,label,pct,probed_at,resets_at_iso,resets_text,upstream_reported_at';
const LIMITS_HEALTH_KEYS = 'attempted_at,error,outcome,probed_at,stale_after_seconds,upstream_reported_at';
const LIMITS_HEALTH_TOP_KEYS = 'claude,schema_version';
const LIMITS_HEALTH_OUTCOMES = new Set([
  'never', 'ok', 'auth_error', 'parser_error', 'provider_error',
  'timeout', 'transport_error', 'internal_error', 'store_error',
]);
const LIMITS_HEALTH_ERRORS: Record<string, { code: string; message: string }> = {
  auth_error: { code: 'claude_not_authenticated', message: 'Claude is not authenticated' },
  provider_error: { code: 'claude_subscription_unavailable', message: 'Claude subscription usage is unavailable' },
  parser_error: { code: 'claude_usage_parse_failed', message: 'Claude usage could not be parsed' },
  timeout: { code: 'claude_usage_timeout', message: 'Claude usage probe timed out' },
  transport_error: { code: 'claude_usage_transport_failed', message: 'Claude usage transport failed' },
  internal_error: { code: 'claude_usage_internal_error', message: 'Claude usage probe failed internally' },
  store_error: { code: 'usage_state_write_failed', message: 'Claude usage state could not be saved' },
};

function utcRfc3339Millis(value: unknown): number | null {
  if (typeof value !== 'string') return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$/.exec(value);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (month < 1 || month > 12 || day < 1 || day > daysInMonth[month - 1]) return null;
  const millis = Date.parse(value);
  return Number.isFinite(millis) ? millis : null;
}

function nullLimits(): PentacleLimit[] {
  return LIMIT_IDENTITIES.map(([id, label]) => ({
    id,
    label,
    pct: null,
    resets_at_iso: null,
    resets_text: null,
    upstream_reported_at: null,
    probed_at: null,
  }));
}

function validatedLimits(value: unknown): PentacleLimit[] | null {
  if (!Array.isArray(value) || value.length !== LIMIT_IDENTITIES.length) return null;
  const result: PentacleLimit[] = [];
  for (let index = 0; index < LIMIT_IDENTITIES.length; index += 1) {
    const row = value[index] as Record<string, unknown> | null;
    const [id, label] = LIMIT_IDENTITIES[index];
    if (!row || typeof row !== 'object' || Array.isArray(row)
      || Object.keys(row).sort().join(',') !== LIMIT_KEYS
      || row.id !== id || row.label !== label
      || (row.pct !== null && (typeof row.pct !== 'number' || !Number.isInteger(row.pct) || row.pct < 0 || row.pct > 100))
      || (row.resets_at_iso !== null && typeof row.resets_at_iso !== 'string')
      || (row.resets_text !== null && typeof row.resets_text !== 'string')) return null;
    const upstreamMillis = row.upstream_reported_at === null
      ? null : utcRfc3339Millis(row.upstream_reported_at);
    const probedMillis = row.probed_at === null ? null : utcRfc3339Millis(row.probed_at);
    const hasUpstream = row.upstream_reported_at !== null;
    const hasProbed = row.probed_at !== null;
    if ((id !== 'codex' && (hasUpstream || hasProbed))
      || hasUpstream !== hasProbed
      || (hasUpstream && (upstreamMillis === null || probedMillis === null || upstreamMillis > probedMillis))) return null;
    result.push({
      id,
      label,
      pct: row.pct as number | null,
      resets_at_iso: row.resets_at_iso as string | null,
      resets_text: row.resets_text as string | null,
      upstream_reported_at: row.upstream_reported_at as string | null,
      probed_at: row.probed_at as string | null,
    });
  }
  return result;
}

function validatedLimitsHealth(value: unknown): PentacleLimitsHealth | null | undefined {
  if (value === null) return null;
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).sort().join(',') !== LIMITS_HEALTH_TOP_KEYS
    || (value as { schema_version?: unknown }).schema_version !== 1) return undefined;
  const health = (value as { claude?: Record<string, unknown> }).claude;
  if (!health || typeof health !== 'object' || Array.isArray(health)
    || Object.keys(health).sort().join(',') !== LIMITS_HEALTH_KEYS
    || !LIMITS_HEALTH_OUTCOMES.has(health.outcome)
    || !Number.isInteger(health.stale_after_seconds)
    || Number(health.stale_after_seconds) < 60 || Number(health.stale_after_seconds) > 86400) return undefined;
  const attemptedMillis = health.attempted_at === null ? null : utcRfc3339Millis(health.attempted_at);
  const upstreamMillis = health.upstream_reported_at === null ? null : utcRfc3339Millis(health.upstream_reported_at);
  const probedMillis = health.probed_at === null ? null : utcRfc3339Millis(health.probed_at);
  const outcome = String(health.outcome);
  if ((health.attempted_at !== null && attemptedMillis === null)
    || (health.upstream_reported_at !== null && upstreamMillis === null)
    || (health.probed_at !== null && probedMillis === null)
    || ((upstreamMillis === null) !== (probedMillis === null))) return undefined;
  if (outcome === 'never') {
    if (health.attempted_at !== null || health.upstream_reported_at !== null
      || health.probed_at !== null || health.error !== null) return undefined;
  } else {
    if (attemptedMillis === null) return undefined;
    if (outcome === 'ok') {
      if (health.error !== null || upstreamMillis === null || probedMillis === null) return undefined;
    } else {
      const expected = LIMITS_HEALTH_ERRORS[outcome];
      const error = health.error as Record<string, unknown> | null;
      if (!expected || !error || typeof error !== 'object' || Array.isArray(error)
        || Object.keys(error).sort().join(',') !== 'code,message'
        || error.code !== expected.code || error.message !== expected.message) return undefined;
    }
  }
  if (upstreamMillis !== null && (probedMillis === null
    || (outcome === 'ok' && (attemptedMillis === null || attemptedMillis > upstreamMillis))
    || upstreamMillis > probedMillis)) return undefined;
  return {
    schema_version: 1,
    claude: {
      attempted_at: health.attempted_at as string | null,
      outcome: outcome as PentacleLimitsHealth['claude']['outcome'],
      error: health.error === null ? null : {
        code: String((health.error as Record<string, unknown>).code),
        message: String((health.error as Record<string, unknown>).message),
      },
      upstream_reported_at: health.upstream_reported_at as string | null,
      probed_at: health.probed_at as string | null,
      stale_after_seconds: Number(health.stale_after_seconds),
    },
  };
}

function limitsHealthFromFrame(frame: RawFrame): { valid: boolean; value: PentacleLimitsHealth | null } {
  if (!Object.prototype.hasOwnProperty.call(frame, 'limits_health')) return { valid: true, value: null };
  const value = validatedLimitsHealth(frame.limits_health);
  return { valid: value !== undefined, value: value ?? null };
}

export type ChatStoreListener = (state: PentacleStreamState) => void;

// Stale-turn settle (spec_pentacle_desktop_chat_ui_e2e_walk_harness): a turn is
// transitioned to 'idle' by an end-of-turn signal — claude emits a turn-summary
// SYSTEM event; codex emits a "Worked for…" divider. But some real turns never
// produce that marker (e.g. a trivial codex reply with no "Worked for" summary,
// leaving the daemon's tokens_phase stuck at 'down'). Without a settle the turn
// stays 'working' forever and the composer is PERMANENTLY disabled — the user
// can never send again. This safety net settles a 'working' turn locally once a
// reply has started AND the stream has been quiet (no server events / working
// updates) for the grace window. Client-side only (no daemon/shared-reducer
// change); uses the shared clearPentacleTurn. Generous so a silent in-turn pause
// (e.g. a slow tool) re-arms rather than settles prematurely. NOTE: correctness
// of "quiet == done" depends on the daemon emitting working.state heartbeats /
// events during long work; if the daemon can go fully silent >grace mid-tool,
// the composer re-enables early (worst case benign — no data loss/double-send,
// just an early-usable composer). The root cause (daemon codex turn-end on
// trivial replies) is tracked separately.
export const STALE_TURN_GRACE_MS = 12000;

// B2 (chat_send_turn_lifecycle_batch2): how long a daemon working-elapsed anchor
// stays authoritative without a refreshing frame. The daemon heartbeats
// working.state every ~5s (working_state.py HEARTBEAT_MS) while a turn is
// active, so an anchor older than ~3× that means the daemon has gone silent
// (turn likely ended without an explicit idle frame, or a long no-output pause).
// Past this window getWorkingElapsedMs returns null so the timer falls back to
// the legacy client clock (seeded from the last daemon value → smooth
// continuation) instead of free-running a stale interpolation. The next real
// frame re-anchors and the daemon value resumes authority.
export const WORKING_ELAPSED_STALE_MS = 15000;

// Phase 6b (desktop_chat_ui_mobile_parity) — OPTIONAL diagnostics hook.
//
// When the dev/CI harness is armed it installs a diagnostics hook to observe
// per-`chat.event` flow (inbound + persisted-delta), mirroring mobile's
// flow-diagnostics. The hook is NEVER installed in shipped runs (gated by the
// harness flag at the install site), so the read path stays byte-for-byte
// identical and zero-overhead in production. The controller calls the hook ONLY
// when one is set; otherwise this is completely inert.
export type ChatStoreDiagnosticsHook = (info: {
  event: PentacleEvent;
  /** Net change in the persisted event ring for this event's stream. */
  persistedDelta: number;
  /** True when the daemon_seq was already present (reducer dedup no-op). */
  duplicate: boolean;
}) => void;

// Phase 5: how the controller dispatches a correlated send onto the wire. The
// renderer OWNS request_id; main passes it through (chat-stream:send →
// chatStreamClient.sendMessage({ ..., requestId })). Returns the IPC result so
// the controller can mark dispatched/failed. Injectable for tests.
export type ChatSendBridge = (args: {
  streamId: string;
  text: string;
  requestId: string;
  optimisticId: string;
  attachments?: ChatAttachment[];
}) => Promise<{ ok?: boolean; error?: string } | undefined>;

// B3 (chat_send_turn_lifecycle_batch2): how the controller asks main to
// interrupt the RUNNING turn for a stream (daemon injects Escape into the agent
// pane). Resolves host+session from the stream like the send bridge. Injectable
// for tests; absent → cancelTurn still does its local UI cleanup.
export type ChatCancelBridge = (args: {
  streamId: string;
}) => Promise<ChatInterruptResult | undefined>;

export type ChatInterruptConfirm =
  | 'interrupted'
  | 'interrupt_unconfirmed'
  | 'not_working'
  | 'pane_unavailable'
  | 'coalesced';

export type ChatInterruptResult = {
  ok?: boolean;
  error?: string;
  interrupted?: boolean;
  landed?: boolean;
  confirm?: string;
  coalesced?: boolean;
};

export type ChatInterruptState = {
  streamId: string;
  turnId: string;
  optimisticId?: string;
  pending: boolean;
  retryable: boolean;
  confirm?: ChatInterruptConfirm;
  message: string;
  requestedAt: number;
  resolvedAt?: number;
  error?: string;
};

export type ReturnedToPromptDraft = {
  optimisticId: string;
  text: string;
  returnedAt: number;
};

export class ChatStoreController {
  private state: PentacleStreamState;
  private limits: PentacleLimit[] = nullLimits();
  private limitsHealth: PentacleLimitsHealth | null = null;
  private readonly listeners = new Set<ChatStoreListener>();
  // Phase 5: monotonic socket generation, advanced by the main-forwarded
  // `__reconnect` frame's next_generation. Optimistic sends are stamped with
  // the current generation so onReconnect re-arms only the right ones.
  private socketGeneration = 0;
  private optimisticCounter = 0;
  private sendBridge: ChatSendBridge | null = null;
  private cancelBridge: ChatCancelBridge | null = null;
  private interruptStates: Record<string, ChatInterruptState> = {};
  // Phase 6b: optional diagnostics hook (harness-only). null in production.
  private diagnosticsHook: ChatStoreDiagnosticsHook | null = null;
  // Stale-turn settle timers, keyed by streamId (re-armed on each server
  // activity; firing == quiet for the grace window).
  private staleTurnTimers: Record<string, ReturnType<typeof setTimeout>> = {};
  // B2 (chat_send_turn_lifecycle_batch2): per-stream anchor for the daemon's
  // authoritative working elapsed. The daemon's working.state frame carries an
  // integer `elapsed_ms` (now − assistant_turn_started_ms, computed server-side
  // per turn). We anchor that value against the local receipt time so the chat
  // UI can show the REAL per-turn elapsed (matching claude/codex) interpolated
  // locally between the daemon's ~5s heartbeats, instead of a client clock
  // seeded from a parsed label string. The anchor is cleared when the daemon
  // reports elapsed_ms 0 (turn idle) or the turn settles, so the timer resets
  // per turn automatically.
  private workingElapsedAnchors: Record<string, { elapsedMs: number; receivedAt: number }> = {};
  // Injectable monotonic-ish clock (tests override for deterministic
  // interpolation assertions). Production uses Date.now.
  private clock: () => number = () => Date.now();

  constructor(seed: PentacleStreamState = initialPentacleStreamState) {
    this.state = seed;
  }

  /** Install the send IPC bridge used by sendTurn. */
  setSendBridge(bridge: ChatSendBridge | null): void {
    this.sendBridge = bridge;
  }

  /** Install the cancel/interrupt IPC bridge used by cancelTurn (B3). */
  setCancelBridge(bridge: ChatCancelBridge | null): void {
    this.cancelBridge = bridge;
  }

  /**
   * Phase 6b: install (or clear with null) the harness diagnostics hook. Only
   * the dev/CI harness calls this; production never does, so the read path is
   * unchanged and zero-overhead in shipped runs.
   */
  setDiagnosticsHook(hook: ChatStoreDiagnosticsHook | null): void {
    this.diagnosticsHook = hook;
  }

  /** Override the internal clock (tests only) for deterministic timing. */
  setClockForTests(clock: () => number): void {
    this.clock = clock;
  }

  /**
   * B2: the daemon-authoritative working elapsed for a stream, interpolated
   * locally since the last working.state frame. Returns null when no in-flight
   * turn is anchored (the caller should then hide the timer or fall back). The
   * value self-corrects on every frame and resets per turn (the daemon resets
   * its per-turn `assistant_turn_started_ms`, so a new turn re-anchors at a
   * small elapsed). This is the value the chat-slot timer should display so it
   * matches claude/codex's own reported time.
   */
  getWorkingElapsedMs(streamId: string, now: number = this.clock()): number | null {
    const anchor = this.workingElapsedAnchors[streamId];
    if (!anchor) return null;
    // Stale anchor (daemon went silent) → defer to the legacy fallback rather
    // than free-running a value we can no longer trust.
    if (now - anchor.receivedAt > WORKING_ELAPSED_STALE_MS) return null;
    return Math.max(0, anchor.elapsedMs + (now - anchor.receivedAt));
  }

  /** Set/clear the working-elapsed anchor from a daemon-reported elapsed_ms. */
  private updateWorkingElapsedAnchor(streamId: string, elapsedMsRaw: unknown): void {
    if (!streamId) return;
    const elapsedMs = Number(elapsedMsRaw);
    // elapsed_ms 0 / absent / non-finite == no active turn → clear the anchor so
    // getWorkingElapsedMs returns null (timer hidden / falls back, resets per
    // turn). A positive value re-anchors against the local receipt time.
    if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) {
      delete this.workingElapsedAnchors[streamId];
      return;
    }
    this.workingElapsedAnchors[streamId] = { elapsedMs, receivedAt: this.clock() };
  }

  /** Current immutable read-path state. */
  getState(): PentacleStreamState {
    return this.state;
  }

  getLimits(): PentacleLimit[] {
    return this.limits.map((limit) => ({ ...limit }));
  }

  getLimitsHealth(): PentacleLimitsHealth | null {
    if (!this.limitsHealth) return null;
    return {
      schema_version: 1,
      claude: {
        ...this.limitsHealth.claude,
        error: this.limitsHealth.claude.error ? { ...this.limitsHealth.claude.error } : null,
      },
    };
  }

  /**
   * Subscribe to state changes. Returns an unsubscribe function. Mirrors the
   * mobile store's subscribe contract (listeners are called after each applied
   * frame that produced a new state object).
   */
  subscribe(listener: ChatStoreListener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  // ── Chat-model selectors (for Phase 4 consumption) ──────────────
  selectChatList(filter: 'all' | string = 'all'): PentacleChatListItem[] {
    return selectChatList(this.state, filter);
  }

  selectSessionDetail(
    streamId: string,
    options?: Parameters<typeof selectSessionDetail>[2],
  ): PentacleSessionDetail | null {
    return selectSessionDetail(this.state, streamId, options);
  }

  // Phase 6b harness helper: count persisted (non-DRAFT) events for a stream.
  // Only called when the diagnostics hook is installed.
  private countEventsForStream(streamId: string): number {
    let count = 0;
    for (const event of this.state.events) {
      if (event.stream_id === streamId && event.kind !== 'DRAFT') count += 1;
    }
    return count;
  }

  private setState(next: PentacleStreamState): void {
    if (next === this.state) return;
    this.state = next;
    this.notifyListeners();
  }

  private notifyListeners(): void {
    for (const listener of this.listeners) listener(this.state);
  }

  // ── Phase 5: optimistic send + turn phase ───────────────────────

  /**
   * Current turn/working phase for a stream (for send-button gating). Returns
   * 'idle' when no turn is in flight. Mirrors mobile's
   * workingByStream[streamId]?.phase read in the composer.
   */
  getTurnPhase(streamId: string): 'idle' | 'pending' | 'working' {
    return (this.state.workingByStream?.[streamId] as TurnState | undefined)?.phase ?? 'idle';
  }

  /** Full turn state for a stream (or undefined when idle/unknown). */
  getTurnState(streamId: string): TurnState | undefined {
    return this.state.workingByStream?.[streamId] as TurnState | undefined;
  }

  getInterruptState(streamId: string): ChatInterruptState | null {
    if (!streamId) return null;
    this.reconcileInterruptStateForStream(streamId);
    return this.interruptStates[streamId] ?? null;
  }

  getReturnedToPromptDraft(streamId: string): ReturnedToPromptDraft | null {
    if (!streamId) return null;
    let best: ReturnedToPromptDraft | null = null;
    for (const send of Object.values(this.state.optimisticSends ?? {})) {
      if (!send || send.stream_id !== streamId || send.status !== 'returned_to_prompt') continue;
      const text = String(send.text || '');
      if (!text.trim()) continue;
      const returnedAt = typeof send.returned_at === 'number' ? send.returned_at : send.created_at;
      if (!best || returnedAt >= best.returnedAt) {
        best = { optimisticId: send.optimistic_id, text, returnedAt };
      }
    }
    return best;
  }

  /**
   * The pending agent-asked question for a stream (claude AskUserQuestion
   * selector), as set by the daemon on the session summary. Returns null when
   * no question is pending. Backward-compatible: reads the same `question`
   * field the daemon writes onto the session summary frame.
   */
  getQuestion(streamId: string): PentacleQuestion | null {
    const session = this.state.sessions.find((item) => item.stream_id === streamId);
    return session?.question ?? null;
  }

  /**
   * Pure: is this turn eligible for a stale-settle? Only a turn that is actively
   * 'working' AND has already produced a server reply (firstServerEventAt set)
   * is eligible — a 'pending' turn (send not yet acknowledged) or one with no
   * reply yet is left alone. Exported-via-static for unit testing.
   */
  static isTurnStaleEligible(turn: TurnState | undefined): boolean {
    return !!turn && turn.phase === 'working' && typeof turn.firstServerEventAt === 'number';
  }

  /**
   * (Re)arm the stale-turn settle timer for a stream. Called after each server
   * event / working.state for a stream with an in-flight turn; re-arming means
   * the timer only fires after the grace window of NO further activity.
   */
  private armStaleTurnSettle(streamId: string): void {
    if (typeof setTimeout !== 'function' || !streamId) return;
    const existing = this.staleTurnTimers[streamId];
    if (existing) clearTimeout(existing);
    const turn = this.getTurnState(streamId);
    if (!ChatStoreController.isTurnStaleEligible(turn)) {
      delete this.staleTurnTimers[streamId];
      return;
    }
    // Capture this turn's identity so a timer armed for turn A can never settle
    // a later turn B for the same stream (e.g. if A was already cleared by a
    // snapshot/inventory falling edge and B started before A's timer fired).
    const armedId = ChatStoreController.turnIdentity(turn);
    this.staleTurnTimers[streamId] = setTimeout(() => {
      delete this.staleTurnTimers[streamId];
      this.maybeSettleStaleTurn(streamId, armedId);
    }, STALE_TURN_GRACE_MS);
    // Don't keep the event loop alive for this timer (Node/tests).
    const t = this.staleTurnTimers[streamId] as unknown as { unref?: () => void };
    if (typeof t?.unref === 'function') t.unref();
  }

  /** Stable per-turn identity used to guard the settle against stale timers. */
  private static turnIdentity(turn: TurnState | undefined): string {
    return turn ? `${turn.optimisticId ?? ''}:${turn.sentAt ?? ''}:${turn.firstServerEventAt ?? ''}` : '';
  }

  /**
   * Settle a still-'working' turn to idle (composer re-enable) if it is still
   * eligible AND still the same turn the timer was armed for (identity match).
   */
  private maybeSettleStaleTurn(streamId: string, armedId: string): void {
    const turn = this.getTurnState(streamId);
    if (!ChatStoreController.isTurnStaleEligible(turn)) return;
    if (ChatStoreController.turnIdentity(turn) !== armedId) return;
    // B2: a settled turn has no live elapsed — drop the anchor so the timer
    // hides/resets rather than continuing to interpolate a stale value.
    delete this.workingElapsedAnchors[streamId];
    this.setState(clearPentacleTurn(this.state, streamId));
  }

  /** Clear all pending settle timers (tests / teardown). */
  dispose(): void {
    for (const id of Object.keys(this.staleTurnTimers)) {
      clearTimeout(this.staleTurnTimers[id]);
      delete this.staleTurnTimers[id];
    }
  }

  private nextOptimisticId(streamId: string): string {
    const short = String(streamId || 'stream').replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 48) || 'stream';
    this.optimisticCounter += 1;
    return `optimistic_${short}_${this.optimisticCounter}`;
  }

  private nextRequestId(prefix: string): string {
    return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  }

  /**
   * Mobile-faithful server-USER-echo reconciliation. Finds an in-flight
   * optimistic send matching this server event (same stream + text within the
   * reconcile window) and, if found, drops the client row + keeps the server
   * event + prunes the optimistic via reconcileOptimisticSendWithServerEvent.
   * Otherwise applies the event plainly. Returns the next state (does NOT call
   * setState; callers do).
   */
  private applyServerUserEventWithReconciliation(event: PentacleEvent): PentacleStreamState {
    const optimisticId = this.findMatchingOptimisticId(event);
    if (!optimisticId) {
      return applyPentacleEvent(this.state, event);
    }
    // Optimistic reconciled by the server USER echo (the send is now 'echoed':
    // client row dropped, server event kept, optimistic pruned). Mirrors
    // mobile's optimistic_reconciled beacon.
    logTelemetry(TELEMETRY_EVENTS.CHAT_COMPOSE_OPTIMISTIC_RECONCILED, {
      stream_id: event.stream_id,
      optimistic_id: optimisticId,
      daemon_seq: event.daemon_seq,
    });
    return reconcileOptimisticSendWithServerEvent(this.state, optimisticId, event);
  }

  private findMatchingOptimisticId(event: PentacleEvent): string | null {
    for (const [optimisticId, optimistic] of Object.entries(this.state.optimisticSends ?? {})) {
      // B4: a HELD (turn_queued) send is not on the wire — exclude it from
      // reconciliation so a server echo of a different in-flight message with
      // the same text cannot prune the queued message before it is dispatched.
      if (optimistic.turn_queued === true) continue;
      const reconcilable = optimistic.status === 'queued'
        || optimistic.status === 'dispatched'
        || optimistic.status === 'acked'
        || optimistic.status === 'indeterminate'
        || optimistic.status === 'echoed';
      if (!reconcilable) continue;
      if (optimisticMatchesServerUser(optimistic, event, OPTIMISTIC_RECONCILE_WINDOW_MS)) {
        return optimisticId;
      }
    }
    return null;
  }

  /**
   * Optimistic send entry point (the desktop analogue of mobile's sendTurn →
   * appendOptimisticUserMessage → sendPentacleMessage). Synchronously:
   *   1. insert the optimistic USER event + dispatch a correlated
   *      (renderer-owned request_id) send immediately;
   *   2. start a local pending turn only when the stream was idle before this
   *      send. If the agent is already working, the provider CLI owns the
   *      native queue; the operator's message is transmitted without replacing
   *      the visible running turn.
   * Returns the optimistic_id ('' only on empty/invalid input).
   */
  sendTurn(streamId: string, text: string, attachments: ChatAttachment[] = []): string {
    const trimmed = String(text || '').trim();
    const sendAttachments = Array.isArray(attachments) ? attachments.filter((item) => item && item.key && item.mime) : [];
    if (!streamId || (!trimmed && sendAttachments.length === 0)) return '';

    const optimisticId = this.nextOptimisticId(streamId);
    const requestId = this.nextRequestId('send');
    const now = Date.now();
    const generation = this.socketGeneration;
    const beginTurn = this.getTurnPhase(streamId) === 'idle';

    this.setState(sendOptimisticMessage(this.state, {
      streamId,
      text: trimmed,
      optimisticId,
      requestId,
      createdAt: now,
      windowStartedAt: this.state.connected ? now : null,
      socketGeneration: generation,
      beginTurn,
      ...(sendAttachments.length ? { attachments: sendAttachments } : {}),
    }));

    // Optimistic-send lifecycle telemetry (mirrors mobile's beacons). Routed
    // through the shared sink; the dev/CI harness captures it. The default sink
    // only logs, so production rendering is unaffected.
    logTelemetry(TELEMETRY_EVENTS.CHAT_COMPOSE_OPTIMISTIC_INSERT, {
      stream_id: streamId,
      optimistic_id: optimisticId,
      request_id: requestId,
    });

    this.dispatchOptimistic(streamId, optimisticId, requestId, trimmed, generation, sendAttachments);

    return optimisticId;
  }

  /**
   * Re-send a visibly recoverable row as an explicit NEW attempt.  The
   * optimistic id (and therefore its rendered row + attachments) remains stable,
   * while the request id rotates so a terminal answer for the abandoned attempt
   * cannot be mistaken for this user-directed retry.
   */
  retryOptimisticSend(optimisticId: string): boolean {
    const send = this.state.optimisticSends?.[optimisticId];
    if (!send || (send.status !== 'failed' && send.status !== 'indeterminate')) return false;
    const requestId = this.nextRequestId('retry');
    this.setState(rotateOptimisticSendRequestId(this.state, optimisticId, requestId));
    const retry = this.state.optimisticSends?.[optimisticId];
    if (!retry) return false;
    this.dispatchOptimistic(retry.stream_id, optimisticId, requestId, retry.text, this.socketGeneration, retry.attachments ?? []);
    logTelemetry(TELEMETRY_EVENTS.CHAT_COMPOSE_OPTIMISTIC_INSERT, {
      stream_id: retry.stream_id,
      optimistic_id: optimisticId,
      request_id: requestId,
      retry: true,
    });
    return true;
  }

  private replayReconnectSurvivors(generation: number): void {
    for (const optimisticId of eligibleReconnectReplayOptimisticIds(this.state, generation)) {
      const send = this.state.optimisticSends?.[optimisticId];
      if (!send) continue;
      this.dispatchOptimistic(send.stream_id, optimisticId, send.request_id, send.text, generation, send.attachments ?? []);
      logTelemetry(TELEMETRY_EVENTS.CHAT_COMPOSE_OPTIMISTIC_INSERT, {
        stream_id: send.stream_id,
        optimistic_id: optimisticId,
        request_id: send.request_id,
        replay: true,
      });
    }
  }

  /**
   * Put an optimistic send on the wire via the injected bridge and resolve its
   * lifecycle: dispatched on success, failed/indeterminate on error.
   */
  private dispatchOptimistic(streamId: string, optimisticId: string, requestId: string, text: string, generation: number, attachments: ChatAttachment[] = []): void {
    if (!this.sendBridge) return;
    Promise.resolve()
      .then(() => this.sendBridge!({
        streamId,
        text,
        requestId,
        optimisticId,
        ...(attachments.length ? { attachments } : {}),
      }))
      .then((result) => {
        if (result && result.ok === false) {
          this.resolveSendDispatchError(streamId, optimisticId, requestId, String(result.error || 'send_error'));
          return;
        }
        this.setState(markOptimisticDispatchedByRequestId(this.state, requestId, Date.now(), generation));
      })
      .catch((error: unknown) => {
        this.resolveSendDispatchError(streamId, optimisticId, requestId, error instanceof Error ? error.message : String(error || 'send_error'));
      });
  }

  private static normalizeInterruptConfirm(result: ChatInterruptResult | undefined): ChatInterruptConfirm {
    const confirm = String(result?.confirm || '');
    if (
      confirm === 'interrupted'
      || confirm === 'interrupt_unconfirmed'
      || confirm === 'not_working'
      || confirm === 'pane_unavailable'
      || confirm === 'coalesced'
    ) {
      return confirm;
    }
    if (result?.coalesced === true) return 'coalesced';
    if (result?.ok === false) return 'pane_unavailable';
    if (result?.interrupted === false) return 'not_working';
    return 'interrupted';
  }

  private static interruptMessageFor(confirm: ChatInterruptConfirm, error?: string): string {
    if (confirm === 'interrupt_unconfirmed' || confirm === 'pane_unavailable') {
      return error
        ? `Stop did not take (${error}). Press Esc again to retry.`
        : 'Stop did not take. Press Esc again to retry.';
    }
    return '';
  }

  private setInterruptState(streamId: string, state: ChatInterruptState | null): void {
    if (!streamId) return;
    if (state) this.interruptStates[streamId] = state;
    else delete this.interruptStates[streamId];
    this.notifyListeners();
  }

  private reconcileInterruptStateForStream(streamId: string): void {
    const state = this.interruptStates[streamId];
    if (!state) return;
    const turn = this.getTurnState(streamId);
    if (!turn) return;
    const currentTurnId = ChatStoreController.turnIdentity(turn);
    if (currentTurnId && currentTurnId !== state.turnId) {
      delete this.interruptStates[streamId];
    }
  }

  private requestInterrupt(streamId: string, turnId: string, optimisticId?: string): void {
    if (!this.cancelBridge) return;
    const requestedAt = Date.now();
    this.setInterruptState(streamId, {
      streamId,
      turnId,
      optimisticId,
      pending: true,
      retryable: false,
      message: 'Stopping...',
      requestedAt,
    });
    Promise.resolve()
      .then(() => this.cancelBridge!({ streamId }))
      .then((result) => {
        const confirm = ChatStoreController.normalizeInterruptConfirm(result);
        const retryable = confirm === 'interrupt_unconfirmed' || confirm === 'pane_unavailable';
        const error = result?.ok === false ? String(result.error || '') : undefined;
        this.setInterruptState(streamId, {
          streamId,
          turnId,
          optimisticId,
          pending: false,
          retryable,
          confirm,
          message: ChatStoreController.interruptMessageFor(confirm, error),
          requestedAt,
          resolvedAt: Date.now(),
          ...(error ? { error } : {}),
        });
        if (result && result.ok === false) {
          logTelemetry(TELEMETRY_EVENTS.CHAT_COMPOSE_OPTIMISTIC_FAILED, {
            stream_id: streamId, optimistic_id: optimisticId, reason: `interrupt_failed:${result.error || ''}`,
          });
        }
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error || '');
        this.setInterruptState(streamId, {
          streamId,
          turnId,
          optimisticId,
          pending: false,
          retryable: true,
          confirm: 'pane_unavailable',
          message: ChatStoreController.interruptMessageFor('pane_unavailable', message),
          requestedAt,
          resolvedAt: Date.now(),
          error: message,
        });
        logTelemetry(TELEMETRY_EVENTS.CHAT_COMPOSE_OPTIMISTIC_FAILED, {
          stream_id: streamId, optimistic_id: optimisticId,
          reason: `interrupt_error:${message}`,
        });
      });
  }

  /**
   * B3: cancel "the latest chat" for a stream — the ESC / cancel-control entry
   * point. Two cases:
   *   A. An in-flight turn → interrupt the RUNNING agent (cancelBridge → daemon
   *      injects Escape, the ESC-in-terminal equivalent), and IMMEDIATELY clear
   *      the working/waiting indicator (don't wait for the 12s stale settle). If
   *      the turn's optimistic send had not yet confirmed delivery, mark it
   *      'cancelled'; if it was already echoed/acked the user message genuinely
   *      landed, so the bubble stays sent and only the response is interrupted.
   *      Native-queued sends have already been transmitted to the provider CLI,
   *      so they are not treated as local cancellable drafts.
   * Returns true if something was cancelled.
   */
  cancelTurn(streamId: string): boolean {
    if (!streamId) return false;
    const turn = this.getTurnState(streamId);

    if (turn) {
      const turnId = ChatStoreController.turnIdentity(turn);
      const existing = this.interruptStates[streamId];
      if (existing && existing.turnId === turnId && (existing.pending || !existing.retryable)) {
        return true;
      }
      // Clear the indicator promptly: drop the elapsed anchor + any pending
      // stale-settle timer, then clear the turn.
      delete this.workingElapsedAnchors[streamId];
      const timer = this.staleTurnTimers[streamId];
      if (timer) {
        clearTimeout(timer);
        delete this.staleTurnTimers[streamId];
      }
      let next = clearPentacleTurn(this.state, streamId);
      const optimisticId = turn.optimisticId;
      if (optimisticId) {
        const send = this.state.optimisticSends?.[optimisticId];
        const confirmed = send && (send.status === 'acked' || send.status === 'echoed' || send.status === 'reconciled');
        if (send && !confirmed) {
          next = markOptimisticCancelledByOptimisticId(next, optimisticId, Date.now());
        }
      }
      this.setState(next);
      this.requestInterrupt(streamId, turnId, optimisticId);
      logTelemetry(TELEMETRY_EVENTS.CHAT_COMPOSE_OPTIMISTIC_FAILED, {
        stream_id: streamId,
        optimistic_id: optimisticId,
        reason: 'cancelled',
      });
      return true;
    }

    const existing = this.interruptStates[streamId];
    if (existing?.pending) return true;
    if (existing?.retryable) {
      this.requestInterrupt(streamId, existing.turnId, existing.optimisticId);
      return true;
    }

    return false;
  }

  /**
   * Resolve a send-dispatch error (IPC rejected or returned ok:false). Mirrors
   * mobile: a TRANSIENT failure (the ws is down / reconnecting, or the error
   * looks like a disconnect) must NOT permanently fail the optimistic — it is
   * marked INDETERMINATE so the daemon's server USER echo (if it received the
   * send) or a send.result/send.indeterminate frame can still reconcile it after
   * the reconnect. Only a genuine failure while connected is marked FAILED.
   */
  private resolveSendDispatchError(streamId: string, optimisticId: string, requestId: string, reason: string): void {
    const transient = this.state.connected !== true || /disconnect|closed|not open|reconnect|socket|ECONN|timed out/i.test(reason);
    if (transient) {
      this.setState(markOptimisticIndeterminateByRequestId(this.state, requestId, Date.now()));
      return;
    }
    this.setState(markOptimisticFailedByRequestId(this.state, requestId, reason));
    logTelemetry(TELEMETRY_EVENTS.CHAT_COMPOSE_OPTIMISTIC_FAILED, {
      stream_id: streamId, optimistic_id: optimisticId, request_id: requestId, reason,
    });
  }

  /**
   * Apply one RAW daemon frame. Switches on `frame.type` exactly like mobile's
   * handleMessage and dispatches to the matching reducer entry point. Unknown
   * or non-read-path frame types are ignored (no-op), keeping this purely
   * additive. Returns the resulting state.
   */
  applyFrame(frame: RawFrame | null | undefined): PentacleStreamState {
    if (!frame || typeof frame !== 'object') return this.state;
    const type = frame.type;

    if (type === 'snapshot') {
      if (Object.prototype.hasOwnProperty.call(frame, 'limits')) {
        const limits = validatedLimits(frame.limits);
        if (limits) {
          const health = limitsHealthFromFrame(frame);
          if (health.valid) {
            this.limits = limits;
            this.limitsHealth = health.value;
          }
        }
      }
      // Mirror mobile: feed the snapshot's component arrays/maps into the
      // snapshot reducer. We pass the raw snake_case fields straight through;
      // the reducer normalizes. No locally-closing-stream filtering (Phase 3
      // has no close/optimistic state) and no optimistic reconciliation side
      // effects beyond what the reducer does with an empty optimistic set.
      const next = applySnapshotWithOptimisticReconciliation(this.state, {
        events: frame.events as PentacleEvent[] | undefined,
        drafts: frame.drafts as Record<string, PentacleEvent> | undefined,
        hosts: frame.hosts as Record<string, PentacleHostStatus> | undefined,
        machine_stats: frame.machine_stats as Record<string, PentacleMachineStats> | undefined,
        sessions: frame.sessions as PentacleSessionSummary[] | undefined,
        updates: frame.updates as PentacleUpdateMessage[] | undefined,
        notifications: frame.notifications as PentacleNotification[] | undefined,
        working_states: frame.working_states as Record<string, WorkingStateData> | undefined,
      });
      this.setState(next);
      // B2: seed working-elapsed anchors from the snapshot so a freshly-opened
      // session that is mid-turn shows the real daemon elapsed immediately.
      const snapWorking = frame.working_states as Record<string, WorkingStateData> | undefined;
      if (snapWorking && typeof snapWorking === 'object') {
        for (const [sid, ws] of Object.entries(snapWorking)) {
          this.updateWorkingElapsedAnchor(sid, (ws as { elapsed_ms?: unknown })?.elapsed_ms);
        }
      }
      return this.state;
    }

    if (type === 'limits.update') {
      const limits = validatedLimits(frame.limits);
      const health = limitsHealthFromFrame(frame);
      if (limits && health.valid) {
        this.limits = limits;
        this.limitsHealth = health.value;
        this.notifyListeners();
      }
      return this.state;
    }

    if (type === 'machine.stats' && frame.stats) {
      this.setState(
        applyPentacleMachineStats(this.state, frame.stats as PentacleMachineStats),
      );
      return this.state;
    }

    if (type === 'machine.stats.inventory' && frame.machine_stats) {
      this.setState(
        applyPentacleMachineStatsInventory(
          this.state,
          frame.machine_stats as Record<string, PentacleMachineStats>,
        ),
      );
      return this.state;
    }

    if (type === 'chat.event' && frame.event) {
      // Phase 5 (desktop_chat_ui_mobile_parity): route server USER echoes
      // through the optimistic-reconciliation variant exactly like mobile's
      // handleMessage. A server-origin USER event (client_origin !== true) may
      // be the daemon echo of an optimistic send — reconcile it (drop the
      // client row, keep the server event, prune the optimistic). Every other
      // event applies plainly. The reconciliation path is a safe superset: with
      // no optimistic sends it falls back to applyPentacleEvent (no-op match).
      const event = frame.event as PentacleEvent;
      // Phase 6b: snapshot the per-stream persisted count ONLY when the harness
      // diagnostics hook is installed. In production (hook null) we skip this
      // entirely — no extra work on the hot read path.
      const hook = this.diagnosticsHook;
      const beforeCount = hook ? this.countEventsForStream(event.stream_id) : 0;
      const isServerUser = String(event.kind || '').toUpperCase() === 'USER' && event.client_origin !== true;
      if (isServerUser) {
        this.setState(this.applyServerUserEventWithReconciliation(event));
      } else {
        this.setState(applyPentacleEvent(this.state, event));
      }
      if (hook) {
        const afterCount = this.countEventsForStream(event.stream_id);
        const persistedDelta = afterCount - beforeCount;
        hook({ event, persistedDelta, duplicate: persistedDelta <= 0 });
      }
      // Re-arm the stale-turn settle on each server event for this stream.
      this.armStaleTurnSettle(event.stream_id);
      return this.state;
    }

    if (type === 'stream_events' && Array.isArray(frame.events)) {
      // Desktop lazy-load backfill: the daemon ships no events under
      // events_mode:'summary', so opening a transcript triggers a
      // request_stream_events RPC whose result main forwards here as a
      // 'stream_events' frame. Merge the fetched history into the reducer state
      // (dedupe + per-stream content-version bump) WITHOUT touching session /
      // working / draft state — this is historical backfill, not live events.
      // Without this, the fetched ring lands only in the legacy renderer store
      // and the parity chat UI (which renders from this reducer state) shows an
      // empty transcript — just the session-summary fallback divider.
      this.setState(applyFetchedStreamEvents(this.state, frame.events as PentacleEvent[], {
        requestedStreamId: typeof frame.stream_id === 'string' ? frame.stream_id : undefined,
      }));
      return this.state;
    }

    if (type === 'send.result' && typeof frame.request_id === 'string') {
      // Phase 5: daemon send-completion frame forwarded by main. landed → ack
      // the optimistic; not_landed (or any non-landed delivery) → fail it. Both
      // map by request_id (the renderer-owned id we put on the wire). No-op if
      // the optimistic was already reconciled/pruned by the server USER echo.
      const requestId = frame.request_id as string;
      if (frame.delivery === 'landed') {
        this.setState(markOptimisticAckedByRequestId(this.state, requestId, Date.now()));
      } else if (frame.action_committed === true || frame.confirmation_pending === true) {
        this.setState(markOptimisticIndeterminateByRequestId(this.state, requestId, Date.now()));
      } else {
        const reason = String(frame.reason || frame.delivery || 'send_failed');
        this.setState(markOptimisticFailedByRequestId(this.state, requestId, reason));
      }
      return this.state;
    }

    if (type === 'send.indeterminate' && typeof frame.request_id === 'string') {
      // Mirror mobile: the daemon could not confirm delivery (e.g. across a
      // disconnect). Keep the optimistic reconcilable (indeterminate) so a later
      // server USER echo can still reconcile it, rather than failing it.
      this.setState(markOptimisticIndeterminateByRequestId(this.state, frame.request_id as string, Date.now()));
      return this.state;
    }

    if (type === '__reconnect') {
      // Phase 5: synthetic reconnect signal from main. Run onReconnect for the
      // generation whose optimistic sends survived the drop, then advance our
      // tracked generation so subsequent sends stamp the new value. Mirrors
      // mobile's ws.onopen pendingReconnectSurvivorGeneration handling.
      const generation = Number(frame.generation);
      const nextGeneration = Number.isFinite(Number(frame.next_generation))
        ? Number(frame.next_generation)
        : generation + 1;
      this.limits = nullLimits();
      this.limitsHealth = null;
      if (Number.isFinite(generation)) {
        if (frame.daemon_restarted === true) {
          this.setState(markDaemonRestartSurvivorsIndeterminate(this.state, generation));
        } else {
          this.setState(onReconnect(this.state, generation, Date.now(), nextGeneration));
          this.replayReconnectSurvivors(nextGeneration);
        }
      }
      this.socketGeneration = nextGeneration;
      return this.state;
    }

    if (type === 'working.state') {
      // Mobile casts the whole frame as WorkingStateData — the working-state
      // fields are top-level on the frame, not nested. Mirror that exactly.
      this.setState(applyPentacleWorkingState(this.state, frame as unknown as WorkingStateData));
      const wsStreamId = typeof frame.stream_id === 'string' ? (frame.stream_id as string) : '';
      if (wsStreamId) {
        // B2: anchor the daemon's authoritative per-turn elapsed for the timer.
        this.updateWorkingElapsedAnchor(wsStreamId, frame.elapsed_ms);
        this.armStaleTurnSettle(wsStreamId);
      }
      return this.state;
    }

    if (type === 'host.status' && frame.host) {
      this.setState(applyPentacleHostStatus(this.state, frame.host as PentacleHostStatus));
      return this.state;
    }

    if (type === 'session.inventory' && Array.isArray(frame.sessions)) {
      this.setState(
        applyPentacleSessionInventory(this.state, frame.sessions as PentacleSessionSummary[]),
      );
      return this.state;
    }

    // Any other frame type (send.result, *.ok/.error, ping, …) is
    // outside Phase 3's read-path scope — ignore it here.
    return this.state;
  }
}
