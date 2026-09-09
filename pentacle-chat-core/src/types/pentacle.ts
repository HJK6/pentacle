import type { ChildAgent } from "../services/daemonUpdates";
export type PentacleEventKind =
  | 'USER'
  | 'ASSIST'
  | 'ASSIST_TEXT'
  | 'TOOL'
  | 'TOOL-OUT'
  | 'TOOL_USE'
  | 'TOOL_RESULT'
  | 'THINK'
  | 'THINKING'
  | 'SYSTEM'
  | 'WORKING'
  | 'DRAFT'
  | string;

// Cross-lane attachment contract (spec
// public protocol contract, ## Attachment model).
// Both lanes code against this single shape: the mobile lane builds the `send`
// RPC payload + renders the bubble; the daemon/chat-core lane resolves the blob
// `key` and injects an agent-local path. Authored here (the platform-neutral
// contract module) and consumed by mobile.
//
// Wire = text + the `key`s. The client uploads each image to chat_streamd via
// the chunked blob RPC, then sends the returned blob sha as `key` on the `send`
// RPC. The daemon stores bytes durably, places agent-local files for vision, and
// keeps `localPath` daemon-side only, so it is intentionally NOT a field on this
// type.
export interface ChatAttachment {
  key: string; // blob sha — the durable ref the client sends on the wire.
  mime: string; // post HEIC→transcode: "image/jpeg" | "image/png".
  width?: number; // px, for bubble layout.
  height?: number; // px, for bubble layout.
  bytes?: number; // size after client-side compression.
  sha256?: string; // content hash (dedupe; local BlobStore parity).
}

// Max attachments per send (spec ## Decisions D2). Shared so both the mobile
// composer (enforce before upload) and the daemon (reject over-limit sends) use
// the same bound.
export const MAX_CHAT_ATTACHMENTS = 5;

export interface PentacleEvent {
  daemon_seq: number;
  host: string;
  provider: string;
  session_id: string;
  session_name: string;
  stream_id: string;
  timestamp: string;
  kind: PentacleEventKind;
  text: string;
  raw?: Record<string, unknown>;
  jsonl_record_uuid?: string;
  jsonl_resolution_for_record_uuid?: string;
  client_origin?: boolean;
  optimistic_id?: string;
  receiptDirectMatch?: boolean;
  correlatedDaemonSeq?: number | null;
  pending?: boolean;
  created_at?: number;
  queued_at?: number;
  // Image attachments carried by a client-origin USER event (optimistic +
  // rendered bubbles). FIFO order is preserved and mirrors the order the daemon
  // names the paths in its inject instruction line.
  attachments?: ChatAttachment[];
}

export interface PentacleHostStatus {
  host: string;
  online: boolean;
  checked_at: string;
  session_count: number;
  error?: string;
  host_status_reason?: 'unreachable' | string;
  host_status_since?: string;
}

// Daemon-owned machine stats sample, projected verbatim from a `hosts.stats`
// frame (docs/chat_protocol.md § Machine stats). The daemon is the sole
// sampler and timestamp authority; the client only projects and flags
// staleness from `sampled_at`.
export interface PentacleMachineStats {
  host: string;
  cpu_load_1m: number;
  memory_used_bytes: number;
  memory_total_bytes: number;
  disk_used_bytes: number;
  disk_total_bytes: number;
  uptime_seconds: number;
  sampled_at: string;
}

export type SessionStatusCardStepStatus = 'pending' | 'active' | 'done';

export interface SessionStatusCardStep {
  text: string;
  status: SessionStatusCardStepStatus;
}

export interface SessionStatusCardSpec {
  id: string;
  label: string;
  ok: boolean;
  updated: string | null;
  note?: string;
  status?: string | null;
}

export interface PentacleSpecStatusCapability {
  name: string;
  order?: number;
  display_label?: string;
  color?: string;
  is_terminal?: boolean;
}

// Agent-written per-session status card set via `agent-orch status`
// (public-session-status-card). The daemon stamps updated_at
// on every successful write; all other fields are optional partial state.
export interface SessionStatusCard {
  goal?: string;
  plan?: SessionStatusCardStep[];
  update?: string;
  updates?: Array<{ ts: string; text: string }>;
  specs?: SessionStatusCardSpec[];
  handoff_planned?: boolean;
  updated_at: string;
}

// One sweeper-lane spec-issue entry (set on obligation expiry, cleared when
// the finding re-validates clean). Shape owned by the sweeper lane; the card
// UI renders count + detail when present.
export interface SessionSpecIssue {
  obligation_id?: string;
  detail?: string;
  set_at?: string;
}

export interface PentacleLastTextProvenance {
  schema_version: 1;
  kind: 'notification.answer';
  tell_id: string;
  injected_text: string;
}

export interface PentacleSessionSummary {
  agents?: ChildAgent[];
  // Daemon-projected session role (e.g. "nexus"). Mobile gates the Sub-agents roster and the
  // chat-row history affordance on role === "nexus"; the daemon passes it through unchanged.
  role?: string | null;
  objective?: string | null;
  session_generation?: string;
  stream_id: string;
  host: string;
  provider: string;
  model?: string | null;
  effort?: string | null;
  session_name: string;
  display_name?: string;
  title?: string;
  visibility?: string;
  last_event_at: string;
  last_text: string;
  last_text_provenance?: PentacleLastTextProvenance | null;
  last_kind: string;
  draft: string;
  pending: boolean;
  working: boolean;
  working_label?: string;
  // Canonical daemon lifecycle for newly admitted sessions. Older daemons and
  // legacy rows may omit it or project a non-modern compatibility value.
  bootstrap_state?: 'queued' | 'starting' | 'ready' | 'failed' | string | null;
  online: boolean;
  host_status?: 'online' | 'offline' | 'unknown' | string;
  host_status_reason?: 'unreachable' | string;
  host_status_since?: string;
  // Additive tmux-observation state. Absent for older daemons and healthy rows.
  pane_status?: 'pane_unresponsive' | string;
  pane_status_reason?: 'tmux_probe_timeout' | string;
  pane_status_since?: string;
  // Set by the daemon when the agent is asking an interactive question
  // (claude AskUserQuestion selector). null/absent when no question is pending.
  question?: PentacleQuestion | null;
  // Agent-written status card; null/absent when the session never set one.
  status_card?: SessionStatusCard | null;
  // Daemon-attached card indicators, populated by sibling lanes
  // (context tracking / sweeper remediation); rendered only when present.
  context_tokens?: number | null;
  model_context_window?: number | null;
  context_level?: string | null;
  spec_issues?: SessionSpecIssue[] | null;
}

export interface PentacleQuestionOption {
  index: number;
  label: string;
  description?: string;
  meta?: boolean;
}

export interface PentacleQuestionItem {
  index: number;
  header?: string;
  prompt: string;
  options: PentacleQuestionOption[];
  multiSelect?: boolean;
  answered?: boolean;
  selected_index?: number;
  free_text?: boolean;
}

export interface PentacleQuestion {
  header?: string;
  prompt: string;
  options: PentacleQuestionOption[];
  multi?: boolean;
  multiSelect?: boolean;
  questions?: PentacleQuestionItem[];
  active_index?: number;
  submit_present?: boolean;
  selected_index?: number;
  scan_incomplete?: boolean;
  question_nonce?: string;
}

export type PentacleLimitId = 'claude' | 'fable' | 'codex';

export interface PentacleLimit {
  id: PentacleLimitId;
  label: 'Claude' | 'Fable' | 'Codex';
  pct: number | null;
  resets_at_iso: string | null;
  resets_text: string | null;
}

// Daemon usage-probe health sidecar (server.py hello `limits_health`;
// usage_publisher.health_snapshot). Projection only: the client renders the
// per-provider outcome as text and never hides a limit row on account of it.
export interface PentacleProviderHealth {
  outcome: string;
  error: { code?: string | null; message?: string | null } | null;
  attempted_at?: string | null;
  upstream_reported_at?: string | null;
  probed_at?: string | null;
  stale_after_seconds?: number | null;
}

export interface PentacleLimitsHealth {
  schema_version: number;
  claude?: PentacleProviderHealth | null;
  fable?: PentacleProviderHealth | null;
  codex?: PentacleProviderHealth | null;
}

export interface PentacleUpdateMessage {
  agent_id: string;
  timestamp: number;
  direction: 'inbound' | 'outbound';
  message: string;
  sender: string;
  image_url?: string;
  ack?: boolean;
}

export type NotificationActionKind = 'ack' | 'yes_no' | 'spawn_worker' | 'run_command' | 'resolved';

export type NotificationSeverity = 'info' | 'warning' | 'critical';

export type NotificationState =
  | 'open'
  | 'running'
  | 'acked'
  | 'answered'
  | 'spawned'
  | 'resolved'
  | 'expired'
  | 'done'
  | 'failed';

export interface PentacleNotificationAction {
  kind: NotificationActionKind;
  // B1 (Spec-QA Round 1): each action carries a stable, required identifier so a
  // multi-button notification can resolve the EXACT action clicked. The daemon
  // selects by `action_id`; legacy single-action notifications that omit it fall
  // back to first-of-kind. `agent-orch notify` auto-assigns ids when a publisher
  // gives buttons without them.
  action_id: string;
  // Optional, publisher-supplied. `label` overrides the per-kind default button
  // text. `command_id`/`host`/`args`/`timeout_seconds` describe a run_command
  // action's registry-bound invocation (host selects WHERE a registry command
  // runs; never a free-form shell fragment).
  label?: string;
  command_id?: string;
  host?: string;
  args?: string[];
  timeout_seconds?: number;
  [key: string]: unknown;
}

export interface PentacleNotificationResolution {
  by: string;
  at: string;
  action_kind: NotificationActionKind;
  action_id?: string;
  label?: string;
  choice?: boolean;
  value?: unknown;
  selections?: unknown[];
  note?: string | null;
  spawned_stream_id?: string;
  // run_command outcome (mirrors the daemon's resolution.result shape).
  result?: {
    command_id?: string;
    exit_code?: number | null;
    stdout_tail?: string;
    stderr_tail?: string;
    timed_out?: boolean;
    ran_at?: string;
  };
}

export type AgentQuestionResponseMode = 'single_choice' | 'multi_choice' | 'ack' | 'free_text';

export interface PentacleAgentQuestionOption {
  label: string;
  value: string;
}

export interface PentacleAgentQuestionPayload {
  question_id: string;
  producer_stream_id: string;
  response_mode: AgentQuestionResponseMode;
  options: PentacleAgentQuestionOption[];
  state: 'open' | 'answered' | 'expired' | string;
  answer: Record<string, unknown> | null;
}

export interface PentacleNotification {
  notification_id: string;
  created_at: string;
  updated_at: string;
  producer: string;
  answer_to_stream_id?: string | null;
  severity: NotificationSeverity;
  title: string;
  body: string;
  dedup_key: string;
  state: NotificationState;
  actions: PentacleNotificationAction[];
  question?: PentacleAgentQuestionPayload | null;
  resolution: PentacleNotificationResolution | null;
  ttl_seconds: number;
  expires_at: string;
  resolved_at: string | null;
}

export type WorkingTokensPhase = 'down' | 'up' | 'idle';

export interface WorkingTaskData {
  id: string;
  subject: string;
  status: 'in_progress' | 'pending' | string;
  blocked_by: string[];
}

export interface WorkingTaskSummary {
  total: number;
  done: number;
  in_progress: number;
  open: number;
}

export interface WorkingStateData {
  stream_id: string;
  timestamp: string;
  tokens_input: number;
  tokens_output: number;
  tokens_cache_read: number;
  tokens_cache_creation: number;
  tokens_phase: WorkingTokensPhase;
  shell_count_started: number;
  tasks: WorkingTaskData[];
  task_summary: WorkingTaskSummary;
  elapsed_ms: number;
}

export type TurnPhase = 'idle' | 'pending' | 'working';

export type EndReason =
  | 'working_false'
  | 'turn_summary'
  | 'terminal_divider'
  | 'provider_done';

export interface TurnState {
  phase: TurnPhase;
  optimisticId?: string;
  sentAt?: number;
  firstServerEventAt?: number;
  lastServerEventKey?: string;
  endedAt?: number;
  endReason?: EndReason;
}

export type OptimisticSendStatus =
  | 'queued'
  | 'dispatched'
  | 'acked'
  | 'echoed'
  | 'indeterminate'
  | 'failed'
  // B3 (chat_send_turn_lifecycle_batch2): an in-flight send the user cancelled
  // (ESC / cancel control, or an external cancel). Distinct from 'failed' so the
  // UI can show a "cancelled" affordance rather than a send error.
  | 'cancelled'
  | 'returned_to_prompt'
  | 'reconciled';

// B1 (chat_send_turn_lifecycle_batch2): the user-visible lifecycle state of an
// optimistic (client-origin) message row, derived from its OptimisticSendState.
// 'queued' while held behind an in-flight turn (B4, turn_queued), 'sending'
// while a dispatched send is unconfirmed (queued/dispatched/indeterminate),
// undefined once daemon-confirmed (acked/echoed/reconciled → normal bubble),
// 'failed'/'cancelled' on a terminal non-delivery. Surfaced onto the transcript
// item so every view (desktop + mobile) can render the affordance consistently.
export type PentacleSendState = 'queued' | 'sending' | 'failed' | 'cancelled';

export interface OptimisticSendState {
  optimistic_id: string;
  request_id: string;
  stream_id: string;
  text: string;
  status: OptimisticSendStatus;
  created_at: number;
  dispatched_at?: number;
  acked_at?: number;
  echoed_at?: number;
  indeterminate_at?: number;
  failed_at?: number;
  returned_at?: number;
  window_started_at?: number | null;
  socket_generation?: number;
  reconnect_count: number;
  failure_reason?: string;
  // B4 (chat_send_turn_lifecycle_batch2): true while this send is HELD behind an
  // in-flight turn (typed/sent by the user but not yet dispatched). It is not
  // gating a turn and has not been put on the wire; it shows as a 'queued' row
  // and is activated (turn_queued cleared, turn begun, dispatched) when the
  // stream returns to idle. Absent/false for an ordinary in-flight send.
  turn_queued?: boolean;
  queued_at?: number;
  // Image attachments the user sent with this message (spec ## Attachment
  // model). Populated on the optimistic send so the queued/sending bubble can
  // render thumbnails before the daemon echoes the message back. FIFO order.
  attachments?: ChatAttachment[];
}

export type EventBucketRequestStatus = 'idle' | 'loading' | 'prefetching' | 'ready' | 'error';

export type EventBucketRequestPurpose =
  | 'mount-fetch'
  | 'prefetch'
  | 'freshness-guard'
  | 'focused-refetch'
  | 'older-page'
  | 'manual';

export type EventBucketRequestWindow = 'history' | 'current-tail' | 'older-page';

export interface EventBucketRequestState {
  /** Live token fields are present only while this exact window may mutate the bucket. */
  token?: string;
  purpose?: EventBucketRequestPurpose;
  window?: EventBucketRequestWindow;
  generation?: number;
  limit?: number;
  before?: number | null;
  status: EventBucketRequestStatus;
}

export interface EventBucketCoverage {
  /** Coverage is reusable only for this generation/window/before cursor and a sufficient requestLimit. */
  window?: EventBucketRequestWindow;
  purpose?: EventBucketRequestPurpose;
  generation: number;
  requestLimit: number;
  before?: number | null;
  completedAt?: number;
  complete: boolean;
  cursor?: number | null;
  authoritativeZero: boolean;
  freshUntil?: number;
}

export interface EventBucketPins {
  focused?: boolean;
  optimistic?: boolean;
  inFlight?: boolean;
}

/** Canonical, serializable transcript state for one stream. */
export interface PentacleEventBucket {
  events: PentacleEvent[];
  contentVersion: number;
  latestEventAt?: string;
  mutationRevision: number;
  lastAccessRevision: number;
  retainedCost: number;
  pins: EventBucketPins;
  explicitPins?: EventBucketPins;
  /** Deprecated history-window alias retained for incremental consumers. */
  coverage?: EventBucketCoverage;
  /** Authoritative window evidence; older-page evidence never satisfies tail/history reuse. */
  coverageByWindow?: Partial<Record<EventBucketRequestWindow, EventBucketCoverage>>;
  /** Deprecated history-window alias retained for incremental consumers. */
  request?: EventBucketRequestState;
  /** Serializable request fences; Promise ownership remains in the mobile service. */
  requestsByWindow?: Partial<Record<EventBucketRequestWindow, EventBucketRequestState>>;
}

export interface PentacleStreamState {
  connected: boolean;
  connecting: boolean;
  hasHydrated?: boolean;
  lastError?: string;
  events: PentacleEvent[];
  // Canonical transcript truth. `events` remains a derived read-only
  // compatibility projection while legacy fixtures and broad consumers move
  // incrementally to bucket/index accessors.
  eventBucketsByStream?: Record<string, PentacleEventBucket>;
  eventBucketMutationRevision?: number;
  drafts: Record<string, PentacleEvent>;
  hosts: Record<string, PentacleHostStatus>;
  machineStats: Record<string, PentacleMachineStats>;
  sessions: PentacleSessionSummary[];
  specStatuses?: PentacleSpecStatusCapability[];
  limits?: PentacleLimit[];
  limitsHealth?: PentacleLimitsHealth | null;
  updates: PentacleUpdateMessage[];
  notifications: PentacleNotification[];
  workingStates?: Record<string, WorkingStateData>;
  workingByStream?: Record<string, TurnState>;
  optimisticSends?: Record<string, OptimisticSendState>;
  optimisticByRequestId?: Record<string, string>;
  // Stage 5c — per-stream content-version counter. Monotonically increases for
  // a stream on every event append, progressive-update replacement, and
  // snapshot/inventory replacement that changes the stream's content set. Used
  // by `selectSessionDetail`'s cache to force a new selector output even when
  // the events array identity matches. Entry is removed when the stream is
  // closed or no longer present in the latest session inventory.
  eventContentVersionByStream?: Record<string, number>;
}
