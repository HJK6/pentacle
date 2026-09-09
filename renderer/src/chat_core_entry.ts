// Public renderer entrypoint. It consumes a reviewed adapter supplied as
// `globalThis.PentaclePublicCore` when one is bundled, and otherwise exposes
// deterministic no-op fallbacks for local fixture pages. No private package or
// source pin is required by this entrypoint.

import * as ChatReliabilityView from './chat_reliability_view';
import * as ChatReconnectReplay from './chat_reconnect_replay';
import { attachDesktopHarness } from './desktop_harness';
import type { ChatStoreControllerLike } from './chat_harness_telemetry';

type PublicFunction = (...args: unknown[]) => unknown;
type PublicCore = Record<string, unknown>;

const browserGlobal = typeof globalThis !== 'undefined' ? globalThis as typeof globalThis & {
  PentaclePublicCore?: PublicCore;
  PentacleChatCore?: Record<string, unknown>;
  PentacleChatStore?: unknown;
  PentacleChatView?: unknown;
  PentacleChatReliability?: unknown;
  PentacleHarness?: unknown;
  cc?: { chatSend?: PublicFunction; chatSendCorrelated?: PublicFunction; chatInterrupt?: PublicFunction };
} : undefined;

const publicCore: PublicCore = browserGlobal?.PentaclePublicCore || {};

function coreFunction(name: string, fallback: PublicFunction): PublicFunction {
  const candidate = publicCore[name];
  return typeof candidate === 'function' ? candidate as PublicFunction : fallback;
}

const identity = (value: unknown) => value;
const emptyState = () => ({ sessions: [], optimisticSends: {}, optimisticByRequestId: {} });

const initialPentacleStreamState = coreFunction('initialPentacleStreamState', emptyState);
const applyPentacleEvent = coreFunction('applyPentacleEvent', identity);
const applyPentacleSessionSummary = coreFunction('applyPentacleSessionSummary', identity);
const applyPentacleWorkingState = coreFunction('applyPentacleWorkingState', identity);
const applyPentacleSnapshotMessage = coreFunction('applyPentacleSnapshotMessage', identity);
const applyPentacleSessionInventory = coreFunction('applyPentacleSessionInventory', identity);
const applyPentacleMachineStats = coreFunction('applyPentacleMachineStats', identity);
const applyPentacleMachineStatsInventory = coreFunction('applyPentacleMachineStatsInventory', identity);
const applyPentacleCodexUsage = coreFunction('applyPentacleCodexUsage', identity);
const applyPentacleUpdates = coreFunction('applyPentacleUpdates', identity);
const applyPentacleHostStatus = coreFunction('applyPentacleHostStatus', identity);
const removePentacleStream = coreFunction('removePentacleStream', identity);
const clearPentacleStreamDraft = coreFunction('clearPentacleStreamDraft', identity);
const beginPentacleTurn = coreFunction('beginPentacleTurn', identity);
const clearPentacleTurn = coreFunction('clearPentacleTurn', identity);
const sendOptimisticMessage = coreFunction('sendOptimisticMessage', identity);
const markOptimisticDispatchedByRequestId = coreFunction('markOptimisticDispatchedByRequestId', identity);
const markOptimisticAckedByRequestId = coreFunction('markOptimisticAckedByRequestId', identity);
const markOptimisticEchoedByOptimisticId = coreFunction('markOptimisticEchoedByOptimisticId', identity);
const markOptimisticFailedByRequestId = coreFunction('markOptimisticFailedByRequestId', identity);
const markOptimisticFailedByOptimisticId = coreFunction('markOptimisticFailedByOptimisticId', identity);
const pruneOptimisticSend = coreFunction('pruneOptimisticSend', identity);
const reconcileOptimisticSendWithServerEvent = coreFunction('reconcileOptimisticSendWithServerEvent', identity);
const onReconnect = coreFunction('onReconnect', identity);
const applySnapshotWithOptimisticReconciliation = coreFunction('applySnapshotWithOptimisticReconciliation', identity);
const interpretPentacleEvent = coreFunction('interpretPentacleEvent', identity);
const coalesceInterpretedEvents = coreFunction('coalesceInterpretedEvents', identity);
const selectChatOverview = coreFunction('selectChatOverview', () => []);
const selectChatList = coreFunction('selectChatList', () => []);
const selectSessionDetail = coreFunction('selectSessionDetail', () => null);
const selectMachineStatusList = coreFunction('selectMachineStatusList', () => []);
const selectMachineStatsTabs = coreFunction('selectMachineStatsTabs', () => []);
const invalidateSessionDetailCache = coreFunction('invalidateSessionDetailCache', () => undefined);
const getPentacleSessionStatus = coreFunction('getPentacleSessionStatus', () => null);
const getPentacleSessionStatusLabel = coreFunction('getPentacleSessionStatusLabel', () => '');
const normalizePentacleHost = coreFunction('normalizePentacleHost', (value) => String(value || 'hosta'));
const setHostConfigProvider = coreFunction('setHostConfigProvider', () => undefined);
const getHostTheme = coreFunction('getHostTheme', () => null);
const buildPentacleQuestionAnswerText = coreFunction('buildPentacleQuestionAnswerText', (value) => String(value || ''));
const clampQuestionPageIndex = coreFunction('clampQuestionPageIndex', (value) => Number(value) || 0);
const questionPageCount = coreFunction('questionPageCount', () => 0);
const questionPageQuestion = coreFunction('questionPageQuestion', () => null);
const MAX_CHAT_ATTACHMENTS = typeof publicCore.MAX_CHAT_ATTACHMENTS === 'number' ? publicCore.MAX_CHAT_ATTACHMENTS : 8;
const QUESTION_ANSWER_FORMAT_EXAMPLES = publicCore.QUESTION_ANSWER_FORMAT_EXAMPLES || [];

const PentacleChatCore = {
  initialPentacleStreamState,
  applyPentacleEvent,
  applyPentacleSessionSummary,
  applyPentacleWorkingState,
  applyPentacleSnapshotMessage,
  applyPentacleSessionInventory,
  applyPentacleMachineStats,
  applyPentacleMachineStatsInventory,
  applyPentacleCodexUsage,
  applyPentacleUpdates,
  applyPentacleHostStatus,
  removePentacleStream,
  clearPentacleStreamDraft,
  beginPentacleTurn,
  clearPentacleTurn,
  sendOptimisticMessage,
  markOptimisticDispatchedByRequestId,
  markOptimisticAckedByRequestId,
  markOptimisticEchoedByOptimisticId,
  markOptimisticFailedByRequestId,
  markOptimisticFailedByOptimisticId,
  pruneOptimisticSend,
  reconcileOptimisticSendWithServerEvent,
  onReconnect,
  applySnapshotWithOptimisticReconciliation,
  interpretPentacleEvent,
  coalesceInterpretedEvents,
  selectChatOverview,
  selectChatList,
  selectSessionDetail,
  selectMachineStatusList,
  selectMachineStatsTabs,
  invalidateSessionDetailCache,
  getPentacleSessionStatus,
  getPentacleSessionStatusLabel,
  normalizePentacleHost,
  setHostConfigProvider,
  getHostTheme,
  buildPentacleQuestionAnswerText,
  QUESTION_ANSWER_FORMAT_EXAMPLES,
  clampQuestionPageIndex,
  questionPageCount,
  questionPageQuestion,
  MAX_CHAT_ATTACHMENTS,
} as const;

type StoreState = { sessions: unknown[]; optimisticSends: Record<string, unknown>; optimisticByRequestId: Record<string, string> };

class PublicChatStoreController implements ChatStoreControllerLike {
  private state: StoreState = { sessions: [], optimisticSends: {}, optimisticByRequestId: {} };
  private diagnosticsHook: ((detail: { event: { stream_id?: string }; persistedDelta: number }) => void) | null = null;
  private sendBridge?: PublicFunction;
  private cancelBridge?: PublicFunction;

  getState(): StoreState { return this.state; }
  setSendBridge(bridge: PublicFunction): void { this.sendBridge = bridge; }
  setCancelBridge(bridge: PublicFunction): void { this.cancelBridge = bridge; }
  setDiagnosticsHook(hook: ((detail: { event: { stream_id?: string }; persistedDelta: number }) => void) | null): void {
    this.diagnosticsHook = hook;
  }
  getSendBridge(): PublicFunction | undefined { return this.sendBridge; }
  getCancelBridge(): PublicFunction | undefined { return this.cancelBridge; }
  getDiagnosticsHook(): unknown { return this.diagnosticsHook; }
}

const PentacleChatStore = (publicCore.ChatStoreController && typeof publicCore.ChatStoreController === 'function'
  ? new (publicCore.ChatStoreController as new () => PublicChatStoreController)()
  : new PublicChatStoreController());

const PentacleChatView = {
  renderStreamTranscript: coreFunction('renderStreamTranscript', () => ''),
  renderTranscriptTimelineHtml: coreFunction('renderTranscriptTimelineHtml', () => ''),
  renderTranscriptItemHtml: coreFunction('renderTranscriptItemHtml', () => ''),
  mountSlotTranscript: coreFunction('mountSlotTranscript', () => undefined),
} as const;

const PentacleChatReliability = { ...ChatReliabilityView, ...ChatReconnectReplay } as const;
const config = browserGlobal && (browserGlobal as { CONFIG?: { features?: { chatHarnessTelemetry?: boolean } } }).CONFIG;
const PentacleHarness = attachDesktopHarness(PentacleChatStore, {
  env: typeof process !== 'undefined' ? process.env : {},
  config,
});

if (browserGlobal) {
  browserGlobal.PentacleChatCore = PentacleChatCore;
  browserGlobal.PentacleChatStore = PentacleChatStore;
  browserGlobal.PentacleChatView = PentacleChatView;
  browserGlobal.PentacleChatReliability = PentacleChatReliability;
  browserGlobal.PentacleHarness = PentacleHarness;
}

export {};
