/* global Terminal, FitAddon */

// Load xterm.js and fit addon via require (Electron renderer with nodeIntegration off — use dynamic import)
const { Terminal } = require('@xterm/xterm');
const { FitAddon } = require('@xterm/addon-fit');
const { Unicode11Addon } = require('@xterm/addon-unicode11'); // REQUIRED: without this, ❯ and other unicode renders as __
const { WebglAddon } = require('@xterm/addon-webgl'); // GPU-accelerated rendering — fixes partial text paint on screen refresh
const { createTerminalPaste } = require('./terminal_paste');
const { normalizeSplit, createGridColResizer } = require('./grid_col_resizer');
const path = require('path');
const chatUi = require('./chat_ui_state');
const assetRender = require('./asset_render');
const { showToast } = require('./toast');
const { decideUseChatClose } = require('./delete_session_gate');
const { configuredAssistantRole, isConfiguredAssistant } = require('./assistant_role');
const { createClosedChatSlots } = require('./closed_chat_slots');
const { applyVersionedConnectionState } = require('./chat_stream_connection_state');
const { resolveMicUrl } = require('../main/mic-url');
const { createSpawnCatalogLoader } = require('./spawn_catalog_loader');
const {
  computeBusyBannerState,
  shouldRenderAlwaysOnUi,
  shouldShowAlwaysOn,
} = require('./mic-state');
const { createRemoteClipboardPoller } = require('./remote-clipboard-poll');
const { createWakeDelivery } = require('./wake_delivery');
const {
  filterSidebarSessions,
  collectSourceFilterHostIds,
  nextChatStreamSessions,
  offlineHostStatus,
  projectChatStreamSessionsToDesktop,
} = require('./sidebar_filter');
const { orderSidebarRows } = require('./sidebar_attention');
const {
  isReportUnread,
  reportUnreadCount,
  reportUnreadKeys,
  markReportRead,
} = require('./report_read_state');
const {
  ensureChatEventsLoaded: _ensureChatEventsLoaded,
  refetchEventsForActiveChatSlots: _refetchEventsForActiveChatSlots,
} = require('./chat_events_lazy');
const {
  answerConstraint,
  questionItems,
  renderQuestionOptionB,
} = require('./question_option_b');
const {
  // Schedule protocol ingestion only. The desktop schedule window plane
  // (sidebar rows + detail panel) was retired; the daemon/CLI schedule protocol
  // and this client-side inventory/event ingestion are preserved so schedules
  // remain available to any future independent consumer.
  SCHEDULE_EVENT_TYPES,
  replaceSchedulesFromInventory,
  applyScheduleEvent,
} = require('./schedule_ui_state');
const loadedConfig = require('../config-loader').loadConfig(path.join(__dirname, '..'));
const CONFIG = loadedConfig.config;
const hostPresentation = require('./host_presentation');
const limitsContract = require('../main/limits_contract');
const CHAT_POPOUT_CONTEXT = window.cc?.chatPopoutContext?.() || null;
const IS_CHAT_POPOUT = !!CHAT_POPOUT_CONTEXT;
let chatPopoutBound = false;
CONFIG.features = CONFIG.features || {};

// ── Settings (user feature-flag overrides) ─────────────────────
// Pentacle's feature flags ship as defaults in the resolved config. The
// Settings panel (gear in the titlebar) lets the user override them per
// machine; overrides persist in localStorage and are merged over the config
// defaults at startup, before anything reads CONFIG.features. UI-only flags
// apply live; backend-dependent flags need a window reload (the panel surfaces
// a "Reload to apply" badge).
const SETTINGS_STORAGE_KEY = 'pentacle.settings.v1';
const DEFAULT_APPEARANCE = { theme: 'dark', density: 'comfortable' };
const DESIGN_THEME_VARS = {
  dark: {
    // Green palette (pre-reskin greens restored): light-green --blue + old green
    // backgrounds; reskin structure/mint --pc-accent otherwise unchanged.
    bg: '#0c1310', bg2: '#121e18', bg3: '#1a2b22', fg: '#e7f1eb', fgDim: '#5f7368',
    blue: '#3fb950', green: '#56d364', red: '#f47067', yellow: '#e8c37b',
    purple: '#4ea67e', cyan: '#6fb3ff', border: '#1b2a22',
    app: '#0c1310', side: '#121e18', pane: '#0c1310', header: '#121e18',
    text: '#e7f1eb', dim: '#8fa89d', soft: '#5f7368', faint: '#43554c',
    line: '#1b2a22', lineSoft: '#14201a', accent: '#7ef0ba', accentDim: '#4ea67e',
    codeBg: '#0b1611', codeLine: '#1e2f26', chipBg: '#1a2b22', chipTx: '#8bf0c0',
    userBg: '#16352880', userLine: '#2e5947',
  },
  light: {
    bg: '#c8d0c6', bg2: '#dbe1d5', bg3: '#c6d3c1', fg: '#18231b', fgDim: '#6c7a70',
    blue: '#1c6b45', green: '#1f7a3e', red: '#b23b32', yellow: '#876717',
    purple: '#357a58', cyan: '#2f6db0', border: '#c2ccbc',
    app: '#d7ddd1', side: '#dbe1d5', pane: '#e5eae0', header: '#dce2d6',
    text: '#18231b', dim: '#4f5d53', soft: '#6c7a70', faint: '#8b988d',
    line: '#c2ccbc', lineSoft: '#cfd8c9', accent: '#1c6b45', accentDim: '#357a58',
    codeBg: '#cfd8ca', codeLine: '#b1bdab', chipBg: '#c6d3c1', chipTx: '#1b5c3c',
    userBg: '#c6d5c7', userLine: '#9cb29c',
  },
};

// Each flag: key (CONFIG.features.<key>), label, description, and `live`
// (true = appliable without a reload). `live:false` flags wire up backends or
// deep slot state at startup, so changing them only takes full effect after a
// reload.
const SETTINGS_FLAGS = [
  { key: 'chatUi',     label: 'Chat UI (experimental)', desc: 'Opt-in structured chat view. Not production-ready; terminals are the default.', live: false },
  { key: 'dashboards', label: 'Dashboards / Widgets', desc: 'The Dashboards view and its widget panels.',                    live: true  },
  { key: 'mic',        label: 'Microphone',          desc: 'Mic panel and per-slot voice record. Requires the mic server.', live: false },
  { key: 'usage',      label: 'Usage bars',          desc: 'Provider usage meters in the sidebar. Requires chat_streamd.',  live: false },
  { key: 'sourceTags', label: 'Source tags',         desc: 'Show the source host tag on each session.',                     live: false },
  { key: 'showTurnDuration', label: 'Show turn duration', desc: 'Show worked-for dividers and timing annotations in chat.', live: true },
];

function loadSettingsRecord() {
  try {
    const parsed = JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) || '{}');
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch { return {}; }
}

function saveSettingsRecord(parsed) {
  try { localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(parsed || {})); } catch { /* storage full / unavailable */ }
}

function loadSettingsOverrides() {
  const parsed = loadSettingsRecord();
  return (parsed && typeof parsed.features === 'object' && parsed.features) || {};
}

function saveSettingsOverride(key, value) {
  const parsed = loadSettingsRecord();
  if (!parsed.features || typeof parsed.features !== 'object') parsed.features = {};
  parsed.features[key] = value;
  saveSettingsRecord(parsed);
}

function normalizeAppearance(raw = {}) {
  const theme = raw.theme === 'light' ? 'light' : 'dark';
  const density = raw.density === 'compact' ? 'compact' : 'comfortable';
  return { theme, density, ...(raw.gridColSplit !== undefined ? { gridColSplit: normalizeSplit(raw.gridColSplit) } : {}) };
}

function loadAppearanceSettings() {
  const parsed = loadSettingsRecord();
  return normalizeAppearance(parsed.appearance || {});
}

function saveAppearanceSetting(key, value) {
  const parsed = loadSettingsRecord();
  if (!parsed.appearance || typeof parsed.appearance !== 'object') parsed.appearance = {};
  parsed.appearance[key] = value;
  saveSettingsRecord(parsed);
}

function showTurnDurationEnabled() {
  return CONFIG.features.showTurnDuration === true;
}

function assistantRole() {
  return configuredAssistantRole(CONFIG.features);
}

function isProtectedAssistantSession(session) {
  return isConfiguredAssistant(session, assistantRole());
}

const ACTIVITY_SPINNER_PERIOD_MS = 1050;

function syncActivitySpinnerPhase(root = document) {
  const phaseMs = Date.now() % ACTIVITY_SPINNER_PERIOD_MS || ACTIVITY_SPINNER_PERIOD_MS;
  document.documentElement.style.setProperty('--activity-spinner-period-ms', String(ACTIVITY_SPINNER_PERIOD_MS));
  const scope = root && typeof root.querySelectorAll === 'function' ? root : document;
  const spinners = [
    ...(typeof scope.matches === 'function' && scope.matches('.activity-spinner') ? [scope] : []),
    ...scope.querySelectorAll('.activity-spinner'),
  ];
  spinners.forEach((spinner) => {
    if (spinner.style.animationDelay) return;
    spinner.style.animationDelay = `-${phaseMs}ms`;
  });
}

const themeVarMap = {
  bg: '--bg', bg2: '--bg2', bg3: '--bg3', fg: '--fg', fgDim: '--fg-dim',
  blue: '--blue', green: '--green', red: '--red', yellow: '--yellow',
  purple: '--purple', cyan: '--cyan', border: '--border',
  app: '--pc-app', side: '--pc-side', pane: '--pc-pane', header: '--pc-header',
  text: '--pc-text', dim: '--pc-dim', soft: '--pc-soft', faint: '--pc-faint',
  line: '--pc-line', lineSoft: '--pc-line-soft', accent: '--pc-accent',
  accentDim: '--pc-accent-dim', codeBg: '--pc-code-bg', codeLine: '--pc-code-line',
  chipBg: '--pc-chip-bg', chipTx: '--pc-chip-tx', userBg: '--pc-user-bg',
  userLine: '--pc-user-line',
};

function applyAppearanceSettings({ emit = false } = {}) {
  state.appearance = normalizeAppearance(state.appearance || loadAppearanceSettings());
  const palette = DESIGN_THEME_VARS[state.appearance.theme] || DESIGN_THEME_VARS.dark;
  document.documentElement.dataset.theme = state.appearance.theme;
  document.documentElement.dataset.density = state.appearance.density;
  for (const [key, cssVar] of Object.entries(themeVarMap)) {
    if (palette[key]) document.documentElement.style.setProperty(cssVar, palette[key]);
  }
  if (window.Terminal && state.terminals) {
    for (const entry of state.terminals) {
      const term = entry?.term;
      if (term && typeof term.options === 'object') term.options.theme = terminalThemeForAppearance();
    }
  }
  for (let slot = 0; slot < state.slots.length; slot += 1) {
    if (state.slotViewModes[slot] === 'chat') {
      state.slotChatLastListHtml[slot] = null;
      renderSlotChat(slot);
    }
  }
  renderSidebar();
  window.PentacleHarness?.emit?.('settings:appearance', { data: { ...state.appearance } });
  if (emit) window.PentacleHarness?.emit?.('settings:toggle', { data: { key: 'appearance', value: { ...state.appearance } } });
}

function terminalThemeForAppearance() {
  const palette = DESIGN_THEME_VARS[state.appearance?.theme || 'dark'] || DESIGN_THEME_VARS.dark;
  return {
    ...(THEME || {}),
    background: palette.pane,
    foreground: palette.text,
    cursor: palette.accent,
    selectionBackground: palette.chipBg,
  };
}

function isTimingSystemTranscriptItem(item) {
  const rule = String(item?.displayRule || '');
  return rule === 'activity:turn-summary' || rule === 'terminal:divider';
}

// `emitRenderTelemetry` (render-telemetry QA layer, defect 1) gates the shared
// selector's per-row `chat:event_rendered` beacon. It defaults FALSE so that the
// terminal-view per-frame call (invoked before the `chatMode` gate in
// renderSlotChat) and any non-render reader do NOT emit — only the real
// chat-render path (post-`chatMode`) passes true, which keeps the beacon from
// being self-fulfilling for harness probes.
// Per-slot session-reliability view-state (bounded paging window + pinned/unread
// tracking). Lazily created so a slot that never opens chat costs nothing.
function ensureSlotReliability(slot) {
  let rel = state.slotReliability[slot];
  if (!rel && window.PentacleChatReliability) {
    rel = window.PentacleChatReliability.createSlotReliabilityState();
    state.slotReliability[slot] = rel;
  }
  return rel;
}

// Reset a slot's paging window (e.g. on a session change) so a newly-bound
// stream starts at the bounded initial window, not the prior stream's grown one.
function resetSlotReliability(slot) {
  if (window.PentacleChatReliability) {
    state.slotReliability[slot] = window.PentacleChatReliability.createSlotReliabilityState();
  } else {
    state.slotReliability[slot] = null;
  }
  state.slotChatPendingPrepend[slot] = null;
  state.slotChatPrevTotal[slot] = null;
}

function selectSlotSessionDetail(streamId, showTurnDuration, emitRenderTelemetry = false, visibleCount = 120) {
  if (!streamId || !window.PentacleChatStore) return null;
  const detail = window.PentacleChatStore.selectSessionDetail(streamId, {
    visibleCount,
    includeDraft: false,
    includeSystem: showTurnDuration === true,
    systemRows: showTurnDuration === true ? 'timing-only' : undefined,
    emitRenderTelemetry: emitRenderTelemetry === true,
  });
  if (!detail || showTurnDuration !== true) return detail;
  const items = Array.isArray(detail.transcriptItems) ? detail.transcriptItems : [];
  const filteredItems = items.filter((item) => item?.tone !== 'system' || isTimingSystemTranscriptItem(item));
  return filteredItems.length === items.length
    ? detail
    : { ...detail, transcriptItems: filteredItems };
}

// ── chat:slot_painted content digest — schema "slot_painted.contentDigest@1" ──
// A bounded, content-bearing summary of the transcript ACTUALLY painted into a
// slot, attached to every `chat:slot_painted` beacon so a walk can assert WHAT
// rendered at the true DOM-commit point — not merely that stream ids were
// committed (render-telemetry QA layer, defect 2; audit §4.3 option (a)).
// Stable shape (do not rename keys without a schema bump):
//   {
//     rowCount: number,                       // transcript rows painted
//     kinds:    { [kind: string]: number },   // histogram of row.kind
//     lastRowDigest: null | {                 // the final (newest) painted row
//       kind:        string,                  // e.g. 'ASSIST_TEXT', 'USER'
//       displayRule: string,                  // e.g. 'bubble:assistant'
//       text_prefix: string,                  // first 40 chars of the row text
//     },
//   }
// Bounded by design: only the LAST row carries text, capped at 40 chars, so the
// payload cannot grow with transcript length on high-volume paint paths.
// Match rule for "the agent reply painted": a `chat:slot_painted` whose
// `lastRowDigest.kind === 'ASSIST_TEXT'` that was not present at the pre-send
// baseline (see test/e2e/lib/flows.js sendOneTurn).
function computeSlotChatContentDigest(items) {
  const rows = Array.isArray(items) ? items : [];
  const kinds = {};
  for (const it of rows) {
    const kind = String(it?.kind || '');
    kinds[kind] = (kinds[kind] || 0) + 1;
  }
  const last = rows.length ? rows[rows.length - 1] : null;
  const lastRowDigest = last
    ? {
      kind: String(last.kind || ''),
      displayRule: String(last.displayRule || ''),
      text_prefix: String(last.text || '').slice(0, 40),
    }
    : null;
  return { rowCount: rows.length, kinds, lastRowDigest };
}

// Merge persisted overrides over the config feature defaults up front.
(function applySettingsOverrides() {
  const overrides = loadSettingsOverrides();
  for (const { key } of SETTINGS_FLAGS) {
    if (typeof overrides[key] === 'boolean') CONFIG.features[key] = overrides[key];
  }
})();

// Renderer no longer holds an API URL — server.py is gone, all session
// management happens via chat_streamd over IPC (window.cc.list/trash/etc).
// IS_CLIENT still needed for terminal-mode peer enumeration vs host-mode.
let IS_CLIENT = false;
let HOST_IDS = ['local'];
const THEME = CONFIG.terminal;

// Bootstrap async config from main so client/host mode is known before
// network-touching code fires from event handlers or timers.
function _perfRecord(event, details) {
  try { window?.cc?.perfRecord?.(event, details); } catch (_) {}
}
_perfRecord('renderer:module-load');

const CFG_READY = (async () => {
  _perfRecord('renderer:cfg-ready-start');
  try {
    const cfg = await window.cc.getConfig();
    if (cfg) {
      Object.assign(CONFIG, cfg);
      const overrides = loadSettingsOverrides();
      for (const { key } of SETTINGS_FLAGS) {
        if (typeof overrides[key] === 'boolean') CONFIG.features[key] = overrides[key];
      }
    }
    IS_CLIENT = !!(cfg && cfg.isClient);
    HOST_IDS = Array.isArray(cfg && cfg.hostIds) && cfg.hostIds.length ? cfg.hostIds : ['local'];
    // Clients default to creating remote sessions (the mac-mini). Users can
    // still toggle to local for WSL/macbook-local sessions.
    if (HOST_IDS.includes('remote')) newSessionLocation = 'remote';
    renderTitlebarMachines();
    renderConfigWarnings();
    _perfRecord('renderer:cfg-ready-end');
    return cfg;
  } catch { _perfRecord('renderer:cfg-ready-error'); return null; }
})();

// ── State ──────────────────────────────────────────────────────

const state = {
  // chat_streamd live-state availability. Sidebar rows remain daemon-owned;
  // this flag only gates actions that require a connected daemon.
  degraded: false,
  slots: [null, null, null, null], // { name, displayName } or null
  terminals: [null, null, null, null], // { term, fitAddon } or null
  slotViewModes: ['terminal', 'terminal', 'terminal', 'terminal'], // 'chat' | 'terminal' | 'asset' | 'status'
  // Sidebar rows with a status card can expand to a compact status summary
  // (spec item 3). streamId -> expanded bool; survives re-renders.
  sidebarExpanded: {},
  slotBuffers: ['', '', '', ''], // rolling PTY text buffer per slot
  slotChatRefs: [null, null, null, null], // { shell, terminalMount, chatMount, scrollEl, listEl, questionEl, inputEl, sendEl, hintEl } per slot
  slotStatusCardOpen: [false, false, false, false], // per-slot Session Status card-view toggle (spec_pentacle__status_card_ui_desktop)
  slotStatusCardOverlaid: [false, false, false, false], // whether the card overlay currently hides the chat body (for exact restore)
  slotActiveAsset: [null, null, null, null],
  slotAssetRenderSignature: [null, null, null, null],
  // Monotonic guards for the async asset-view pipeline: a full render bumps
  // both (superseding in-flight renders AND pending in-place comment applies);
  // a comment refetch bumps only the comments gen. Every async continuation
  // re-checks its captured gen so stale results are dropped instead of applied
  // out of order (transport ordering is not assumed).
  slotAssetRenderGen: [0, 0, 0, 0],
  slotAssetCommentsGen: [0, 0, 0, 0],
  slotAssetLabelTimers: [null, null, null, null],
  slotDrafts: ['', '', '', ''], // chat composer draft per slot
  slotDraftTouched: [false, false, false, false], // whether the user has taken ownership of the chat bar text
  returnedPromptDrafts: {}, // `${streamId}:${optimisticId}` -> restored/skipped
  slotAttachments: [[], [], [], []], // pending image attachments per chat composer slot
  chatBlobObjectUrls: {}, // blob sha -> object URL for fetched transcript attachments
  slotSendPending: [false, false, false, false],
  slotSendErrors: ['', '', '', ''],
  slotChatRenderTimers: [null, null, null, null],
  slotChatLastListHtml: [null, null, null, null],
  slotChatPendingListRender: [null, null, null, null],
  questionDrafts: {}, // streamId -> { sig, answers: { [questionIndex]: { optionIndex?, text? } } }
  questionPageIndexByStream: {},
  durableQuestionNotificationsById: {}, // notification_id -> open agent_question.v1 notification
  durableQuestionHydrationByStream: {}, // streamId -> in-flight/done scoped prompt.list hydration
  // Bug1 (chat_ui_hardening_batch3) new-chat flash guard: the desktop session
  // key + the stream id LAST PAINTED for each slot. On a session change these
  // hold the PREVIOUS binding (intentionally NOT reset by attachSession) so
  // renderSlotChat can suppress a transient id-only fallback that resolves the
  // newly-bound session to the stream the slot was just showing.
  slotChatBoundSession: [null, null, null, null], // `${hostId}:${name}` last painted per slot
  slotChatBoundStream: [null, null, null, null], // stream id last painted per slot
  // Session-reliability view-state per slot (bounded history paging window,
  // pinned/unread tracking) — see renderer/src/chat_reliability_view.ts
  // (spec_pentacle__desktop_chat_session_reliability_parity_2026_07_29).
  slotReliability: [null, null, null, null],
  slotChatPendingPrepend: [null, null, null, null], // {prevScrollHeight, prevScrollTop} captured before a load-earlier render
  slotChatPrevTotal: [null, null, null, null], // last render's total renderable row count per slot (drives unpinned window growth)
  wheelThrottles: [0, 0, 0, 0], // last scroll timestamp per slot for throttling
  botSlots: [false, false, false, false], // true if slot has a bot detail panel (not a terminal)
  workingSince: {}, // slot -> epoch ms when this slot's current working turn began (drives the live status timer; legacy fallback for B2)
  workingSlotStream: {}, // slot -> streamId of the working session, so the live timer can query the daemon-authoritative elapsed (B2)
  workingTimer: null, // shared 1s interval handle; armed only while >=1 slot is working
  slotGen: [0, 0, 0, 0], // generation counter per slot — incremented on each attach to detect stale PTY exits
  maximizedSlot: null, // null = grid view, 0-3 = maximized slot
  sourceFilter: null, // null = all, or hostId string to filter by
  sessionSearch: '',
  // Dashboard state — all mutable dashboard state lives here
  currentView: 'chats',            // 'chats' | 'dashboards'
  selectedDashboard: null,          // dashboard id string
  dashboardPollToken: 0,            // generation counter
  dashboardPollTimer: null,         // setInterval id
  dashboardPollNow: null,           // active dashboard's immediate poll function
  dashboardRefs: null,              // cached DOM refs from mount()
  dashboardLastData: null,          // last successful poll data
  dashboardLastUpdated: null,       // Date of last successful poll
  dashboardState: 'loading',        // 'loading' | 'loaded' | 'stale' | 'error'
  dashboardError: null,             // error message string
  chatStream: {
    connected: false,
    error: null,
    stateVersion: -1,
    events: [],
    drafts: {},
    sessions: [],
    spawnFailureNotifications: new Set(),
    schedules: [],
    assets: {},
    hostsStats: {},
    // Streams whose recent events have been backfilled via
    // window.cc.requestStreamEvents. The hello snapshot carries no events
    // (events_mode: 'summary'); chat-view slots lazy-fetch their stream's
    // ring on first render and again after WS reconnect.
    eventsLoadedFor: new Set(),
  },
  sidebarSections: {
    usage: true,
    machineStats: true,
    mic: true,
  },
  appearance: loadAppearanceSettings(),
  // Optimistic-close overlay: stream ids the operator just closed, hidden from
  // the sidebar until the next daemon inventory frame reconciles them away.
  // Keeps state.chatStream.sessions single-frame-fed (see desktop_single_roster).
  locallyClosedStreamIds: new Set(),
};

Object.defineProperty(state, 'sessions', {
  enumerable: true,
  get: () => projectChatStreamSessionsToDesktop(state.chatStream.sessions, _streamHostToHostId),
});

const SLOT_BUFFER_LIMIT = 120000;
const CHAT_STREAM_LIMIT = CONFIG.chatStream?.recentLimit || 5000;

function chatUiEnabled() {
  return CONFIG.features?.chatUi === true;
}

function notificationRecordFromPayload(payload) {
  if (!payload || typeof payload !== 'object') return null;
  if (payload.notification && typeof payload.notification === 'object') return payload.notification;
  return payload.producer || payload.question ? payload : null;
}

function durableQuestionNotificationId(notification) {
  return String(notification?.notification_id || notification?.id || '').trim();
}

function durableQuestionStreamId(notification) {
  return String(
    notification?.answer_to_stream_id
    || notification?.question?.producer_stream_id
    || ''
  ).trim();
}

function sessionSummaryStreamId(summary) {
  return String(
    summary?.stream_id
    || (summary?.host && (summary?.session_name || summary?.name) ? `${summary.host}:${summary.session_name || summary.name}` : '')
    || ''
  ).trim();
}

// Prune the optimistic-close overlay: once a daemon inventory frame no longer
// lists a locally-closed stream, the authoritative removal has landed so the
// overlay entry is dropped. Does not write state.chatStream.sessions — the
// roster keeps its single frame-fed source (see desktop_single_roster_static).
function reconcileLocallyClosedStreamIds() {
  if (!state.locallyClosedStreamIds.size) return;
  const present = new Set((state.chatStream.sessions || []).map(sessionSummaryStreamId));
  for (const id of state.locallyClosedStreamIds) {
    if (!present.has(id)) state.locallyClosedStreamIds.delete(id);
  }
}

function effectiveRendererVisibility(summary) {
  return String(summary?.visibility || 'default');
}

function nearestVisibleAncestorStreamId(streamId) {
  const target = String(streamId || '').trim();
  if (!target) return '';
  const sessions = Array.isArray(state.chatStream.sessions) ? state.chatStream.sessions : [];
  const byStreamId = new Map();
  for (const summary of sessions) {
    const sid = sessionSummaryStreamId(summary);
    if (sid) byStreamId.set(sid, summary);
  }
  let current = target;
  const seen = new Set();
  for (let depth = 0; current && depth < 64 && !seen.has(current); depth += 1) {
    seen.add(current);
    const summary = byStreamId.get(current);
    if (!summary) return '';
    if (effectiveRendererVisibility(summary) === 'default') return current;
    current = String(summary.parent_stream_id || '').trim();
  }
  return '';
}

function durableQuestionSurfacesInStream(notification, streamId) {
  const wanted = String(streamId || '').trim();
  const questionStreamId = durableQuestionStreamId(notification);
  if (!wanted || !questionStreamId) return false;
  if (questionStreamId === wanted) return true;
  const nearestVisibleAncestor = nearestVisibleAncestorStreamId(questionStreamId);
  if (nearestVisibleAncestor) return nearestVisibleAncestor === wanted;
  return String(notification?.surfaced_to_stream_id || '').trim() === wanted;
}

function isOpenDurableQuestionNotification(notification) {
  const question = notification?.question;
  if (!notification || notification.producer !== 'agent_question.v1' || !question) return false;
  return String(notification.state || 'open') === 'open' && String(question.state || 'open') === 'open';
}

function indexDurableQuestionNotification(notification) {
  const id = durableQuestionNotificationId(notification);
  if (!id) return false;
  const previous = state.durableQuestionNotificationsById[id];
  const merged = { ...previous, ...notification, question: { ...previous?.question, ...notification.question } };
  if (merged.producer !== 'agent_question.v1' || !durableQuestionStreamId(merged)) return false;
  // A stale open inventory cannot resurrect an already resolved identity.
  if (previous && !isOpenDurableQuestionNotification(previous) && isOpenDurableQuestionNotification(merged)) return false;
  state.durableQuestionNotificationsById[id] = merged;
  if (!isOpenDurableQuestionNotification(merged)) {
    for (const uncertain of Object.values(state.questionUncertainIdsByStream || {})) uncertain.delete(id);
  }
  return true;
}

function getOpenQuestionsForStream(streamId) {
  const wanted = String(streamId || '').trim();
  if (!wanted) return [];
  return Object.values(state.durableQuestionNotificationsById || {})
    .filter(notification => isOpenDurableQuestionNotification(notification) && durableQuestionSurfacesInStream(notification, wanted))
    .sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')) || durableQuestionNotificationId(a).localeCompare(durableQuestionNotificationId(b)));
}

function resolvedQuestionsForStream(streamId) {
  return Object.values(state.durableQuestionNotificationsById || {})
    .filter(notification => !isOpenDurableQuestionNotification(notification) && durableQuestionSurfacesInStream(notification, streamId));
}

// unreadReportCountForStream — number of report assets in this stream the
// operator has not yet opened (spec_pentacle__desktop_chat_navigation_status_parity
// scope items 2 & 5). Feeds the sidebar row's unread-report badge and the
// attention comparator's per-row primary-action precedence. Backed by the
// report-discovery read-state tracker (see unreadReportCountForStreamImpl); the
// sidebar consumes it without knowing how read-state is stored.
function unreadReportCountForStream(streamId) {
  const wanted = String(streamId || '').trim();
  if (!wanted) return 0;
  if (typeof unreadReportCountForStreamImpl === 'function') {
    return unreadReportCountForStreamImpl(wanted);
  }
  return 0;
}

// unreadReportCountForStreamImpl backs unreadReportCountForStream once the
// report-discovery read-state tracker is loaded. Counts non-dismissed unread
// report assets in the stream's bucket (report_read_state.js owns the rule).
function unreadReportCountForStreamImpl(streamId) {
  const bucket = state.chatStream.assets[streamId];
  if (!bucket) return 0;
  return reportUnreadCount(
    bucket,
    (key) => !!(bucket.dismissedById || {})[assetTabKey(streamId, key)],
  );
}

// refreshSidebarUnread re-renders the sidebar when report unread state may have
// changed (asset arrival/republish/removal/dismiss/open). Guarded so early
// asset frames before the sidebar mounts are safe no-ops.
function refreshSidebarUnread() {
  if (document.getElementById('session-list')) renderSidebar();
}

function notificationFromAgentQuestion(question, surfacedToStreamId = '') {
  if (!question || typeof question !== 'object') return null;
  const envelope = question.envelope && typeof question.envelope === 'object' ? question.envelope : {};
  const notificationId = String(question.notification_id || '').trim();
  const producerStreamId = String(question.producer_stream_id || envelope.producer_stream_id || '').trim();
  if (!notificationId || !producerStreamId) return null;
  return {
    notification_id: notificationId,
    producer: 'agent_question.v1',
    state: question.state || 'open',
    title: envelope.title || 'Question',
    body: envelope.body || '',
    created_at: question.created_at || envelope.created_at || '',
    resolved_at: question.resolved_at || question.answered_at || question.answer?.at || '',
    answer_to_stream_id: producerStreamId,
    surfaced_to_stream_id: String(surfacedToStreamId || '').trim() || undefined,
    question: {
      ...envelope,
      question_id: question.question_id,
      producer_stream_id: producerStreamId,
      response_mode: envelope.response_mode || 'single_choice',
      options: Array.isArray(envelope.options) ? envelope.options : [],
      state: question.state || 'open',
      answer: question.answer || null,
    },
  };
}

function applyPromptListQuestions(reply, streamId) {
  if (!reply || !Array.isArray(reply.questions)) return;
  const scopedStreamIds = new Set(
    (Array.isArray(reply.producer_stream_ids) ? reply.producer_stream_ids : [])
      .map((value) => String(value || '').trim())
      .filter(Boolean)
  );
  const returnedIds = new Set();
  const surfacedToStreamId = String(reply.surfaced_to_stream_id || '').trim();
  for (const question of reply.questions) {
    const notification = notificationFromAgentQuestion(question, surfacedToStreamId);
    if (!notification) continue;
    returnedIds.add(durableQuestionNotificationId(notification));
    indexDurableQuestionNotification(notification);
  }
  for (const uncertain of Object.values(state.questionUncertainIdsByStream || {})) {
    for (const id of returnedIds) uncertain.delete(id);
  }
  if (scopedStreamIds.size > 0) {
    for (const [notificationId, notification] of Object.entries(state.durableQuestionNotificationsById || {})) {
      if (returnedIds.has(notificationId)) continue;
      const producerStreamId = durableQuestionStreamId(notification);
      if (producerStreamId && scopedStreamIds.has(producerStreamId)) {
        delete state.durableQuestionNotificationsById[notificationId];
      }
    }
  }
}

function ensureDurableQuestionsHydrated(streamId) {
  const wanted = String(streamId || '').trim();
  if (!wanted || !window.cc || typeof window.cc.promptList !== 'function') return;
  if (state.durableQuestionHydrationByStream[wanted]) return;
  state.durableQuestionHydrationByStream[wanted] = 'loading';
  window.cc.promptList({ producer_stream_id: wanted, open: false })
    .then((reply) => {
      applyPromptListQuestions(reply, wanted);
      state.durableQuestionHydrationByStream[wanted] = 'done';
      scheduleDurableQuestionSlotRenders();
    })
    .catch(() => {
      delete state.durableQuestionHydrationByStream[wanted];
    });
}

function durableQuestionOptionBModel(notification) {
  const question = notification?.question || {};
  let options = Array.isArray(question.options) ? question.options : [];
  if (!options.length && question.response_mode === 'ack') {
    options = [{ label: 'Acknowledge', value: 'ack' }];
  }
  const allowCustom = question.allow_custom === true || question.allowCustom === true;
  return {
    ...question,
    free_text: question.response_mode === 'free_text' || allowCustom,
    _unavailable: Array.isArray(question.questions) && question.questions.length > 0,
    question_key: question.question_id || durableQuestionNotificationId(notification),
    header: notification?.title || 'Question',
    prompt: notification?.body || '',
    multiSelect: question.response_mode === 'multi_choice',
    customText: allowCustom,
    options: options.map((opt, index) => ({
      index: index + 1,
      label: opt?.label || String(opt?.value ?? `Option ${index + 1}`),
      value: Object.prototype.hasOwnProperty.call(opt || {}, 'value')
        ? opt.value
        : (opt?.label || String(index + 1)),
      description: typeof opt?.description === 'string' ? opt.description : undefined,
    })),
  };
}

function questionModelSignature(model) {
  // Hydration metadata (answer/state/timestamps) does not change an open form.
  // Only its identity and answerable content can invalidate a draft.
  return JSON.stringify([
    model.header || '', model.prompt || '', !!model.multiSelect, !!model.customText, model.free_text !== false,
    !!model._unavailable,
    (model.options || []).map(option => [option.index, option.label || '', option.value ?? null, option.description || '', !!option.meta]),
    ...['min_select', 'min_selected', 'minSelections', 'min', 'max_select', 'max_selected', 'maxSelections', 'max'].map(key => model[key] ?? null),
  ]);
}

async function reconcileQuestionAnswers(streamId) {
  if (!window.cc?.promptList) throw new Error('Question status is unavailable.');
  const reply = await window.cc.promptList({ producer_stream_id: streamId, open: false });
  if (!reply || reply.ok === false || !Array.isArray(reply.questions)) throw new Error(reply?.error || 'Question status could not be checked.');
  applyPromptListQuestions(reply, streamId);
  scheduleDurableQuestionSlotRenders();
}

function durableQuestionActionKind(notification) {
  const actions = Array.isArray(notification?.actions) ? notification.actions : [];
  const firstKind = actions.find((action) => action && action.kind)?.kind;
  if (firstKind) return firstKind;
  return notification?.question?.response_mode === 'ack' ? 'ack' : 'yes_no';
}

function clampQuestionPageIndex(index, count) {
  if (window.PentacleChatCore?.clampQuestionPageIndex) return window.PentacleChatCore.clampQuestionPageIndex(index, count);
  if (count <= 0) return 0;
  return Math.min(Math.max(Number.isFinite(Number(index)) ? Math.trunc(Number(index)) : 0, 0), count - 1);
}

function captureQuestionFocus(questionEl) {
  const active = document.activeElement;
  if (!questionEl || !active || !questionEl.contains(active)) return null;
  if (!active.classList?.contains('slot-chat-question-freetext') && !active.classList?.contains('slot-chat-question-note')) {
    return null;
  }
  return {
    className: active.classList.contains('slot-chat-question-note')
      ? 'slot-chat-question-note'
      : 'slot-chat-question-freetext',
    questionIndex: active.dataset.questionIndex || '',
    questionKey: active.dataset.questionKey || '',
    selectionStart: active.selectionStart,
    selectionEnd: active.selectionEnd,
  };
}

function cssAttrValue(value) {
  const raw = String(value || '');
  if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') return CSS.escape(raw);
  return raw.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

function restoreQuestionFocus(questionEl, focus) {
  if (!questionEl || !focus) return;
  const identity = focus.questionKey ? `data-question-key="${cssAttrValue(focus.questionKey)}"` : `data-question-index="${cssAttrValue(focus.questionIndex)}"`;
  const selector = `.${focus.className}[${identity}]`;
  const target = questionEl.querySelector(selector);
  if (!target || typeof target.focus !== 'function') return;
  target.focus({ preventScroll: true });
  if (typeof target.setSelectionRange === 'function'
    && Number.isFinite(focus.selectionStart)
    && Number.isFinite(focus.selectionEnd)) {
    target.setSelectionRange(focus.selectionStart, focus.selectionEnd);
  }
}

function desktopQuestionPortal(streamId, questionEl) {
  const portalId = `desktop-question-portal-${String(streamId || 'active').replace(/[^a-zA-Z0-9_-]/g, '-')}`;
  let portal = document.getElementById(portalId);
  if (!portal) {
    portal = document.createElement('section');
    portal.id = portalId;
    portal.className = 'desktop-question-portal cosmic';
    portal.setAttribute('role', 'dialog');
    portal.setAttribute('aria-modal', 'true');
    portal.innerHTML = '<div class="desktop-question-portal__frame"><header class="desktop-question-portal__header"><span class="desktop-question-portal__eyebrow">QUESTIONS</span><button type="button" class="desktop-question-portal__close">Cancel</button></header><div class="desktop-question-portal__body"></div></div>';
    document.body.appendChild(portal);
  }
  const body = portal.querySelector('.desktop-question-portal__body');
  if (body && questionEl.parentElement !== body) body.appendChild(questionEl);
  return portal;
}

function closeDesktopQuestionPortal(streamId, questionEl) {
  const portalId = `desktop-question-portal-${String(streamId || 'active').replace(/[^a-zA-Z0-9_-]/g, '-')}`;
  const portal = document.getElementById(portalId);
  const home = state.desktopQuestionPortalHomes?.[streamId];
  if (home?.parent?.isConnected && questionEl.parentElement !== home.parent) {
    home.parent.insertBefore(questionEl, home.next || null);
  }
  portal?.remove();
  if (state.desktopQuestionPortalHomes) delete state.desktopQuestionPortalHomes[streamId];
}

function scheduleDurableQuestionSlotRenders() {
  for (let slot = 0; slot < 4; slot++) {
    if (state.slots[slot] && !state.botSlots[slot] && state.slotViewModes[slot] === 'chat') {
      scheduleSlotChatRender(slot);
    }
  }
}

if (typeof window !== 'undefined') {
  window.PentacleDurableQuestions = {
    getOpenQuestionsForStream,
    nearestVisibleAncestorStreamId,
  };
}

function stripAnsi(text) {
  return String(text || '').replace(/\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])/g, '');
}

function normalizeTranscript(text) {
  return stripAnsi(text)
    .replace(/\r/g, '')
    .replace(/[^\S\n]+$/gm, '')
    .replace(/\u00a0/g, ' ')
    .replace(/^\s+$/gm, '')
    .trim();
}

function providerForSession(sessionName, hostId) {
  const streamHost = hostId ? streamHostForHostId(hostId) : null;
  const streamSession = (state.chatStream.sessions || []).find((candidate) => {
    const candidateName = candidate?.session_name || candidate?.name;
    if (candidateName !== sessionName) return false;
    return !streamHost || !candidate?.host || candidate.host === streamHost;
  });
  const canonical = String(streamSession?.provider || '').trim().toLowerCase();
  if (canonical === 'codex' || canonical === 'claude') return canonical;
  return '';
}

function providerLabelForHero(provider) {
  const normalized = String(provider || '').trim().toLowerCase();
  if (normalized === 'codex') return 'Codex';
  if (normalized === 'claude') return 'Claude';
  return provider ? String(provider).trim() : 'Agent';
}

function focusStreamId(streamId) {
  // Specs dashboard nav-to-leader entry point. Reuses the existing
  // assignToSlot affordance the Chats sidebar already uses.
  if (!streamId) return false;
  const streamSession = (state.chatStream.sessions || []).find(
    (s) => String(s.stream_id || '') === String(streamId)
  );
  if (!streamSession) return false;
  const sessionName = streamSession.session_name || streamSession.name;
  if (!sessionName) return false;
  const displayName = streamSession.display_name || streamSession.title || sessionName;
  const hostId = _streamHostToHostId(streamSession.host) || (IS_CLIENT ? 'remote' : 'local');
  switchView('chats');
  assignToSlot(sessionName, displayName, hostId);
  return true;
}

if (typeof window !== 'undefined') window.focusStreamId = focusStreamId;

// E2E walk harness action shim (spec_pentacle_desktop_chat_ui_e2e_walk_harness).
// Renderer handlers (newSession/deleteSession/etc.) are module-scoped (CommonJS
// wrapper), so they are NOT reachable from window for the CDP runner. When — and
// ONLY when — the desktop harness is armed (PENTACLE_HARNESS=1), expose a small
// set of REAL handler wrappers + parameterized helpers the walks need but that
// are non-deterministic / impossible to reach by a single DOM click. This object
// is ABSENT in normal runs (the harness is inert), so production is unaffected.
if (typeof window !== 'undefined' && window.PentacleHarness && window.PentacleHarness.armed) {
  window.PentacleHarnessActions = {
    // Spawn a fresh THROWAWAY agent chat (default local = this machine), via the
    // REAL newSession path (chatSpawn -> attach -> chat view). Returns the
    // spawned { sessionName, hostId, displayName, streamId, slot }.
    spawnThrowaway: async (options = {}, location = 'local') => {
      return await newSession(typeof options === 'string' ? { provider: options, hostId: location } : { ...options, hostId: options.hostId || location });
    },
    // Spawn a THROWAWAY raw terminal (tmux/PTY) session via the real
    // newTerminalSession path. Returns { sessionName, hostId, slot }.
    spawnTerminalThrowaway: async (agent = 'claude', location = 'local') => {
      return await newTerminalSession(agent, location);
    },
    // Close/delete a session by host+name — delegates to the REAL production
    // delete path (deleteSession: operator_confirm chatClose + slot detach +
    // state cleanup, no UI prompt) so there is still exactly ONE chatClose
    // operator_confirm call site. Used by the runner to clean up throwaways.
    closeSessionThrowaway: async (hostId, sessionName) => {
      await deleteSession(sessionName, hostId || 'local');
      return { ok: true };
    },
    // Force a ws reconnect (reconnect_survival walk). No-ops if the preload/main
    // harness channel is absent.
    forceReconnect: async () => {
      if (window.cc && typeof window.cc.forceReconnect === 'function') return await window.cc.forceReconnect();
      return { ok: false, error: 'no force-reconnect channel' };
    },
  };
}

function streamHostForHostId(hostId) {
  return hostPresentation.streamHost(CONFIG, hostId);
}

// Resolve configured desktop aliases without inferring identity from labels.
function _streamHostToHostId(streamHost) {
  const target = String(streamHost || '').trim().toLowerCase();
  if (!target) return null;
  return HOST_IDS.find(id => String(streamHostForHostId(id)).toLowerCase() === target) || null;
}

// Coalesce rapid chat-stream inventory updates into a single sidebar
// re-render per animation frame. Guarded because chat-stream handlers can
// fire before renderSidebar is reachable (e.g. when the session-list element
// hasn't been mounted yet).
let _sidebarRerenderScheduled = false;
function scheduleSidebarRerender() {
  if (_sidebarRerenderScheduled) return;
  if (typeof renderSidebar !== 'function') return;
  if (!document.getElementById('session-list')) return;
  _sidebarRerenderScheduled = true;
  const run = () => {
    _sidebarRerenderScheduled = false;
    try { renderSidebar(); } catch (e) { console.warn('[sidebar] rerender failed:', e?.message || e); }
  };
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run);
  else setTimeout(run, 0);
}

function onChatHistoryChanged(streamId) {
  for (let slot = 0; slot < state.slots.length; slot++) {
    if (state.slotChatBoundStream[slot] === streamId) scheduleSlotChatRender(slot);
  }
}

function ensureChatEventsLoaded(streamId, retry = false) {
  return _ensureChatEventsLoaded(state.chatStream, streamId, window?.cc, console, { retry, onChange: onChatHistoryChanged });
}

function refetchEventsForActiveChatSlots() {
  return _refetchEventsForActiveChatSlots({
    slots: state.slots,
    slotViewModes: state.slotViewModes,
    botSlots: state.botSlots,
    streamState: state.chatStream,
    cc: window?.cc,
    streamHostForHostId,
    findStreamSession: chatUi.findStreamSessionForDesktopSession,
    onChange: onChatHistoryChanged,
  });
}

function assetStreamIdFromSessionKey(sessionKey = {}) {
  const host = String(sessionKey.host || '').trim();
  const sessionName = String(sessionKey.session_name || '').trim();
  return String(sessionKey.stream_id || (host && sessionName ? `${host}:${sessionName}` : '')).trim();
}

function assetScopedKey(assetId, specId) {
  const id = String(assetId || '').trim();
  const spec = String(specId || '').trim();
  return spec ? `spec:${spec}:${id}` : `asset:${id}`;
}

function assetBucketForStream(streamId) {
  const id = String(streamId || '').trim();
  if (!id) return null;
  if (!state.chatStream.assets[id]) {
    state.chatStream.assets[id] = {
      itemsById: {},
      order: [],
      bodiesById: {},
      commentsById: {},
      dismissedById: {},
      // readById[assetKey] = the updated_at at which the operator last opened
      // this report. Drives unread state (report_read_state.js).
      readById: {},
    };
  }
  return state.chatStream.assets[id];
}

function normalizeAssetMetadata(payload) {
  const source = payload && typeof payload === 'object' ? payload : {};
  const sessionKey = source.session_key && typeof source.session_key === 'object'
    ? source.session_key
    : {
      host: source.host,
      session_name: source.session_name,
      stream_id: source.stream_id,
    };
  const streamId = assetStreamIdFromSessionKey(sessionKey);
  const host = String(sessionKey.host || '').trim();
  const sessionName = String(sessionKey.session_name || '').trim();
  const assetId = String(source.asset_id || source.assetId || '').trim();
  if (!streamId || !host || !sessionName || !assetId) return null;
  const specId = source.spec_id || source.specId || null;
  return {
    asset_id: assetId,
    asset_key: assetScopedKey(assetId, specId),
    title: source.title || assetId,
    content_type: source.content_type || source.contentType || 'unknown',
    review_status: source.review_status || source.reviewStatus || 'pending_review',
    spec_id: specId,
    tags: Array.isArray(source.tags) ? source.tags.slice() : [],
    updated_at: source.updated_at || source.updatedAt || source.created_at || '',
    session_key: {
      host,
      session_name: sessionName,
      stream_id: streamId,
    },
  };
}

function normalizeSpecIds(value, fallback) {
  const values = [];
  if (Array.isArray(value)) values.push(...value);
  else if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) values.push(...parsed);
      else values.push(value);
    } catch (_) {
      values.push(...value.split(','));
    }
  }
  if (fallback) values.unshift(fallback);
  return Array.from(new Set(values.map((item) => String(item || '').trim()).filter(Boolean)));
}

function upsertAssetMetadata(payload) {
  const metadata = normalizeAssetMetadata(payload);
  if (!metadata) return null;
  const streamId = metadata.session_key.stream_id;
  const bucket = assetBucketForStream(streamId);
  const key = metadata.asset_key;
  const previous = bucket.itemsById[key] || {};
  if (previous.updated_at && metadata.updated_at && previous.updated_at !== metadata.updated_at) {
    delete bucket.bodiesById[key];
    delete bucket.commentsById[key];
  }
  bucket.itemsById[key] = {
    ...previous,
    ...metadata,
    updated_at: metadata.updated_at || previous.updated_at || '',
  };
  if (!bucket.order.includes(key)) bucket.order.push(key);
  bucket.order.sort((a, b) => {
    const left = bucket.itemsById[a]?.updated_at || '';
    const right = bucket.itemsById[b]?.updated_at || '';
    return String(right).localeCompare(String(left));
  });
  return { streamId, assetId: metadata.asset_id, assetKey: key, metadata: bucket.itemsById[key], bucket };
}

function slotMatchesAssetSession(slot, sessionKey = {}) {
  const session = state.slots[slot];
  if (!session || state.botSlots[slot]) return false;
  const host = streamHostForHostId(session.hostId);
  if (sessionKey.host !== host || sessionKey.session_name !== session.name) return false;
  const streamSession = chatUi.findStreamSessionForDesktopSession(state.chatStream, session, host);
  return !streamSession?.stream_id || streamSession.stream_id === sessionKey.stream_id;
}

function slotSpecIds(slot) {
  const session = state.slots[slot];
  if (!session || state.botSlots[slot]) return [];
  const host = streamHostForHostId(session.hostId);
  const streamSession = chatUi.findStreamSessionForDesktopSession(state.chatStream, session, host);
  return normalizeSpecIds(streamSession?.spec_ids, streamSession?.spec_id);
}

function slotMatchesAssetSpec(slot, metadata) {
  const specId = String(metadata?.spec_id || '').trim();
  return !!specId && slotSpecIds(slot).includes(specId);
}

function routeAssetMetadataToSlots(metadata) {
  if (!metadata?.session_key) return [];
  const routed = [];
  for (let slot = 0; slot < 4; slot++) {
    const sessionMatch = slotMatchesAssetSession(slot, metadata.session_key);
    const specMatch = !sessionMatch && slotMatchesAssetSpec(slot, metadata);
    if (!sessionMatch && !specMatch) continue;
    let routedMetadata = metadata;
    if (specMatch) {
      const context = assetContextForSlot(slot);
      if (!context) continue;
      const result = upsertAssetMetadata({
        ...metadata,
        session_key: {
          host: context.host,
          session_name: context.session.name,
          stream_id: context.streamId,
        },
      });
      routedMetadata = result?.metadata || metadata;
    }
    routed.push(slot);
    ensureSlotAssetTabs(slot);
    const active = state.slotActiveAsset[slot];
    if (active?.streamId === routedMetadata.session_key.stream_id && active?.assetKey === routedMetadata.asset_key) {
      refreshActiveSlotAsset(slot);
    }
    window.PentacleHarness?.emit?.('asset:update', {
      slot,
      data: {
        streamId: routedMetadata.session_key.stream_id,
        assetId: routedMetadata.asset_id,
      },
    });
  }
  return routed;
}

function applyAssetUpdate(payload) {
  const result = upsertAssetMetadata(payload);
  if (!result) return null;
  routeAssetMetadataToSlots(result.metadata);
  // New or republished report evidence may change the stream's unread count.
  refreshSidebarUnread();
  return result;
}

// Metadata update for the slot's mounted asset. A changed render signature
// (republish, review status — both bump updated_at) re-renders fully. An
// unchanged signature on a report is a comment-only mutation broadcast
// (comments never bump the asset's updated_at): refresh comment state in place
// so the report DOM and the operator's scroll position survive. This also keeps
// an open viewer fresh for CLI/agent-side comment mutations.
function refreshActiveSlotAsset(slot) {
  const active = state.slotActiveAsset[slot];
  const refs = state.slotChatRefs[slot];
  const bucket = state.chatStream.assets[active?.streamId];
  const item = bucket?.itemsById?.[active?.assetKey || active?.assetId];
  const mounted = !!refs?.assetMount && refs.assetMount.childElementCount > 0;
  if (!item || !mounted || state.slotAssetRenderSignature[slot] !== assetRenderSignature(active, item)) {
    renderSlotAsset(slot);
    return;
  }
  if (item.content_type !== 'report') return;
  const fetchedSignature = assetRenderSignature(active, item);
  const gen = ++state.slotAssetCommentsGen[slot];
  listAssetComments(active.streamId, item).then((comments) => {
    // Stale-guards: dropped if a later refetch or a full render superseded this
    // fetch, if the slot moved to another asset, or if a republish/review-status
    // change landed mid-flight (its full re-render owns comment state then).
    if (state.slotAssetCommentsGen[slot] !== gen) return;
    const current = state.slotActiveAsset[slot];
    if (current?.streamId !== active.streamId || current?.assetKey !== active.assetKey) return;
    const latest = state.chatStream.assets[active.streamId]?.itemsById?.[active.assetKey || active.assetId];
    if (assetRenderSignature(current, latest) !== fetchedSignature) return;
    assetRender.updateReportComments(item.asset_key || item.asset_id, comments);
  });
}

function normalizeAssetListResponse(reply, fallbackSessionKey) {
  if (!reply || typeof reply !== 'object') return [];
  const records = Array.isArray(reply.assets)
    ? reply.assets
    : Array.isArray(reply.items)
      ? reply.items
      : Array.isArray(reply.results)
        ? reply.results
        : [];
  return records.map((record) => ({
    ...(record || {}),
    session_key: (record && record.session_key) || fallbackSessionKey,
  }));
}

function assetListArgsForSession(session) {
  if (!session) return null;
  const host = streamHostForHostId(session.hostId);
  const streamSession = chatUi.findStreamSessionForDesktopSession(state.chatStream, session, host);
  if (streamSession?.stream_id) {
    const args = { stream_id: streamSession.stream_id };
    const specIds = normalizeSpecIds(streamSession.spec_ids, streamSession.spec_id);
    if (specIds.length) args.spec_ids = specIds;
    return args;
  }
  return { host, session_name: session.name };
}

function fallbackAssetSessionKeyForSession(session) {
  if (!session) return null;
  const host = streamHostForHostId(session.hostId);
  const streamSession = chatUi.findStreamSessionForDesktopSession(state.chatStream, session, host);
  return {
    host,
    session_name: session.name,
    stream_id: streamSession?.stream_id || `${host}:${session.name}`,
  };
}

async function fetchSlotAssetSnapshot(slot, gen = state.slotGen[slot]) {
  const session = state.slots[slot];
  if (!session || state.botSlots[slot] || !window.cc || typeof window.cc.assetList !== 'function') return null;
  const args = assetListArgsForSession(session);
  if (!args) return null;
  const fallbackSessionKey = fallbackAssetSessionKeyForSession(session);
  try {
    const reply = await window.cc.assetList(args);
    if (state.slotGen[slot] !== gen || state.slots[slot] !== session) return null;
    const records = normalizeAssetListResponse(reply, fallbackSessionKey);
    const applied = [];
    for (const record of records) {
      const result = upsertAssetMetadata(record);
      if (result) {
        routeAssetMetadataToSlots(result.metadata);
        applied.push(result.metadata);
      }
    }
    return applied;
  } catch (error) {
    console.warn('[asset] list failed:', error?.message || error);
    return null;
  }
}

function refetchAssetSnapshotsForActiveSlots() {
  for (let slot = 0; slot < 4; slot++) {
    if (state.slots[slot] && !state.botSlots[slot]) fetchSlotAssetSnapshot(slot, state.slotGen[slot]);
  }
}

async function ensureAssetBodyLoaded(streamId, assetId, assetKey = null) {
  const bucket = assetBucketForStream(streamId);
  const id = String(assetId || '').trim();
  const key = String(assetKey || assetScopedKey(id)).trim();
  if (!bucket || !id || !key) return null;
  if (Object.prototype.hasOwnProperty.call(bucket.bodiesById, key)) return bucket.bodiesById[key];
  if (!window.cc || typeof window.cc.assetGet !== 'function') return null;
  const metadata = bucket.itemsById[key];
  const sessionKey = metadata?.session_key || {};
  const args = { stream_id: streamId, asset_id: id };
  if (sessionKey.host) args.host = sessionKey.host;
  if (sessionKey.session_name) args.session_name = sessionKey.session_name;
  if (metadata?.spec_id) args.spec_id = metadata.spec_id;
  try {
    const reply = await window.cc.assetGet(args);
    const asset = reply?.asset && typeof reply.asset === 'object' ? reply.asset : null;
    if (asset) {
      upsertAssetMetadata({ ...asset, session_key: asset.session_key || sessionKey });
    }
    const body = asset && Object.prototype.hasOwnProperty.call(asset, 'body')
      ? asset.body
      : Object.prototype.hasOwnProperty.call(reply || {}, 'body')
        ? reply.body
        : reply?.payload;
    bucket.bodiesById[key] = body;
    return body;
  } catch (error) {
    console.warn('[asset] get failed:', error?.message || error);
    return null;
  }
}

function assetTabKey(streamId, assetId) {
  return `${streamId}:${assetId}`;
}

function assetContextForSlot(slot) {
  const session = state.slots[slot];
  if (!session || state.botSlots[slot]) return null;
  const host = streamHostForHostId(session.hostId);
  const streamSession = chatUi.findStreamSessionForDesktopSession(state.chatStream, session, host);
  const streamId = streamSession?.stream_id || `${host}:${session.name}`;
  const bucket = state.chatStream.assets[streamId];
  return { session, host, streamId, bucket };
}

function visibleAssetsForSlot(slot) {
  const context = assetContextForSlot(slot);
  if (!context?.bucket) return [];
  return context.bucket.order
    .map((assetKey) => context.bucket.itemsById[assetKey])
    .filter((item) => item && !context.bucket.dismissedById[assetTabKey(context.streamId, item.asset_key)]);
}

function assetPayloadForRender(contentType, body) {
  if (contentType === 'json_table' && typeof body === 'string') {
    try { return JSON.parse(body); } catch (_) { return { columns: [], rows: [] }; }
  }
  if (contentType === 'report' && typeof body === 'string') {
    try { return JSON.parse(body); } catch (_) { return body; }
  }
  return body;
}

function assetRpcArgs(streamId, item) {
  const sessionKey = item?.session_key || {};
  const args = { stream_id: streamId, asset_id: item?.asset_id };
  if (sessionKey.host) args.host = sessionKey.host;
  if (sessionKey.session_name) args.session_name = sessionKey.session_name;
  if (item?.spec_id) args.spec_id = item.spec_id;
  return args;
}

function assetPopoutArgs(streamId, item) {
  return {
    ...assetRpcArgs(streamId, item),
    title: item?.title || item?.asset_id,
    asset: item,
  };
}

async function listAssetComments(streamId, item) {
  if (!window.cc || typeof window.cc.assetCommentsList !== 'function') return [];
  const bucket = state.chatStream.assets[streamId];
  if (!bucket || !item?.asset_id) return [];
  const key = item.asset_key || assetScopedKey(item.asset_id, item.spec_id);
  try {
    const reply = await window.cc.assetCommentsList(assetRpcArgs(streamId, item));
    const comments = Array.isArray(reply?.comments) ? reply.comments : [];
    bucket.commentsById[key] = comments;
    return comments;
  } catch (error) {
    console.warn('[asset] comments list failed:', error?.message || error);
    return bucket.commentsById[key] || [];
  }
}

function reportAssetActions(slot, streamId, item) {
  const call = async (method, args) => {
    if (!window.cc || typeof window.cc[method] !== 'function') throw new Error(`${method} unavailable`);
    const reply = await window.cc[method]({ ...assetRpcArgs(streamId, item), ...(args || {}) });
    if (reply && reply.ok === false) throw new Error(reply.error || `${method} failed`);
    if (reply?.asset) upsertAssetMetadata({ ...reply.asset, session_key: reply.asset.session_key || item.session_key });
    const bucket = state.chatStream.assets[streamId];
    const key = item.asset_key || assetScopedKey(item.asset_id, item.spec_id);
    if (bucket) delete bucket.commentsById[key];
    const latest = state.chatStream.assets[streamId]?.itemsById?.[key] || item;
    const fetchedAt = latest.updated_at || '';
    const gen = ++state.slotAssetCommentsGen[slot];
    const comments = await listAssetComments(streamId, latest);
    // Superseded by a later refetch or a full render — that writer owns state.
    if (state.slotAssetCommentsGen[slot] !== gen) return reply;
    // Comment mutations update the mounted report in place — no tab teardown, no
    // scroll loss. Metadata-bearing replies (review status) change the render
    // signature, which the unforced render below turns into a full re-render.
    // Stale-guard: if a republish landed while the refetch was in flight, its
    // own render path owns comment state — apply nothing and reconcile unforced.
    const current = state.chatStream.assets[streamId]?.itemsById?.[key] || latest;
    const fresh = (current.updated_at || '') === fetchedAt;
    if (fresh && assetRender.updateReportComments(latest.asset_key || latest.asset_id, comments)) {
      renderSlotAsset(slot);
    } else if (fresh) {
      renderSlotAsset(slot, true);
    } else {
      renderSlotAsset(slot);
    }
    return reply;
  };
  return {
    addComment(comment) {
      return call('assetCommentAdd', {
        section_id: comment.section_id,
        block_id: comment.block_id,
        run_index: comment.run_index,
        excerpt: comment.excerpt,
        body: comment.body,
      });
    },
    editComment(commentId, body) {
      return call('assetCommentEdit', { comment_id: commentId, body });
    },
    deleteComment(commentId) {
      return call('assetCommentDelete', { comment_id: commentId });
    },
    resolveComment(commentId, resolved) {
      return call('assetCommentResolve', { comment_id: commentId, resolved });
    },
    sendToChat() {
      return call('assetCommentsSendToChat');
    },
    setReviewStatus(reviewStatus) {
      return call('assetReviewSet', { review_status: reviewStatus });
    },
  };
}

function assetRenderSignature(active, item) {
  return [
    active?.streamId || '',
    active?.assetKey || active?.assetId || '',
    item?.updated_at || '',
    item?.review_status || '',
    item?.content_type || '',
    item?.title || '',
  ].join('|');
}

function renderSlotAsset(slot, force = false) {
  const refs = state.slotChatRefs[slot];
  const active = state.slotActiveAsset[slot];
  if (!refs?.assetMount || !active) return;
  const bucket = state.chatStream.assets[active.streamId];
  const item = bucket?.itemsById?.[active.assetKey || active.assetId];
  if (!item) {
    state.slotAssetRenderSignature[slot] = null;
    refs.assetMount.innerHTML = '<div class="slot-asset-empty">Select an asset.</div>';
    return;
  }
  const signature = assetRenderSignature(active, item);
  if (!force && state.slotAssetRenderSignature[slot] === signature && refs.assetMount.childElementCount > 0) return;
  state.slotAssetRenderSignature[slot] = signature;
  // Supersede any in-flight render and any pending in-place comment apply: this
  // render's completion (re-)seeds report comment state.
  const renderGen = ++state.slotAssetRenderGen[slot];
  ++state.slotAssetCommentsGen[slot];
  refs.assetMount.innerHTML = '';
  const header = document.createElement('div');
  header.className = 'slot-asset-view-header';
  const title = document.createElement('div');
  title.className = 'slot-asset-view-title';
  title.textContent = item.title || item.asset_id;
  const type = document.createElement('div');
  type.className = 'slot-asset-view-type';
  type.textContent = item.content_type || 'unknown';
  const popout = document.createElement('button');
  popout.type = 'button';
  popout.className = 'slot-asset-view-popout';
  popout.textContent = 'Pop out';
  popout.addEventListener('click', async () => {
    if (!window.cc || typeof window.cc.assetPopOut !== 'function') return;
    const reply = await window.cc.assetPopOut(assetPopoutArgs(active.streamId, item));
    if (reply && reply.ok === false) console.warn('[asset] pop-out failed:', reply.error);
  });
  header.appendChild(title);
  header.appendChild(type);
  header.appendChild(popout);
  refs.assetMount.appendChild(header);
  const bodyMount = document.createElement('div');
  bodyMount.className = 'slot-asset-view-body';
  // Only show the loading placeholder on a cold load; a cached body (e.g. a
  // review-status re-render) skips the placeholder. Comment mutations never
  // reach this teardown path — they update in place via updateReportComments.
  const cacheKey = active.assetKey || assetScopedKey(active.assetId);
  const bodyCached = !!bucket?.bodiesById && Object.prototype.hasOwnProperty.call(bucket.bodiesById, cacheKey);
  if (!bodyCached) bodyMount.textContent = 'Loading...';
  refs.assetMount.appendChild(bodyMount);
  ensureAssetBodyLoaded(active.streamId, active.assetId, active.assetKey).then(async (body) => {
    if (state.slotAssetRenderGen[slot] !== renderGen) return;
    if (state.slotActiveAsset[slot]?.streamId !== active.streamId || state.slotActiveAsset[slot]?.assetKey !== active.assetKey) return;
    bodyMount.innerHTML = '';
    const latest = state.chatStream.assets[active.streamId]?.itemsById?.[active.assetKey] || item;
    const comments = latest.content_type === 'report' ? await listAssetComments(active.streamId, latest) : [];
    // Superseded renders must not reach renderAsset: a stale completion would
    // re-seed the report's shared ui state against a detached tree.
    if (state.slotAssetRenderGen[slot] !== renderGen) return;
    if (state.slotActiveAsset[slot]?.streamId !== active.streamId || state.slotActiveAsset[slot]?.assetKey !== active.assetKey) return;
    const rendered = assetRender.renderAsset(
      document,
      latest.content_type,
      assetPayloadForRender(latest.content_type, body),
      {
        classPrefix: 'slot-asset',
        asset: latest,
        comments,
        reviewStatus: latest.review_status,
        actions: latest.content_type === 'report' ? reportAssetActions(slot, active.streamId, latest) : null,
      },
    );
    bodyMount.appendChild(rendered);
    window.PentacleHarness?.emit?.('asset:rendered', {
      slot,
      data: { streamId: active.streamId, assetId: active.assetId, contentType: latest.content_type },
    });
  }).catch((error) => {
    if (state.slotAssetRenderGen[slot] !== renderGen) return;
    if (state.slotActiveAsset[slot]?.streamId !== active.streamId || state.slotActiveAsset[slot]?.assetKey !== active.assetKey) return;
    bodyMount.textContent = `Asset failed: ${error?.message || error}`;
  });
}

function openSlotAsset(slot, streamId, assetId, assetKey = null) {
  const key = assetKey || assetScopedKey(assetId);
  state.slotActiveAsset[slot] = { streamId, assetId, assetKey: key };
  state.slotAssetRenderSignature[slot] = null;
  // Opening a report marks only that report read at its current version
  // (spec item 5). Republished evidence re-flags itself unread automatically.
  const bucket = state.chatStream.assets[streamId];
  if (bucket && markReportRead(bucket, key)) refreshSidebarUnread();
  ensureSlotAssetTabs(slot);
  updateSlotViewMode(slot, 'asset');
}

function dockAssetFromPayload(payload) {
  const item = normalizeAssetMetadata(payload?.asset || payload);
  if (!item) return false;
  const result = upsertAssetMetadata(item);
  const metadata = result?.metadata || item;
  let targetSlot = -1;
  for (let slot = 0; slot < 4; slot++) {
    if (slotMatchesAssetSession(slot, metadata.session_key) || slotMatchesAssetSpec(slot, metadata)) {
      targetSlot = slot;
      break;
    }
  }
  if (targetSlot < 0) return false;
  const context = assetContextForSlot(targetSlot);
  const streamId = context?.streamId || metadata.session_key.stream_id;
  openSlotAsset(targetSlot, streamId, metadata.asset_id, metadata.asset_key);
  window.PentacleHarness?.emit?.('asset:dock', {
    slot: targetSlot,
    data: { streamId, assetId: metadata.asset_id },
  });
  return true;
}

function dismissSlotAsset(slot, streamId, assetId, assetKey = null) {
  const bucket = state.chatStream.assets[streamId];
  if (!bucket) return;
  const key = assetKey || assetScopedKey(assetId);
  bucket.dismissedById[assetTabKey(streamId, key)] = true;
  const active = state.slotActiveAsset[slot];
  if (active?.streamId === streamId && active?.assetKey === key) {
    state.slotActiveAsset[slot] = null;
    updateSlotViewMode(slot, 'chat');
  }
  ensureSlotAssetTabs(slot);
  refreshSidebarUnread();
}

// Closing an asset tab DELETES the asset (and its comments) on the daemon; the
// daemon then broadcasts asset.removed to every view. Closing a chat never
// deletes an asset. The local dismiss is optimistic so the tab clears at once.
async function deleteSlotAsset(slot, streamId, assetId, assetKey = null) {
  const bucket = state.chatStream.assets[streamId];
  const key = assetKey || assetScopedKey(assetId);
  const item = bucket?.itemsById?.[key];
  dismissSlotAsset(slot, streamId, assetId, assetKey);
  if (!window.cc || typeof window.cc.assetDelete !== 'function') return;
  const sessionKey = item?.session_key || {};
  const args = { stream_id: sessionKey.stream_id || streamId, asset_id: assetId };
  if (sessionKey.host) args.host = sessionKey.host;
  if (sessionKey.session_name) args.session_name = sessionKey.session_name;
  if (item?.spec_id) args.spec_id = item.spec_id;
  try {
    const reply = await window.cc.assetDelete(args);
    if (reply && reply.ok === false) console.warn('[asset] delete failed:', reply.error);
  } catch (error) {
    console.warn('[asset] delete error:', error?.message || error);
  }
}

function removeAssetFromBucket(streamId, bucket, key) {
  if (!bucket) return;
  delete bucket.itemsById[key];
  delete bucket.bodiesById[key];
  delete bucket.commentsById[key];
  const idx = bucket.order.indexOf(key);
  if (idx >= 0) bucket.order.splice(idx, 1);
  delete bucket.dismissedById[assetTabKey(streamId, key)];
  for (let slot = 0; slot < 4; slot++) {
    const active = state.slotActiveAsset[slot];
    if (active?.streamId === streamId && active?.assetKey === key) {
      state.slotActiveAsset[slot] = null;
      updateSlotViewMode(slot, 'chat');
    }
    ensureSlotAssetTabs(slot);
  }
}

function applyAssetRemoved(payload) {
  const sessionKey = payload?.session_key || {};
  const assetId = String(payload?.asset_id || '').trim();
  const specId = String(payload?.spec_id || '').trim();
  if (!assetId) return;
  // Remove from the producer bucket and any spec-routed copies in other slots.
  for (const streamId of Object.keys(state.chatStream.assets || {})) {
    const bucket = state.chatStream.assets[streamId];
    if (!bucket?.itemsById) continue;
    for (const key of Object.keys(bucket.itemsById)) {
      const item = bucket.itemsById[key];
      if (!item || item.asset_id !== assetId) continue;
      const producerMatch = streamId === sessionKey.stream_id;
      const specMatch = !!specId && String(item.spec_id || '').trim() === specId;
      if (producerMatch || specMatch) removeAssetFromBucket(streamId, bucket, key);
    }
  }
  refreshSidebarUnread();
}

function clearSlotAssetDismissals(slot) {
  const context = assetContextForSlot(slot);
  if (!context?.bucket) return;
  for (const key of Object.keys(context.bucket.dismissedById)) {
    if (key.startsWith(`${context.streamId}:`)) delete context.bucket.dismissedById[key];
  }
}

function ensureSlotAssetTabs(slot) {
  const header = document.getElementById(`header-${slot}`);
  if (!header) return null;
  let tabs = header.querySelector('.slot-asset-tabs');
  const actions = header.querySelector('.cell-actions');
  if (!state.slots[slot] || state.botSlots[slot]) {
    tabs?.remove();
    return null;
  }
  if (!tabs) {
    tabs = document.createElement('div');
    tabs.className = 'slot-asset-tabs';
    if (actions) header.insertBefore(tabs, actions);
    else header.appendChild(tabs);
  }
  const context = assetContextForSlot(slot);
  const assets = visibleAssetsForSlot(slot);
  tabs.innerHTML = '';
  tabs.style.display = assets.length ? 'flex' : 'none';
  if (!assets.length || !context) return tabs;
  const bucket = state.chatStream.assets[context.streamId];
  const isDismissed = (key) => !!(bucket?.dismissedById || {})[assetTabKey(context.streamId, key)];
  for (const item of assets) {
    const tab = document.createElement('button');
    tab.type = 'button';
    tab.className = 'slot-asset-tab';
    tab.dataset.assetId = item.asset_id;
    tab.dataset.assetKey = item.asset_key;
    tab.dataset.streamId = context.streamId;
    tab.title = item.title || item.asset_id;
    const active = state.slotActiveAsset[slot]?.streamId === context.streamId
      && state.slotActiveAsset[slot]?.assetKey === item.asset_key;
    tab.classList.toggle('active', active);
    // Unread report indicator (spec item 5): a dot on report tabs whose current
    // version the operator has not opened. Cleared the moment the tab is opened.
    const unread = isReportUnread(bucket, item.asset_key, isDismissed);
    tab.classList.toggle('is-unread', unread);
    tab.setAttribute('aria-label', unread
      ? `${item.title || item.asset_id} (unread report)`
      : (item.title || item.asset_id));
    if (unread) {
      const dot = document.createElement('span');
      dot.className = 'slot-asset-tab-unread';
      dot.setAttribute('aria-hidden', 'true');
      tab.appendChild(dot);
    }
    const label = document.createElement('span');
    label.className = 'slot-asset-tab-label';
    label.textContent = item.title || item.asset_id;
    const close = document.createElement('span');
    close.className = 'slot-asset-tab-close';
    close.setAttribute('aria-hidden', 'true');
    close.textContent = '\u00d7';
    tab.appendChild(label);
    tab.appendChild(close);
    tab.addEventListener('click', (event) => {
      event.stopPropagation();
      if (event.target && event.target.closest('.slot-asset-tab-close')) {
        deleteSlotAsset(slot, context.streamId, item.asset_id, item.asset_key);
        return;
      }
      openSlotAsset(slot, context.streamId, item.asset_id, item.asset_key);
    });
    tabs.appendChild(tab);
  }
  return tabs;
}

const closedChatSlots = createClosedChatSlots({
  readState: () => ({
    connected: state.chatStream.connected,
    epoch: state.chatStream.stateVersion,
    slots: state.slots.map((slot, index) => slot && ({
      host: streamHostForHostId(slot.hostId), name: slot.name,
      generation: state.slotGen[index], bot: state.botSlots[index],
    })),
  }),
  retire: (slot, streamId) => {
    // The portal owns a moved question node; restore/remove it before refs vanish.
    const questionEl = state.slotChatRefs[slot]?.questionEl;
    if (questionEl) closeDesktopQuestionPortal(streamId, questionEl);
    delete state.desktopQuestionOverlayOpen[streamId];
    delete state.desktopQuestionPortalHomes[streamId];
    delete state.questionDrafts[`${streamId}:flow`];
    delete state.questionPageIndexByStream[`${streamId}:flow`];
    clearSlotAttachments(slot);
    detachSlot(slot);
  },
});

function applyChatStreamState(data) {
  // NOT gated on chatUiEnabled(): the sidebar visibility filter consumes
  // state.chatStream.sessions regardless of whether the in-slot chat UI is
  // turned on. Slot rendering inside this function is self-guarded by
  // `state.slotViewModes[slot] === 'chat'`, so chat-UI work only fires when
  // chat UI is actually active.
  if (!data) return;
  const wasConnected = state.chatStream.connected;
  // Flip degraded mode immediately on WS connection state change rather
  // than waiting for the next inventory-frame render. Without this,
  // there's a stale-UI window where the sidebar still shows live state
  // but the daemon is unreachable.
  if (!applyVersionedConnectionState(state.chatStream, data, setDegradedMode)) return;
  if (Array.isArray(data.events)) {
    state.chatStream.events = data.events.slice(-CHAT_STREAM_LIMIT);
  }
  state.chatStream.drafts = data.drafts || {};
  if (Object.prototype.hasOwnProperty.call(data, 'hosts_stats')) {
    state.chatStream.hostsStats = data.hosts_stats && typeof data.hosts_stats === 'object'
      ? { ...data.hosts_stats }
      : {};
    renderHostsStats(state.chatStream.hostsStats);
  }
  // Preserve the last daemon inventory across status-only disconnect payloads.
  state.chatStream.sessions = nextChatStreamSessions(state.chatStream.sessions, data, {
    spawnFailureNotifications: state.chatStream.spawnFailureNotifications,
    onSpawnFailure: (session) => showToast(session.reason, { type: 'error' }),
  });
  closedChatSlots.update(data);
  reconcileLocallyClosedStreamIds();
  state.durableQuestionHydrationByStream = {};
  if (Array.isArray(data.schedules)) {
    // Retain the schedule protocol inventory for future consumers; the desktop
    // schedule window plane was retired so there is nothing to re-render here.
    state.chatStream.schedules = replaceSchedulesFromInventory(state.chatStream.schedules, data.schedules);
  }
  // A healthy daemon snapshot replaces the managed inventory.
  if (state.chatStream.connected) {
    _perfRecord('renderer:sidebar-seeded-from-snapshot', { count: state.sessions.length });
    syncSlotDisplayNames();
  }
  // On disconnect, drop the lazy-load tracker so the next connect re-fetches
  // each active chat slot's stream events. Live events keep flowing into
  // state.chatStream.events via _push on the main side; only the visible
  // chat panes need an RPC backfill on reconnect.
  if (!state.chatStream.connected) {
    state.chatStream.eventsLoadedFor.clear();
    state.chatStream.historyLoads = {};
  } else if (!wasConnected) {
    refetchEventsForActiveChatSlots();
    refetchAssetSnapshotsForActiveSlots();
    resyncSpawnCatalogAfterReconnect();
  }
  for (let slot = 0; slot < 4; slot++) {
    if (state.slots[slot] && !state.botSlots[slot] && state.slotViewModes[slot] === 'chat') {
      scheduleSlotChatRender(slot);
    }
  }
  // Sidebar visibility filter depends on chat-stream metadata; re-render so a
  // nested session that just arrived in inventory disappears from the sidebar
  // immediately when the next inventory frame lands.
  scheduleSidebarRerender();
}

function applyChatStreamPayload(payload) {
  // NOT gated on chatUiEnabled(): see applyChatStreamState.
  if (!payload) return;
  if (payload.type === 'chat.event' && payload.event) {
    applyChatStreamPayload(payload.event);
    return;
  }
  if (payload.type === 'specs.changed') {
    // Push event from chat_streamd's filesystem watcher. The Specs dashboard
    // registers a listener via window.__pentacleSpecsChangedListeners on
    // mount; the polling fallback covers the case where this push is lost.
    const listeners = window.__pentacleSpecsChangedListeners || [];
    for (const cb of listeners.slice()) {
      try { cb(payload); } catch (e) { console.warn('[specs.changed listener]', e); }
    }
    return;
  }
  if (payload.type === 'notification') {
    // Push event from chat_streamd's notification store (created/updated record).
    // The Notifications dashboard registers a listener via
    // window.__pentacleNotificationsListeners on mount; the polling fallback
    // covers the case where this push is lost.
    if (indexDurableQuestionNotification(notificationRecordFromPayload(payload))) {
      scheduleDurableQuestionSlotRenders();
    }
    const listeners = window.__pentacleNotificationsListeners || [];
    for (const cb of listeners.slice()) {
      try { cb(payload); } catch (e) { console.warn('[notification listener]', e); }
    }
    return;
  }
  if (payload.type === 'asset.update') {
    applyAssetUpdate(payload);
    return;
  }
  if (payload.type === 'asset.removed') {
    applyAssetRemoved(payload);
    return;
  }
  if (payload.type === 'snapshot') {
    if (Array.isArray(payload.notifications)) {
      for (const notification of payload.notifications) {
        indexDurableQuestionNotification(notification);
      }
      scheduleDurableQuestionSlotRenders();
    }
    applyChatStreamState({
      connected: payload.connected ?? state.chatStream.connected,
      state_version: payload.state_version,
      events: payload.events || [],
      drafts: payload.drafts || {},
      sessions: payload.sessions,
      schedules: payload.schedules || [],
      notifications: payload.notifications || [],
      hosts_stats: payload.hosts_stats || state.chatStream.hostsStats,
    }, 'snapshot');
    if (Object.prototype.hasOwnProperty.call(payload, 'limits')) {
      renderLimits(
        payload.limits,
        Object.prototype.hasOwnProperty.call(payload, 'limits_health') ? payload.limits_health : null,
      );
    }
    return;
  }
  if (payload.type === 'limits.update') {
    if (Object.prototype.hasOwnProperty.call(payload, 'limits')) {
      renderLimits(
        payload.limits,
        Object.prototype.hasOwnProperty.call(payload, 'limits_health') ? payload.limits_health : null,
      );
    }
    return;
  }
  if (payload.type === 'hosts.stats' && payload.hosts && typeof payload.hosts === 'object') {
    state.chatStream.hostsStats = { ...payload.hosts };
    renderHostsStats(state.chatStream.hostsStats);
    return;
  }
  if (payload.connected === true || payload.connected === false) {
    applyChatStreamState(payload);
    return;
  }
  if (payload.type === 'session.inventory' && Array.isArray(payload.sessions)) {
    if (!applyVersionedConnectionState(state.chatStream,
      { ...payload, connected: state.chatStream.connected }, setDegradedMode)) return;
    state.chatStream.sessions = nextChatStreamSessions(state.chatStream.sessions, payload, {
      spawnFailureNotifications: state.chatStream.spawnFailureNotifications,
      onSpawnFailure: (session) => showToast(session.reason, { type: 'error' }),
    });
    closedChatSlots.update(payload);
    reconcileLocallyClosedStreamIds();
    state.durableQuestionHydrationByStream = {};
    if (state.chatStream.connected) {
      syncSlotDisplayNames();
    }
    for (let slot = 0; slot < 4; slot++) {
      if (state.slots[slot] && !state.botSlots[slot] && state.slotViewModes[slot] === 'chat') {
        scheduleSlotChatRender(slot);
      }
    }
    // Same reasoning as applyChatStreamState: nested sessions arriving via
    // delta inventory must drop out of the sidebar immediately.
    scheduleSidebarRerender();
    refetchAssetSnapshotsForActiveSlots();
    return;
  }
  if (payload.type === 'schedule.inventory' && Array.isArray(payload.schedules)) {
    // Protocol ingestion only — the desktop schedule window plane was retired.
    state.chatStream.schedules = replaceSchedulesFromInventory(state.chatStream.schedules, payload.schedules);
    return;
  }
  if (SCHEDULE_EVENT_TYPES.includes(String(payload.type || ''))) {
    state.chatStream.schedules = applyScheduleEvent(state.chatStream.schedules, payload);
    return;
  }
  if (payload.type) return;
  if (payload.kind === 'DRAFT') {
    if (payload.stream_id) state.chatStream.drafts[payload.stream_id] = payload;
    for (let slot = 0; slot < 4; slot++) {
      const session = state.slots[slot];
      if (!session || state.botSlots[slot] || state.slotViewModes[slot] !== 'chat') continue;
      const host = streamHostForHostId(session.hostId);
      if (payload.host === host && payload.session_name === session.name) {
        scheduleSlotChatRender(slot);
      }
    }
    return;
  }
  state.chatStream.events = [...state.chatStream.events, payload].slice(-CHAT_STREAM_LIMIT);
  for (let slot = 0; slot < 4; slot++) {
    const session = state.slots[slot];
    if (!session || state.botSlots[slot] || state.slotViewModes[slot] !== 'chat') continue;
    const host = streamHostForHostId(session.hostId);
    if (payload.host === host && payload.session_name === session.name) {
      scheduleSlotChatRender(slot);
    }
  }
}

function chatEventsForSession(session) {
  if (!session) return [];
  const host = streamHostForHostId(session.hostId);
  const streamSession = chatUi.findStreamSessionForDesktopSession(state.chatStream, session, host);
  if (streamSession?.stream_id) {
    return state.chatStream.events.filter((event) => event.stream_id === streamSession.stream_id);
  }
  return state.chatStream.events.filter((event) => event.host === host && event.session_name === session.name);
}

function hasRecentWorkingEvent(session) {
  if (!session) return false;
  const now = Date.now();
  return chatEventsForSession(session).some((event) => {
    if (!/^Working \(\d+[smh]/.test(String(event?.text || ''))) return false;
    const ts = event?.timestamp ? new Date(event.timestamp).getTime() : 0;
    return ts && (now - ts) < 20 * 1000;
  });
}

function extractWorkingTime(text) {
  return chatUi.extractWorkingTime(text);
}

function recentWorkingLabel(session) {
  if (!session) return '';
  const now = Date.now();
  let latest = null;
  for (const event of chatEventsForSession(session)) {
    const text = String(event?.text || '');
    const parsed = extractWorkingTime(text);
    if (!parsed) continue;
    const ts = event?.timestamp ? new Date(event.timestamp).getTime() : 0;
    if (!ts || (now - ts) >= 20 * 1000) continue;
    latest = parsed;
  }
  return latest || '';
}

function chatDraftForSession(session) {
  if (!session) return '';
  return chatDraftStateForSession(session)?.text || '';
}

function chatDraftStateForSession(session) {
  if (!session) return null;
  const host = streamHostForHostId(session.hostId);
  const streamSession = chatSessionStateForSession(session);
  return (
    state.chatStream.drafts?.[streamSession?.stream_id] ||
    state.chatStream.drafts?.[`${host}:${session.name}`] ||
    null
  );
}

function chatSessionStateForSession(session) {
  if (!session) return null;
  const host = streamHostForHostId(session.hostId);
  return chatUi.findStreamSessionForDesktopSession(state.chatStream, session, host);
}

function pendingPeerMessagesForSession(session) {
  const count = Number(chatSessionStateForSession(session)?.pending_peer_messages || 0);
  return Number.isFinite(count) && count > 0 ? Math.floor(count) : 0;
}

function pendingPeerBadgeHtml(count, className = 'pending-peer-badge') {
  if (!count) return '';
  const label = `${count} pending peer ${count === 1 ? 'message' : 'messages'}`;
  return `<span class="${className}" title="${esc(label)}"><span class="pending-peer-icon" aria-hidden="true">&#9993;</span><span class="pending-peer-count">${esc(String(count))}</span></span>`;
}

function updateSlotPendingPeerBadge(slot, count) {
  const header = document.getElementById(`header-${slot}`);
  if (!header) return;
  header.querySelector('.cell-pending-peer-badge')?.remove();
  if (!count) return;
  const providerTag = header.querySelector('.cell-provider-tag');
  const sourceTag = header.querySelector('.cell-source-tag');
  const label = header.querySelector('.cell-label');
  const template = document.createElement('template');
  template.innerHTML = pendingPeerBadgeHtml(count, 'pending-peer-badge cell-pending-peer-badge');
  const badge = template.content.firstElementChild;
  if (!badge) return;
  if (providerTag) providerTag.after(badge);
  else if (sourceTag) sourceTag.after(badge);
  else if (label) label.after(badge);
}

function chatSessionStateForNameHost(sessionName, hostId) {
  if (!sessionName) return null;
  return chatSessionStateForSession({ name: sessionName, hostId });
}

function sessionStateForNameHost(sessionName, hostId) {
  return state.sessions.find((session) => session.name === sessionName && session.hostId === hostId) || null;
}

function canonicalChatSessionStateForNameHost(sessionName, hostId) {
  if (!sessionName) return null;
  const streamHost = streamHostForHostId(hostId);
  return (state.chatStream.sessions || []).find((session) => {
    if (session?.host !== streamHost) return false;
    return [session.stream_id, session.session_name, session.session_id, session.name]
      .some((identifier) => identifier === sessionName);
  }) || null;
}

function isProtectedAssistantNameHost(sessionName, hostId) {
  return isProtectedAssistantSession(canonicalChatSessionStateForNameHost(sessionName, hostId))
    || isProtectedAssistantSession(sessionStateForNameHost(sessionName, hostId));
}

function syncSlotAssistantControls(slot) {
  const header = document.getElementById(`header-${slot}`);
  if (!header) return;
  const session = state.slots[slot];
  const protectedAssistant = !!session && isProtectedAssistantNameHost(session.name, session.hostId);
  header.querySelectorAll('.cell-trash, .cell-edit').forEach((control) => {
    control.hidden = protectedAssistant;
    control.disabled = protectedAssistant;
    control.setAttribute('aria-hidden', protectedAssistant ? 'true' : 'false');
  });
}

function isCodexHelperPromptText(text) {
  const normalized = normalizeTranscript(text).toLowerCase();
  if (!normalized) return false;
  return (
    normalized === 'run /review on my current changes.' ||
    normalized === 'run /review on my current changes' ||
    normalized === 'summarize recent commits' ||
    (/^(summarize|explore|inspect|review|find|run)\b.+/.test(normalized) && normalized.split(/\s+/).length <= 8)
  );
}

function isSystemHelperPrompt(event) {
  if (!event) return false;
  if (String(event.provider || '').toLowerCase() !== 'codex') return false;
  if (String(event.kind || '').toUpperCase() !== 'USER') return false;
  const text = normalizeTranscript(event.text).toLowerCase();
  if (!text) return false;
  return isCodexHelperPromptText(text);
}

function displayChatEvents(session) {
  return chatEventsForSession(session).filter((event) => event.kind !== 'DRAFT' && !isSystemHelperPrompt(event));
}

function formatEventTime(timestamp) {
  if (!timestamp) return '';
  try {
    return new Date(timestamp).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  } catch {
    return '';
  }
}

const CHAT_ATTACHMENT_MIMES = new Set(['image/jpeg', 'image/png']);

function chatAttachmentLimit() {
  const value = Number(window.PentacleChatCore?.MAX_CHAT_ATTACHMENTS);
  return Number.isFinite(value) && value > 0 ? value : 5;
}

function slotAttachmentDrafts(slot) {
  if (!Array.isArray(state.slotAttachments[slot])) state.slotAttachments[slot] = [];
  return state.slotAttachments[slot];
}

function nextAttachmentDraftId(slot) {
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    return `att-${window.crypto.randomUUID()}`;
  }
  return `att-${slot}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function readFileAsDataBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const value = String(reader.result || '');
      resolve(value.includes(',') ? value.split(',').pop() : value);
    };
    reader.onerror = () => reject(reader.error || new Error('Failed to read image.'));
    reader.readAsDataURL(file);
  });
}

function readImageDimensions(previewUrl) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve({ width: img.naturalWidth || undefined, height: img.naturalHeight || undefined });
    img.onerror = () => resolve({});
    img.src = previewUrl;
  });
}

async function addSlotAttachmentFiles(slot, files) {
  const allFiles = Array.from(files || []);
  const drafts = slotAttachmentDrafts(slot);
  const limit = chatAttachmentLimit();
  const accepted = allFiles.filter((file) => file && CHAT_ATTACHMENT_MIMES.has(String(file.type || '')));
  if (accepted.length !== allFiles.length) {
    setSlotSendError(slot, 'Only JPEG and PNG image attachments are supported.');
  }
  for (const file of accepted) {
    if (drafts.length >= limit) {
      setSlotSendError(slot, `At most ${limit} images can be attached.`);
      break;
    }
    const previewUrl = URL.createObjectURL(file);
    const [dataBase64, dims] = await Promise.all([
      readFileAsDataBase64(file),
      readImageDimensions(previewUrl),
    ]);
    drafts.push({
      id: nextAttachmentDraftId(slot),
      name: file.name || 'image',
      mime: file.type,
      bytes: file.size,
      dataBase64,
      previewUrl,
      width: dims.width,
      height: dims.height,
    });
  }
  renderSlotAttachmentTray(slot);
  updateSendControls(slot);
}

function removeSlotAttachment(slot, id) {
  const drafts = slotAttachmentDrafts(slot);
  const idx = drafts.findIndex((item) => item.id === id);
  if (idx < 0) return;
  const [removed] = drafts.splice(idx, 1);
  if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
  renderSlotAttachmentTray(slot);
  updateSendControls(slot);
}

function clearSlotAttachments(slot) {
  const drafts = slotAttachmentDrafts(slot);
  for (const item of drafts) {
    if (item?.previewUrl && !item.sent) URL.revokeObjectURL(item.previewUrl);
  }
  state.slotAttachments[slot] = [];
  renderSlotAttachmentTray(slot);
}

function renderSlotAttachmentTray(slot) {
  const refs = state.slotChatRefs[slot];
  if (!refs?.attachmentTrayEl) return;
  const drafts = slotAttachmentDrafts(slot);
  refs.attachmentTrayEl.innerHTML = '';
  refs.attachmentTrayEl.style.display = drafts.length ? '' : 'none';
  for (const item of drafts) {
    const card = document.createElement('div');
    card.className = 'slot-chat-attachment-chip';
    const img = document.createElement('img');
    img.src = item.previewUrl;
    img.alt = item.name || 'Attached image';
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'slot-chat-attachment-remove';
    remove.dataset.attachmentId = item.id;
    remove.title = 'Remove attachment';
    remove.textContent = '×';
    card.appendChild(img);
    card.appendChild(remove);
    refs.attachmentTrayEl.appendChild(card);
  }
}

async function uploadSlotAttachments(slot) {
  const drafts = slotAttachmentDrafts(slot);
  const uploaded = [];
  for (const item of drafts) {
    const result = await window.cc.chatUploadBlob({
      dataBase64: item.dataBase64,
      sizeHintBytes: item.bytes,
    });
    if (!result?.ok || !result.blob_sha) {
      throw new Error(result?.error || 'Image upload failed.');
    }
    const key = String(result.blob_sha).toLowerCase();
    uploaded.push({
      key,
      mime: item.mime,
      ...(Number.isFinite(Number(item.width)) ? { width: Number(item.width) } : {}),
      ...(Number.isFinite(Number(item.height)) ? { height: Number(item.height) } : {}),
      ...(Number.isFinite(Number(item.bytes)) ? { bytes: Number(item.bytes) } : {}),
      sha256: key,
      uri: item.previewUrl,
      name: item.name,
    });
    item.sent = true;
  }
  return uploaded;
}

function openChatImageViewer(src, alt) {
  if (!src) return;
  let viewer = document.querySelector('.slot-chat-image-viewer');
  if (!viewer) {
    viewer = document.createElement('div');
    viewer.className = 'slot-chat-image-viewer';
    viewer.innerHTML = '<button type="button" class="slot-chat-image-viewer-close" aria-label="Close image viewer">×</button><img class="slot-chat-image-viewer-img" alt="">';
    viewer.addEventListener('click', (event) => {
      if (event.target === viewer || event.target.closest('.slot-chat-image-viewer-close')) {
        viewer.classList.remove('is-open');
      }
    });
    document.body.appendChild(viewer);
  }
  const img = viewer.querySelector('.slot-chat-image-viewer-img');
  img.src = src;
  img.alt = alt || 'Image attachment';
  viewer.classList.add('is-open');
}

async function hydrateChatAttachmentMedia(root) {
  if (!root || !window.cc || typeof window.cc.chatFetchBlob !== 'function') return;
  const imgs = Array.from(root.querySelectorAll('.slot-chat-media-img[data-needs-blob="1"]'));
  for (const img of imgs) {
    const button = img.closest('.slot-chat-media-button');
    const key = button?.dataset.attachmentKey || '';
    const mime = button?.dataset.attachmentMime || 'image/png';
    if (!key || state.chatBlobObjectUrls[key]) {
      if (state.chatBlobObjectUrls[key]) {
        img.src = state.chatBlobObjectUrls[key];
        img.removeAttribute('data-needs-blob');
        if (button) button.dataset.viewerSrc = state.chatBlobObjectUrls[key];
      }
      continue;
    }
    img.removeAttribute('data-needs-blob');
    try {
      const result = await window.cc.chatFetchBlob(key);
      if (!result?.ok || !result.content_b64) throw new Error(result?.error || 'Image fetch failed.');
      const binary = atob(String(result.content_b64));
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
      const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
      state.chatBlobObjectUrls[key] = url;
      img.src = url;
      if (button) button.dataset.viewerSrc = url;
      button?.querySelector('.slot-chat-media-loading')?.remove();
    } catch (error) {
      button?.classList.add('is-error');
      const loading = button?.querySelector('.slot-chat-media-loading');
      if (loading) loading.textContent = 'Image unavailable';
    }
  }
}

function ensureSlotChatSurface(slot) {
  const container = document.getElementById(`term-${slot}`);
  if (!container) return null;
  if (state.slotChatRefs[slot]) return state.slotChatRefs[slot];

  container.innerHTML = '';
  container.style.position = 'relative';

  const shell = document.createElement('div');
  shell.className = 'slot-shell';
  shell.style.cssText = 'position:relative;width:100%;height:100%;';

  const terminalMount = document.createElement('div');
  terminalMount.className = 'slot-terminal-layer';
  terminalMount.style.cssText = 'position:absolute;inset:0;';

  // The `.cosmic` class opts THIS chat surface (and only this subtree) into the
  // scoped cosmic theme token layer (renderer/cosmic_theme.css +
  // cosmic_chat_surface.css). Non-chat screens never get `.cosmic`, so they keep
  // the existing --bg/--fg look. The cosmic background lives in the scoped CSS
  // (`.slot-chat-layer.cosmic`) rather than inline so the starfield shows through.
  const chatMount = document.createElement('div');
  chatMount.className = 'slot-chat-layer cosmic';
  chatMount.style.cssText = 'position:absolute;inset:0;display:flex;flex-direction:column;overflow:hidden;';

  const assetMount = document.createElement('div');
  assetMount.className = 'slot-asset-layer cosmic';
  assetMount.style.cssText = 'position:absolute;inset:0;display:none;flex-direction:column;overflow:auto;';

  // Full slot-scoped status view layer (spec item 3/4). Sibling of the asset
  // layer; shown when the slot's view mode is 'status'.
  const statusMount = document.createElement('div');
  statusMount.className = 'slot-status-layer cosmic';
  statusMount.style.cssText = 'position:absolute;inset:0;display:none;flex-direction:column;overflow:auto;';

  const chatShell = document.createElement('div');
  chatShell.className = 'slot-chat-shell';

  const scrollEl = document.createElement('div');
  scrollEl.className = 'slot-chat-scroll';

  const listEl = document.createElement('div');
  listEl.className = 'slot-chat-list';
	  listEl.addEventListener('click', async (event) => {
	    const mediaButton = event.target?.closest?.('.slot-chat-media-button');
	    if (mediaButton && listEl.contains(mediaButton)) {
	      event.preventDefault();
	      event.stopPropagation();
	      const src = mediaButton.dataset.viewerSrc || mediaButton.querySelector('img')?.src || '';
	      openChatImageViewer(src, mediaButton.getAttribute('aria-label') || 'Image attachment');
	      return;
	    }
	    const button = event.target?.closest?.('.slot-chat-copy-btn');
	    if (!button || !listEl.contains(button)) return;
    event.preventDefault();
    event.stopPropagation();
    try {
      await writeChatCopyText(button.dataset.copyText || '');
      markCopyButtonCopied(button);
    } catch (error) {
      console.warn('[chat] copy failed:', error);
    }
  });
  // Bounded-history paging affordance (session-reliability parity): reveals up
  // to 48 older held rows per click, anchored so the reading position is kept.
  // Hidden until the store holds older rows above the window (remainingCount>0).
  const loadEarlierEl = document.createElement('button');
  loadEarlierEl.type = 'button';
  loadEarlierEl.className = 'slot-chat-load-earlier';
  loadEarlierEl.dataset.slot = String(slot);
  loadEarlierEl.style.display = 'none';
  loadEarlierEl.textContent = 'Load earlier messages';
  loadEarlierEl.addEventListener('click', () => loadEarlierChat(slot));
  scrollEl.appendChild(loadEarlierEl);
  scrollEl.appendChild(listEl);

  const draftPreviewEl = document.createElement('div');
  draftPreviewEl.className = 'slot-chat-draft-preview-host';

  const errorEl = document.createElement('div');
  errorEl.className = 'slot-chat-error';
  errorEl.style.display = 'none';

  // Agent-asked question card (claude AskUserQuestion). Rendered just above the
  // composer when the daemon reports a pending question on this stream; empty +
  // hidden otherwise (zero DOM/behavior change when no question is present).
  const questionEl = document.createElement('div');
  questionEl.className = 'slot-chat-question';
  questionEl.style.display = 'none';

	  const composeEl = document.createElement('div');
	  composeEl.className = 'slot-chat-compose';

  const statusEl = document.createElement('div');
  statusEl.className = 'slot-chat-status';

	  const inputEl = document.createElement('textarea');
	  inputEl.className = 'slot-chat-compose-input';
  inputEl.rows = 1;
  inputEl.placeholder = '';
  inputEl.dataset.slot = String(slot);

	  const attachmentTrayEl = document.createElement('div');
	  attachmentTrayEl.className = 'slot-chat-attachment-tray';
	  attachmentTrayEl.style.display = 'none';

	  const fileInputEl = document.createElement('input');
	  fileInputEl.type = 'file';
	  fileInputEl.accept = 'image/jpeg,image/png';
	  fileInputEl.multiple = true;
	  fileInputEl.className = 'slot-chat-attachment-input';
	  fileInputEl.dataset.slot = String(slot);

	  const attachEl = document.createElement('button');
	  attachEl.type = 'button';
	  attachEl.className = 'slot-chat-attach';
	  attachEl.dataset.slot = String(slot);
	  attachEl.title = 'Attach image';
	  attachEl.innerHTML = '<span aria-hidden="true">＋</span>';

	  const sendEl = document.createElement('button');
  sendEl.className = 'slot-chat-compose-send';
  sendEl.dataset.slot = String(slot);
  sendEl.title = 'Send';
  sendEl.innerHTML = '<span aria-hidden="true">↑</span>';

	  composeEl.appendChild(fileInputEl);
	  composeEl.appendChild(attachEl);
	  composeEl.appendChild(inputEl);
	  composeEl.appendChild(sendEl);
	  chatShell.appendChild(scrollEl);

  // Jump-to-latest pill (session-reliability parity): shown while the reader is
  // scrolled up; its label carries the unread count. Click pins + clears unread.
  const jumpPillEl = document.createElement('button');
  jumpPillEl.type = 'button';
  jumpPillEl.className = 'slot-chat-jump-latest';
  jumpPillEl.dataset.slot = String(slot);
  jumpPillEl.style.display = 'none';
  jumpPillEl.addEventListener('click', () => jumpToLatestChat(slot));
  chatShell.appendChild(jumpPillEl);

  // Pinned/unread follows the scroll position: returning to the bottom pins +
  // clears unread; scrolling up unpins. DOM-only pill sync per scroll event (no
  // full transcript re-render).
  scrollEl.addEventListener('scroll', () => onSlotChatScroll(slot), { passive: true });

	  chatShell.appendChild(statusEl);
	  chatShell.appendChild(draftPreviewEl);
	  chatShell.appendChild(errorEl);
	  chatShell.appendChild(questionEl);
	  chatShell.appendChild(attachmentTrayEl);
	  chatShell.appendChild(composeEl);
	  // Session Status card view — a toggle-pane that replaces the transcript/
	  // composer when the header glyph is open (spec_pentacle__status_card_ui_desktop).
	  const cardViewEl = document.createElement('div');
	  cardViewEl.className = 'slot-chat-status-card-view';
	  cardViewEl.addEventListener('click', (event) => {
	    if (event.target?.closest?.('.session-status-close')) toggleSlotStatusCard(slot);
	  });
	  chatShell.appendChild(cardViewEl);
  // Subtle deterministic starfield behind the transcript (cosmic theme). Purely
  // decorative: absolute + pointer-events:none, mounted once and never touched by
  // the per-render path, so it adds zero behavior. Appended BEFORE chatShell so
  // the transcript paints on top. Degrades to no starfield when the cosmic bundle
  // is absent (e.g. a non-renderer environment).
  if (window.PentacleCosmic && typeof window.PentacleCosmic.starfield === 'function') {
    try {
      const field = window.PentacleCosmic.starfield();
      field.classList.add('slot-chat-starfield');
      chatMount.appendChild(field);
    } catch (_) { /* styling only — never block the chat surface on a theme error */ }
  }
  chatMount.appendChild(chatShell);

  const autoSize = () => {
    inputEl.style.height = '';
    const h = Math.min(inputEl.scrollHeight, 120);
    inputEl.style.height = `${h}px`;
    inputEl.style.overflowY = inputEl.scrollHeight > 120 ? 'auto' : 'hidden';
  };
	  inputEl.addEventListener('input', () => {
	    state.slotDrafts[slot] = inputEl.value;
	    state.slotDraftTouched[slot] = true;
	    autoSize();
	    updateSendControls(slot);
	  });
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendChatComposer(slot);
    } else if (e.key === 'Escape') {
      // B3 (chat_send_turn_lifecycle_batch2): ESC cancels the latest in-flight
      // turn (interrupts the running agent — the ESC-in-terminal equivalent).
      if (cancelChatComposer(slot)) e.preventDefault();
    }
	  });
	  attachmentTrayEl.addEventListener('click', (event) => {
	    const remove = event.target?.closest?.('.slot-chat-attachment-remove');
	    if (!remove || !attachmentTrayEl.contains(remove)) return;
	    removeSlotAttachment(slot, remove.dataset.attachmentId || '');
	  });
	  attachEl.addEventListener('click', () => fileInputEl.click());
	  fileInputEl.addEventListener('change', () => {
	    addSlotAttachmentFiles(slot, fileInputEl.files).catch((error) => {
	      setSlotSendError(slot, error instanceof Error ? error.message : String(error || 'Attachment failed.'));
	    }).finally(() => {
	      fileInputEl.value = '';
	    });
	  });
	  composeEl.addEventListener('dragover', (event) => {
	    if (!event.dataTransfer?.files?.length) return;
	    event.preventDefault();
	    composeEl.classList.add('is-drag-over');
	  });
	  composeEl.addEventListener('dragleave', () => {
	    composeEl.classList.remove('is-drag-over');
	  });
	  composeEl.addEventListener('drop', (event) => {
	    if (!event.dataTransfer?.files?.length) return;
	    event.preventDefault();
	    composeEl.classList.remove('is-drag-over');
	    addSlotAttachmentFiles(slot, event.dataTransfer.files).catch((error) => {
	      setSlotSendError(slot, error instanceof Error ? error.message : String(error || 'Attachment failed.'));
	    });
	  });
	  sendEl.addEventListener('click', () => sendChatComposer(slot));
	  autoSize();

  shell.appendChild(terminalMount);
  shell.appendChild(chatMount);
  shell.appendChild(assetMount);
  shell.appendChild(statusMount);
  container.appendChild(shell);

	  state.slotChatRefs[slot] = { shell, chatShell, terminalMount, chatMount, assetMount, statusMount, scrollEl, listEl, loadEarlierEl, jumpPillEl, statusEl, draftPreviewEl, errorEl, questionEl, attachmentTrayEl, composeEl, cardViewEl, fileInputEl, attachEl, inputEl, sendEl };
  return state.slotChatRefs[slot];
}

function setSlotSendError(slot, message) {
  state.slotSendErrors[slot] = String(message || '');
  const refs = state.slotChatRefs[slot];
  if (refs?.errorEl) {
    refs.errorEl.textContent = state.slotSendErrors[slot];
    refs.errorEl.style.display = state.slotSendErrors[slot] ? 'block' : 'none';
  }
}

function updateSendControls(slot) {
  // B4 (chat_send_turn_lifecycle_batch2): the composer stays ENABLED while a
  // turn is in flight so the user can queue a follow-up message (sendTurn
  // records it as 'queued' and flushes it when the stream returns to idle).
  // Keep drafts editable while websocket detail is arriving, but do not offer
  // Send before the same control target used by sendChatComposer is available.
  const pending = !!state.slotSendPending[slot];
  const target = chatControlTargetForSlot(slot);
  const refs = state.slotChatRefs[slot];
  if (refs?.sendEl) refs.sendEl.disabled = pending || !target || !!target.error;
  if (refs?.inputEl) refs.inputEl.disabled = pending;
  if (refs?.attachEl) refs.attachEl.disabled = pending;
}

function maybeRestoreReturnedToPromptDraft(slot, streamId) {
  if (!streamId || !window.PentacleChatStore || typeof window.PentacleChatStore.getReturnedToPromptDraft !== 'function') return;
  const draft = window.PentacleChatStore.getReturnedToPromptDraft(streamId);
  if (!draft?.optimisticId || !draft.text) return;
  const key = `${streamId}:${draft.optimisticId}`;
  if (state.returnedPromptDrafts[key]) return;
  const refs = state.slotChatRefs[slot];
  const current = refs?.inputEl ? refs.inputEl.value : state.slotDrafts[slot];
  if (!String(current || '').trim() || current === draft.text) {
    state.slotDrafts[slot] = draft.text;
    state.slotDraftTouched[slot] = true;
    if (refs?.inputEl) {
      refs.inputEl.value = draft.text;
      refs.inputEl.style.height = '';
      const h = Math.min(refs.inputEl.scrollHeight, 120);
      refs.inputEl.style.height = `${h}px`;
      refs.inputEl.style.overflowY = refs.inputEl.scrollHeight > 120 ? 'auto' : 'hidden';
    }
    setSlotSendError(slot, '');
    state.returnedPromptDrafts[key] = 'restored';
    return;
  }
  state.returnedPromptDrafts[key] = 'skipped';
  setSlotSendError(slot, 'Stopped message returned to prompt; current draft kept.');
}

function chatControlTargetForSlot(slot) {
  const session = state.slots[slot];
  if (!session || state.botSlots[slot]) return null;
  if (!state.chatStream.connected) {
    return { error: 'Chat stream offline.' };
  }
  const streamSession = chatSessionStateForSession(session);
  if (!streamSession?.stream_id) {
    return { error: 'Waiting for websocket session detail.' };
  }
  return {
    hostId: session.hostId || (IS_CLIENT ? 'remote' : 'local'),
    sessionName: session.name,
    streamSession,
  };
}

function activeSelectionInside(el) {
  if (!el || typeof window === 'undefined' || !window.getSelection) return false;
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed) return false;
  const contains = (node) => {
    if (!node) return false;
    return el === node || el.contains(node.nodeType === Node.ELEMENT_NODE ? node : node.parentNode);
  };
  return contains(selection.anchorNode) || contains(selection.focusNode);
}

function maybeApplyDeferredSlotChatRenders() {
  for (let slot = 0; slot < state.slotChatPendingListRender.length; slot += 1) {
    const pending = state.slotChatPendingListRender[slot];
    if (!pending) continue;
    const refs = state.slotChatRefs[slot];
    if (!refs?.listEl) {
      state.slotChatPendingListRender[slot] = null;
      continue;
    }
    if (activeSelectionInside(refs.listEl)) continue;
    state.slotChatPendingListRender[slot] = null;
    applySlotChatListRender(slot, refs, pending);
    window.PentacleHarness?.emit?.('chat:slot_deferred_applied', {
      slot,
      streamId: pending.boundStreamId || undefined,
      data: {
        paintedStreamId: pending.paintedStreamId,
        resolvedStreamId: pending.resolvedStreamId,
      },
    });
  }
}

function scrollSlotChatToBottom(refs) {
  if (!refs?.scrollEl) return;
  const scheduledScrollTop = refs.scrollEl.scrollTop;
  requestAnimationFrame(() => {
    // A view refresh may queue this before the reader scrolls or loads history.
    // Preserve that newer position instead of applying the stale bottom follow.
    if (Math.abs(refs.scrollEl.scrollTop - scheduledScrollTop) > 1) return;
    refs.scrollEl.scrollTop = refs.scrollEl.scrollHeight;
  });
}

// Reveal up to 48 older held rows for this slot (bounded by what the store
// holds), capturing the pre-growth scroll geometry so the following render
// anchors the reading position. No-op when there is no earlier history.
function loadEarlierChat(slot) {
  const refs = state.slotChatRefs[slot];
  const rel = ensureSlotReliability(slot);
  const reliability = window.PentacleChatReliability;
  if (!refs || !rel || !reliability) return;
  const streamId = state.slotChatBoundStream[slot];
  if (!streamId) return;
  const detail = selectSlotSessionDetail(streamId, showTurnDurationEnabled(), false, rel.visibleCount);
  const remaining = detail ? (detail.remainingCount || 0) : 0;
  if (!reliability.hasEarlierHistory(remaining)) return;
  state.slotChatPendingPrepend[slot] = {
    prevScrollHeight: refs.scrollEl ? refs.scrollEl.scrollHeight : 0,
    prevScrollTop: refs.scrollEl ? refs.scrollEl.scrollTop : 0,
  };
  rel.visibleCount = reliability.visibleCountForEarlierPage(rel, remaining);
  window.PentacleHarness?.emit?.('chat:history_page', {
    slot,
    streamId,
    data: { visibleCount: rel.visibleCount, revealed: reliability.earlierPageSize(remaining) },
  });
  renderSlotChat(slot);
}

// The row ids currently selectable for a slot's window (used to re-baseline the
// unread watermark on jump / reaching bottom).
function slotChatRowIds(slot) {
  const rel = state.slotReliability[slot];
  const streamId = state.slotChatBoundStream[slot];
  if (!streamId) return [];
  const detail = selectSlotSessionDetail(streamId, showTurnDurationEnabled(), false, rel ? rel.visibleCount : 120);
  return detail ? (detail.transcriptItems || []).map((it) => it && it.id).filter(Boolean) : [];
}

// Show/hide the jump-to-latest pill from a slot's reliability state: hidden while
// pinned; while unpinned it shows the unread count (or a bare arrow at 0).
function updateSlotJumpPill(slot, rel) {
  const refs = state.slotChatRefs[slot];
  if (!refs || !refs.jumpPillEl) return;
  if (!rel || rel.pinnedToBottom) {
    refs.jumpPillEl.style.display = 'none';
    return;
  }
  const unread = rel.unreadCount || 0;
  refs.jumpPillEl.textContent = unread > 0 ? `${unread} new message${unread === 1 ? '' : 's'} ↓` : '↓';
  refs.jumpPillEl.setAttribute('aria-label', unread > 0 ? `Jump to latest, ${unread} unread` : 'Jump to latest');
  refs.jumpPillEl.style.display = '';
}

// Scroll-driven pinned/unread sync (DOM-only, no transcript re-render): returning
// to the bottom pins + clears unread; leaving the bottom unpins.
function onSlotChatScroll(slot) {
  const refs = state.slotChatRefs[slot];
  const rel = state.slotReliability[slot];
  const R = window.PentacleChatReliability;
  if (!refs || !refs.scrollEl || !rel || !R) return;
  const nearBottom = R.isNearBottom({
    scrollTop: refs.scrollEl.scrollTop,
    scrollHeight: refs.scrollEl.scrollHeight,
    clientHeight: refs.scrollEl.clientHeight,
  });
  if (nearBottom) {
    if (!rel.pinnedToBottom || rel.unreadCount) {
      state.slotReliability[slot] = R.markCaughtUp(rel, slotChatRowIds(slot));
      window.PentacleHarness?.emit?.('chat:unread_cleared', {
        slot, streamId: state.slotChatBoundStream[slot] || undefined, data: { via: 'scroll' },
      });
    }
    updateSlotJumpPill(slot, state.slotReliability[slot]);
  } else if (rel.pinnedToBottom) {
    state.slotReliability[slot] = { ...rel, pinnedToBottom: false };
    updateSlotJumpPill(slot, state.slotReliability[slot]);
  }
}

// Jump-to-latest pill click: pin, clear unread, scroll to the tail, re-render.
function jumpToLatestChat(slot) {
  const refs = state.slotChatRefs[slot];
  const rel = ensureSlotReliability(slot);
  const R = window.PentacleChatReliability;
  if (!refs || !rel || !R) return;
  state.slotReliability[slot] = R.markCaughtUp(rel, slotChatRowIds(slot));
  updateSlotJumpPill(slot, state.slotReliability[slot]);
  window.PentacleHarness?.emit?.('chat:unread_cleared', {
    slot, streamId: state.slotChatBoundStream[slot] || undefined, data: { via: 'jump' },
  });
  scrollSlotChatToBottom(refs);
  renderSlotChat(slot);
}

function applySlotChatListRender(slot, refs, render) {
  if (!refs?.listEl || !render) return;
  if (window.PentacleChatView?.replaceTranscriptHtml) window.PentacleChatView.replaceTranscriptHtml(refs.listEl, render.html);
  else refs.listEl.innerHTML = render.html;
  refs.listEl.querySelector('.slot-chat-history-retry')?.addEventListener('click', () => {
    ensureChatEventsLoaded(render.paintedStreamId, true);
    renderSlotChat(slot);
  });
  refs.listEl.dataset.streamId = render.paintedStreamId || '';
  state.slotChatLastListHtml[slot] = render.cacheKey;
  // Explicit retry belongs to the transcript surface: it keeps the failed or
  // indeterminate optimistic row visible, rotates only its request id, and lets
  // the store re-dispatch its original text/attachments through the usual bridge.
  refs.listEl.querySelectorAll('.slot-chat-send-retry').forEach((button) => {
    button.addEventListener('click', () => {
      const optimisticId = button.dataset.optimisticId || '';
      if (!optimisticId || !window.PentacleChatStore?.retryOptimisticSend) return;
      if (window.PentacleChatStore.retryOptimisticSend(optimisticId)) {
        window.PentacleHarness?.emit?.('chat:explicit_retry', { slot, streamId: render.paintedStreamId || undefined, data: { optimisticId } });
        renderSlotChat(slot);
      }
    });
  });

  // Commit the binding ONLY when this session's own stream was actually painted,
  // so a leaked/empty frame leaves the previous binding in place and the leak
  // guard keeps suppressing the old stream until this session's stream resolves.
  if (render.detailPresent) {
    state.slotChatBoundSession[slot] = render.sessionKey;
    state.slotChatBoundStream[slot] = render.paintedStreamId;
  }

  // Render-time beacon (armed-telemetry seam): fires on EVERY paint, including
  // the cleared/empty path, so the walk's assertNoBeacon negative window works.
  window.PentacleHarness?.emit?.('chat:slot_painted', {
    slot,
    streamId: render.boundStreamId || undefined,
    data: {
      boundStreamId: render.boundStreamId,
      paintedStreamId: render.paintedStreamId,
      clearedBeforePaint: render.clearedBeforePaint,
      resolvedStreamId: render.resolvedStreamId,
      leaked: render.leaked,
      // Content digest (schema slot_painted.contentDigest@1, defect 2): what
      // actually rendered at this DOM commit. See computeSlotChatContentDigest.
      rowCount: render.contentDigest ? render.contentDigest.rowCount : 0,
      kinds: render.contentDigest ? render.contentDigest.kinds : {},
      lastRowDigest: render.contentDigest ? render.contentDigest.lastRowDigest : null,
    },
  });

  // Prepend anchoring: a load-earlier render added older rows at the top and
  // grew the content; keep the reading position by pushing scrollTop down by the
  // exact height gained. Otherwise honor sticky-bottom follow. (A pending prepend
  // and sticky-bottom are mutually exclusive — load-earlier only fires while
  // scrolled up.)
  const pendingPrepend = state.slotChatPendingPrepend[slot];
  if (pendingPrepend && refs.scrollEl && window.PentacleChatReliability) {
    refs.scrollEl.scrollTop = window.PentacleChatReliability.anchorScrollTopAfterPrepend(
      pendingPrepend.prevScrollHeight,
      pendingPrepend.prevScrollTop,
      refs.scrollEl.scrollHeight,
    );
    state.slotChatPendingPrepend[slot] = null;
  } else if (render.shouldStick) {
    scrollSlotChatToBottom(refs);
    window.PentacleHarness?.emit?.('chat:bottom_follow', {
      slot, streamId: render.paintedStreamId || undefined,
    });
  }
  hydrateChatAttachmentMedia(refs.listEl);
  syncActivitySpinnerPhase(document);
}

async function writeChatCopyText(text) {
  const value = String(text || '');
  if (window.cc && typeof window.cc.writeClipboard === 'function') {
    await Promise.resolve(window.cc.writeClipboard(value));
    return true;
  }
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    await navigator.clipboard.writeText(value);
    return true;
  }
  return false;
}

function markCopyButtonCopied(button) {
  if (!button) return;
  const original = button.dataset.copyLabel || button.textContent || 'Copy';
  button.dataset.copyLabel = original;
  button.textContent = 'Copied';
  button.classList.add('is-copied');
  if (button._slotChatCopyTimer) clearTimeout(button._slotChatCopyTimer);
  button._slotChatCopyTimer = setTimeout(() => {
    button.textContent = button.dataset.copyLabel || 'Copy';
    button.classList.remove('is-copied');
    button._slotChatCopyTimer = null;
  }, 1000);
}

document.addEventListener('selectionchange', maybeApplyDeferredSlotChatRenders);
document.addEventListener('mouseup', maybeApplyDeferredSlotChatRenders);

function renderSlotChat(slot) {
  const refs = state.slotChatRefs[slot];
  const session = state.slots[slot];
  if (!refs || !session || state.botSlots[slot]) return;

  // Restore any Session Status overlay BEFORE this render recomputes body
  // visibility, so the normal render always works on clean state and never
  // leaves a subregion stuck hidden. applySlotStatusCardView re-applies at the
  // end if the card is still open (capturing the fresh normal displays).
  clearStatusCardOverlay(slot);

  const streamHost = streamHostForHostId(session.hostId);
  const remoteDraftState = chatDraftStateForSession(session);
  const remoteSessionState = chatSessionStateForSession(session);
  // Provider labels are projections of the same per-session status summary
  // used by this render. This also refreshes a tag created before the first
  // canonical stream snapshot arrived; never derive it from selected/default
  // provider state.
  updateSlotProviderTag(slot);
  const pendingPeerMessages = pendingPeerMessagesForSession(session);
  updateSlotPendingPeerBadge(slot, pendingPeerMessages);
  // Shared-core transcript path (desktop_chat_ui_mobile_parity): source the
  // session detail from the shared pentacle-chat-core store via this desktop
  // session's resolved stream id. The store's selectSessionDetail is the SINGLE
  // interpretation/coalesce/select implementation (shared verbatim with mobile).
  const showTurnDuration = showTurnDurationEnabled();
  let streamId = remoteSessionState?.stream_id || null;
  // `detail` is selected AFTER the `chatMode` gate below (render-telemetry QA
  // layer, defect 1): the shared selector's per-row `chat:event_rendered` beacon
  // must fire ONLY on the real chat-render path, never for terminal-view frames
  // (which previously called this selector once per frame before the gate and
  // re-emitted "rendered" for content the user could not see).
  let detail = null;
  const chrome = chatUi.hostChrome(_streamHostToHostId(remoteSessionState?.host || streamHost) || streamHost, CONFIG, HOST_IDS);
  const remoteDraft = isCodexHelperPromptText(remoteDraftState?.text || '') ? '' : (remoteDraftState?.text || '');
  const remotePending = !!remoteDraftState?.raw?.pending;
  const remoteWorking = !!remoteDraftState?.raw?.working;
  const remoteWorkingLabel = String(remoteDraftState?.raw?.working_label || '');
  if (!remoteDraft && !state.slotDrafts[slot]) {
    state.slotDraftTouched[slot] = false;
  }
  const snapshotWorking = !!remoteSessionState?.working;
  let activity = (remoteWorking || snapshotWorking || hasRecentWorkingEvent(session))
    ? 'working'
    : (findSession(session.name, session.hostId)?.working ? 'working' : 'idle');
  const workingLabel = remoteWorkingLabel || recentWorkingLabel(session) || chatUi.extractWorkingTime(remoteSessionState?.last_text || '');
  const mode = state.slotViewModes[slot];
  const chatMode = mode === 'chat';
  const assetMode = mode === 'asset';
  const statusMode = mode === 'status';
  refs.terminalMount.style.display = (chatMode || assetMode || statusMode) ? 'none' : '';
  refs.chatMount.style.display = chatMode ? 'flex' : 'none';
  if (refs.assetMount) refs.assetMount.style.display = assetMode ? 'flex' : 'none';
  if (refs.statusMount) refs.statusMount.style.display = statusMode ? 'flex' : 'none';
  if (assetMode) {
    updateSlotStatusGlyph(slot, null); // no Session Status glyph outside chat view
    renderSlotAsset(slot);
    return;
  }
  if (statusMode) {
    updateSlotStatusGlyph(slot, null); // no Session Status glyph outside chat view
    renderSlotStatus(slot, remoteSessionState);
    return;
  }
  if (!chatMode) {
    updateSlotStatusGlyph(slot, null);
    return;
  }

  // Real chat-render path: select the transcript detail with render telemetry
  // ENABLED, so the shared selector emits `chat:event_rendered` exclusively here
  // (post-`chatMode`) — never for terminal-view frames or harness probes
  // (render-telemetry QA layer, defect 1).
  ensureSlotReliability(slot);
  // Reset the bounded paging window when this slot binds a DIFFERENT session, so
  // a switched-to chat mounts exactly the initial window (16), not the prior
  // session's grown one. boundSession commits only on a real paint, so this
  // fires until the new stream actually paints. (Uses the same session key the
  // flash-guard below recomputes.)
  const relSessionKey = `${session.hostId || ''}:${session.name || ''}`;
  if (state.slotChatBoundSession[slot] !== relSessionKey) {
    resetSlotReliability(slot);
  }
  const rel = state.slotReliability[slot];
  detail = selectSlotSessionDetail(streamId, showTurnDuration, true, rel ? rel.visibleCount : 120);

  // ── Bug1 (chat_ui_hardening_batch3) — new-chat flash guard ──────────────
  // Rule: a slot must NEVER paint a transcript belonging to a different stream
  // than the one it is currently bound to. Two protections:
  //   1. CLEAR refs.listEl (+ status/question sub-regions) BEFORE the first
  //      paint of a newly-bound stream, so no previous transcript can show for
  //      a frame (covers a render that runs before the new detail loads).
  //   2. SUPPRESS the transient wrong-stream resolution: the id-only fallback
  //      in findStreamSessionForDesktopSession can resolve a newly-bound
  //      desktop session to the stream the slot WAS showing (shared/blank name)
  //      for a frame while this session's own stream is not-yet-present. When
  //      the resolved stream equals the slot's previously-painted stream on a
  //      session change, paint EMPTY (not the stale transcript) until this
  //      session's own stream resolves.
  // `boundStreamId` (the strong host+id match for the slot's CURRENT desktop
  // session) is the authoritative intended stream and is reported in the
  // `chat:slot_painted` beacon so a regression that paints another stream is
  // caught deterministically; the loose-fallback `streamId` is reported as
  // `resolvedStreamId` for cause diagnosis.
  const sessionKey = `${session.hostId || ''}:${session.name || ''}`;
  const prevSessionKey = state.slotChatBoundSession[slot];
  const prevStreamId = state.slotChatBoundStream[slot];
  const sessionChanged = prevSessionKey !== sessionKey;
  const resolvedStreamId = streamId;
  const boundStreamId =
    chatUi.findStrongStreamSessionForDesktopSession(state.chatStream, session, streamHost)?.stream_id || null;
  // The leak: on a session change, a resolved stream identical to the stream
  // this slot last painted is the id-only fallback grabbing the OLD stream.
  // BUT only when it is NOT this session's OWN strong host+id match: two
  // distinct desktop sessions sharing host + a blank/shared session_name/
  // display_name/title both idMatch the same stream, so the NEW session's
  // genuine strong match can also equal prevStreamId — that is its real stream
  // and MUST paint (else detail stays null, the binding never commits,
  // sessionChanged stays true forever, and the slot is permanently blanked).
  // `resolvedStreamId !== boundStreamId` distinguishes the id-only leak (bound
  // null/different) from a legitimate same-stream rebind (bound === resolved).
  const leaked = sessionChanged
    && !!resolvedStreamId
    && resolvedStreamId === prevStreamId
    && resolvedStreamId !== boundStreamId;
  if (leaked) {
    streamId = null;
    detail = null;
  }
  if (detail?.status === 'unresponsive') {
    activity = 'unresponsive';
  }
  if (sessionChanged) {
    // Clear stale transcript + live sub-regions before the first paint of the
    // newly-bound stream. (attachSession also clears listEl on bind; this also
    // covers re-bind paths and the status/question regions.)
    refs.listEl.innerHTML = '';
    refs.listEl.dataset.streamId = '';
    state.slotChatLastListHtml[slot] = null;
    state.slotChatPendingListRender[slot] = null;
    if (refs.statusEl) refs.statusEl.innerHTML = '';
    if (refs.questionEl) {
      refs.questionEl.innerHTML = '';
      refs.questionEl.style.display = 'none';
    }
  }

  // The hello snapshot ships no events under events_mode:'summary'; fetch
  // this stream's recent ring on first chat render. The post-RPC snapshot
  // re-emit re-enters renderSlotChat and replaces the empty placeholder
  // with the transcript.
  if (detail?.streamId) {
    ensureChatEventsLoaded(detail.streamId);
  }

  const shouldStick = !refs.scrollEl || (refs.scrollEl.scrollHeight - refs.scrollEl.scrollTop - refs.scrollEl.clientHeight) < 32;

  // Load-earlier affordance: shown iff the store holds older rows above the
  // current bounded window (detail.remainingCount>0); reaching history start
  // hides it (final short page).
  if (refs.loadEarlierEl) {
    const remaining = detail ? (detail.remainingCount || 0) : 0;
    const showEarlier = !!(detail && window.PentacleChatReliability
      && window.PentacleChatReliability.hasEarlierHistory(remaining));
    refs.loadEarlierEl.style.display = showEarlier ? '' : 'none';
  }

  // Scope 2: reconcile pinned/unread for this render and drive the jump pill.
  if (rel && detail && window.PentacleChatReliability) {
    const R = window.PentacleChatReliability;
    const rowIds = (detail.transcriptItems || []).map((it) => it && it.id).filter(Boolean);
    const total = rowIds.length + (detail.remainingCount || 0);
    // While unpinned, grow the window to cover newly-appended tail rows so the
    // reading position stays put and the unread watermark stays in-window; the
    // grown window takes effect on the next render (new rows land below the fold).
    const prevTotal = state.slotChatPrevTotal[slot];
    if (!shouldStick && prevTotal != null && total > prevTotal) {
      rel.visibleCount += (total - prevTotal);
    }
    state.slotChatPrevTotal[slot] = total;
    const reconciled = R.reconcileOnRender(rel, rowIds, shouldStick);
    state.slotReliability[slot] = reconciled;
    if (reconciled.unreadCount > (rel.unreadCount || 0)) {
      window.PentacleHarness?.emit?.('chat:unread_accumulated', {
        slot, streamId: detail.streamId, data: { unread: reconciled.unreadCount },
      });
    }
    updateSlotJumpPill(slot, reconciled);
  } else if (refs.jumpPillEl) {
    refs.jumpPillEl.style.display = 'none';
  }

  refs.chatMount.style.setProperty('--machine', chrome.accent);
  refs.chatMount.style.setProperty('--machine-header', chrome.header);
  refs.chatMount.style.setProperty('--machine-surface', chrome.surface);
  refs.chatMount.style.setProperty('--machine-border', chrome.border);

  // Shared-core transcript render (desktop_chat_ui_mobile_parity): render the
  // transcript timeline HTML via window.PentacleChatView from the store-derived
  // `detail`. This is the ONLY transcript render path.
  const renderedTranscript = (detail && window.PentacleChatView
    ? window.PentacleChatView.renderTranscriptTimelineHtml(detail, chrome, {
      showTurnDuration,
      resolvedQuestions: resolvedQuestionsForStream(streamId),
    })
    : '') || '';
  const history = state.chatStream.historyLoads?.[streamId];
  const historyMessage = !state.chatStream.connected ? 'Reconnecting…'
    : history?.status === 'error' ? 'Messages could not be loaded.'
      : history?.status !== 'loaded' ? (renderedTranscript ? 'Syncing messages…' : 'Loading messages…') : '';
  const historyRetry = state.chatStream.connected && history?.status === 'error'
    ? '<button type="button" class="slot-chat-history-retry">Retry</button>' : '';
  const transcriptHtml = renderedTranscript
    ? `${historyMessage ? `<div class="slot-chat-history-state" role="status">${historyMessage}${historyRetry}</div>` : ''}${renderedTranscript}`
    : `<div class="slot-chat-empty" role="status">${historyMessage || 'No messages yet.'}${historyRetry}</div>`;

  // Cosmic theme (workstream D): arcane header ornaments for the ACTIVE machine —
  // the ArcaneRingFrame-wrapped MachineSigil, the Cinzel epithet, and the
  // provider + working/idle status tags. Styling-only: degrades to the plain hero
  // when the cosmic bundle is absent.
  // Optional decorative metadata is keyed by sigil name, independently of host identity.
  let cosmicSigilHtml = '';
  let cosmicEpithetHtml = '';
  let cosmicTagsHtml = '';
  const cosmic = window.PentacleCosmic;
  const sigil = hostPresentation.hostSigil(CONFIG, _streamHostToHostId(session.hostId) || session.hostId, HOST_IDS);
  const machineMeta = cosmic && cosmic.MACHINES ? cosmic.MACHINES[sigil] : null;
  if (cosmic && machineMeta) {
    try {
      cosmicSigilHtml = cosmic.arcaneRingFrame({ machine: sigil, size: 44, sigilSize: 27 }).outerHTML;
      cosmicEpithetHtml = `<span class="slot-chat-session-epithet cosmic-myth">${esc(machineMeta.epithet)}</span>`;
      const providerName = providerForSession(session.name, session.hostId);
      const tagsWrap = document.createElement('div');
      tagsWrap.className = 'slot-chat-session-tags';
      if (providerName === 'codex' || providerName === 'claude') {
        tagsWrap.appendChild(cosmic.providerTag(providerName, { color: machineMeta.accent }));
      }
      tagsWrap.appendChild(cosmic.statusTag(activity, { color: machineMeta.accent }));
      cosmicTagsHtml = tagsWrap.outerHTML;
    } catch (_) {
      // styling only — never block the transcript on a theme error
      cosmicSigilHtml = '';
      cosmicEpithetHtml = '';
      cosmicTagsHtml = '';
    }
  }

  const listHtml = detail
    ? `
      <div class="slot-chat-session-hero${cosmicSigilHtml ? ' is-cosmic' : ''}" style="--machine:${esc(chrome.accent)};--machine-surface:${esc(chrome.surface)};--machine-border:${esc(chrome.border)};">
        <div class="slot-chat-session-band"></div>
        ${cosmicSigilHtml ? `<div class="slot-chat-session-sigil">${cosmicSigilHtml}</div>` : ''}
        <div class="slot-chat-session-head">
          <div class="slot-chat-session-kicker">
            <span class="slot-chat-session-machine cosmic-display">${esc(chrome.title)}</span>
            ${cosmicEpithetHtml}
            <span class="slot-chat-session-provider">${esc(providerLabelForHero(providerForSession(session.name, session.hostId)))}</span>
          </div>
          <div class="slot-chat-session-title cosmic-display">${esc(detail.title || session.displayName || session.name)}</div>
          ${cosmicTagsHtml}
        </div>
      </div>
      ${transcriptHtml}`
    : `<div class="slot-chat-empty">${state.chatStream.connected ? 'Loading chat…' : 'Reconnecting…'}</div>`;

  // Bug1 (chat_ui_hardening_batch3): tag the rendered list with the stream whose
  // transcript was actually painted (empty/leaked => ''), so a DOM check can
  // corroborate the slot never shows another stream's transcript, AND so a later
  // byte-identical-skip optimization (Bug4/S4) can key its cache on the bound
  // stream. `paintedStreamId` is null when we wrote the empty/placeholder body.
  const paintedStreamId = detail ? streamId : null;
  // Content digest from the transcript items in scope (defect 2). Computed here,
  // before HTML commit, and carried on listRender so BOTH the direct and the
  // deferred (selection-active) paint paths emit the same digest. Null detail
  // (empty/leaked frame) yields a rowCount:0 digest — the negative window still
  // sees a paint, just with no content.
  const contentDigest = computeSlotChatContentDigest(detail ? detail.transcriptItems : null);
  const listRender = {
    html: listHtml,
    cacheKey: `${paintedStreamId || ''}\u0000${listHtml}`,
    paintedStreamId,
    sessionKey,
    detailPresent: !!detail,
    boundStreamId,
    resolvedStreamId,
    leaked,
    clearedBeforePaint: sessionChanged,
    shouldStick,
    contentDigest,
  };
  const cachedListHtml = state.slotChatLastListHtml[slot];
  const canDeferListRender = !sessionChanged && activeSelectionInside(refs.listEl);
  if (cachedListHtml === listRender.cacheKey) {
    state.slotChatPendingListRender[slot] = null;
    window.PentacleHarness?.emit?.('chat:slot_render_skipped', {
      slot,
      streamId: boundStreamId || undefined,
      data: { paintedStreamId, resolvedStreamId, reason: 'byte-identical' },
    });
  } else if (canDeferListRender) {
    state.slotChatPendingListRender[slot] = listRender;
    window.PentacleHarness?.emit?.('chat:slot_render_deferred', {
      slot,
      streamId: boundStreamId || undefined,
      data: { paintedStreamId, resolvedStreamId, reason: 'active-selection' },
    });
  } else {
    applySlotChatListRender(slot, refs, listRender);
  }

  if (refs.statusEl) {
    // Live working timer (B2 chat_send_turn_lifecycle_batch2): prefer the
    // daemon's AUTHORITATIVE per-turn elapsed (working.state.elapsed_ms,
    // interpolated locally between heartbeats by the store) so the displayed
    // timer matches what claude/codex actually report. Only when the daemon
    // value is unavailable do we fall back to the legacy client clock seeded
    // from the parsed working label (no regression for streams without a
    // working.state anchor). `workingSlotStream` lets the 1s tick re-query the
    // daemon elapsed for this slot. Cleared the moment the slot leaves 'working'.
    let timerLabel = '';
    if (activity === 'working') {
      const daemonElapsed = streamId && window.PentacleChatStore
        && typeof window.PentacleChatStore.getWorkingElapsedMs === 'function'
        ? window.PentacleChatStore.getWorkingElapsedMs(streamId)
        : null;
      if (streamId) state.workingSlotStream[slot] = streamId;
      if (typeof daemonElapsed === 'number') {
        timerLabel = chatUi.formatElapsed(daemonElapsed);
        // Keep workingSince as a marker (so syncWorkingTimer arms) + fallback
        // seed should the daemon anchor disappear mid-turn.
        state.workingSince[slot] = Date.now() - daemonElapsed;
      } else {
        if (!state.workingSince[slot]) {
          const seededSec = chatUi.parseWorkingSeconds(workingLabel) || 0;
          state.workingSince[slot] = Date.now() - seededSec * 1000;
        }
        timerLabel = chatUi.formatElapsed(Date.now() - state.workingSince[slot]);
      }
    } else {
      delete state.workingSince[slot];
      delete state.workingSlotStream[slot];
    }
    refs.statusEl.className = `slot-chat-status is-${activity}`;
    refs.statusEl.innerHTML = chatUi.renderStatusBadges({
      activity,
      workingLabel: timerLabel,
      pending: remotePending,
    });
    syncActivitySpinnerPhase(document);
    // B3 (chat_send_turn_lifecycle_batch2): a visible cancel control while a turn
    // is in flight (alongside the ESC keybind). Interrupts the running agent and
    // clears the indicator. Appended after the badges; innerHTML above replaces
    // it each render, so the listener never leaks across renders.
    const turnInFlight = streamId && window.PentacleChatStore
      && typeof window.PentacleChatStore.getTurnPhase === 'function'
      && window.PentacleChatStore.getTurnPhase(streamId) !== 'idle';
    const interruptState = streamId && window.PentacleChatStore
      && typeof window.PentacleChatStore.getInterruptState === 'function'
      ? window.PentacleChatStore.getInterruptState(streamId)
      : null;
    const showCancel = turnInFlight || !!interruptState?.pending || !!interruptState?.retryable;
    if (showCancel) {
      const cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.className = 'slot-chat-cancel-btn';
      cancelBtn.textContent = interruptState?.retryable ? 'Retry stop' : (interruptState?.pending ? 'Stopping' : 'Cancel');
      cancelBtn.title = interruptState?.retryable ? 'Retry stopping the current turn (Esc)' : 'Cancel the current turn (Esc)';
      cancelBtn.disabled = !!interruptState?.pending;
      cancelBtn.addEventListener('click', () => cancelChatComposer(slot));
      refs.statusEl.appendChild(cancelBtn);
    }
    if (interruptState?.message) {
      const messageEl = document.createElement('span');
      messageEl.className = 'slot-chat-interrupt-msg';
      messageEl.textContent = interruptState.message;
      refs.statusEl.appendChild(messageEl);
    }
    syncWorkingTimer();
  }

  // One ordered flow covers pane children followed by durable notifications.
  // Draft and settlement identities are source identities, never page positions.
  if (refs.questionEl) {
    const question = streamId ? window.PentacleChatStore?.getQuestion?.(streamId) : null;
    if (streamId) ensureDurableQuestionsHydrated(streamId);
    const paneItems = question ? questionItems(question) : [];
    const paneEntries = paneItems.map((item, index) => ({
      source: 'pane', key: `${streamId}:pane:${question.question_key}:${item.question_id || item.id || item.index || index}`,
      model: item,
    }));
    const entries = [...paneEntries, ...getOpenQuestionsForStream(streamId).map(notification => ({
      source: 'durable', key: `${streamId}:durable:${durableQuestionNotificationId(notification)}:${notification.question?.question_id || ''}`,
      model: durableQuestionOptionBModel(notification), notification,
    }))];
    state.answeredQuestionSig = state.answeredQuestionSig || {};
    state.questionSubmissionPending = state.questionSubmissionPending || {};
    state.desktopQuestionOverlayOpen = state.desktopQuestionOverlayOpen || {};
    const draftKey = `${streamId}:flow`;
    const draft = state.questionDrafts[draftKey] || (state.questionDrafts[draftKey] = { sig: 'flow', answers: {} });
    const validKeys = new Set(entries.map(entry => entry.key));
    for (const key of Object.keys(draft.answers)) if (!validKeys.has(key)) delete draft.answers[key];
    for (const entry of entries) {
      entry.signature = questionModelSignature(entry.model);
      entry.locked = state.answeredQuestionSig[entry.key] === entry.signature;
      if (draft.answers[entry.key]?._signature !== entry.signature) draft.answers[entry.key] = { _signature: entry.signature };
    }
    const incomplete = entry => !entry.locked && !!answerConstraint(entry.model, draft.answers[entry.key]);
    const unsettled = entries.filter(entry => !entry.locked);
    const closeQuestions = () => {
      state.desktopQuestionOverlayOpen[streamId] = false;
      closeDesktopQuestionPortal(streamId, refs.questionEl);
      renderSlotChat(slot);
      refs.questionEl.querySelector('.slot-chat-question-open')?.focus();
    };
    if (!unsettled.length) {
      state.desktopQuestionOverlayOpen[streamId] = false;
      closeDesktopQuestionPortal(streamId, refs.questionEl);
      refs.questionEl.innerHTML = '';
      refs.questionEl.style.display = 'none';
    } else if (!state.desktopQuestionOverlayOpen[streamId]) {
      refs.questionEl.innerHTML = '';
      refs.questionEl.style.display = '';
      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'slot-chat-question-open';
      open.textContent = `${entries.filter(incomplete).length || unsettled.length} unanswered`;
      open.addEventListener('click', () => { state.desktopQuestionOverlayOpen[streamId] = true; renderSlotChat(slot); });
      refs.questionEl.appendChild(open);
    } else {
      const focus = captureQuestionFocus(refs.questionEl);
      const activeKey = state.questionPageIndexByStream[draftKey];
      const activeIndex = Math.max(0, entries.findIndex(entry => entry.key === activeKey));
      const navigate = index => {
        state.questionPageIndexByStream[draftKey] = entries[clampQuestionPageIndex(index, entries.length)].key;
        renderSlotChat(slot);
      };
      refs.questionEl.innerHTML = '';
      refs.questionEl.style.display = '';
      const dots = [];
      if (entries.length > 1) {
        const pager = document.createElement('div');
        pager.className = 'slot-chat-question-pager';
        const dotGroup = document.createElement('div');
        dotGroup.className = 'desktop-question-dots';
        const pageButton = (label, index, disabled) => {
          const button = document.createElement('button');
          button.type = 'button'; button.className = 'slot-chat-question-page'; button.textContent = label;
          button.disabled = disabled || !!state.questionSubmissionPending[streamId];
          button.addEventListener('click', () => navigate(index));
          return button;
        };
        pager.appendChild(pageButton('Prev', activeIndex - 1, activeIndex === 0));
        entries.forEach((entry, index) => {
          const dot = pageButton(String(index + 1), index, false);
          dot.className = `desktop-question-dot${index === activeIndex ? ' is-active' : ''}`;
          dot.setAttribute('aria-label', `Question ${index + 1}`);
          dot.classList.toggle('is-answered', !incomplete(entry));
          dots.push(dot); dotGroup.appendChild(dot);
        });
        pager.appendChild(dotGroup);
        pager.appendChild(pageButton('Next', activeIndex + 1, activeIndex === entries.length - 1));
        refs.questionEl.appendChild(pager);
      }
      const container = document.createElement('div');
      container.className = 'slot-chat-question-card';
      if (entries[activeIndex].notification) container.dataset.notificationId = durableQuestionNotificationId(entries[activeIndex].notification);
      refs.questionEl.appendChild(container);
      const flowQuestion = { multi: true, questions: entries.map((entry, index) => ({
        ...entry.model, index, _draftKey: entry.key, _signature: entry.signature, _locked: entry.locked,
      })) };
      const lock = entry => { state.answeredQuestionSig[entry.key] = entry.signature; entry.locked = true; };
      const needsReconcile = () => (state.questionUncertainIdsByStream?.[streamId]?.size || 0) > 0;
      const submit = async (_text, detail) => {
        if (state.questionSubmissionPending[streamId] || needsReconcile()) return;
        let resolvingNotificationId = '';
        state.questionSubmissionPending[streamId] = true;
        try {
          if (paneEntries.some(entry => !entry.locked)) {
            if (!question.question_key || !window.cc?.chatDismissQuestion) throw new Error('Question dismiss is unavailable.');
            const answers = detail.answers.slice(0, paneEntries.length).map(answer => answer.customText ? { ...answer, text: answer.customText } : answer);
            const text = window.PentacleChatCore.buildPentacleQuestionAnswerText({ question, answers });
            const result = await window.cc.chatDismissQuestion(session.hostId, session.name, { questionKey: question.question_key, text });
            if (!result?.ok && !['stale_question', 'text_send_failed'].includes(result?.error_code)) throw new Error(result?.error || 'Question dismiss failed.');
            paneEntries.forEach(lock);
            if (!result.ok && text.trim()) {
              state.slotDrafts[slot] = text; state.slotDraftTouched[slot] = true;
              if (refs.inputEl) refs.inputEl.value = text;
              setSlotSendError(slot, result.error_code === 'text_send_failed' ? result.error || 'Answer was not sent. Review the composer and send it normally.' : '');
              updateSendControls(slot);
            } else setSlotSendError(slot, '');
          }
          for (let index = paneEntries.length; index < entries.length; index++) {
            const entry = entries[index];
            if (entry.locked) continue;
            const answer = detail.answers[index];
            const options = { submit: true };
            if (answer.customText?.trim()) options.custom_text = answer.customText.trim();
            else if (answer.text?.trim()) options.text = answer.text.trim();
            else options.selections = answer.selectedOptionValues || [];
            if (!options.custom_text && answer.note?.trim()) options.note = answer.note;
            const notificationId = durableQuestionNotificationId(entry.notification);
            if (!window.cc?.notificationResolve) throw new Error('Question answer is unavailable.');
            resolvingNotificationId = notificationId;
            const result = await window.cc.notificationResolve(notificationId, durableQuestionActionKind(entry.notification), options);
            if (!result?.ok) throw new Error(result?.error || 'Question answer failed.');
            resolvingNotificationId = '';
            lock(entry);
            indexDurableQuestionNotification({ ...entry.notification, state: 'answered', resolved_at: new Date().toISOString(), ...result.notification, question: { ...entry.notification.question, state: 'answered', answer: options, ...result.notification?.question } });
          }
        } catch (error) {
          // A lost reply can follow a committed resolution. Reconcile the durable
          // inventory before exposing retry; successful source identities stay locked.
          if (resolvingNotificationId) {
            state.questionUncertainIdsByStream = state.questionUncertainIdsByStream || {};
            const uncertain = state.questionUncertainIdsByStream[streamId] || (state.questionUncertainIdsByStream[streamId] = new Set());
            uncertain.add(resolvingNotificationId);
            try { await reconcileQuestionAnswers(streamId); } catch (_) { /* Explicit check or reconnect retries reconciliation. */ }
          }
          setSlotSendError(slot, error.message || 'Question answer failed.');
          throw error;
        } finally {
          state.questionSubmissionPending[streamId] = false;
          scheduleSlotChatRender(slot);
        }
      };
      renderQuestionOptionB({
        container, doc: document, question: flowQuestion, streamId: draftKey, questionSig: 'flow',
        alreadyAnswered: !!state.questionSubmissionPending[streamId] || needsReconcile(), visibleItemIndex: activeIndex,
        drafts: state.questionDrafts, answeredSig: state.answeredQuestionSig,
        buildAnswerText: () => '', onSubmit: submit, onCancel: closeQuestions, showCancel: false,
        onDraftChange: () => dots.forEach((dot, index) => dot.classList.toggle('is-answered', !incomplete(entries[index]))),
      });
      if (needsReconcile()) {
        const check = document.createElement('button');
        check.type = 'button'; check.className = 'slot-chat-question-reconcile'; check.textContent = 'Check answer status';
        check.disabled = !state.chatStream.connected;
        check.addEventListener('click', async () => {
          check.disabled = true;
          try { await reconcileQuestionAnswers(streamId); }
          catch (error) { setSlotSendError(slot, error.message); }
          finally { renderSlotChat(slot); }
        });
        refs.questionEl.appendChild(check);
      }
      restoreQuestionFocus(refs.questionEl, focus);
      state.desktopQuestionPortalHomes = state.desktopQuestionPortalHomes || {};
      if (!state.desktopQuestionPortalHomes[streamId]) state.desktopQuestionPortalHomes[streamId] = { parent: refs.questionEl.parentElement, next: refs.questionEl.nextSibling };
      const portal = desktopQuestionPortal(streamId, refs.questionEl);
      portal.style.setProperty('--machine', chrome.accent);
      portal.querySelector('.desktop-question-portal__close').onclick = closeQuestions;
      if (!portal.contains(document.activeElement)) portal.querySelector('.desktop-question-portal__close').focus({ preventScroll: true });
      portal.onkeydown = event => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeQuestions(); }
        if (event.key === 'Tab') {
          const controls = [...portal.querySelectorAll('button:not(:disabled), textarea:not(:disabled), input:not(:disabled)')].filter(el => !el.hidden && el.getClientRects().length);
          const first = controls[0], last = controls[controls.length - 1];
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
        }
      };
    }
  }

  if (refs.draftPreviewEl) {
    refs.draftPreviewEl.innerHTML = '';
    refs.draftPreviewEl.style.display = 'none';
  }
  setSlotSendError(slot, state.slotSendErrors[slot]);
  if (streamId) maybeRestoreReturnedToPromptDraft(slot, streamId);
  updateSendControls(slot);

  const composerValue = chatUi.deriveComposerInputValue(
    state.slotDrafts[slot],
    remoteDraft,
    state.slotDraftTouched[slot],
  );
  if (refs.inputEl && refs.inputEl !== document.activeElement && refs.inputEl.value !== composerValue) {
    refs.inputEl.value = composerValue;
    refs.inputEl.style.height = '';
    const h = Math.min(refs.inputEl.scrollHeight, 120);
    refs.inputEl.style.height = `${h}px`;
    refs.inputEl.style.overflowY = refs.inputEl.scrollHeight > 120 ? 'auto' : 'hidden';
  }
  refs.inputEl?.classList.toggle('is-remote-draft', !!remoteDraft && !state.slotDraftTouched[slot]);
  refs.inputEl?.classList.toggle('is-remote-pending', !!remotePending && !state.slotDraftTouched[slot]);
  refs.inputEl?.setAttribute('placeholder', composerValue ? '' : 'Type a message');

  if (refs.scrollEl && shouldStick) scrollSlotChatToBottom(refs);

  // Session Status card view + header glyph (spec_pentacle__status_card_ui_desktop).
  // Applied LAST so the normal render has already set every body element's
  // display; when the card is open we override those to none and show the card.
  applySlotStatusCardView(slot, remoteSessionState);
}

// ── Session Status card view (spec_pentacle__status_card_ui_desktop) ─────────
// A neutral glyph in the fixed .cell-header toggles a dedicated Session Status
// card into the chat body, in place of the transcript/composer. The glyph stays
// visible (and carries a closed-state attention dot) so the at-a-glance signal
// the old always-inline card gave is not lost.

function updateSlotStatusGlyph(slot, streamSession) {
  const header = document.getElementById(`header-${slot}`);
  const btn = header?.querySelector('.cell-status');
  if (!btn) return;
  const info = streamSession ? chatUi.statusCardAttentionState(streamSession) : { hasContent: false, attention: false };
  if (!streamSession) {
    // Not a chat slot / no session: hide the glyph entirely.
    btn.style.display = 'none';
    btn.classList.remove('is-open', 'has-attention');
    btn.setAttribute('aria-pressed', 'false');
    return;
  }
  btn.style.display = '';
  if (!info.hasContent) {
    // Chat slot but no card/indicators yet: neutral + disabled, never an empty card.
    btn.disabled = true;
    btn.classList.remove('is-open', 'has-attention');
    btn.setAttribute('aria-pressed', 'false');
    btn.title = 'Session status — none yet';
    return;
  }
  const open = !!state.slotStatusCardOpen[slot];
  btn.disabled = false;
  btn.classList.toggle('is-open', open);
  btn.classList.toggle('has-attention', !open && !!info.attention);
  btn.setAttribute('aria-pressed', open ? 'true' : 'false');
  btn.title = open ? 'Close session status' : 'Session status';
}

// The overlay hides the chat body via a SINGLE class on the shell
// (`is-status-card-open`, CSS `display:none !important`), never per-element
// inline display. This is immune to a direct display mutation on any body
// element (e.g. an async renderSlotAttachments / setSlotSendError that lands
// while the card is open) — such a write cannot leak through the overlay, and
// on close the element simply shows whatever its own updater last set. No
// stash, no staleness. Called at the top of renderSlotChat (clear) and end (apply).
function clearStatusCardOverlay(slot) {
  const refs = state.slotChatRefs[slot];
  if (!refs) return;
  if (refs.chatShell) refs.chatShell.classList.remove('is-status-card-open');
  if (refs.cardViewEl) refs.cardViewEl.innerHTML = '';
  state.slotStatusCardOverlaid[slot] = false;
}

function applySlotStatusCardView(slot, streamSession) {
  const refs = state.slotChatRefs[slot];
  if (!refs) return;
  const info = streamSession ? chatUi.statusCardAttentionState(streamSession) : { hasContent: false };
  const open = !!state.slotStatusCardOpen[slot] && info.hasContent; // never keep an empty card open
  state.slotStatusCardOpen[slot] = open;
  updateSlotStatusGlyph(slot, streamSession);
  if (open) {
    if (refs.cardViewEl) refs.cardViewEl.innerHTML = chatUi.renderSessionStatusCardView(streamSession, { nowMs: Date.now() });
    if (refs.chatShell) refs.chatShell.classList.add('is-status-card-open');
    state.slotStatusCardOverlaid[slot] = true;
  }
}

function toggleSlotStatusCard(slot) {
  if (state.slotViewModes[slot] !== 'chat' || !state.slots[slot]) return;
  const streamSession = chatSessionStateForSession(state.slots[slot]);
  const info = streamSession ? chatUi.statusCardAttentionState(streamSession) : { hasContent: false };
  if (!info.hasContent) return; // nothing to show — glyph is disabled anyway
  state.slotStatusCardOpen[slot] = !state.slotStatusCardOpen[slot];
  renderSlotChat(slot);
}

// Tick the live working-timer label for every slot currently working, without a
// full transcript re-render. Self-heals: a slot that is no longer a working
// chat slot (turn ended, detached, switched to terminal) is dropped from
// `workingSince` here even if no render fired for it.
function updateWorkingTimers() {
  const now = Date.now();
  for (const key of Object.keys(state.workingSince)) {
    const slot = Number(key);
    const isWorkingChatSlot =
      state.slots[slot] && !state.botSlots[slot] && state.slotViewModes[slot] === 'chat';
    if (!isWorkingChatSlot) {
      delete state.workingSince[slot];
      delete state.workingSlotStream[slot];
      continue;
    }
    const timerEl = state.slotChatRefs[slot]?.statusEl?.querySelector('.slot-chat-status-timer');
    if (!timerEl) continue;
    // B2: prefer the daemon-authoritative elapsed (interpolated by the store);
    // fall back to the legacy client clock when no daemon anchor is available.
    const sid = state.workingSlotStream[slot];
    const daemonElapsed = sid && window.PentacleChatStore
      && typeof window.PentacleChatStore.getWorkingElapsedMs === 'function'
      ? window.PentacleChatStore.getWorkingElapsedMs(sid)
      : null;
    timerEl.textContent = chatUi.formatElapsed(
      typeof daemonElapsed === 'number' ? daemonElapsed : (now - state.workingSince[slot]),
    );
  }
}

// Arm the shared 1s timer interval iff at least one slot is working; stop it
// (and free the handle) once none are. One interval drives all slots, so it
// cannot leak per-row timers across re-renders or slot swaps.
function syncWorkingTimer() {
  const anyWorking = Object.keys(state.workingSince).length > 0;
  if (anyWorking && !state.workingTimer) {
    state.workingTimer = setInterval(() => {
      updateWorkingTimers();
      if (Object.keys(state.workingSince).length === 0) {
        clearInterval(state.workingTimer);
        state.workingTimer = null;
      }
    }, 1000);
  } else if (!anyWorking && state.workingTimer) {
    clearInterval(state.workingTimer);
    state.workingTimer = null;
  }
}

async function sendStructuredSlotMessage(slot, text, { onSuccess } = {}) {
  const cleanText = String(text || '').trim();
  if (!cleanText || !state.slots[slot] || state.botSlots[slot]) return false;
  const target = chatControlTargetForSlot(slot);
  if (!target || target.error) {
    setSlotSendError(slot, target?.error || 'No chat session is attached.');
    requestAnimationFrame(() => renderSlotChat(slot));
    return false;
  }
  state.slotSendPending[slot] = true;
  setSlotSendError(slot, '');
  updateSendControls(slot);
  try {
    const result = await window.cc.chatSend(target.hostId, target.sessionName, cleanText);
    if (!result?.ok) {
      throw new Error(result?.error || 'Chat send failed');
    }
    if (typeof onSuccess === 'function') onSuccess();
    return true;
  } catch (error) {
    setSlotSendError(slot, error instanceof Error ? error.message : String(error || 'Chat send failed'));
    return false;
  } finally {
    state.slotSendPending[slot] = false;
    updateSendControls(slot);
    requestAnimationFrame(() => renderSlotChat(slot));
  }
}

function sendRawPtyInput(slot, text) {
  if (!text || !state.slots[slot] || state.botSlots[slot] || !state.terminals[slot]) return;

  window.cc.exitCopyMode(slot);

  const lines = text.split('\n');
  for (let i = 0; i < lines.length; i++) {
    if (lines[i]) window.cc.writePty(slot, lines[i]);
    if (i < lines.length - 1) {
      window.cc.tmuxSend(slot, '-H', '1B', '5B', '31', '33', '3B', '35', '75');
    }
  }
  // Delay the submit \r so it lands as a separate keystroke event, not part of the
  // text burst. Codex's TUI batches bytes received in a short window into one chunk
  // and treats \r inside the chunk as a literal paste-newline (insert) instead of
  // Enter (submit) — without this gap, "hello\r" appears in the input but never
  // submits. Claude submits on \r regardless of timing, so the delay is harmless
  // there. 100ms is well above the empirical threshold (~20ms) with margin for slow
  // systems; barely perceptible to users.
  setTimeout(() => window.cc.writePty(slot, '\r'), 100);
}

async function sendProgrammaticInput(slot, text, options = {}) {
  if (options.forcePty) {
    sendRawPtyInput(slot, text);
    return true;
  }
  return sendStructuredSlotMessage(slot, text);
}

// B3 (chat_send_turn_lifecycle_batch2): cancel/ESC the latest in-flight turn for
// a slot's stream. Returns true if a turn/queued send was cancelled (so the
// caller can preventDefault). Re-renders so the indicator clears immediately.
function cancelChatComposer(slot) {
  if (!chatUiEnabled() || !window.PentacleChatStore) return false;
  if (!state.slots[slot] || state.botSlots[slot]) return false;
  const target = chatControlTargetForSlot(slot);
  const streamId = target && !target.error ? target.streamSession?.stream_id : null;
  if (!streamId) return false;
  const cancelled = window.PentacleChatStore.cancelTurn(streamId);
  if (cancelled) renderSlotChat(slot);
  return cancelled;
}

async function sendChatComposer(slot) {
  if (!chatUiEnabled()) return;
  const refs = state.slotChatRefs[slot];
  const inputEl = refs?.inputEl;
  const text = (inputEl ? inputEl.value : state.slotDrafts[slot]).trim();
  const pendingAttachments = slotAttachmentDrafts(slot);
  if ((!text && pendingAttachments.length === 0) || !state.slots[slot] || state.botSlots[slot]) return;

  // Shared-core optimistic send (desktop_chat_ui_mobile_parity): when the slot
  // has a resolved stream, the composer routes through PentacleChatStore.sendTurn
  // — which inserts the optimistic USER row + sets the turn phase pending +
  // dispatches a correlated (renderer-owned request_id) send via the IPC bridge.
  // This is the live send path; the no-stream fallback below only runs when no
  // websocket stream is attached yet (sendTurn cannot operate without one).
  if (window.PentacleChatStore) {
    const target = chatControlTargetForSlot(slot);
    const streamId = target && !target.error ? target.streamSession?.stream_id : null;
    if (streamId) {
      // Native queue: do NOT gate on idle here. sendTurn dispatches immediately;
      // provider CLIs queue mid-turn input natively.
      if (await sendComposerQuestionAnswer(slot, streamId, text, inputEl, pendingAttachments.length)) {
        return;
      }
      state.slotSendPending[slot] = true;
      setSlotSendError(slot, '');
      updateSendControls(slot);
      try {
        const attachments = pendingAttachments.length ? await uploadSlotAttachments(slot) : [];
        const optimisticId = window.PentacleChatStore.sendTurn(streamId, text, attachments);
        if (optimisticId) {
          state.slotDrafts[slot] = '';
          state.slotDraftTouched[slot] = true;
          clearSlotAttachments(slot);
          if (inputEl) {
            inputEl.value = '';
            inputEl.style.height = '';
          }
          renderSlotChat(slot);
        }
      } catch (error) {
        setSlotSendError(slot, error instanceof Error ? error.message : String(error || 'Chat send failed'));
      } finally {
        state.slotSendPending[slot] = false;
        updateSendControls(slot);
      }
      return;
    }
  }

  await sendStructuredSlotMessage(slot, text, {
    onSuccess: () => {
      state.slotDrafts[slot] = '';
      state.slotDraftTouched[slot] = true;
      if (inputEl) {
        inputEl.value = '';
        inputEl.style.height = '';
      }
    },
  });
}

async function sendComposerQuestionAnswer(slot, streamId, text, inputEl, attachmentCount = 0) {
  if (!window.PentacleChatStore || typeof window.PentacleChatStore.getQuestion !== 'function') return false;
  const question = window.PentacleChatStore.getQuestion(streamId);
  if (!question || !question.question_key) return false;
  const session = state.slots[slot];
  if (!session || state.botSlots[slot]) return false;
  if (questionItems(question).length > 1 || getOpenQuestionsForStream(streamId).length) {
    state.slotDrafts[slot] = text;
    state.slotDraftTouched[slot] = true;
    state.desktopQuestionOverlayOpen = state.desktopQuestionOverlayOpen || {};
    state.desktopQuestionOverlayOpen[streamId] = true;
    setSlotSendError(slot, 'Answer every page in Questions before sending the group.');
    renderSlotChat(slot);
    return true;
  }
  if (!text) {
    setSlotSendError(slot, attachmentCount > 0
      ? 'Answer the open question before sending attachments.'
      : 'Answer the open question before sending.');
    return true;
  }
  if (!window.cc || typeof window.cc.chatDismissQuestion !== 'function') {
    setSlotSendError(slot, 'Question dismiss is unavailable.');
    return true;
  }

  state.slotSendPending[slot] = true;
  setSlotSendError(slot, '');
  updateSendControls(slot);
  try {
    const result = await window.cc.chatDismissQuestion(session.hostId, session.name, {
      questionKey: question.question_key,
      text,
    });
    if (result?.ok) {
      state.slotDrafts[slot] = '';
      state.slotDraftTouched[slot] = true;
      if (streamId && state.questionDrafts) delete state.questionDrafts[streamId];
      if (inputEl) {
        inputEl.value = '';
        inputEl.style.height = '';
      }
      renderSlotChat(slot);
      return true;
    }
    const errorCode = result?.error_code || '';
    if (errorCode === 'stale_question' || errorCode === 'text_send_failed') {
      state.slotDrafts[slot] = text;
      state.slotDraftTouched[slot] = true;
      if (inputEl) inputEl.value = text;
      if (streamId && state.questionDrafts) delete state.questionDrafts[streamId];
      renderSlotChat(slot);
      setSlotSendError(slot, errorCode === 'text_send_failed'
        ? (result?.error || 'Question dismissed, but answer text was not sent. Review the composer and send it normally.')
        : '');
      return true;
    }
    setSlotSendError(slot, result?.error || 'Question dismiss failed.');
    return true;
  } catch (error) {
    setSlotSendError(slot, error instanceof Error ? error.message : String(error || 'Question dismiss failed'));
    return true;
  } finally {
    state.slotSendPending[slot] = false;
    updateSendControls(slot);
  }
}

function scheduleSlotChatRender(slot) {
  if (!chatUiEnabled()) return;
  if (state.slotChatRenderTimers[slot]) return;
  state.slotChatRenderTimers[slot] = setTimeout(() => {
    state.slotChatRenderTimers[slot] = null;
    renderSlotChat(slot);
  }, 120);
}

// renderSlotStatus paints the full slot-scoped status view (spec items 3/4)
// into the status layer. Live re-renders (renderSlotChat re-enters on every
// frame while in status mode) preserve the update-history scroll position and
// keyboard focus, and wire the return paths: Transcript -> chat mode, Update
// log -> scroll/focus the history region.
function renderSlotStatus(slot, remoteSessionState) {
  const refs = state.slotChatRefs[slot];
  if (!refs?.statusMount) return;
  const session = remoteSessionState || {};
  const prevHistory = refs.statusMount.querySelector('[data-status-scroll="updates"]');
  const prevScrollTop = prevHistory ? prevHistory.scrollTop : null;
  const activeEl = document.activeElement;
  const focusedInView = activeEl && refs.statusMount.contains(activeEl);
  const focusReturn = focusedInView ? activeEl.getAttribute('data-status-return') : null;
  // A focused update-history region must survive the live innerHTML replacement
  // too, not just the return buttons (G-QA-3).
  const focusHistory = focusedInView && !!activeEl.getAttribute('data-status-scroll');

  refs.statusMount.innerHTML = chatUi.renderStatusView(session, { nowMs: Date.now() });

  const history = refs.statusMount.querySelector('[data-status-scroll="updates"]');
  if (history && prevScrollTop != null) history.scrollTop = prevScrollTop;

  refs.statusMount.querySelectorAll('[data-status-return]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = btn.getAttribute('data-status-return');
      if (target === 'transcript') {
        updateSlotViewMode(slot, 'chat');
        return;
      }
      if (target === 'updates') {
        const h = refs.statusMount.querySelector('[data-status-scroll="updates"]');
        if (h) { h.scrollTop = 0; h.focus(); }
      }
    });
  });

  // Restore focus across the live re-render so streaming updates never steal
  // keyboard focus — the history region takes priority, else the return button.
  if (focusHistory && history) {
    history.focus();
  } else if (focusReturn) {
    const el = refs.statusMount.querySelector(`[data-status-return="${focusReturn}"]`);
    if (el) el.focus();
  }
}

function updateSlotViewMode(slot, mode) {
  if (!chatUiEnabled()) mode = 'terminal';
  state.slotViewModes[slot] = mode;
  window.PentacleHarness?.emit?.('slot:viewmode', { slot, data: { mode } });
  const header = document.getElementById(`header-${slot}`);
  if (header) {
    header.querySelectorAll('.cell-view-toggle').forEach((btn) => {
      const active = btn.dataset.mode === mode;
      btn.classList.toggle('active', active);
      btn.style.opacity = active ? '1' : '0.65';
      btn.style.borderColor = active ? '#2dd4bf' : '#284137';
      btn.style.color = active ? '#dff8ea' : '#86a595';
      btn.style.background = active ? '#173126' : '#101815';
    });
  }
  if (chatUiEnabled()) renderSlotChat(slot);
  ensureSlotAssetTabs(slot);
  if (mode === 'terminal') scheduleVisibleSlotFits();
}

function ensureSlotModeToggle(slot) {
  const header = document.getElementById(`header-${slot}`);
  if (!header) return;
  if (!chatUiEnabled()) {
    header.querySelector('.cell-view-toggle-group')?.remove();
    return;
  }
  if (header.querySelector('.cell-view-toggle-group')) return;
  const actions = header.querySelector('.cell-actions');
  if (!actions) return;
  const group = document.createElement('div');
  group.className = 'cell-view-toggle-group';
  group.style.cssText = 'display:flex;gap:4px;margin-right:4px;';
  group.innerHTML = `
    <button class="cell-view-toggle" data-slot="${slot}" data-mode="status" title="Session status view" aria-label="Session status view" style="font-size:10px;line-height:1;padding:5px 7px;border-radius:999px;border:1px solid #284137;background:#101815;color:#86a595;">Status</button>
    <button class="cell-view-toggle" data-slot="${slot}" data-mode="terminal" title="Terminal view" aria-label="Terminal view" style="font-size:10px;line-height:1;padding:5px 7px;border-radius:999px;border:1px solid #284137;background:#101815;color:#86a595;">Terminal</button>
    <button class="cell-view-toggle" data-slot="${slot}" data-mode="chat" title="Chat view" aria-label="Chat view" style="font-size:10px;line-height:1;padding:5px 7px;border-radius:999px;border:1px solid #284137;background:#101815;color:#86a595;">Chat</button>`;
  actions.prepend(group);
  group.querySelectorAll('.cell-view-toggle').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (btn.dataset.mode === 'chat') state.slotActiveAsset[slot] = null;
      updateSlotViewMode(slot, btn.dataset.mode);
      if (btn.dataset.mode === 'terminal') focusTerminal(slot);
    });
  });
  updateSlotViewMode(slot, state.slotViewModes[slot]);
}

function ensureChatPopoutAction(slot) {
  const header = document.getElementById(`header-${slot}`);
  const actions = header?.querySelector('.cell-actions');
  const session = state.slots[slot];
  if (!actions || !session || header.querySelector('.cell-chat-popout-action')) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'cell-chat-popout-action';
  button.title = IS_CHAT_POPOUT ? 'Dock' : 'Pop out';
  button.setAttribute('aria-label', button.title);
  button.innerHTML = IS_CHAT_POPOUT
    ? '<svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path fill-rule="evenodd" d="M9.636 2.5a.5.5 0 0 0-.5-.5H2.5A1.5 1.5 0 0 0 1 3.5v10A1.5 1.5 0 0 0 2.5 15h10a1.5 1.5 0 0 0 1.5-1.5V6.864a.5.5 0 0 0-1 0V13.5a.5.5 0 0 1-.5.5h-10a.5.5 0 0 1-.5-.5v-10a.5.5 0 0 1 .5-.5h6.636a.5.5 0 0 0 .5-.5z"/><path fill-rule="evenodd" d="M5 10.5a.5.5 0 0 0 .5.5h5a.5.5 0 0 0 0-1H6.707l8.147-8.146a.5.5 0 0 0-.708-.708L6 9.293V5.5a.5.5 0 0 0-1 0v5z"/></svg>'
    : '<svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path fill-rule="evenodd" d="M8.636 3.5a.5.5 0 0 0-.5-.5H1.5A1.5 1.5 0 0 0 0 4.5v10A1.5 1.5 0 0 0 1.5 16h10a1.5 1.5 0 0 0 1.5-1.5V7.864a.5.5 0 0 0-1 0V14.5a.5.5 0 0 1-.5.5h-10a.5.5 0 0 1-.5-.5v-10a.5.5 0 0 1 .5-.5h6.636a.5.5 0 0 0 .5-.5z"/><path fill-rule="evenodd" d="M16 .5a.5.5 0 0 0-.5-.5h-5a.5.5 0 0 0 0 1h3.793L6.146 9.146a.5.5 0 1 0 .708.708L15 1.707V5.5a.5.5 0 0 0 1 0v-5z"/></svg>';
  button.addEventListener('click', async (event) => {
    event.stopPropagation();
    const stream = chatSessionStateForNameHost(session.name, session.hostId);
    const args = {
      stream_id: stream?.stream_id || CHAT_POPOUT_CONTEXT?.stream_id,
      host: stream?.host || CHAT_POPOUT_CONTEXT?.host || session.hostId,
      desktop_host: session.hostId,
      session_name: stream?.session_name || session.name,
      title: session.displayName || session.name,
    };
    const reply = IS_CHAT_POPOUT
      ? await window.cc.chatDock?.(args)
      : await window.cc.chatPopOut?.(args);
    if (reply?.ok === false) console.warn('[chat-popout] action failed:', reply.error);
  });
  const maximizeBtn = actions.querySelector('.cell-maximize');
  if (maximizeBtn) maximizeBtn.insertAdjacentElement('afterend', button);
  else actions.prepend(button);
}

function updateSlotProviderTag(slot) {
  const header = document.getElementById(`header-${slot}`);
  const session = state.slots[slot];
  if (!header) return;
  header.querySelector('.cell-provider-tag')?.remove();
  if (!session || state.botSlots[slot]) return;
  const provider = providerForSession(session.name, session.hostId);
  const tag = document.createElement('span');
  tag.className = 'cell-provider-tag';
  tag.textContent = providerLabelForHero(provider);
  const sourceTag = header.querySelector('.cell-source-tag');
  const label = header.querySelector('.cell-label');
  if (sourceTag) sourceTag.after(tag);
  else if (label) label.after(tag);
}

function setDegradedMode(degraded) {
  state.degraded = !!degraded;
  document.body.classList.toggle('chat-stream-degraded', state.degraded);
  renderDegradedBanner();
}

function renderDegradedBanner() {
  const banner = document.getElementById('chat-stream-degraded-banner');
  if (!banner) return;
  const reason = String(state.chatStream.error || '').trim();
  const visible = state.degraded && reason && reason !== 'Stream disconnected';
  banner.hidden = !visible;
  banner.textContent = visible ? `Chat stream unavailable: ${reason}` : '';
}

// ── Sidebar Rendering ──────────────────────────────────────────

function getSlotForSession(name, hostId) {
  for (let i = 0; i < 4; i++) {
    if (!state.slots[i] || state.slots[i].name !== name) continue;
    if (hostId && state.slots[i].hostId !== hostId) continue;
    return i;
  }
  return -1;
}

// ── Activity Detection & Summaries ────────────────────────────

function getSourceForSession(sessionName, hostId) {
  return hostId ? hostPresentation.hostLabel(CONFIG, hostId) : null;
}

function getSourceColorForSession(sessionName, hostId) {
  return hostPresentation.hostColor(CONFIG, hostId, HOST_IDS);
}

function renderConfigWarnings() {
  const banner = document.getElementById('config-warning-banner');
  if (!banner) return;
  const warnings = CONFIG.configWarnings || loadedConfig.warnings || [];
  banner.textContent = warnings.map(warning => warning.message).join(' ');
  banner.hidden = warnings.length === 0;
}

function renderTitlebarMachines() {
  const mount = document.getElementById('titlebar-machines');
  if (!mount) return;
  const ids = (Array.isArray(HOST_IDS) && HOST_IDS.length ? HOST_IDS : ['local'])
    .filter((id) => id && getSourceForSession('', id));
  mount.innerHTML = ids.map((id) => {
    const name = getSourceForSession('', id) || id;
    const color = getSourceColorForSession('', id);
    return `<span class="titlebar-machine color-${color}" title="${esc(name)}">${esc(getSourceInitial(name))}</span>`;
  }).join('');
}

function getSourceInitial(source) {
  return hostPresentation.initial(source);
}

function findSession(sessionName, hostId) {
  return state.sessions.find((session) => session.name === sessionName && session.hostId === hostId);
}

function syncSlotDisplayNames() {
  for (let i = 0; i < 4; i++) {
    const slot = state.slots[i];
    if (!slot || state.botSlots[i]) continue;
    const session = findSession(slot.name, slot.hostId);
    const displayName = session?.display_name || slot.displayName || slot.name;
    if (slot.displayName === displayName) continue;
    slot.displayName = displayName;
    const label = document.querySelector(`#header-${i} .cell-label`);
    if (label) label.textContent = displayName;
  }
}

function updateActivityStrips() {
  for (let i = 0; i < 4; i++) {
    const strip = document.getElementById(`activity-strip-${i}`);
    if (!strip) continue;
    strip.className = 'cell-activity-strip';
    if (state.slots[i] && !state.botSlots[i]) {
      const sessionName = state.slots[i].name;
      const hostId = state.slots[i].hostId;
      const activity = findSession(sessionName, hostId)?.working ? 'working' : 'idle';
      if (activity === 'working' || activity === 'waiting') {
        strip.classList.add(activity);
      }
      // Refresh source tag on slot header (may appear after activity poll)
      if (CONFIG.features.sourceTags) {
        const header = document.getElementById(`header-${i}`);
        if (header && !header.querySelector('.cell-source-tag')) {
          const source = getSourceForSession(sessionName, hostId);
          if (source) {
            const tag = document.createElement('span');
            tag.className = `cell-source-tag color-${getSourceColorForSession(sessionName, hostId)}`;
            tag.textContent = source;
            tag.title = source;
            const label = header.querySelector('.cell-label');
            if (label) label.after(tag);
          }
        }
      }
    }
  }
}

function renderSourceFilterBar(visibleSessions) {
  const bar = document.getElementById('source-filter-bar');
  if (!bar) return;

  // Collect unique hostIds that have at least one *visible* session.
  // visibleSessions is the post-visibility-filter list from renderSidebar so a
  // host with only nested QA subagents does not produce a filter button.
  const sessions = Array.isArray(visibleSessions) ? visibleSessions : state.sessions;
  const visibleHostIds = collectSourceFilterHostIds(sessions);
  const configuredHostIds = (Array.isArray(HOST_IDS) ? HOST_IDS : [])
    .filter((id) => id && getSourceForSession('', id));
  const hostIds = configuredHostIds.length ? configuredHostIds : visibleHostIds;
  if (hostIds.length < 1) { bar.style.display = 'none'; return; }

  bar.style.display = 'flex';
  let html = `<button class="source-filter-btn${state.sourceFilter === null ? ' active' : ''}" data-host="all">All</button>`;
  for (const id of hostIds) {
    const name = getSourceForSession('', id) || id;
    const color = getSourceColorForSession('', id);
    const isActive = state.sourceFilter === id;
    html += `<button class="source-filter-btn color-${color}${isActive ? ' active' : ''}" data-host="${esc(id)}" title="${esc(name)}">${esc(getSourceInitial(name))}</button>`;
  }
  bar.innerHTML = html;

  bar.querySelectorAll('.source-filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      state.sourceFilter = btn.dataset.host === 'all' ? null : btn.dataset.host;
      renderSidebar();
    });
  });
}

let _firstNonEmptyRender = false;
// Sidebar attention tiers (sidebar_attention.js): 0 needs-answer, 1 working,
// 2 ordinary. Tier 2 is only labelled when an attention tier precedes it, so a
// homogeneous list reads as a flat recency-ordered list (mobile parity).
const SIDEBAR_TIER_LABELS = { 0: 'Needs answer', 1: 'Working', 2: 'Other' };
const SIDEBAR_TIER_CLASS = { 0: 'needs-answer', 1: 'working', 2: 'idle' };

// sidebarStreamIdForSession resolves a sidebar session to its chat-stream
// stream_id so per-row question/report counts can be looked up. Chat-stream
// sessions carry stream_id directly; tmux-only scratch shells resolve to '' and
// therefore have no attention counts (they cannot hold durable questions).
function sidebarStreamIdForSession(s) {
  if (s && s.stream_id) return String(s.stream_id);
  const name = s && s.name;
  if (!name) return '';
  const summary = (state.chatStream.sessions || []).find(
    (x) => (x.session_name || x.name) === name,
  );
  return summary ? sessionSummaryStreamId(summary) : '';
}

function renderSidebar() {
  const list = document.getElementById('session-list');
  const stats = document.getElementById('stats');

  // Admit only daemon rows explicitly marked for top-level display.
  const all = filterSidebarSessions(state.sessions)
    .filter((s) => !state.locallyClosedStreamIds.has(sessionSummaryStreamId(s)));
  if (!_firstNonEmptyRender && all.length > 0) {
    _firstNonEmptyRender = true;
    _perfRecord('renderer:first-non-empty-sidebar', { count: all.length });
  }

  // Apply source/title filters
  const sourceFiltered = state.sourceFilter
    ? all.filter(s => s.hostId === state.sourceFilter)
    : all;
  const search = state.sessionSearch.trim().toLowerCase();
  const active = search
    ? sourceFiltered.filter((s) => {
      const haystack = [
        s.display_name,
        s.title,
        s.name,
        getSourceForSession(s.name, s.hostId),
      ].filter(Boolean).join(' ').toLowerCase();
      return haystack.includes(search);
    })
    : sourceFiltered;
  // Attention-ordered session rows (mobile parity). sidebar_attention.js owns
  // the tier/order/primary-action decision; here we only assemble descriptors
  // (open-question and unread-report counts per stream + working state).
  const attentionRows = orderSidebarRows(active.map((s) => {
    const streamId = sidebarStreamIdForSession(s);
    return {
      session: s,
      streamId,
      isPinned: isProtectedAssistantSession(s),
      isWorking: !!s.working,
      lastEventAt: s.last_event_at || null,
      openQuestionCount: streamId ? getOpenQuestionsForStream(streamId).length : 0,
      unreadReportCount: streamId ? unreadReportCountForStream(streamId) : 0,
    };
  }));
  const needsAnswerCount = attentionRows.filter((r) => r.attentionTier === 0).length;
  const workingCount = attentionRows.filter((r) => r.attentionTier === 1).length;
  stats.textContent = search
    ? `${active.length} of ${sourceFiltered.length} sessions`
    : `${active.length} sessions | ${needsAnswerCount} need answer | ${workingCount} working`;

  renderSourceFilterBar(all);
  for (let slot = 0; slot < 4; slot++) syncSlotAssistantControls(slot);

  function renderSessionItem(row) {
    const s = row.session;
    const hostId = s.hostId || (IS_CLIENT ? 'remote' : 'local');
    const slotIdx = getSlotForSession(s.name, hostId);
    const isActive = slotIdx >= 0;
    const activity = s.working ? 'working' : 'idle';
    const displayName = s.display_name || s.name;
    const protectedAssistant = isProtectedAssistantSession(s);
    const suppressDetail = activity === 'working' || activity === 'waiting' || activity === 'sending';
    const preview = suppressDetail ? '' : chatUi.sanitizeSidebarDetail(s.preview);
    const activityBadge = activity === 'working'
      ? `<span class="activity-indicator working" aria-label="In progress"><span class="activity-spinner"></span></span>`
      : '';
    const machineName = getSourceForSession(s.name, hostId) || hostId;
    const machineColor = getSourceColorForSession(s.name, hostId);
    const machineAvatar = `<span class="s-machine-avatar color-${machineColor}" title="${esc(machineName)}">${esc(getSourceInitial(machineName))}</span>`;
    const offlineLabel = offlineHostStatus(s);
    const offline = !!offlineLabel;
    const offlineBadge = offline
      ? `<span class="s-offline-badge" title="${esc(offlineLabel)}" aria-label="${esc(offlineLabel)}">Offline</span>`
      : '';
    const pendingPeerBadge = pendingPeerBadgeHtml(pendingPeerMessagesForSession({ name: s.name, hostId }), 'pending-peer-badge s-pending-peer-badge');

    // Attention badges (spec scope item 2): open-question count then unread
    // report count, mirroring the row's primary-action precedence.
    const qCount = row.openQuestionCount || 0;
    const rCount = row.unreadReportCount || 0;
    const qLabel = `${qCount} open question${qCount === 1 ? '' : 's'}`;
    const rLabel = `${rCount} unread report${rCount === 1 ? '' : 's'}`;
    const questionBadge = qCount > 0
      ? `<span class="s-attention-badge s-question-badge" title="${esc(qLabel)}" aria-label="${esc(qLabel)}">?${qCount > 1 ? `<span class="s-badge-count">${qCount}</span>` : ''}</span>`
      : '';
    const reportBadge = rCount > 0
      ? `<span class="s-attention-badge s-report-badge" title="${esc(rLabel)}" aria-label="${esc(rLabel)}"><span class="s-badge-count">${rCount}</span></span>`
      : '';
    const ariaBits = [displayName, `on ${machineName}`];
    if (qCount > 0) ariaBits.push(qLabel);
    if (rCount > 0) ariaBits.push(rLabel);
    if (activity === 'working') ariaBits.push('working');
    if (offline) ariaBits.push(offlineLabel);

    // Expandable compact status summary (spec item 3): the row can reveal the
    // compact status card inline when the session has one. Sourced from the
    // matching chat-stream summary (status_card + context/spec indicators).
    const streamSummary = row.streamId
      ? (state.chatStream.sessions || []).find((x) => sessionSummaryStreamId(x) === row.streamId)
      : null;
    const compactStatusHtml = streamSummary ? chatUi.renderStatusCard(streamSummary) : '';
    const expanded = !!(row.streamId && state.sidebarExpanded[row.streamId]);
    const statusToggle = compactStatusHtml
      ? `<button class="s-status-toggle" data-status-toggle-stream="${esc(row.streamId)}" aria-expanded="${expanded}" aria-label="${expanded ? 'Hide' : 'Show'} status summary" title="${expanded ? 'Hide' : 'Show'} status summary">${expanded ? '▾' : '▸'}</button>`
      : '';
    const compactStatusBlock = compactStatusHtml && expanded
      ? `<div class="s-status-summary">${compactStatusHtml}</div>`
      : '';

    return `<div class="session-item ${protectedAssistant ? 'assistant-protected ' : ''}${isActive ? 'active' : ''}"
                 role="button" tabindex="0"
                 aria-label="${esc(ariaBits.join(', '))}"
                 data-name="${esc(s.name)}"
                 data-host="${esc(hostId)}"
                 data-display="${esc(displayName)}"
                 data-primary-action="${esc(row.primaryAction || 'status')}"
                 data-stream-id="${esc(row.streamId || '')}">
      <div class="s-top">
        ${machineAvatar}
        <span class="s-name">${esc(displayName)}</span>
        ${offlineBadge}
        ${questionBadge}
        ${reportBadge}
        ${activityBadge}
        ${pendingPeerBadge}
        ${statusToggle}
        <button class="s-edit-btn" data-edit-name="${esc(s.name)}" data-edit-display="${esc(displayName)}" data-edit-host="${esc(hostId)}" title="Rename"><svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M12.146.146a.5.5 0 0 1 .708 0l3 3a.5.5 0 0 1 0 .708l-10 10a.5.5 0 0 1-.168.11l-5 2a.5.5 0 0 1-.65-.65l2-5a.5.5 0 0 1 .11-.168l10-10zM11.207 2.5L13.5 4.793 14.793 3.5 12.5 1.207 11.207 2.5zm1.586 3L10.5 3.207 4 9.707V10h.5a.5.5 0 0 1 .5.5v.5h.5a.5.5 0 0 1 .5.5v.5h.293l6.5-6.5zm-9.761 5.175l-.106.106-1.528 3.821 3.821-1.528.106-.106A.5.5 0 0 1 5 12.5V12h-.5a.5.5 0 0 1-.5-.5V11h-.5a.5.5 0 0 1-.468-.325z"/></svg></button>
        <button class="s-trash-btn" data-trash-name="${esc(s.name)}" data-trash-host="${esc(hostId)}" title="Move to trash"><svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M5.5 5.5A.5.5 0 0 1 6 6v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm2.5 0a.5.5 0 0 1 .5.5v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm3 .5a.5.5 0 0 0-1 0v6a.5.5 0 0 0 1 0V6z"/><path fill-rule="evenodd" d="M14.5 3a1 1 0 0 1-1 1H13v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4h-.5a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1H6a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1h3.5a1 1 0 0 1 1 1v1zM4.118 4L4 4.059V13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V4.059L11.882 4H4.118zM2.5 3V2h11v1h-11z"/></svg></button>
      </div>
      ${preview ? `<div class="s-preview">${esc(preview)}</div>` : ''}
      ${compactStatusBlock}
    </div>`;
  }

  let html = '';
  // Session rows in attention order, with a tier divider whenever the tier
  // changes. Tier 2 (ordinary) is only labelled when an attention tier
  // precedes it (see SIDEBAR_TIER_LABELS note).
  const hasAttentionTier = needsAnswerCount > 0 || workingCount > 0;
  let lastTier = null;
  for (const row of attentionRows) {
    if (row.attentionTier !== lastTier) {
      const label = SIDEBAR_TIER_LABELS[row.attentionTier];
      if (label && (row.attentionTier !== 2 || hasAttentionTier)) {
        const tierCount = attentionRows.filter((r) => r.attentionTier === row.attentionTier).length;
        html += `<div class="sidebar-group-label ${SIDEBAR_TIER_CLASS[row.attentionTier]}">${label} (${tierCount})</div>`;
      }
      lastTier = row.attentionTier;
    }
    html += renderSessionItem(row);
  }
  list.innerHTML = html;
  list.querySelectorAll('.session-item.assistant-protected .s-trash-btn, .session-item.assistant-protected .s-edit-btn').forEach((button) => button.remove());
  setupSidebarCollapsibles(list);
  syncActivitySpinnerPhase(document);
  syncWorkingTimer();

  // Click handlers
  const defaultHostId = IS_CLIENT ? 'remote' : 'local';
  list.querySelectorAll('.session-item').forEach(el => {
    el.addEventListener('click', (e) => {
      // Nested controls (rename/delete/status-toggle) handle their own clicks
      // and stopPropagation; guard anyway so a bubbled click never double-acts.
      if (e.target && e.target.closest('.s-edit-btn, .s-trash-btn, .s-status-toggle')) return;
      // Route through the row's computed primary action (open question / first
      // unread report / status), each preserving the grid.
      activateSidebarRow(el, defaultHostId);
    });
    // Keyboard parity: the row is role="button"; Enter/Space activate it — but
    // ONLY when the row itself is the key target, so Enter/Space on a nested
    // Status/rename/delete button keeps its native activation (G-QA-2).
    el.addEventListener('keydown', (e) => {
      if (e.target !== el) return;
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
        e.preventDefault();
        activateSidebarRow(el, defaultHostId);
      }
    });
    el.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      const name = el.dataset.name;
      const hostId = el.dataset.host || defaultHostId;
      if (isProtectedAssistantNameHost(name, hostId)) return;
      window.cc.showContextMenu(name, el.dataset.display, hostId);
    });
  });

  // Edit button handlers
  list.querySelectorAll('.s-edit-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      showRenameModal(btn.dataset.editName, btn.dataset.editDisplay, btn.dataset.editHost);
    });
  });

  // Delete button handlers \u2014 hard kill, no trash drawer
  list.querySelectorAll('.s-trash-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteSession(btn.dataset.trashName, btn.dataset.trashHost);
    });
  });

  // Sidebar status-summary expand/collapse (spec item 3). Toggling never
  // assigns the session to a slot (stopPropagation) \u2014 it only reveals the
  // compact status card inline.
  list.querySelectorAll('.s-status-toggle').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const streamId = btn.dataset.statusToggleStream;
      if (!streamId) return;
      state.sidebarExpanded[streamId] = !state.sidebarExpanded[streamId];
      renderSidebar();
      // renderSidebar rebuilds the DOM, discarding this button. Restore focus to
      // the recreated toggle for the same stream so keyboard users aren't
      // dropped to the top of the list (G-QA-2).
      const restored = document.querySelector(`.s-status-toggle[data-status-toggle-stream="${cssAttrEscape(streamId)}"]`);
      if (restored) restored.focus();
    });
  });
}

// cssAttrEscape safely embeds an arbitrary attribute value in a querySelector
// attribute-equals selector (stream ids contain ':').
function cssAttrEscape(value) {
  return String(value).replace(/["\\]/g, '\\$&');
}

function esc(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── Bots ───────────────────────────────────────────────────────
//
// The bots tab was removed (see feature/pentacle__settings-panel-dark-only).
// Slot-level `state.botSlots[]` guards remain as a harmless slot-model
// invariant (no slot is ever a bot panel now). If a bots surface is ever
// reintroduced, add a chat_streamd RPC — do NOT bring back the HTTP path.

// ── Slot Assignment ────────────────────────────────────────────

function assignToSlot(sessionName, displayName, hostId) {
  // Already in a slot? Focus it (and maximize if in maximized mode)
  const existing = getSlotForSession(sessionName, hostId);
  if (existing >= 0) {
    if (state.maximizedSlot !== null && state.maximizedSlot !== existing) {
      // In maximized mode, switch the maximized view to this slot
      maximizeSlot(existing);
    }
    focusTerminal(existing);
    return existing;
  }

  if (state.maximizedSlot !== null) {
    // In maximized mode — replace the currently maximized slot
    const slot = state.maximizedSlot;
    attachSession(slot, sessionName, displayName, hostId);
    maximizeSlot(slot);
    return slot;
  }

  // Find first empty slot
  let slot = state.slots.findIndex(s => s === null);
  if (slot === -1) {
    // All full — replace the last slot (slot 3)
    slot = 3;
  }

  attachSession(slot, sessionName, displayName, hostId);
  return slot;
}

// Route a sidebar row's activation through its computed primary action
// (spec item 2): every action first assigns the session to a slot (preserving
// the grid), then opens the required existing surface — open question (chat
// view, where the question card renders), first unread report (asset tab), or
// the status view. Falls back to chat when the target surface is unavailable.
function activateSidebarRow(el, defaultHostId) {
  const name = el.dataset.name;
  const display = el.dataset.display;
  const hostId = el.dataset.host || defaultHostId;
  const streamId = el.dataset.streamId || '';
  const action = el.dataset.primaryAction || 'status';
  const slot = assignToSlot(name, display, hostId);
  if (typeof slot !== 'number' || slot < 0) return;
  if (action === 'report' && streamId) {
    const target = firstUnreadReportForStream(streamId);
    // Only an actual unread report forces the asset view. If it can't be
    // resolved, fall through to the ordinary Terminal default below rather than
    // forcing the slot into an unrelated mode.
    if (target) {
      openSlotAsset(slot, streamId, target.assetId, target.assetKey);
      return;
    }
  } else if (action === 'question') {
    // Explicit open question: force chat so the durable question card is visible
    // even when the session was ALREADY attached with its slot in asset, status
    // or terminal mode (assignToSlot's already-attached path only focuses).
    updateSlotViewMode(slot, 'chat');
    return;
  }
  // Ordinary open (primary-action 'status') and any default: land on Terminal.
  // A fresh attach/replacement resets the slot to Terminal in attachSession
  // (state.slotViewModes[slot] = 'terminal'); re-selecting an already-attached
  // slot leaves its explicit in-session Chat/Status/Terminal toggle untouched.
  // Status stays reachable through the per-cell view toggle.
}

// firstUnreadReportForStream returns the {assetId, assetKey} of the newest
// unread, non-dismissed report in the stream's bucket (order is updated_at
// desc), or null.
function firstUnreadReportForStream(streamId) {
  const bucket = state.chatStream.assets[streamId];
  if (!bucket) return null;
  const unread = new Set(reportUnreadKeys(bucket, (k) => !!(bucket.dismissedById || {})[assetTabKey(streamId, k)]));
  const key = (bucket.order || []).find((k) => unread.has(k));
  if (!key) return null;
  const item = bucket.itemsById[key];
  return { assetId: item.asset_id, assetKey: key };
}

async function attachSession(slot, sessionName, displayName, hostId) {
  hostId = hostId || 'local';
  // Kill existing terminal in this slot
  detachSlot(slot);

  state.slotGen[slot]++;
  const gen = state.slotGen[slot]; // capture generation to detect stale async resumes
  state.slots[slot] = { name: sessionName, displayName, hostId };
  closedChatSlots.update();
  window.PentacleHarness?.emit?.('slot:attach', { slot, host: hostId, data: { sessionName } });

  // Update header
  const header = document.getElementById(`header-${slot}`);
  ensureSlotModeToggle(slot);
  ensureChatPopoutAction(slot);
  const label = header.querySelector('.cell-label');
  label.textContent = displayName;
  label.classList.add('has-session');
  label.onclick = (event) => {
    if (state.slotViewModes[slot] !== 'asset') return;
    event.stopPropagation();
    if (state.slotAssetLabelTimers[slot]) {
      clearTimeout(state.slotAssetLabelTimers[slot]);
      state.slotAssetLabelTimers[slot] = null;
    }
    if (event.detail > 1) return;
    state.slotAssetLabelTimers[slot] = setTimeout(() => {
      state.slotAssetLabelTimers[slot] = null;
      if (state.slotViewModes[slot] !== 'asset') return;
      state.slotActiveAsset[slot] = null;
      updateSlotViewMode(slot, 'chat');
    }, 180);
  };
  label.ondblclick = (event) => {
    if (state.slotViewModes[slot] !== 'asset') return;
    if (state.slotAssetLabelTimers[slot]) {
      clearTimeout(state.slotAssetLabelTimers[slot]);
      state.slotAssetLabelTimers[slot] = null;
    }
  };
  document.getElementById(`cell-${slot}`).classList.add('occupied');

  // Source tag on slot header
  header.querySelector('.cell-source-tag')?.remove();
  if (CONFIG.features.sourceTags) {
    const source = getSourceForSession(sessionName, hostId);
    if (source) {
      const tag = document.createElement('span');
      tag.className = `cell-source-tag color-${getSourceColorForSession(sessionName, hostId)}`;
      tag.textContent = source;
      tag.title = source;
      label.after(tag);
    }
  }
  updateSlotProviderTag(slot);
  syncSlotAssistantControls(slot);

  // Create terminal
  const refs = chatUiEnabled() ? ensureSlotChatSurface(slot) : null;
  const container = refs?.terminalMount || document.getElementById(`term-${slot}`);
  if (!refs) container.innerHTML = '';
  if (refs?.listEl) refs.listEl.innerHTML = '';
  if (refs?.inputEl) refs.inputEl.value = '';
  state.slotBuffers[slot] = '';
  state.slotDrafts[slot] = '';
  state.slotDraftTouched[slot] = false;
  state.slotViewModes[slot] = 'terminal';
  fetchSlotAssetSnapshot(slot, gen);
  ensureSlotAssetTabs(slot);

  if (IS_CHAT_POPOUT) {
    maximizeSlot(slot);
    renderSlotChat(slot);
    return;
  }

  const term = new Terminal({
    theme: terminalThemeForAppearance(),
    fontFamily: "'SFMono-Regular', 'SF Mono', '.SF NS Mono', 'Menlo', 'Monaco', monospace",
    fontSize: 13,
    cursorBlink: true,
    allowProposedApi: true,
    scrollback: 0,
  });

  const fitAddon = new FitAddon();
  const unicode11Addon = new Unicode11Addon();
  term.loadAddon(fitAddon);
  term.loadAddon(unicode11Addon);
  term.unicode.activeVersion = '11';
  term.open(container);

  // Enable WebGL renderer for GPU-accelerated full-frame draws.
  // The DOM renderer creates individual spans per cell — browser paint cycles
  // can leave cells partially rendered until a repaint is forced (e.g. by
  // selecting text). WebGL does full-frame GPU draws, eliminating this.
  try {
    const webglAddon = new WebglAddon();
    webglAddon.onContextLoss(() => {
      // Fall back to DOM renderer if GPU context is lost
      webglAddon.dispose();
    });
    term.loadAddon(webglAddon);
  } catch (e) {
    console.warn('[App] WebGL renderer unavailable, using DOM fallback:', e.message);
  }

  // Fit terminal BEFORE spawning PTY so tmux renders at the correct size
  // from the very first frame. Without this, the PTY spawns at 80x24,
  // tmux redraws at the wrong size (garbled flash), then fit fires and
  // tmux redraws again correctly — causing the "unicode flash" on old chats.
  await new Promise(resolve => requestAnimationFrame(resolve));
  // Bail if another attachSession took over this slot while we awaited
  if (state.slotGen[slot] !== gen) { try { term.dispose(); } catch {} return; }
  fitAddon.fit();

  const terminalPaste = createTerminalPaste({
    term,
    readClipboard: () => window.cc.readClipboard(),
    pastePty: (data) => window.cc.pastePty(slot, data),
    isCurrent: () => state.slotGen[slot] === gen && state.terminals[slot]?.term === term && !!state.slots[slot]?.paneId,
  });
  term.element.addEventListener('paste', terminalPaste.nativePaste, { capture: true });

  // Keyboard enhancements for terminal input
  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== 'keydown') return true;

    // Copy: Cmd+C on mac (intercept always — mac users don't rely on Ctrl
    // here). On Win/Linux, Ctrl+Shift+C always copies; plain Ctrl+C copies
    // only when there's a selection, otherwise falls through to SIGINT
    // (standard Windows Terminal / VSCode behavior).
    const isMac = navigator.platform.toLowerCase().includes('mac');
    if (e.metaKey && e.key === 'c') {
      const sel = term.getSelection();
      if (sel) { navigator.clipboard.writeText(sel); return false; }
    }
    if (!isMac && e.ctrlKey && !e.metaKey && (e.key === 'c' || e.key === 'C')) {
      const sel = term.getSelection();
      if (e.shiftKey) {
        if (sel) navigator.clipboard.writeText(sel);
        return false;  // Ctrl+Shift+C always swallowed
      }
      if (sel) { navigator.clipboard.writeText(sel); return false; }
      // no selection + no shift → fall through so Ctrl+C becomes SIGINT
    }

    if (!terminalPaste.key(e, isMac)) return false;

    // Ctrl+Enter → insert newline in Claude Code (send CSI u via tmux send-keys -H)
    if (e.ctrlKey && e.key === 'Enter') {
      window.cc.exitCopyMode(slot);
      window.cc.tmuxSend(slot, '-H', '1B', '5B', '31', '33', '3B', '35', '75');
      return false;
    }

    // Backspace with selection → delete selected chars worth of input
    if (e.key === 'Backspace' && !e.metaKey && !e.ctrlKey) {
      const sel = term.getSelection();
      if (sel && sel.length > 0) {
        term.clearSelection();
        // Send one backspace per selected character to erase from shell input
        window.cc.writePty(slot, '\x7f'.repeat(sel.length));
        return false;
      }
    }

    // Fn+Up / Fn+Down (PageUp/PageDown) → tmux scrollback
    if (e.key === 'PageUp') {
      if (state.slots[slot]?.paneId) window.cc.scrollTmux(slot, 'up', 15);
      return false;
    }
    if (e.key === 'PageDown') {
      if (state.slots[slot]?.paneId) window.cc.scrollTmux(slot, 'down', 15);
      return false;
    }

    return true;
  });

  // Wire input — exit tmux copy-mode before sending keystrokes
  term.onData(data => {
    if (terminalPaste.captureData(data)) return;
    window.cc.exitCopyMode(slot);
    window.cc.writePty(slot, data);
  });

  state.terminals[slot] = { term, fitAddon };

  // Spawn PTY at the fitted terminal size (not the default 80x24).
  // createPty returns the immutable tmux pane ID (e.g. %5) which survives
  // session renames. All tmux commands (scroll, copy-mode) target this ID.
  const paneId = await window.cc.createPty(slot, sessionName, hostId, term.cols, term.rows);
  // Bail if another attachSession took over this slot while we awaited
  if (state.slotGen[slot] !== gen) return;
  if (!paneId) {
    console.warn(`[attach] createPty returned null for session=${sessionName}, detaching`);
    detachSlot(slot);
    return;
  }
  state.slots[slot].paneId = paneId;

  // Observe resize — only send resizePty when cols/rows ACTUALLY change.
  // Without this guard, attaching a session to one slot triggers ResizeObservers
  // on ALL slots (DOM layout recalc), causing unnecessary tmux redraws that
  // flash garbled content on the other terminals.
  let resizeTimer = null;
  const ro = new ResizeObserver(() => {
    if (!state.terminals[slot]) return;
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!state.terminals[slot]) return;
      fitVisibleSlot(slot);
    }, 50);
  });
  ro.observe(container);
  state.terminals[slot]._ro = ro;

  renderSidebar();
  if (chatUiEnabled()) renderSlotChat(slot);
  updateSlotViewMode(slot, state.slotViewModes[slot]);
}

function detachSlot(slot) {
  closedChatSlots.forget(slot);
  const wasBot = state.botSlots[slot];
  const sessionName = state.slots[slot] && state.slots[slot].name;

  // Bump generation FIRST — this invalidates any pending onPtyExit setTimeout
  // callbacks for the OLD session. Without this, killing the old PTY fires an
  // async pty:exit event that captures the NEW session's generation (because
  // attachSession increments slotGen after detachSlot returns), making the
  // guard in onPtyExit think the NEW session exited and detaching it.
  state.slotGen[slot]++;

  // Reset UI FIRST — this must always happen regardless of cleanup errors
  const header = document.getElementById(`header-${slot}`);
  const label = header.querySelector('.cell-label');
  label.textContent = `Slot ${slot + 1}`;
  label.classList.remove('has-session');
  label.style.color = '';
  if (state.slotAssetLabelTimers[slot]) {
    clearTimeout(state.slotAssetLabelTimers[slot]);
    state.slotAssetLabelTimers[slot] = null;
  }
  label.onclick = null;
  label.ondblclick = null;
  // Reset the Session Status glyph + card-view state for the freed slot.
  state.slotStatusCardOpen[slot] = false;
  state.slotStatusCardOverlaid[slot] = false;
  const statusGlyph = header.querySelector('.cell-status');
  if (statusGlyph) { statusGlyph.style.display = 'none'; statusGlyph.classList.remove('is-open', 'has-attention'); statusGlyph.disabled = true; }
  header.querySelector('.cell-source-tag')?.remove();
  header.querySelector('.cell-provider-tag')?.remove();
  header.querySelector('.cell-pending-peer-badge')?.remove();
  const trashButton = header.querySelector('.cell-trash');
  if (trashButton) {
    trashButton.hidden = false;
    trashButton.disabled = false;
    trashButton.setAttribute('aria-hidden', 'false');
  }
  clearSlotAssetDismissals(slot);
  header.querySelector('.slot-asset-tabs')?.remove();
  document.getElementById(`cell-${slot}`).classList.remove('occupied');

  // Clear state before async/throwing operations
  const hadSlot = !!state.slots[slot];
  state.slots[slot] = null;
  state.botSlots[slot] = false;
  state.slotBuffers[slot] = '';
  state.slotDrafts[slot] = '';
  state.slotDraftTouched[slot] = false;
  state.slotViewModes[slot] = 'terminal';
  state.slotActiveAsset[slot] = null;
  state.slotChatRefs[slot] = null;
  state.slotChatLastListHtml[slot] = null;
  state.slotChatPendingListRender[slot] = null;
  // NOTE: deliberately do NOT reset slotChatBoundSession/slotChatBoundStream
  // here. attachSession() calls detachSlot() FIRST on every rebind, so the
  // PREVIOUS binding (session key + last-painted stream) must survive detach for
  // the renderSlotChat leak guard to detect a transient id-only fallback that
  // resolves the newly-bound session to the stream the slot was just showing
  // (Bug1 chat_ui_hardening_batch3). They are overwritten on the next committed
  // paint; a stale value on a fully-freed slot is inert (self-heals on rebind).

  // Dispose terminal (may throw if WebGL context is lost, etc.)
  try {
    if (state.terminals[slot]) {
      if (state.terminals[slot]._ro) {
        state.terminals[slot]._ro.disconnect();
      }
      state.terminals[slot].term.dispose();
    }
  } catch (e) {
    console.warn('[App] terminal dispose error:', e);
  }
  state.terminals[slot] = null;

  // Kill PTY (async IPC, fire-and-forget)
  if (hadSlot && !wasBot) {
    window.cc.killPty(slot);
  }

  // Clear terminal container
  const container = document.getElementById(`term-${slot}`);
  container.innerHTML = '<div class="cell-empty">Click a session or bot to attach</div>';

  // If this was the maximized slot, go back to grid view
  if (state.maximizedSlot === slot) {
    minimizeAll();
  }

  renderSidebar();
}

function focusTerminal(slot) {
  if (state.terminals[slot]) {
    state.terminals[slot].term.focus();
  }
}

// A hidden terminal must retain its last real geometry. Both IPC and browser
// transports use the same resizePty path after FitAddon changes dimensions.
function fitVisibleSlot(slot) {
  if (state.currentView !== 'chats' || (state.maximizedSlot !== null && state.maximizedSlot !== slot)) return false;
  const entry = state.terminals[slot];
  const mount = entry?.term.element?.parentElement;
  if (!mount || mount.clientWidth <= 0 || mount.clientHeight <= 0 || !mount.getClientRects().length) return false;
  const css = window.getComputedStyle(mount);
  if (css.display === 'none' || css.visibility === 'hidden') return false;
  const { term, fitAddon } = entry;
  const oldCols = term.cols, oldRows = term.rows;
  fitAddon.fit();
  if (term.cols !== oldCols || term.rows !== oldRows) window.cc.resizePty(slot, term.cols, term.rows);
  return true;
}
let slotFitFrame = null;
function scheduleVisibleSlotFits() {
  if (slotFitFrame !== null) return;
  slotFitFrame = requestAnimationFrame(() => {
    slotFitFrame = null;
    for (let slot = 0; slot < 4; slot++) fitVisibleSlot(slot);
  });
}
let gridColResizer = null;

// ── Maximize / Minimize ───────────────────────────────────────

function maximizeSlot(slot) {
  const grid = document.querySelector('.grid');

  if (state.maximizedSlot === slot) {
    // Already maximized — minimize back to grid
    minimizeAll();
    return;
  }

  state.maximizedSlot = slot;
  grid.classList.add('maximized');
  window.PentacleHarness?.emit?.('slot:maximize', { slot });

  for (let i = 0; i < 4; i++) {
    const cell = document.getElementById(`cell-${i}`);
    cell.classList.toggle('maximized-cell', i === slot);
  }

  gridColResizer?.refresh();
  requestAnimationFrame(() => {
    if (fitVisibleSlot(slot)) state.terminals[slot].term.focus();
  });

  renderSidebar();
}

function minimizeAll() {
  state.maximizedSlot = null;
  const grid = document.querySelector('.grid');
  grid.classList.remove('maximized');
  window.PentacleHarness?.emit?.('slot:minimize', {});

  for (let i = 0; i < 4; i++) {
    document.getElementById(`cell-${i}`).classList.remove('maximized-cell');
  }

  gridColResizer?.refresh();
  scheduleVisibleSlotFits();

  renderSidebar();
}

// ── PTY Events ─────────────────────────────────────────────────

window.cc.onPtyData((slot, data) => {
  state.slotBuffers[slot] = (state.slotBuffers[slot] + stripAnsi(data)).slice(-SLOT_BUFFER_LIMIT);
  if (state.terminals[slot]) {
    state.terminals[slot].term.write(data);
  }
});

window.cc.onChatStreamFrame((frame) => {
  // No chatUiEnabled() gate: the sidebar visibility filter relies on
  // state.chatStream.sessions being kept current with chat_streamd's
  // inventory even when the in-slot chat UI is off.
  window.PentacleChatStore?.applyFrame?.(frame);
  applyChatStreamPayload(frame);
});

// Web only: the /cc websocket reconnected to a (possibly fresh) web host
// process — e.g. after `pentacle-web-start stop && pentacle-web-start`. The
// desktop preload has no onReconnect (its ipcRenderer transport never drops),
// so this is a no-op there. A fresh web host pushes no snapshot on connect and
// may already have completed its daemon handshake, so no connected:true frame
// arrives on its own; without this the live app stays connected:false and the
// composer send-gate keeps the input frozen until a manual reload. Re-pull the
// snapshot (mirroring the startup pull) so the input re-enables and the
// inventory re-syncs. Reset the connection-state version baseline first: the
// fresh host's state_version namespace restarts at 0, so a lower version would
// otherwise be rejected as stale by applyVersionedConnectionState.
window.cc.onReconnect?.(() => {
  state.chatStream.stateVersion = -1;
  window.cc.getChatStreamState().then((snapshot) => {
    applyChatStreamState(snapshot);
    if (snapshot && Object.prototype.hasOwnProperty.call(snapshot, 'limits')) {
      renderLimits(snapshot.limits, snapshot.limits_health ?? null);
    }
    window.PentacleChatStore?.applyFrame?.({ type: 'snapshot', ...(snapshot || {}) });
  }).catch(() => { /* the next reconnect retries the re-sync */ });
});

window.cc.onAssetDock?.((payload) => {
  dockAssetFromPayload(payload);
});

window.cc.onChatPopoutDock?.((payload) => {
  if (!payload?.session_name || !payload?.host) return;
  assignToSlot(payload.session_name, payload.title || payload.session_name, payload.desktop_host || payload.host);
});

// Shared-core store-driven re-render (desktop_chat_ui_mobile_parity): the
// shared store consumes the raw frame channel and drives the live render path.
// Re-render the chat slots on every store state change so optimistic inserts,
// send.result ack/fail, server-echo reconciliation, and turn-phase transitions
// are reflected (transcript + send-button gating). No-op when the store global
// is absent (defensive against an older preload).
if (window.PentacleChatStore && typeof window.PentacleChatStore.subscribe === 'function') {
  window.PentacleChatStore.subscribe(() => {
    if (!chatUiEnabled()) return;
    for (let slot = 0; slot < 4; slot += 1) {
      if (state.slots[slot] && !state.botSlots[slot]) scheduleSlotChatRender(slot);
    }
  });
}

window.cc.onPtyExit(async (slot, exitCode) => {
  // Capture the slot generation so we can detect if the slot was reused
  const gen = state.slotGen[slot];

  // If slot was already manually detached (user clicked X), skip entirely
  if (!state.slots[slot] && !state.terminals[slot]) return;

  const sessionName = state.slots[slot]?.name;
  console.warn(`[pty:exit] slot=${slot} session=${sessionName} exitCode=${exitCode}`);

  // If the tmux session still exists, auto-reconnect instead of detaching.
  // This handles cases where the PTY process dies (crash, signal) but the
  // tmux session and its Claude Code process are still alive.
  const hostId = state.slots[slot]?.hostId || 'local';
  if (sessionName) {
    try {
      const sessionAlive = await window.cc.checkSession(sessionName, hostId);
      if (state.slotGen[slot] !== gen) return; // slot was reused during await
      if (sessionAlive) {
        console.warn(`[pty:exit] auto-reconnecting slot=${slot} session=${sessionName}`);
        if (state.terminals[slot]) {
          state.terminals[slot].term.writeln('\r\n\x1b[90m--- Reconnecting... ---\x1b[0m');
        }
        const displayName = state.slots[slot]?.displayName || sessionName;
        // Small delay to avoid rapid reconnect loops if PTY keeps dying
        setTimeout(() => {
          if (state.slotGen[slot] === gen) {
            attachSession(slot, sessionName, displayName, hostId);
          }
        }, 500);
        return;
      }
    } catch (e) {
      console.warn(`[pty:exit] checkSession failed:`, e);
    }
    if (state.slotGen[slot] !== gen) return;
  }

  // Session is truly gone — show message and detach
  if (state.terminals[slot]) {
    state.terminals[slot].term.writeln('\r\n\x1b[90m--- Session ended ---\x1b[0m');
  }
  setTimeout(() => {
    if (state.slotGen[slot] === gen) {
      detachSlot(slot);
    }
  }, 1000);
});

// ── IPC from Main Process ──────────────────────────────────────

window.cc.onAssignSlot((slot, sessionName, hostId) => {
  hostId = hostId || 'local';
  const session = state.sessions.find((candidate) => candidate.name === sessionName && candidate.hostId === hostId);
  const displayName = session ? session.display_name : sessionName;
  attachSession(slot, sessionName, displayName, hostId);
});

window.cc.onAction((action, sessionName, extra) => {
  if (action === 'rename') {
    if (extra && typeof extra === 'object') showRenameModal(sessionName, extra.displayName, extra.hostId);
    else showRenameModal(sessionName, extra);
  } else if (action === 'trash') {
    deleteSession(sessionName, extra);
  }
});

// ── Actions ────────────────────────────────────────────────────

// Hard delete: tear down the chat-stream record (if any) and kill the tmux
// session immediately. No soft-trash, no restore.
async function deleteSession(name, hostId) {
  const apiHostId = IS_CLIENT ? 'remote' : 'local';
  hostId = hostId || apiHostId;
  if (isProtectedAssistantNameHost(name, hostId)) {
    showToast('This configured assistant cannot be deleted', { type: 'error' });
    return;
  }
  window.PentacleHarness?.emit?.('session:delete', { host: hostId, data: { sessionName: name } });
  const streamSession = chatSessionStateForNameHost(name, hostId);
  const useChatClose = decideUseChatClose(state, streamSession);

  if (useChatClose) {
    // operator_confirm:true must stay opt-in at this trash-click site — do
    // NOT default it inside chat_stream_client.closeSession, or other close
    // callers would silently bypass the daemon's operator-target gate.
    const result = await window.cc.chatClose(hostId, name, { operatorConfirm: true, force: true });
    if (!result?.ok) {
      showToast(result?.error || 'Failed to close chat', { type: 'error' });
      return;
    }
  }

  // Detach from any local slot first so the UI doesn't flash "Session ended"
  // when the tmux kill below closes the SSH stream.
  for (let i = 0; i < 4; i++) {
    if (state.slots[i] && state.slots[i].name === name && (state.slots[i].hostId || apiHostId) === hostId) {
      detachSlot(i);
    }
  }

  if (useChatClose) {
    delete state.chatStream.drafts[streamSession.stream_id];
    state.chatStream.events = state.chatStream.events.filter(e => e.stream_id !== streamSession.stream_id);
    // Optimistically hide the closed session from the sidebar immediately.
    // state.chatStream.sessions has a single frame-fed write source (daemon
    // inventory), so instead of mutating it we record the closed stream id in an
    // overlay that the sidebar projection filters out; the next inventory frame
    // that omits the row clears the overlay entry (authoritative reconcile).
    // Without this a closed chat lingered as a dead row whose click re-attached
    // an already-closed stream.
    if (streamSession.stream_id) state.locallyClosedStreamIds.add(streamSession.stream_id);
    scheduleSidebarRerender();
    return;
  }

  // Non-chat-stream session (legacy or degraded) — go straight to tmux kill.
  // chat_streamd.chatKill would be preferred for chat-stream-tracked sessions,
  // but the chatClose branch above already handled those.
  if (state.degraded) {
    await window.cc.killTmuxSession(hostId, name);
    return;
  }
  const killResult = await window.cc.chatKill({ sessionName: name, hostId });
  if (!killResult || killResult.ok !== true) {
    await window.cc.killTmuxSession(hostId, name);
  }
}

// Default depends on mode: clients prefer 'remote' (the mac-mini); hosts use 'local'.
// CFG_READY flips this to 'remote' once we know IS_CLIENT.
let newSessionLocation = 'local';
let newSessionStep = 'machine';
let newSessionTerminalMode = false;
const SPAWN_PREFERENCES_KEY = 'pentacle.spawnPreferences.v1';
const SPAWN_PREFERENCES_WRITER_ID = globalThis.crypto?.randomUUID?.() || `window-${Date.now()}-${Math.random().toString(16).slice(2)}`;
let newSessionCatalog = null;
let newSessionSelection = null;
let newSessionSubmitting = false;
const spawnCatalogLoader = createSpawnCatalogLoader(() => window.cc.chatSpawnCatalog());
// Bumped whenever a spawn is aborted (operator escaped) or the modal is closed,
// so a late-resolving in-flight spawn cannot re-drive/reopen a modal the operator
// has already dismissed. The modal must ALWAYS be escapable.
let newSessionSubmitToken = 0;
// After this point, keep waiting for the authoritative daemon result but make
// the delay explicit. The modal remains dismissible throughout.
const NEW_SESSION_LONG_WAIT_MS = 30000;
let newSessionError = '';

function waitForNewSessionSpawn(spawnPromise, onLongWait, waitMs) {
  const timer = setTimeout(onLongWait, waitMs);
  return Promise.resolve(spawnPromise).finally(() => clearTimeout(timer));
}

// Abort any in-flight spawn continuation and clear the submitting flag. Callers
// that dismiss or navigate the modal use this so the "spawning" state can never
// get permanently stuck.
function abortNewSessionSubmit() {
  newSessionSubmitToken++;
  newSessionSubmitting = false;
}
let observedSpawnPreferences = null;

function spawnPreferences() {
  try {
    const value = JSON.parse(localStorage.getItem(SPAWN_PREFERENCES_KEY) || 'null');
    if (value?.schemaVersion === 1 && value?.byProvider) {
      if (!observedSpawnPreferences || preferenceIsNewer(value, observedSpawnPreferences)) observedSpawnPreferences = value;
      return value;
    }
  } catch (_) { /* a corrupt local preference must not block a chat */ }
  return { schemaVersion: 1, revision: 0, defaultProvider: 'codex', byProvider: {} };
}

function saveSpawnPreference(provider, tuple) {
  const current = spawnPreferences();
  const storedTuple = (id, preference) => {
    const resolved = catalogTuple(id, preference);
    return resolved ? { model: resolved.model, effort: resolved.effort } : null;
  };
  const byProvider = {
    claude: storedTuple('claude', current.byProvider?.claude),
    codex: storedTuple('codex', current.byProvider?.codex),
    [provider]: { model: tuple.model, effort: tuple.effort },
  };
  try {
    const next = {
      schemaVersion: 1,
      revision: Number(current.revision || 0) + 1,
      writerId: SPAWN_PREFERENCES_WRITER_ID,
      defaultProvider: provider,
      byProvider,
      updatedAt: new Date().toISOString(),
    };
    localStorage.setItem(SPAWN_PREFERENCES_KEY, JSON.stringify(next));
    observedSpawnPreferences = next;
  } catch (_) {
    newSessionError = 'This selection will be used now but cannot be saved in this window.';
  }
}

function preferenceIsNewer(incoming, current) {
  const revision = Number(incoming?.revision || 0) - Number(current?.revision || 0);
  return revision > 0 || (revision === 0 && String(incoming?.writerId || '') > String(current?.writerId || ''));
}

function catalogTuple(provider, preferred = null) {
  const catalog = newSessionCatalog || {};
  const profile = catalog.profiles?.desktop_manual || {};
  const defaults = profile[provider];
  const entries = catalog.models?.[provider] || {};
  const remembered = preferred || spawnPreferences().byProvider?.[provider];
  const model = remembered?.model && entries[remembered.model] ? remembered.model : defaults?.[0];
  const efforts = entries[model]?.efforts || [];
  const effort = remembered?.effort && efforts.includes(remembered.effort) ? remembered.effort : (defaults?.[1] || efforts[0]);
  return model && effort ? { provider, model, effort } : null;
}

function selectionForProvider(provider) {
  return catalogTuple(provider, newSessionSelection?.provider === provider ? newSessionSelection : null);
}

function catalogProviders(catalog) {
  const profile = catalog?.profiles?.desktop_manual || {};
  const models = catalog?.models || {};
  return Object.keys(profile).filter((provider) => {
    const tuple = profile[provider];
    const efforts = Array.isArray(tuple) ? models[provider]?.[tuple[0]]?.efforts : null;
    return Array.isArray(efforts) && efforts.includes(tuple[1]);
  });
}

function modelLabel(model) {
  const labels = {
    'claude-opus-4-8': 'Opus 4.8',
    'claude-opus-5': 'Opus 5',
    'claude-sonnet-5': 'Sonnet 5',
    'claude-fable-5': 'Fable 5',
    'claude-fable-5-1': 'Fable 5.1',
    'gpt-5.6-sol': '5.6 Sol',
    'gpt-6-astra': '6 Astra',
    'gpt-5.6-terra': '5.6 Terra',
  };
  return labels[model] ?? model;
}

function updateNewSessionStatus(message = '', isError = false) {
  const status = document.getElementById('new-session-status');
  if (!status) return;
  status.textContent = message;
  status.classList.toggle('is-error', !!isError);
}

function setNewSessionBackgroundInert(inert) {
  const overlay = document.getElementById('new-session-overlay');
  Array.from(document.body.children).forEach((child) => {
    if (child === overlay) return;
    if (inert) {
      child.dataset.newSessionAriaHidden = child.getAttribute('aria-hidden') || '';
      child.setAttribute('aria-hidden', 'true');
      child.inert = true;
    } else if (Object.hasOwn(child.dataset, 'newSessionAriaHidden')) {
      const prior = child.dataset.newSessionAriaHidden;
      if (prior) child.setAttribute('aria-hidden', prior); else child.removeAttribute('aria-hidden');
      delete child.dataset.newSessionAriaHidden;
      child.inert = false;
    }
  });
}

function getNewSessionHostOptions() {
  const hostIds = Array.isArray(HOST_IDS) && HOST_IDS.length ? HOST_IDS : ['local'];
  return hostIds.map((id) => ({
    id,
    label: getSourceForSession('', id) || id,
    color: getSourceColorForSession('', id),
  }));
}

function getNewSessionAgentOptions() {
  const configured = CONFIG.agents || {};
  const preferred = ['claude', 'codex'].filter((id) => configured[id]);
  const rest = Object.keys(configured).filter((id) => !preferred.includes(id)).sort();
  const ids = [...preferred, ...rest];
  if (!ids.length) ids.push('codex');
  return ids.map((id) => ({
    id,
    label: configured[id]?.label || providerLabelForHero(id),
  }));
}

function renderNewSessionLocationOptions(container) {
  const options = getNewSessionHostOptions();
  if (options.length === 0) {
    container.innerHTML = '<div class="new-session-empty">No machines configured.</div>';
    return;
  }

  if (!options.some((opt) => opt.id === newSessionLocation)) {
    newSessionLocation = options[0].id;
  }

  container.innerHTML = options.map((opt) => (
    `<button class="new-session-option new-session-machine color-${esc(opt.color)}" data-loc="${esc(opt.id)}" title="Select ${esc(opt.label)}">
      <span class="new-session-option-mark">${esc(getSourceInitial(opt.label))}</span>
      <span class="new-session-option-label">${esc(opt.label)}</span>
      <span class="new-session-option-meta">Machine</span>
    </button>`
  )).join('');

  container.querySelectorAll('[data-loc]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      newSessionLocation = btn.dataset.loc;
      newSessionStep = newSessionTerminalMode ? 'agent' : 'profile';
      renderNewSessionModal();
      if (!newSessionTerminalMode) await loadSpawnCatalog();
    });
  });
}

function renderNewSessionAgentOptions(container) {
  const machine = getNewSessionHostOptions().find((opt) => opt.id === newSessionLocation);
  const machineLabel = machine?.label || newSessionLocation || 'machine';
  const agents = getNewSessionAgentOptions();
  container.innerHTML = agents.map((agent) => (
    `<button class="new-session-option new-session-agent" data-agent="${esc(agent.id)}" title="Start ${esc(agent.label)} on ${esc(machineLabel)}">
      <span class="new-session-option-mark">${esc(getSourceInitial(agent.label))}</span>
      <span class="new-session-option-label">${esc(agent.label)}</span>
      <span class="new-session-option-meta">${esc(machineLabel)}</span>
    </button>`
  )).join('');

  container.querySelectorAll('[data-agent]').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (newSessionTerminalMode) {
        newTerminalSession(btn.dataset.agent, newSessionLocation);
      } else {
        newSession(btn.dataset.agent, newSessionLocation);
      }
    });
  });
}

function renderSpawnProfileOptions(container) {
  const selection = newSessionSelection;
  if (!selection || !newSessionCatalog) {
    if (newSessionError) {
      // A bounded catalog load that failed/timed out must show a visible error
      // and a Retry — not an infinite "Loading…" spinner. Retry re-issues the
      // load (the loader's in-flight already cleared on the reject/timeout).
      container.innerHTML = `<div class="new-session-empty new-session-error" role="alert">
        <div class="new-session-error-msg">${esc(newSessionError)}</div>
        <button type="button" id="spawn-catalog-retry" class="new-session-retry">Retry</button>
      </div>`;
      container.querySelector('#spawn-catalog-retry')?.addEventListener('click', () => { loadSpawnCatalog(); });
      updateNewSessionStatus(newSessionError, true);
      return;
    }
    container.innerHTML = '<div class="new-session-empty">Loading available spawn profiles…</div>';
    updateNewSessionStatus('Loading available choices.', false);
    return;
  }
  const entries = newSessionCatalog.models?.[selection.provider] || {};
  const models = Object.keys(entries);
  const providers = catalogProviders(newSessionCatalog);
  const efforts = entries[selection.model]?.efforts || [];
  const title = document.getElementById('new-session-title');
  const subtitle = document.getElementById('new-session-subtitle');
  if (title) title.textContent = 'New Chat';
  if (subtitle) subtitle.textContent = 'Choose the provider and launch profile.';
  container.innerHTML = `
    <div class="spawn-profile-controls" aria-label="Chat spawn profile">
      <div class="spawn-profile-row"><label for="spawn-provider">Provider</label><select id="spawn-provider" class="spawn-profile-control" aria-label="Provider">${providers.map((provider) => `<option value="${esc(provider)}"${provider === selection.provider ? ' selected' : ''}>${esc(providerLabelForHero(provider))}</option>`).join('')}</select></div>
      <div class="spawn-profile-row"><label for="spawn-model">Model</label><select id="spawn-model" class="spawn-profile-control" aria-label="Model">${models.map((model) => `<option value="${esc(model)}"${model === selection.model ? ' selected' : ''}>${esc(modelLabel(model))}</option>`).join('')}</select></div>
      <div class="spawn-profile-row"><label for="spawn-effort">Effort</label><select id="spawn-effort" class="spawn-profile-control" aria-label="Reasoning effort">${efforts.map((effort) => `<option value="${esc(effort)}"${effort === selection.effort ? ' selected' : ''}>${esc(effort)}</option>`).join('')}</select></div>
    </div>`;
  container.querySelector('#spawn-provider')?.addEventListener('change', (event) => {
    newSessionSelection = selectionForProvider(event.target.value);
    saveSpawnPreference(newSessionSelection.provider, newSessionSelection);
    newSessionError = '';
    renderNewSessionModal();
    updateNewSessionStatus(`Provider changed to ${newSessionSelection.provider}.`);
  });
  container.querySelector('#spawn-model')?.addEventListener('change', (event) => {
    const model = event.target.value;
    const next = catalogTuple(selection.provider, { model, effort: selection.effort });
    newSessionSelection = next;
    saveSpawnPreference(next.provider, next);
    newSessionError = '';
    renderNewSessionModal();
    updateNewSessionStatus(`Version changed to ${modelLabel(model)}.`);
  });
  container.querySelector('#spawn-effort')?.addEventListener('change', (event) => {
    newSessionSelection = { ...selection, effort: event.target.value };
    saveSpawnPreference(newSessionSelection.provider, newSessionSelection);
    newSessionError = '';
    renderNewSessionModal();
    updateNewSessionStatus(`Effort changed to ${event.target.value}.`);
  });
  updateNewSessionStatus(newSessionError || `${selection.provider} ${modelLabel(selection.model)} ${selection.effort}.`, !!newSessionError);
}

async function loadSpawnCatalog() {
  newSessionError = '';
  newSessionCatalog = spawnCatalogLoader.peek();
  if (!newSessionCatalog) renderNewSessionModal();
  let response;
  try {
    response = await spawnCatalogLoader.load();
  } catch (error) {
    response = { ok: false, error: error?.message || String(error) };
  }
  if (!response?.ok || !response.catalog?.profiles?.desktop_manual) {
    newSessionError = response?.error || 'Could not load available spawn profiles.';
    renderNewSessionModal();
    return false;
  }
  newSessionCatalog = response.catalog;
  const prefs = spawnPreferences();
  newSessionSelection = selectionForProvider(prefs.defaultProvider === 'claude' ? 'claude' : 'codex');
  renderNewSessionModal();
  return true;
}

function warmSpawnCatalog() {
  spawnCatalogLoader.load().then((response) => {
    if (!response?.ok || !response.catalog?.profiles?.desktop_manual) return;
    newSessionCatalog = response.catalog;
    const prefs = spawnPreferences();
    newSessionSelection = selectionForProvider(prefs.defaultProvider === 'claude' ? 'claude' : 'codex');
  }).catch(() => {
    // A disconnected cold start remains retryable through loadSpawnCatalog().
  });
}

function newSessionModalIsOpen() {
  return document.getElementById('new-session-overlay')?.style.display !== 'none';
}

// On the disconnect→connect edge the WS rejected every in-flight command
// (spawn_catalog_get included), so no stale 'loading'/'inFlight' survives the
// socket cycle. If the New Chat dialog is parked on the profile step without a
// catalog (it was spinning or showing the error+retry), re-issue the load so it
// repaints; otherwise just re-warm the cache. The roster itself re-syncs from
// the reconnect snapshot (nextChatStreamSessions) that this same frame carries.
function resyncSpawnCatalogAfterReconnect() {
  if (newSessionModalIsOpen() && newSessionStep === 'profile' && !newSessionCatalog) {
    loadSpawnCatalog();
  } else {
    warmSpawnCatalog();
  }
}

function renderNewSessionModal() {
  const container = document.getElementById('new-session-location');
  const title = document.getElementById('new-session-title');
  const subtitle = document.getElementById('new-session-subtitle');
  const back = document.getElementById('new-session-back');
  if (!container) return;

  const spawn = document.getElementById('new-session-spawn');
  const cancel = document.getElementById('new-session-cancel');
  if (newSessionStep === 'profile') {
    if (title) title.textContent = 'New Chat';
    if (subtitle) subtitle.textContent = 'Choose the provider and launch profile.';
    if (back) back.style.display = '';
    if (spawn) {
      spawn.style.display = '';
      spawn.disabled = !newSessionSelection || newSessionSubmitting;
      spawn.textContent = newSessionSubmitting ? 'Spawning…' : 'Spawn';
      spawn.classList.toggle('is-spawning', newSessionSubmitting);
    }
    // Cancel is NEVER disabled — the modal must always be escapable, even mid-spawn.
    if (cancel) cancel.disabled = false;
    if (back) back.disabled = false;
    renderSpawnProfileOptions(container);
    return;
  }
  if (spawn) spawn.style.display = 'none';
  if (cancel) cancel.disabled = false;
  if (newSessionStep === 'agent') {
    const machine = getNewSessionHostOptions().find((opt) => opt.id === newSessionLocation);
    const machineLabel = machine?.label || newSessionLocation || 'machine';
    if (title) title.textContent = newSessionTerminalMode ? 'New Terminal' : 'New Chat';
    if (subtitle) subtitle.textContent = newSessionTerminalMode
      ? `Start a raw terminal session on ${machineLabel}.`
      : `Start a structured chat on ${machineLabel}.`;
    if (back) back.style.display = '';
    renderNewSessionAgentOptions(container);
    return;
  }

  if (title) title.textContent = newSessionTerminalMode ? 'New Terminal' : 'New Chat';
  if (subtitle) subtitle.textContent = 'Select a machine.';
  if (back) back.style.display = 'none';
  renderNewSessionLocationOptions(container);
}

function showNewSessionModal(terminalMode = false) {
  newSessionTerminalMode = !!terminalMode;
  newSessionStep = 'machine';
  newSessionError = '';
  newSessionSubmitting = false;
  renderNewSessionModal();
  document.getElementById('new-session-overlay').style.display = 'flex';
  setNewSessionBackgroundInert(true);
  document.getElementById('new-session-title')?.focus();
}

function hideNewSessionModal() {
  // Always closeable — never gate dismissal on submit state, or a stuck spawn
  // would trap the operator (restart-only recovery). Aborting the in-flight
  // submit also clears the "spawning" flag.
  abortNewSessionSubmit();
  document.getElementById('new-session-overlay').style.display = 'none';
  setNewSessionBackgroundInert(false);
}

async function newSession(options = {}, locationOverride = null) {
  if (newSessionSubmitting) return null; // guard against overlapping spawns
  const input = typeof options === 'string' ? { provider: options, hostId: locationOverride } : options;
  const location = input.hostId || locationOverride || newSessionLocation || 'local';
  if (!newSessionCatalog && !await loadSpawnCatalog()) return null;
  const selection = input.provider && input.model && input.effort
    ? { provider: input.provider, model: input.model, effort: input.effort }
    : (newSessionSelection || selectionForProvider(input.provider || spawnPreferences().defaultProvider || 'codex'));
  if (!selection) return null;
  const profileDefault = newSessionCatalog.profiles?.desktop_manual?.[selection.provider] || [];
  const myToken = ++newSessionSubmitToken;
  newSessionSubmitting = true;
  newSessionError = '';
  renderNewSessionModal();
  updateNewSessionStatus('Starting…');

  // Find first empty slot, or default to slot 3 (swap out)
  let slot = state.slots.findIndex(s => s === null);
  if (slot === -1) slot = 3;

  let result;
  try {
    result = await waitForNewSessionSpawn(
      window.cc.chatSpawnV2({
        hostId: location,
        provider: selection.provider,
        model: selection.model,
        effort: selection.effort,
        spawnProfile: 'desktop_manual',
        catalogVersion: newSessionCatalog.catalog_version,
        resolutionSource: input.provider && input.model && input.effort
          ? 'explicit_override'
          : (selection.model === profileDefault[0] && selection.effort === profileDefault[1] ? 'profile_default' : 'explicit_override'),
      }),
      () => {
        if (myToken !== newSessionSubmitToken || !newSessionSubmitting) return;
        updateNewSessionStatus('Still waiting for startup to finish. You can close this window; the chat will appear in the sidebar if it succeeds.');
      },
      NEW_SESSION_LONG_WAIT_MS,
    );
  } catch (err) {
    result = { ok: false, error: (err && err.message) || 'Failed to start chat' };
  } finally {
    // Only this submit clears the flag; an operator escape already bumped the
    // token and cleared it for a possible next attempt.
    if (myToken === newSessionSubmitToken) {
      newSessionSubmitting = false;
    }
  }

  // Operator escaped (Cancel/ESC/Back) while the spawn was in flight: abandon
  // the UI flow. A backend session that still spawned surfaces via the normal
  // sidebar/stream refresh, so nothing is orphaned.
  if (myToken !== newSessionSubmitToken) return null;

  if (!result?.ok) {
    newSessionError = result?.error?.message || result?.error || 'Failed to start chat';
    renderNewSessionModal();
    updateNewSessionStatus(newSessionError, true);
    return null;
  }
  if (!result.session) {
    hideNewSessionModal();
    return null;
  }
  hideNewSessionModal();
  const sessionName = result.session.sessionName || result.session.session_name;
  const hostId = result.session.hostId || location;
  const displayName = result.session.displayName || result.session.display_name || sessionName;
  if (!sessionName) return;

  // Attach it to the slot. A new session attaches on the Terminal default
  // (attachSession sets 'terminal'); Chat/Status remain per-cell toggles. This
  // keeps new-session opens consistent with ordinary sidebar opens.
  await attachSession(slot, sessionName, displayName, hostId);
  updateSlotViewMode(slot, 'terminal');

  const agentConfig = CONFIG.agents[selection.provider] || CONFIG.agents.codex || {};
  if (agentConfig.startupMessage) {
    const startupDelayMs = Number(agentConfig.startupDelayMs) || 1500;
    setTimeout(() => {
      if (state.slots[slot]?.name !== sessionName) return;
      sendProgrammaticInput(slot, agentConfig.startupMessage);
    }, startupDelayMs);
  }

  window.PentacleHarness?.emit?.('session:spawn', {
    provider: selection.provider,
    slot,
    data: {
      sessionName,
      streamId: result.session.stream_id || null,
      kind: 'chat',
      selection: { ...selection },
      requested: result.requested,
      resolved: result.resolved,
      actualLaunch: result.actualLaunch,
      resolutionSource: result.resolutionSource,
      catalogVersion: result.catalogVersion,
    },
  });

  // Return the spawned session info (existing UI callers ignore it; the E2E
  // harness action shim uses it to drive + assert a freshly-spawned throwaway).
  return {
    sessionName, hostId, displayName, streamId: result.session.stream_id || null, slot,
    spawnAck: { requested: result.requested, resolved: result.resolved, actualLaunch: result.actualLaunch, resolutionSource: result.resolutionSource, catalogVersion: result.catalogVersion },
  };
}

async function newTerminalSession(agent = 'codex', locationOverride = null) {
  const location = locationOverride || newSessionLocation || 'local';
  hideNewSessionModal();
  let slot = state.slots.findIndex(s => s === null);
  if (slot === -1) slot = 3;
  const result = await window.cc.newSession(agent, location);
  if (!result) return;
  const sessionName = result.sessionName || result;
  const hostId = result.hostId || location;
  await attachSession(slot, sessionName, sessionName, hostId);
  window.PentacleHarness?.emit?.('session:spawn', { provider: agent, slot, data: { sessionName, kind: 'terminal' } });
  return { sessionName, hostId, slot };
}

// ── Rename Modal ───────────────────────────────────────────────

let renameTarget = null;

function showRenameModal(sessionName, currentTitle, hostId) {
  const resolvedHostId = hostId || (IS_CLIENT ? 'remote' : 'local');
  if (isProtectedAssistantNameHost(sessionName, resolvedHostId)) {
    showToast('This configured assistant cannot be renamed', { type: 'error' });
    return;
  }
  renameTarget = { sessionName, hostId: resolvedHostId };
  document.getElementById('modal-title').textContent = `Rename: ${sessionName}`;
  document.getElementById('modal-input').value = currentTitle || sessionName;
  document.getElementById('modal-overlay').style.display = 'flex';
  document.getElementById('modal-input').focus();
  document.getElementById('modal-input').select();
}

function hideRenameModal() {
  document.getElementById('modal-overlay').style.display = 'none';
  renameTarget = null;
}

document.getElementById('modal-cancel').addEventListener('click', hideRenameModal);
document.getElementById('modal-overlay').addEventListener('click', (e) => {
  if (e.target === document.getElementById('modal-overlay')) hideRenameModal();
});

document.getElementById('modal-confirm').addEventListener('click', async () => {
  const newName = document.getElementById('modal-input').value.trim();
  if (newName && renameTarget) {
    if (isProtectedAssistantNameHost(renameTarget.sessionName, renameTarget.hostId)) {
      showToast('This configured assistant cannot be renamed', { type: 'error' });
      hideRenameModal();
      return;
    }
    const streamSession = chatSessionStateForNameHost(renameTarget.sessionName, renameTarget.hostId);
    const isChatSession = !!streamSession?.stream_id;
    if (isChatSession) {
      // Chat sessions are chat_streamd-stateful: rename has to round-trip
      // through the daemon to keep agent_id ↔ display_name in sync. If
      // the WS is unreachable, refuse rather than fall through to
      // setWindowTitle (which would desync the chat_streamd metadata).
      if (state.degraded || !state.chatStream.connected) return;
      const result = await window.cc.chatRename(renameTarget.hostId, renameTarget.sessionName, newName);
      if (!result?.ok) {
        showToast(result?.error || 'Failed to rename chat', { type: 'error' });
        return;
      }
    } else {
      // Terminal-only session: direct tmux:set-window-title is
      // direct-tmux-safe and stays available in degraded mode.
      await window.cc.setWindowTitle(renameTarget.hostId, renameTarget.sessionName, newName, 'manual');
    }
    window.PentacleHarness?.emit?.('session:rename', { host: renameTarget.hostId, data: { sessionName: renameTarget.sessionName, newName } });
    hideRenameModal();
  }
});

document.getElementById('modal-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') document.getElementById('modal-confirm').click();
  if (e.key === 'Escape') hideRenameModal();
});

// ── View Switcher (Chats / Dashboards) ───────────────────────

function switchView(view) {
  if (state.currentView === view) return;
  window.PentacleHarness?.emit?.('view:switch', { data: { view } });

  // Cleanup current dashboard if leaving dashboards view
  if (state.currentView === 'dashboards') {
    stopDashboardPolling();
    unmountCurrentDashboard();
  }

  state.currentView = view;

  // Toggle DOM visibility
  document.querySelector('.grid').style.display = view === 'chats' ? '' : 'none';
  gridColResizer?.refresh();
  document.getElementById('dashboard-content').style.display = view === 'dashboards' ? '' : 'none';
  document.getElementById('panel-dashboards').style.display = view === 'dashboards' ? 'flex' : 'none';

  // Update view switcher buttons
  document.querySelectorAll('.view-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.view === view));

  if (view === 'chats') {
    // Restore sessions panel.
    document.getElementById('panel-sessions').style.display = 'flex';
    scheduleVisibleSlotFits();
  } else {
    // Hide chat panels
    document.getElementById('panel-sessions').style.display = 'none';
    renderDashboardList();
    if (!state.selectedDashboard && window.DASHBOARDS.length > 0) {
      const preferred = window.DASHBOARDS.find(d => d.id === 'foreclosure-pipeline');
      selectDashboard((preferred || window.DASHBOARDS[0]).id);
    } else if (state.selectedDashboard) {
      mountAndPoll(state.selectedDashboard);
    }
  }
}

function selectDashboard(id) {
  if (state.selectedDashboard === id) return;
  stopDashboardPolling();
  unmountCurrentDashboard();
  state.selectedDashboard = id;
  state.dashboardLastData = null;
  state.dashboardState = 'loading';
  renderDashboardList(); // update active highlight
  mountAndPoll(id);
}

function mountAndPoll(id) {
  const db = window.DASHBOARDS.find(d => d.id === id);
  if (!db) return;
  // Reset dashboard state for fresh mount
  state.dashboardState = 'loading';
  state.dashboardError = null;
  state.dashboardLastData = null;
  state.dashboardLastUpdated = null;
  const container = document.getElementById('dashboard-content');
  container.innerHTML = ''; // clear previous
  state.dashboardRefs = db.mount(container);
  window.PentacleHarness?.emit?.('dashboard:mount', { data: { id, name: db.name } });
  updateDashboardStatusBadge();
  startDashboardPolling();
}

function unmountCurrentDashboard() {
  if (state.dashboardRefs && state.selectedDashboard) {
    const db = window.DASHBOARDS.find(d => d.id === state.selectedDashboard);
    if (db && db.unmount) db.unmount(state.dashboardRefs);
    window.PentacleHarness?.emit?.('dashboard:unmount', { data: { id: state.selectedDashboard } });
  }
  state.dashboardRefs = null;
}

function renderDashboardList() {
  const list = document.getElementById('dashboard-list');
  if (!list) return;
  list.innerHTML = window.DASHBOARDS.map(d => {
    const isActive = d.id === state.selectedDashboard;
    return `<div class="dashboard-item ${isActive ? 'active' : ''}" data-dashboard-id="${d.id}">
      <div class="dashboard-item-top">
        <span class="dashboard-dot" style="background:${d.color}"></span>
        <span class="dashboard-name">${d.name}</span>
      </div>
      <div class="dashboard-desc">${d.description}</div>
    </div>`;
  }).join('');

  list.querySelectorAll('.dashboard-item').forEach(el => {
    el.addEventListener('click', () => selectDashboard(el.dataset.dashboardId));
  });
}

// ── Dashboard Polling ─────────────────────────────────────────

function startDashboardPolling() {
  stopDashboardPolling();
  const db = window.DASHBOARDS.find(d => d.id === state.selectedDashboard);
  if (!db || !state.dashboardRefs) return;
  if (typeof db.pollFn !== 'function' || !db.pollInterval) {
    state.dashboardState = 'loaded';
    state.dashboardError = null;
    updateDashboardStatusBadge();
    return;
  }

  state.dashboardPollToken++;
  const token = state.dashboardPollToken;
  let inFlight = false; // closure-scoped per generation
  let currentInterval = db.pollInterval; // may flip to idlePollInterval when idle

  // Re-arm the setInterval with a new period if it changed (e.g. active→idle).
  // Dashboards that opt in provide `idleFn(data) -> bool` and `idlePollInterval`
  // in their registration — when idleFn returns true, we poll at the slower
  // rate until idleFn returns false again.
  function _retuneInterval(nextInterval) {
    if (nextInterval === currentInterval) return;
    currentInterval = nextInterval;
    if (state.dashboardPollTimer) clearInterval(state.dashboardPollTimer);
    state.dashboardPollTimer = setInterval(poll, currentInterval);
  }

  async function poll() {
    if (inFlight) return;
    if (state.dashboardPollToken !== token) return;
    inFlight = true;
    try {
      // Pass refs so dashboards that need state (e.g. 0DTE selected trader)
      // can read it without going through localStorage on every tick.
      const data = await db.pollFn(state.dashboardRefs);
      if (state.dashboardPollToken !== token) return; // stale generation
      if (data && !data.error) {
        state.dashboardLastData = data;
        state.dashboardLastUpdated = new Date();
        state.dashboardState = 'loaded';
        state.dashboardError = null;
        db.update(state.dashboardRefs, data);
        window.PentacleHarness?.emit?.('dashboard:loaded', { data: { id: db.id, name: db.name } });
        updateDashboardStatusBadge();

        // Idle-slowdown: if the dashboard declared itself idle for this data,
        // bump the interval down to `idlePollInterval`. If it's no longer
        // idle, snap back to the normal interval.
        if (typeof db.idleFn === 'function' && db.idlePollInterval) {
          const isIdle = !!db.idleFn(data);
          _retuneInterval(isIdle ? db.idlePollInterval : db.pollInterval);
        }
      } else {
        state.dashboardError = data?.error || 'Unknown error';
        state.dashboardState = state.dashboardLastData ? 'stale' : 'error';
        window.PentacleHarness?.emit?.('dashboard:error', { data: { id: db.id, error: String(state.dashboardError) } });
        updateDashboardStatusBadge();
      }
    } catch (e) {
      if (state.dashboardPollToken !== token) return;
      state.dashboardError = e.message;
      state.dashboardState = state.dashboardLastData ? 'stale' : 'error';
      window.PentacleHarness?.emit?.('dashboard:error', { data: { id: db.id, error: e.message } });
      updateDashboardStatusBadge();
    } finally {
      inFlight = false;
    }
  }

  state.dashboardPollNow = poll;
  poll();
  state.dashboardPollTimer = setInterval(poll, currentInterval);
}

function stopDashboardPolling() {
  if (state.dashboardPollTimer) {
    clearInterval(state.dashboardPollTimer);
    state.dashboardPollTimer = null;
  }
  state.dashboardPollNow = null;
  state.dashboardPollToken++; // invalidates any in-flight poll for the old generation
}

function updateDashboardStatusBadge() {
  if (!state.dashboardRefs) return;
  const { statusBadge, lastUpdated, retryBtn } = state.dashboardRefs;
  if (!statusBadge) return;

  if (state.dashboardState === 'loading') {
    statusBadge.textContent = 'Loading...';
    statusBadge.className = 'pipeline-status loading';
    if (retryBtn) retryBtn.style.display = 'none';
  } else if (state.dashboardState === 'loaded') {
    statusBadge.textContent = 'Live';
    statusBadge.className = 'pipeline-status live';
    if (lastUpdated) lastUpdated.textContent = 'Updated just now';
    if (retryBtn) retryBtn.style.display = 'none';
  } else if (state.dashboardState === 'stale') {
    const ago = state.dashboardLastUpdated
      ? Math.round((Date.now() - state.dashboardLastUpdated) / 1000)
      : '?';
    statusBadge.textContent = 'Stale';
    statusBadge.className = 'pipeline-status stale';
    if (lastUpdated) lastUpdated.textContent = `Last updated ${ago}s ago`;
    if (retryBtn) retryBtn.style.display = 'none';
  } else {
    statusBadge.textContent = 'Error';
    statusBadge.className = 'pipeline-status error';
    if (lastUpdated) lastUpdated.textContent = state.dashboardError || 'Connection failed';
    if (retryBtn) retryBtn.style.display = '';
  }
}

// Exposed for dashboard DOM actions.
window.refreshDashboardNow = function() {
  if (typeof state.dashboardPollNow === 'function') return state.dashboardPollNow();
  return startDashboardPolling();
};
window.retryDashboardPoll = function() { return window.refreshDashboardNow(); };
window.selectDashboard = selectDashboard;

document.querySelectorAll('.view-btn').forEach(btn => {
  btn.addEventListener('click', () => switchView(btn.dataset.view));
});

// ── Toolbar Buttons ────────────────────────────────────────────

document.getElementById('btn-new').addEventListener('click', () => showNewSessionModal(false));
document.getElementById('new-session-cancel').addEventListener('click', hideNewSessionModal);
document.getElementById('new-session-back').addEventListener('click', () => {
  // Back must work even mid-spawn — abort any in-flight submit, then navigate.
  abortNewSessionSubmit();
  newSessionStep = 'machine';
  newSessionError = '';
  renderNewSessionModal();
});
document.getElementById('new-session-overlay').addEventListener('click', (e) => {
  // Background clicks never dismiss a partially configured spawn.
});
document.getElementById('new-session-spawn').addEventListener('click', () => newSession());
document.addEventListener('keydown', (event) => {
  const overlay = document.getElementById('new-session-overlay');
  if (overlay?.style.display === 'none') return;
  if (event.key === 'Escape') {
    // Always escapable, even mid-spawn — hideNewSessionModal aborts the submit.
    hideNewSessionModal();
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = [...overlay.querySelectorAll('button:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])')];
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});
window.addEventListener('storage', (event) => {
  if (event.key !== SPAWN_PREFERENCES_KEY || !event.newValue) return;
  try {
    const incoming = JSON.parse(event.newValue);
    if (incoming?.schemaVersion !== 1 || !preferenceIsNewer(incoming, observedSpawnPreferences)) return;
    observedSpawnPreferences = incoming;
    if (document.getElementById('new-session-overlay')?.style.display === 'none' && newSessionCatalog) {
      newSessionSelection = catalogTuple(incoming.defaultProvider === 'claude' ? 'claude' : 'codex', incoming.byProvider?.[incoming.defaultProvider]);
    }
  } catch (_) { /* ignore malformed external storage */ }
});
document.getElementById('session-search')?.addEventListener('input', (e) => {
  state.sessionSearch = e.target.value || '';
  renderSidebar();
});

// ── Slot Header Buttons ───────────────────────────────────────

document.querySelectorAll('.cell-close').forEach(btn => {
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const slot = parseInt(btn.dataset.slot);
    detachSlot(slot);
  });
});

document.querySelectorAll('.cell-edit').forEach(btn => {
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const slot = parseInt(btn.dataset.slot);
    if (state.slots[slot] && !state.botSlots[slot]) {
      showRenameModal(state.slots[slot].name, state.slots[slot].displayName, state.slots[slot].hostId);
    }
  });
});

document.querySelectorAll('.cell-trash').forEach(btn => {
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const slot = parseInt(btn.dataset.slot);
    if (state.slots[slot] && !state.botSlots[slot] && !isProtectedAssistantNameHost(state.slots[slot].name, state.slots[slot].hostId)) {
      deleteSession(state.slots[slot].name, state.slots[slot].hostId);
    }
  });
});

document.querySelectorAll('.cell-maximize').forEach(btn => {
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const slot = parseInt(btn.dataset.slot);
    maximizeSlot(slot);
  });
});

// Also allow double-click on cell header to toggle maximize
document.querySelectorAll('.cell-header').forEach(header => {
  header.addEventListener('dblclick', (e) => {
    const slot = parseInt(header.id.replace('header-', ''));
    if (state.slots[slot]) {
      maximizeSlot(slot);
    }
  });
});

// Session Status glyph — toggles the dedicated card view for a chat slot.
document.querySelectorAll('.cell-status').forEach(btn => {
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (btn.disabled) return;
    toggleSlotStatusCard(parseInt(btn.dataset.slot));
  });
});

// ── Usage Footer ──────────────────────────────────────────────

function setSidebarSectionExpanded(sectionId, expanded, persist = false) {
  state.sidebarSections[sectionId] = !!expanded;
  const domId = sectionId === 'machineStats' ? 'machine-stats' : sectionId;
  const section = document.getElementById(`${domId}-section`);
  const toggle = document.getElementById(`${domId}-section-toggle`);
  const caret = document.querySelector(`[data-sidebar-section-caret="${sectionId}"]`);
  const body = document.getElementById(`${domId}-section-body`);
  if (section) section.classList.toggle('is-collapsed', !expanded);
  if (toggle) toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  if (caret) caret.textContent = expanded ? '\u25BC' : '\u25B6';
  if (body) body.hidden = !expanded;
}

function setupSidebarCollapsibles(root = document) {
  root.querySelectorAll('[data-sidebar-section-toggle]').forEach((toggle) => {
    const sectionId = toggle.dataset.sidebarSectionToggle;
    setSidebarSectionExpanded(sectionId, state.sidebarSections[sectionId] !== false);
    toggle.addEventListener('click', () => {
      setSidebarSectionExpanded(sectionId, state.sidebarSections[sectionId] === false, true);
    });
    toggle.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      setSidebarSectionExpanded(sectionId, state.sidebarSections[sectionId] === false, true);
    });
  });
}

function usageBarClass(pct) {
  if (pct >= 80) return 'high';
  if (pct >= 50) return 'mid';
  return 'low';
}

function formatResetRelative(ms) {
  const totalMin = Math.max(0, Math.round(Number(ms) / 60000) || 0);
  const hours = Math.floor(totalMin / 60);
  const minutes = totalMin % 60;
  if (hours > 0 && minutes > 0) return `in ${hours}h ${minutes}m`;
  if (hours > 0) return `in ${hours}h`;
  return `in ${minutes}m`;
}

function parseUsageResetDate(text, now = new Date()) {
  const value = String(text || '').trim();
  if (!value) return null;
  const currentYear = now.getFullYear();
  const cleaned = value
    .replace(/^resets?\s+/i, '')
    .replace(/\s+at\s+/i, ' ')
    .replace(/\s+\(([A-Za-z_]+\/[A-Za-z_]+)\)$/i, '')
    .trim();
  const relativeDay = cleaned.match(/^(today|tomorrow)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)$/i);
  if (relativeDay) {
    const candidate = new Date(now);
    let hour = Number(relativeDay[2]);
    const minute = Number(relativeDay[3] || 0);
    const meridiem = relativeDay[4].toLowerCase();
    if (meridiem === 'pm' && hour < 12) hour += 12;
    if (meridiem === 'am' && hour === 12) hour = 0;
    candidate.setHours(hour, minute, 0, 0);
    if (relativeDay[1].toLowerCase() === 'tomorrow') candidate.setDate(candidate.getDate() + 1);
    if (candidate.getTime() <= now.getTime()) candidate.setDate(candidate.getDate() + 1);
    return candidate;
  }
  const timeOnly = cleaned.match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$/i);
  if (timeOnly) {
    const candidate = new Date(now);
    let hour = Number(timeOnly[1]);
    const minute = Number(timeOnly[2] || 0);
    const meridiem = timeOnly[3].toLowerCase();
    if (meridiem === 'pm' && hour < 12) hour += 12;
    if (meridiem === 'am' && hour === 12) hour = 0;
    candidate.setHours(hour, minute, 0, 0);
    if (candidate.getTime() <= now.getTime()) candidate.setDate(candidate.getDate() + 1);
    return candidate;
  }
  const monthDate = cleaned.match(/^([A-Za-z]{3,9})\s+(\d{1,2})\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)$/i);
  if (monthDate) {
    const parsed = new Date(`${monthDate[1]} ${monthDate[2]} ${currentYear} ${monthDate[3]}:${monthDate[4] || '00'} ${monthDate[5]}`);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }
  const withYear = cleaned.match(/[A-Za-z]{3,9}\s+\d{1,2}/) && !/\b\d{4}\b/.test(cleaned)
    ? `${cleaned} ${currentYear}`
    : cleaned;
  const parsed = new Date(withYear);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function usageResetText(value, mode = 'relative') {
  let text = String(value || '').trim();
  text = text.replace(/^resets?\s+/i, '').trim();
  text = text.replace(/\s+/g, ' ');
  text = text.replace(/\s+at\s+/i, ' ');
  text = text.replace(/\s+\(([A-Za-z_]+\/[A-Za-z_]+)\)$/i, '');
  if (!text) return '↻ —';

  if (mode === 'relative') {
    const relative = text
      .replace(/\bhrs?\b/gi, 'h')
      .replace(/\bhours?\b/gi, 'h')
      .replace(/\bmins?\b/gi, 'm')
      .replace(/\bminutes?\b/gi, 'm')
      .replace(/(\d+)\s+([hm])/gi, '$1$2')
      .replace(/\s+/g, ' ')
      .trim();
    if (/^in\s+\d+/i.test(relative)) return `↻ ${relative}`;
    if (/^\d+[hm](?:\s+\d+[hm])?$/i.test(relative)) return `↻ in ${relative}`;
    const date = parseUsageResetDate(text);
    if (date) return `↻ ${formatResetRelative(date.getTime() - Date.now())}`;
    return `↻ ${relative}`;
  }

  const weekdayTime = text.match(/^(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)$/i);
  if (weekdayTime) {
    const day = weekdayTime[1].slice(0, 3);
    return `↻ ${day.charAt(0).toUpperCase()}${day.slice(1).toLowerCase()} · ${Number(weekdayTime[2])}:${String(weekdayTime[3] || '00').padStart(2, '0')} ${String(weekdayTime[4]).toUpperCase()}`;
  }
  const date = parseUsageResetDate(text);
  if (date) {
    const weekday = new Intl.DateTimeFormat(undefined, { weekday: 'short' }).format(date);
    const time = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit', hour12: true })
      .format(date)
      .replace(/\s*([ap])\.?m\.?/i, ' $1M')
      .toUpperCase();
    return `↻ ${weekday} · ${time}`;
  }

  return text.startsWith('↻ ') ? text : `↻ ${text}`;
}

function paintLimits(limits) {
  const usageSection = document.getElementById('usage-section');
  const footer = document.getElementById('limits-footer');
  const visible = Array.isArray(limits) && limits.length === 3;
  if (usageSection) usageSection.style.display = visible ? '' : 'none';
  if (!footer) return;
  if (!visible) {
    footer.innerHTML = '';
    return;
  }
  footer.innerHTML = limits.map((limit) => {
    const pct = limit.pct;
    const pctText = pct == null ? '—' : `${pct}%`;
    const reset = limit.resets_text ?? limit.resets_at_iso;
    return `
      <div class="usage-compact-item" data-limit-id="${esc(limit.id)}">
        <div class="usage-label">
          <span>${esc(limit.label)}</span>
          <span>${pctText}</span>
        </div>
        <div class="usage-bar">
          <div class="usage-bar-fill ${usageBarClass(pct == null ? 0 : pct)}" style="width:${pct == null ? 0 : pct}%"></div>
        </div>
        <div class="usage-resets">${esc(usageResetText(reset, 'absolute'))}</div>
      </div>`;
  }).join('');
}

function renderLimits(limits, health) {
  const rows = limitsContract.validatedLimits(limits);
  const validatedHealth = limitsContract.validatedLimitsHealth(health ?? null);
  if (!rows || validatedHealth === undefined) return;
  paintLimits(rows);
  const banner = document.getElementById('limits-health');
  if (banner) {
    const error = validatedHealth?.claude?.error;
    banner.textContent = error ? `Usage probe failed; showing retained values. ${error.message.slice(0, 500)}` : '';
    banner.hidden = !error;
  }
}

// ── Machine Stats Footer ─────────────────────────────────────

function statNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function fmtStatPct(value) {
  const number = statNumber(value);
  return number === null ? '--' : Math.round(number) + '%';
}

function fmtStatBytes(value) {
  const number = statNumber(value);
  if (number === null || number < 0) return '--';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let scaled = number;
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) {
    scaled /= 1024;
    unit += 1;
  }
  const digits = unit === 0 ? 0 : (scaled >= 10 ? 1 : 2);
  return scaled.toFixed(digits).replace(/\.0+$/, '') + ' ' + units[unit];
}

function fmtStatLoad(value) {
  const number = statNumber(value);
  return number === null ? '--' : number.toFixed(2).replace(/\.00$/, '').replace(/(\.\d)0$/, '$1');
}

function fmtStatUptime(value) {
  let seconds = statNumber(value);
  if (seconds === null || seconds < 0) return '--';
  seconds = Math.floor(seconds);
  const days = Math.floor(seconds / 86400);
  seconds %= 86400;
  const hours = Math.floor(seconds / 3600);
  seconds %= 3600;
  const minutes = Math.floor(seconds / 60);
  if (days > 0) return days + 'd ' + hours + 'h';
  if (hours > 0) return hours + 'h ' + minutes + 'm';
  return minutes + 'm';
}

function statUsagePct(used, total) {
  const usedNumber = statNumber(used);
  const totalNumber = statNumber(total);
  if (usedNumber === null || totalNumber === null || totalNumber <= 0) return null;
  return Math.max(0, Math.min(100, (usedNumber / totalNumber) * 100));
}

function machineStatsIsStale(stats) {
  const sampledAt = Date.parse(String(stats?.sampled_at || ''));
  return !Number.isFinite(sampledAt) || Date.now() - sampledAt > 90 * 1000;
}

const MACHINE_STATS_STALE_INTERVAL_MS = 30 * 1000;
let machineStatsStaleTimer = null;

function startMachineStatsStaleReevaluation() {
  if (machineStatsStaleTimer !== null) return;
  machineStatsStaleTimer = setInterval(() => {
    if (Object.keys(state.chatStream.hostsStats || {}).length > 0) {
      renderHostsStats(state.chatStream.hostsStats);
    }
  }, MACHINE_STATS_STALE_INTERVAL_MS);
}

function applyUsageAndMachineStatsVisibility() {
  const usageSection = document.getElementById('usage-section');
  if (usageSection) usageSection.style.display = CONFIG.features.usage ? '' : 'none';
  const machineStatsSection = document.getElementById('machine-stats-section');
  const hasStats = Object.keys(state.chatStream.hostsStats || {}).length > 0;
  if (machineStatsSection) machineStatsSection.style.display = hasStats ? '' : 'none';
}

function renderHostsStats(hosts = state.chatStream.hostsStats) {
  const section = document.getElementById('machine-stats-section');
  const footer = document.getElementById('machine-stats-footer');
  const rawHosts = hosts && typeof hosts === 'object' && !Array.isArray(hosts) ? hosts : {};
  const entries = Object.entries(rawHosts).filter(([, stats]) => stats && typeof stats === 'object' && !Array.isArray(stats));
  const localStreamHost = String(streamHostForHostId('local') || '').toLowerCase();
  const rosterOrder = new Map((Array.isArray(HOST_IDS) ? HOST_IDS : []).map((id, index) => [
    String(streamHostForHostId(id) || '').toLowerCase(),
    index,
  ]));
  entries.sort(([leftHost], [rightHost]) => {
    const left = String(leftHost).toLowerCase();
    const right = String(rightHost).toLowerCase();
    if (left === localStreamHost && right !== localStreamHost) return -1;
    if (right === localStreamHost && left !== localStreamHost) return 1;
    const leftOrder = rosterOrder.has(left) ? rosterOrder.get(left) : 1000;
    const rightOrder = rosterOrder.has(right) ? rosterOrder.get(right) : 1000;
    return leftOrder - rightOrder || left.localeCompare(right);
  });
  if (section) section.style.display = entries.length > 0 ? '' : 'none';
  if (!footer) return;
  if (entries.length === 0) {
    footer.innerHTML = '';
    return;
  }

  footer.innerHTML = entries.map(([streamHost, stats]) => {
    const hostId = _streamHostToHostId(streamHost) || streamHost;
    const label = getSourceForSession('', hostId) || streamHost;
    const color = getSourceColorForSession('', hostId);
    const memoryPct = statUsagePct(stats.memory_used_bytes, stats.memory_total_bytes);
    const diskPct = statUsagePct(stats.disk_used_bytes, stats.disk_total_bytes);
    const stale = machineStatsIsStale(stats);
    const card = [];
    card.push('<div class="machine-stat-card color-' + esc(color) + '" data-machine-stats-host="' + esc(streamHost) + '">');
    card.push('<div class="machine-stat-head">');
    card.push('<span class="machine-stat-mark">' + esc(getSourceInitial(label)) + '</span>');
    card.push('<span class="machine-stat-name">' + esc(label) + '</span>');
    card.push('<span class="machine-stat-state ' + (stale ? 'error' : 'ok') + '">' + (stale ? 'Stale' : 'Live') + '</span>');
    card.push('</div>');
    card.push('<div class="machine-stat-row"><span>Load 1m</span><b>' + esc(fmtStatLoad(stats.cpu_load_1m)) + '</b></div>');
    card.push('<div class="machine-stat-row"><span>RAM</span><b>' + esc(fmtStatPct(memoryPct)) + '</b></div>');
    card.push('<div class="machine-stat-note">' + esc(fmtStatBytes(stats.memory_used_bytes) + ' / ' + fmtStatBytes(stats.memory_total_bytes)) + '</div>');
    if (memoryPct !== null) {
      card.push('<div class="usage-bar"><div class="usage-bar-fill ' + usageBarClass(memoryPct) + '" style="width:' + Math.round(memoryPct) + '%"></div></div>');
    }
    card.push('<div class="machine-stat-row"><span>Storage</span><b>' + esc(fmtStatPct(diskPct)) + '</b></div>');
    card.push('<div class="machine-stat-note">' + esc(fmtStatBytes(stats.disk_used_bytes) + ' / ' + fmtStatBytes(stats.disk_total_bytes)) + '</div>');
    if (diskPct !== null) {
      card.push('<div class="usage-bar"><div class="usage-bar-fill ' + usageBarClass(diskPct) + '" style="width:' + Math.round(diskPct) + '%"></div></div>');
    }
    card.push('<div class="machine-stat-note">Up ' + esc(fmtStatUptime(stats.uptime_seconds)) + '</div>');
    card.push('</div>');
    return card.join('');
  }).join('');
}

startMachineStatsStaleReevaluation();

// ── Mic API ──────────────────────────────────────────────────

const MIC_API = resolveMicUrl(CONFIG);

function resolveLocalMicCaller() {
  return hostPresentation.localIdentity(CONFIG);
}

function micModeBody(extra = {}) {
  return { caller: resolveLocalMicCaller(), ...extra };
}

async function micApi(method, path, body) {
  try {
    const opts = { method };
    if (body !== undefined) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(MIC_API + path, opts);
    let data = null;
    try { data = await r.json(); } catch {}
    if (!r.ok) return { ...(data || {}), ok: false, status: r.status };
    return data;
  } catch (e) {
    return null;
  }
}

async function postMicMode(mode, extra) {
  if (mode === 'off') wakeDelivery?.cancel();
  const result = await micApi('POST', `/mode/${mode}`, micModeBody(extra));
  if (result && result.status === 409) {
    showMicConflict(result);
  }
  return result;
}

function useRemoteClipboardMode() {
  return !!(CONFIG.mic && CONFIG.mic.useStreamHost);
}

function alwaysOnVisible() {
  return shouldShowAlwaysOn({ alwaysOnEnabled: CONFIG.mic && CONFIG.mic.alwaysOnEnabled });
}

function formatMicCaller(caller) {
  return caller || 'unknown';
}

function stopRemoteClipboardPoller() {
  if (micState.remoteClipboardPoller) {
    const stopped = micState.remoteClipboardPoller.stop();
    micState.remoteClipboardPoller = null;
    return stopped;
  }
  return Promise.resolve();
}

function startRemoteClipboardPoller(writeClipboard = window.cc && window.cc.writeClipboard) {
  stopRemoteClipboardPoller();
  micState.remoteClipboardPoller = createRemoteClipboardPoller({
    fetchSince: async (idx) => {
      const data = await micApi('GET', `/clipboard/since/${idx}`);
      if (!data) throw new Error('mic server offline');
      return data;
    },
    writeClipboard,
    intervalMs: 1000,
  });
  micState.remoteClipboardPoller.start();
}

function showMicConflict(result) {
  const info = document.getElementById('mic-info');
  if (!info) return;
  const caller = formatMicCaller(result.caller);
  // Inform-only: this machine cannot start the requested mode while another
  // caller has the mic. The server's owner is the only one who controls the
  // session; no takeover affordance.
  info.innerHTML = `${esc(caller)} is using the mic.`;
}

// ── Voice Record per Slot (uses mic server copy ability) ──────

const voiceState = { activeSlot: null, mode: null, pollTimer: null, capture: null, busy: false };

function setVoiceButtonRecording(slot, recording) {
  const btn = document.querySelector(`.cell-voice[data-slot="${slot}"]`);
  if (!btn) return;
  btn.classList.toggle('recording', recording);
}

async function stopRemoteSlotVoiceSession({ postOff = true } = {}) {
  if (voiceState.mode !== 'remote_slot') return;
  const slot = voiceState.activeSlot;
  voiceState.activeSlot = null;
  voiceState.mode = null;
  if (slot !== null) setVoiceButtonRecording(slot, false);
  if (postOff) await postMicMode('off');
  await stopRemoteClipboardPoller();
}

async function startRemoteSlotVoiceSession(slot) {
  await stopRemoteClipboardPoller();
  const r = await postMicMode('clipboard', { remote: true });
  if (!r || r.status || r.ok === false) {
    console.error('Failed to start remote slot voice:', r);
    return;
  }

  voiceState.activeSlot = slot;
  voiceState.mode = 'remote_slot';
  setVoiceButtonRecording(slot, true);
  startRemoteClipboardPoller((text) => sendProgrammaticInput(slot, text));
}

async function deliverVoiceCapture(capture, text) {
  if (!capture || capture.delivered) return;
  capture.delivered = true; // claim before awaiting: polling and stop share one completion
  const clean = String(text || '').trim();
  if (!clean) return;
  let ok = false;
  try {
    if (capture.streamId && window.PentacleChatStore) {
      ok = await window.PentacleChatStore.sendTurn(capture.streamId, clean);
    } else {
      const result = await window.cc.chatSend(capture.hostId, capture.sessionName, clean);
      ok = !!result?.ok;
    }
  } catch (error) {
    console.error('Voice send failed:', error);
  }
  if (ok) voiceState.unsentText = '';
  if (!ok) {
    // Keep the recognized words available even if the original chat closed.
    voiceState.unsentText = clean;
    showToast('Voice message could not be sent. Transcript retained in the microphone panel.', { type: 'error' });
    const preview = document.getElementById('mic-transcript-preview');
    if (preview) preview.textContent = clean;
  }
}

async function toggleVoiceRecord(slot) {
  if (voiceState.busy) return;
  voiceState.busy = true;
  try { await toggleVoiceRecordInner(slot); }
  finally { voiceState.busy = false; }
}

async function toggleVoiceRecordInner(slot) {
  if (!state.terminals[slot]) return;

  const btn = document.querySelector(`.cell-voice[data-slot="${slot}"]`);
  if (!alwaysOnVisible()) {
    if (useRemoteClipboardMode()) {
      if (voiceState.mode === 'remote_slot' && voiceState.activeSlot === slot) {
        await stopRemoteSlotVoiceSession({ postOff: true });
        return;
      }
      if (voiceState.mode === 'remote_slot' && voiceState.activeSlot !== null) {
        await stopRemoteSlotVoiceSession({ postOff: true });
      }
      await startRemoteSlotVoiceSession(slot);
      return;
    }
    if (btn) btn.title = 'Always-on disabled and no stream-host mic configured';
    return;
  }

  // If already recording this slot, stop capture.
  // IMPORTANT: clear activeSlot + stop poller BEFORE awaiting /copy/stop so
  // any in-flight poll tick bails out (it checks voiceState.activeSlot after
  // its await resumes). Otherwise the manual-stop response and the poller
  // both see the new on_last_copied and paste it twice.
  if (voiceState.activeSlot === slot) {
    const capture = voiceState.capture;
    stopVoicePoll(slot);
    const r = await micApi('POST', '/copy/stop');
    if (r?.ok) await deliverVoiceCapture(capture, r.copied);
    else showToast('Could not finish voice transcription. Check the microphone panel.', { type: 'error' });
    return;
  }

  // If recording another slot, stop that first
  if (voiceState.activeSlot !== null) {
    const previous = voiceState.capture;
    stopVoicePoll(voiceState.activeSlot);
    const stopped = await micApi('POST', '/copy/stop');
    if (!stopped?.ok) return;
    await deliverVoiceCapture(previous, stopped.copied);
  }
  const target = chatControlTargetForSlot(slot);
  if (!target || target.error) return;
  const capture = {
    slot, streamId: target.streamSession?.stream_id,
    hostId: target.hostId, sessionName: target.sessionName, delivered: false,
  };

  // Mic must be in "on" mode — auto-enable if off
  let status = await micApi('GET', '/status');
  if (!status) {
    console.error('Mic server not reachable');
    return;
  }
  if (status.mode === 'off' || status.mode === 'offline') {
    if (status.mode === 'offline') {
      await window.cc.startMicServer();
      await new Promise(r => setTimeout(r, 1000));
    }
    await postMicMode('on');
    // Wait for model loading
    for (let i = 0; i < 10; i++) {
      await new Promise(r => setTimeout(r, 500));
      status = await micApi('GET', '/status');
      if (status && status.mode === 'on') break;
    }
    if (!status || status.mode !== 'on') {
      console.error('Failed to auto-enable mic');
      return;
    }
    // Update sidebar mic UI
    setTimeout(fetchMicStatus, 100);
  }

  // Start copy ability via mic server
  const r = await micApi('POST', '/copy/start');
  if (!r || !r.ok) {
    console.error('Failed to start copy:', r);
    return;
  }

  voiceState.activeSlot = slot;
  voiceState.mode = 'always_on';
  voiceState.capture = capture;
  if (btn) btn.classList.add('recording');

  // Poll mic status — when capture ends (user said "over" / "end copy"),
  // paste result. If the user clicked the slot button instead, the manual-stop
  // path above already handled it; this poller bails on the activeSlot check
  // so we don't double-paste.
  voiceState.pollTimer = setInterval(async () => {
    const s = await micApi('GET', '/status');
    if (voiceState.capture !== capture) return; // stopped, or a newer recording took over
    if (!s) return;
    if (s.on_listener_state !== 'CAPTURING' || s.capture_origin === 'wake') {
      stopVoicePoll(slot);
      await deliverVoiceCapture(capture, s.on_last_copied);
    }
  }, 500);
}

function stopVoicePoll(slot) {
  if (voiceState.pollTimer) {
    clearInterval(voiceState.pollTimer);
    voiceState.pollTimer = null;
  }
  const btn = document.querySelector(`.cell-voice[data-slot="${slot}"]`);
  if (btn) btn.classList.remove('recording');
  voiceState.activeSlot = null;
  voiceState.mode = null;
  voiceState.capture = null;
}

// Wire up voice buttons
for (const btn of document.querySelectorAll('.cell-voice')) {
  btn.addEventListener('click', (e) => {
    const slot = parseInt(btn.dataset.slot, 10);
    toggleVoiceRecord(slot);
  });
}

// ── Mic Control ───────────────────────────────────────────────

let wakeDelivery = null;

const micState = {
  mode: 'off',
  lastTranscriptIdx: 0,
  meetingWindowOpen: false,
  remoteClipboardPoller: null,
  busyVisible: false,
};

function updateMicUI(data) {
  const dot = document.getElementById('mic-status-dot');
  const info = document.getElementById('mic-info');
  const btn = document.getElementById('mic-btn-toggle');
  const copyBtn = document.getElementById('mic-btn-copy');
  const meetingBtn = document.getElementById('mic-btn-meeting');
  const preview = document.getElementById('mic-transcript-preview');
  const showAlways = alwaysOnVisible();
  const renderAlwaysOnUi = shouldRenderAlwaysOnUi({
    status: data,
    alwaysOnEnabled: CONFIG.mic && CONFIG.mic.alwaysOnEnabled,
  });

  dot.className = 'mic-status-dot';
  btn.style.display = showAlways ? '' : 'none';

  if (!data) {
    if (voiceState.mode === 'remote_slot') {
      stopRemoteSlotVoiceSession({ postOff: false });
    } else {
      stopRemoteClipboardPoller();
    }
    info.textContent = 'Mic server offline';
    btn.textContent = 'Start';
    btn.className = 'mic-btn mic-toggle mic-start';
    copyBtn.disabled = true;
    meetingBtn.disabled = true;
    copyBtn.className = 'mic-btn mic-btn-copy';
    meetingBtn.className = 'mic-btn mic-btn-meeting';
    micState.mode = 'offline';
    return;
  }

  micState.mode = data.mode;
  window.PentacleHarness?.emit?.('mic:mode', { data: { mode: data.mode, meeting_active: !!data.meeting_active } });
  const mode = data.mode;
  const isCopy = mode === 'clipboard';
  const isMeeting = mode === 'meeting' || data.meeting_active;
  const isOn = mode === 'on';
  const localHostId = resolveLocalMicCaller();
  const busy = computeBusyBannerState({ status: data, localHostId });
  micState.busyVisible = busy.visible;
  if (!isCopy || data.caller !== localHostId) {
    if (voiceState.mode === 'remote_slot') {
      stopRemoteSlotVoiceSession({ postOff: false });
    } else {
      stopRemoteClipboardPoller();
    }
  }

  // Toggle button
  btn.textContent = isOn ? 'On' : 'Off';
  btn.className = 'mic-btn mic-toggle' + (isOn ? ' selected-on' : '');

  // Copy button
  copyBtn.textContent = isCopy && !busy.visible ? 'Stop Copy' : 'Copy';
  copyBtn.className = 'mic-btn mic-btn-copy' + (isCopy ? ' active' : '');
  copyBtn.disabled = busy.visible ? false : isMeeting;

  // Meeting button
  meetingBtn.textContent = isMeeting && !busy.visible ? 'Stop Meeting' : 'Meeting';
  meetingBtn.className = 'mic-btn mic-btn-meeting' + (isMeeting ? ' active' : '');
  meetingBtn.disabled = busy.visible ? false : isCopy;

  if (isCopy) {
    dot.classList.add('active-clipboard');
    info.innerHTML = '<span style="color:var(--green)">Clipboard capture active</span>';
    preview.textContent = voiceState.unsentText || '';
  } else if (isMeeting) {
    dot.classList.add('active-meeting');
    const mins = Math.floor(data.duration / 60);
    const secs = Math.floor(data.duration % 60);
    info.innerHTML = `<span style="color:var(--red)">Recording ${String(mins).padStart(2,'0')}:${String(secs).padStart(2,'0')}</span> — ${data.transcript_count} lines`;
    // Auto-open meeting window
    if (!micState.meetingWindowOpen) {
      micState.meetingWindowOpen = true;
      window.cc.openMeeting();
    }
  } else if (!isOn) {
    info.textContent = 'Mic off';
    preview.textContent = voiceState.unsentText || '';
  } else if (!renderAlwaysOnUi) {
    info.textContent = 'Mic ready';
    preview.textContent = voiceState.unsentText || '';
  } else {
    // Always-on mode
    const listenerState = data.on_listener_state || 'LISTENING';

    if (listenerState === 'AWAKE') {
      dot.classList.add('active-awake');
      info.innerHTML = '<span style="color:#00ff66">Listening for command...</span>';
      if (!data.on_last_copied) preview.textContent = voiceState.unsentText || '';
    } else if (listenerState === 'CAPTURING') {
      dot.classList.add('active-capturing');
      info.innerHTML = '<span style="color:#00ff66">Copying...</span>';
      const texts = data.on_captured_texts || [];
      if (texts.length > 0) {
        preview.innerHTML = texts.map(t =>
          `<div class="mic-transcript-line">${esc(t)}</div>`
        ).join('');
        preview.scrollTop = preview.scrollHeight;
      }
    } else if (listenerState === 'MEETING') {
      dot.classList.add('active-meeting');
      const mins = Math.floor(data.duration / 60);
      const secs = Math.floor(data.duration % 60);
      info.innerHTML = `<span style="color:var(--red)">Recording ${String(mins).padStart(2,'0')}:${String(secs).padStart(2,'0')}</span> — ${data.transcript_count} lines`;
      if (!micState.meetingWindowOpen) {
        micState.meetingWindowOpen = true;
        window.cc.openMeeting();
      }
    } else if (listenerState === 'CALIBRATING') {
      dot.classList.add('active-awake');
      info.innerHTML = '<span style="color:var(--yellow)">Calibrating...</span>';
    } else {
      dot.classList.add('active-on');
      if (data.on_last_copied) {
        info.innerHTML = '<span style="color:var(--green)">[Copied]</span>';
        preview.textContent = voiceState.unsentText || '';
      } else {
        info.textContent = `Say "${CONFIG.wakeWord}" to wake`;
        preview.textContent = voiceState.unsentText || '';
      }
    }
  }

  if (busy.visible) {
    const caller = formatMicCaller(busy.caller);
    const modeLabel = busy.mode || 'mic';
    let html = `${esc(caller)} using mic - ${esc(modeLabel)}`;
    if (busy.pausedMode === 'on') {
      html += `<br><span>always-on paused - ${esc(formatMicCaller(busy.pausedCaller))} listener will resume when ${esc(caller)} stops</span>`;
    }
    info.innerHTML = html;
  } else if (busy.lastError) {
    const text = String(busy.lastError);
    info.innerHTML = text.startsWith('always_on_resume_failed')
      ? '<span style="color:var(--yellow)">always-on did not resume - see logs</span>'
      : `<span style="color:var(--yellow)">${esc(text)}</span>`;
  }

  if (!busy.visible && !busy.lastError && wakeDelivery) {
    const wakeMessage = wakeDelivery.message(data);
    if (wakeMessage) info.textContent = wakeMessage;
  }

  // Close meeting window tracking if we left meeting
  const inMeeting = isMeeting || (isOn && (data.on_listener_state === 'MEETING'));
  if (!inMeeting && micState.meetingWindowOpen) {
    micState.meetingWindowOpen = false;
  }
}

async function fetchMicStatus() {
  const data = await micApi('GET', '/status');
  updateMicUI(data);
  void wakeDelivery?.tick(data);

  // If in meeting mode, fetch new transcript lines
  if (data && data.mode === 'meeting') {
    const tData = await micApi('GET', `/transcript/since/${micState.lastTranscriptIdx}`);
    if (tData) {
      const preview = document.getElementById('mic-transcript-preview');
      if (tData.lines.length > 0) {
        for (const line of tData.lines) {
          const div = document.createElement('div');
          div.className = 'mic-transcript-line';
          div.textContent = line;
          preview.appendChild(div);
        }
        micState.lastTranscriptIdx = tData.total;
        preview.scrollTop = preview.scrollHeight;
      }
      // Update partial
      let partialEl = preview.querySelector('.mic-partial');
      if (tData.partial) {
        if (!partialEl) {
          partialEl = document.createElement('div');
          partialEl.className = 'mic-partial';
          preview.appendChild(partialEl);
        }
        partialEl.textContent = '\u25B8 ' + tData.partial;
        preview.scrollTop = preview.scrollHeight;
      } else if (partialEl) {
        partialEl.remove();
      }
    }
  }
}

document.getElementById('mic-btn-toggle').addEventListener('click', async () => {
  if (!alwaysOnVisible()) return;
  if (micState.mode === 'offline') {
    // Server is down — start it
    const btn = document.getElementById('mic-btn-toggle');
    const info = document.getElementById('mic-info');
    btn.textContent = 'Starting...';
    btn.className = 'mic-btn mic-toggle mic-start';
    info.textContent = 'Starting mic server...';
    const ok = await window.cc.startMicServer();
    if (ok) {
      setTimeout(fetchMicStatus, 500);
    } else {
      info.textContent = 'Failed to start mic server';
      btn.textContent = 'Start';
    }
    return;
  }
  const newMode = micState.mode === 'on' && !micState.busyVisible ? 'off' : 'on';
  await postMicMode(newMode);
  micState.lastTranscriptIdx = 0;
  document.getElementById('mic-transcript-preview').innerHTML = '';
  setTimeout(fetchMicStatus, newMode === 'on' ? 1500 : 500);
});

document.getElementById('mic-btn-copy').addEventListener('click', async () => {
  if (micState.mode === 'offline') return;
  if (voiceState.mode === 'remote_slot' && voiceState.activeSlot !== null) {
    const info = document.getElementById('mic-info');
    if (info) info.textContent = `Slot ${voiceState.activeSlot + 1} is using the mic. Stop the slot first.`;
    return;
  }
  const newMode = micState.mode === 'clipboard' && !micState.busyVisible ? 'off' : 'clipboard';
  if (newMode === 'off') {
    await postMicMode('off');
    await stopRemoteClipboardPoller();
  } else {
    const result = await postMicMode('clipboard', useRemoteClipboardMode() ? { remote: true } : {});
    if (result && !result.status && useRemoteClipboardMode()) startRemoteClipboardPoller();
  }
  setTimeout(fetchMicStatus, 500);
});

document.getElementById('mic-btn-meeting').addEventListener('click', async () => {
  if (micState.mode === 'offline') return;
  const isMeeting = micState.mode === 'meeting' && !micState.busyVisible;
  const newMode = isMeeting ? 'off' : 'meeting';
  if (newMode === 'off') stopRemoteClipboardPoller();
  await postMicMode(newMode);
  micState.lastTranscriptIdx = 0;
  document.getElementById('mic-transcript-preview').innerHTML = '';
  setTimeout(fetchMicStatus, isMeeting ? 500 : 2000);
});

// ── Init ───────────────────────────────────────────────────────

// Apply config — theme, app name, feature flags
(function applyConfig() {
  // Set titlebar text and document title
  const titleEl = document.getElementById('titlebar-text');
  if (titleEl) titleEl.textContent = CONFIG.appName.toUpperCase();
  document.title = CONFIG.appName;
  renderTitlebarMachines();
  renderConfigWarnings();

  applyAppearanceSettings();

  // Hide mic panel + voice record buttons if disabled
  if (!CONFIG.features.mic) {
    const micSection = document.getElementById('mic-section');
    if (micSection) micSection.style.display = 'none';
    document.querySelectorAll('.cell-voice').forEach(btn => btn.style.display = 'none');
  } else if (!alwaysOnVisible()) {
    const toggle = document.getElementById('mic-btn-toggle');
    if (toggle) toggle.style.display = 'none';
    document.querySelectorAll('.cell-voice').forEach(btn => {
      btn.title = useRemoteClipboardMode()
        ? 'Per-slot voice coding via remote mic'
        : 'Always-on disabled and no stream-host mic configured';
    });
  }

  // Usage visibility is local UI configuration; machine stats arrive with the
  // daemon-owned hosts.stats projection.
  applyUsageAndMachineStatsVisibility();

  if (!chatUiEnabled()) {
    const btnNew = document.getElementById('btn-new');
    if (btnNew) {
      btnNew.title = 'New Terminal';
      btnNew.setAttribute('aria-label', 'New Terminal');
      btnNew.textContent = '+';
    }
    for (let i = 0; i < 4; i++) {
      state.slotViewModes[i] = 'terminal';
      document.getElementById(`header-${i}`)?.querySelector('.cell-view-toggle-group')?.remove();
    }
  }

  // Hide dashboards view switcher if disabled — just show chats, no label
  if (!CONFIG.features.dashboards) {
    const viewSwitcher = document.querySelector('.view-switcher');
    if (viewSwitcher) viewSwitcher.style.display = 'none';
  }
  setupSidebarCollapsibles();
})();

// ── Settings Panel ─────────────────────────────────────────────
// The titlebar gear opens this panel. Each toggle flips CONFIG.features.<key>,
// persists the override (saveSettingsOverride), and either applies live or
// flags that a reload is needed. See SETTINGS_FLAGS for the live/reload split.
function setupSettingsPanel() {
  const btn = document.getElementById('settings-btn');
  const overlay = document.getElementById('settings-overlay');
  const modal = document.getElementById('settings-modal');
  const list = document.getElementById('settings-list');
  const closeBtn = document.getElementById('settings-close');
  const reloadBtn = document.getElementById('settings-reload');
  if (!btn || !overlay || !modal || !list) return;

  const setSwitch = (el, on) => el.setAttribute('aria-checked', on ? 'true' : 'false');
  const setSegment = (row, value) => {
    row.querySelectorAll('.settings-segment-btn').forEach((button) => {
      const selected = button.dataset.value === value;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-pressed', selected ? 'true' : 'false');
    });
  };

  // Apply a flag change without a reload. Only flags marked live:true route
  // here; everything else waits for a reload so its startup wiring re-runs.
  function applyLiveFlag(key, on) {
    if (key === 'dashboards') {
      const vs = document.querySelector('.view-switcher');
      if (vs) vs.style.display = on ? '' : 'none';
      if (!on && state.currentView === 'dashboards') switchView('chats');
    } else if (key === 'showTurnDuration') {
      for (let slot = 0; slot < state.slots.length; slot += 1) {
        if (state.slotViewModes[slot] === 'chat') {
          state.slotChatLastListHtml[slot] = null;
          state.slotChatPendingListRender[slot] = null;
          renderSlotChat(slot);
        }
      }
    }
  }

  function markPendingReload() {
    modal.classList.add('has-pending-reload');
    if (reloadBtn) reloadBtn.style.display = '';
  }

  function buildAppearanceRow(key, label, desc, options) {
    const row = document.createElement('div');
    row.className = 'settings-row settings-row-segmented';
    row.dataset.setting = key;
    row.innerHTML =
      `<div class="settings-row-text">
        <div class="settings-row-label">${esc(label)}</div>
        <div class="settings-row-desc">${esc(desc)}</div>
      </div>
      <div class="settings-segment" role="group" aria-label="${esc(label)}">
        ${options.map((option) => `<button class="settings-segment-btn" type="button" data-value="${esc(option.value)}" aria-pressed="false">${esc(option.label)}</button>`).join('')}
      </div>`;
    row.querySelectorAll('.settings-segment-btn').forEach((button) => {
      button.addEventListener('click', () => {
        const next = button.dataset.value;
        state.appearance = normalizeAppearance({ ...state.appearance, [key]: next });
        saveAppearanceSetting(key, state.appearance[key]);
        setSegment(row, state.appearance[key]);
        applyAppearanceSettings({ emit: true });
      });
    });
    setSegment(row, state.appearance[key]);
    list.appendChild(row);
  }

  buildAppearanceRow('theme', 'Theme', 'Switch between the dark cosmic palette and the sage-paper light palette.', [
    { value: 'dark', label: 'Dark' },
    { value: 'light', label: 'Light' },
  ]);
  buildAppearanceRow('density', 'Density', 'Adjust transcript, sidebar, composer, and control spacing.', [
    { value: 'comfortable', label: 'Comfortable' },
    { value: 'compact', label: 'Compact' },
  ]);

  // Build the feature rows once; switch state is re-synced from CONFIG.features on open.
  for (const flag of SETTINGS_FLAGS) {
    const row = document.createElement('div');
    row.className = 'settings-row';
    row.dataset.flag = flag.key;
    const on = CONFIG.features[flag.key] === true;
    row.innerHTML =
      `<div class="settings-row-text">
        <div class="settings-row-label">${esc(flag.label)}<span class="settings-reload-badge">Reload to apply</span></div>
        <div class="settings-row-desc">${esc(flag.desc)}</div>
      </div>
      <button class="settings-switch" type="button" role="switch" aria-checked="${on ? 'true' : 'false'}" aria-label="${esc(flag.label)}"></button>`;
    const sw = row.querySelector('.settings-switch');
    sw.addEventListener('click', () => {
      const next = CONFIG.features[flag.key] !== true;
      CONFIG.features[flag.key] = next;
      saveSettingsOverride(flag.key, next);
      setSwitch(sw, next);
      window.PentacleHarness?.emit?.('settings:toggle', { data: { key: flag.key, value: next } });
      if (flag.live) {
        applyLiveFlag(flag.key, next);
      } else {
        row.classList.add('pending-reload');
        markPendingReload();
      }
    });
    list.appendChild(row);
  }

  function open() {
    list.querySelectorAll('.settings-row[data-setting]').forEach(row => {
      setSegment(row, state.appearance[row.dataset.setting]);
    });
    list.querySelectorAll('.settings-row').forEach(row => {
      if (row.dataset.flag) setSwitch(row.querySelector('.settings-switch'), CONFIG.features[row.dataset.flag] === true);
    });
    overlay.style.display = 'flex';
    window.PentacleHarness?.emit?.('settings:open', { data: {} });
  }
  const close = () => { overlay.style.display = 'none'; };

  btn.addEventListener('click', open);
  closeBtn?.addEventListener('click', close);
  reloadBtn?.addEventListener('click', () => {
    // location.reload() is cancelled by the will-navigate guard in main.js;
    // reload from the main process over IPC instead. Fall back to the direct
    // call in non-Electron contexts (e.g. the test harness) where cc is absent.
    if (window.cc?.reloadApp) window.cc.reloadApp();
    else location.reload();
  });
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && overlay.style.display !== 'none') close();
  });
}
setupSettingsPanel();
const slotGrid = document.querySelector('.grid');
const columnHandle = slotGrid?.querySelector('.grid-col-resizer');
if (columnHandle) gridColResizer = createGridColResizer({
  grid: slotGrid, handle: columnHandle, initialSplit: state.appearance.gridColSplit,
  isVisible: () => state.currentView === 'chats',
  save: fraction => {
    state.appearance.gridColSplit = fraction;
    saveAppearanceSetting('gridColSplit', fraction);
  },
  onResize: scheduleVisibleSlotFits,
  emit: (name, data) => window.PentacleHarness?.emit?.(`slot-layout:${name}`, { data }),
});
window.addEventListener('beforeunload', () => gridColResizer?.destroy());

// Set empty state for all cells.
for (let i = 0; i < 4; i++) {
  const container = document.getElementById(`term-${i}`);
  container.innerHTML = '<div class="cell-empty">Click a session or bot to attach</div>';
}

// Single document-level capture-phase wheel handler for ALL slots.
// Previous approach: per-container handlers. These failed silently when xterm's
// WebGL canvas or viewport absorbed wheel events before the container's capture
// handler could fire (Chromium compositor-level scroll interception).
// Document-level capture fires FIRST, before any element-level handler.
document.addEventListener('wheel', (e) => {
  // Walk up from event target to find which .cell-terminal slot we're in
  let el = e.target;
  let container = null;
  while (el && el !== document) {
    if (el.classList && el.classList.contains('cell-terminal')) {
      container = el;
      break;
    }
    el = el.parentElement;
  }
  if (!container) return; // not over a terminal slot

  const slotMatch = container.id.match(/^term-(\d)$/);
  if (!slotMatch) return;
  const slot = parseInt(slotMatch[1], 10);

  // Only scroll if slot has an active terminal session (not a bot panel, not empty)
  if (!state.slots[slot] || state.botSlots[slot] || !state.terminals[slot]) {
    const now = Date.now();
    if (now - (state._scrollDebugTs?.[slot] || 0) > 1000) {
      if (!state._scrollDebugTs) state._scrollDebugTs = [0,0,0,0];
      state._scrollDebugTs[slot] = now;
      console.warn(`[scroll:blocked] slot=${slot} slots=${!!state.slots[slot]} botSlots=${state.botSlots[slot]} terminals=${!!state.terminals[slot]}`);
    }
    return;
  }
  if (state.slotViewModes[slot] === 'chat' || state.slotViewModes[slot] === 'asset') return;
  e.preventDefault();
  e.stopImmediatePropagation();
  const now = Date.now();
  if (now - state.wheelThrottles[slot] < 50) return;
  state.wheelThrottles[slot] = now;
  if (!state.slots[slot].paneId) return; // PTY not yet created or createPty failed
  const lines = Math.max(1, Math.round(Math.abs(e.deltaY) / 25));
  window.cc.scrollTmux(slot, e.deltaY < 0 ? 'up' : 'down', lines);
}, { passive: false, capture: true });

// Wait for main-process config (isClient, hostIds, feature flags) before
// kicking off any chat-stream IPC or activity polling. The pre-config
// bootstrap doesn't know whether this is host or client mode and would
// pick the wrong default new-session location.
function bindChatPopout() {
  if (!IS_CHAT_POPOUT || chatPopoutBound) return;
  chatPopoutBound = true;
  document.body.classList.add('chat-popout');
  const stylesheet = document.createElement('link');
  stylesheet.rel = 'stylesheet';
  stylesheet.href = 'chat_popout.css';
  document.head.appendChild(stylesheet);
  attachSession(0, CHAT_POPOUT_CONTEXT.session_name, CHAT_POPOUT_CONTEXT.title, CHAT_POPOUT_CONTEXT.desktop_host || CHAT_POPOUT_CONTEXT.host);
}

CFG_READY.then((cfg) => {
  // Pull the initial chat-stream snapshot regardless of chatUiEnabled — the
  // sidebar visibility filter needs state.chatStream.sessions populated
  // before the first renderSidebar tick. Without this, a renderer that loads
  // after main has already received the snapshot starts with an empty cache
  // and renders nested tmux sessions until the next inventory event.
  window.cc.getChatStreamState().then((snapshot) => {
    applyChatStreamState(snapshot);
    if (snapshot && Object.prototype.hasOwnProperty.call(snapshot, 'limits')) {
      renderLimits(snapshot.limits, snapshot.limits_health ?? null);
    }
    warmSpawnCatalog();
    window.PentacleChatStore?.applyFrame?.({ type: 'snapshot', ...(snapshot || {}) });
    bindChatPopout();
  }).catch(() => {
    if (IS_CHAT_POPOUT) bindChatPopout();
  });

  // Mic panel — enabled on any platform when features.mic is true.
  // The mic server is cross-platform (MicServer.app on macOS, Python direct on Windows/Linux).
  if (CONFIG.features.mic) {
    wakeDelivery = createWakeDelivery({
      config: CONFIG,
      getState: () => window.PentacleChatStore?.sendTurn ? window.cc.getChatStreamState() : null,
      api: micApi,
      spawnAgent: (request) => window.cc.chatSpawnV2(request),
      getSpawnCatalog: () => window.cc.chatSpawnCatalog(),
      sendTurn: (streamId, text) => window.PentacleChatStore.sendTurn(streamId, text),
      onStatus: (text) => {
        if (text && micState.mode === 'on') document.getElementById('mic-info').textContent = text;
      },
    });
    fetchMicStatus();
    setInterval(fetchMicStatus, 1000);
  } else {
    const section = document.getElementById('mic-section');
    if (section) section.style.display = 'none';
  }
});
