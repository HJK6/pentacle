// Opt-in diagnostics capture around the actual shared reducer and renderer.
import { teeTelemetrySink, logTelemetry, TELEMETRY_EVENTS, recordInbound, recordPersistedDelta, recordDrop, classifyDrop, snapshotCounts, reset } from 'chat-core';

export type HarnessConfigLike = { features?: { chatHarnessTelemetry?: boolean } };
export type TelemetryPayload = {
  subsystem: string;
  message: string;
  data?: Record<string, unknown>;
};
export type StreamFlowCounts = { inbound: number; persisted: number; dropped: number };
export type DiagnosticsEvent = { stream_id?: string };
export type ChatStoreDiagnosticsHook = (detail: {
  event: DiagnosticsEvent;
  persistedDelta: number;
  duplicate?: boolean;
}) => void;
export type ChatStoreControllerLike = {
  setDiagnosticsHook?: (hook: ChatStoreDiagnosticsHook | null) => void;
};

export function isHarnessArmed(
  env: Record<string, string | undefined> = (typeof process !== 'undefined' ? process.env : {}),
  config?: HarnessConfigLike,
): boolean {
  return env.PENTACLE_HARNESS === '1' || config?.features?.chatHarnessTelemetry === true;
}

export type CapturedTelemetry = TelemetryPayload;

export type HarnessHandle = {
  readonly armed: boolean;
  readonly events: ReadonlyArray<CapturedTelemetry>;
  eventsOfType(name: string): CapturedTelemetry[];
  flowCounts(): Map<string, StreamFlowCounts>;
  detach(): void;
};

const INERT: HarnessHandle = {
  armed: false,
  events: [],
  eventsOfType: () => [],
  flowCounts: () => new Map(),
  detach: () => {},
};

export function attachChatHarnessTelemetry(
  controller: ChatStoreControllerLike,
  options: {
    env?: Record<string, string | undefined>;
    config?: HarnessConfigLike;
    force?: boolean;
    resetCounts?: boolean;
  } = {},
): HarnessHandle {
  const armed = options.force === true || isHarnessArmed(options.env, options.config);
  if (!armed) return INERT;

  if (options.resetCounts) reset();
  const events: CapturedTelemetry[] = [];
  const detachSink = teeTelemetrySink((payload) => events.push(payload));
  logTelemetry(TELEMETRY_EVENTS.HARNESS_HARNESS_ARMED, { surface: 'desktop' });
  const hook: ChatStoreDiagnosticsHook = ({ event, persistedDelta, duplicate }) => {
    recordInbound(event as never);
    recordPersistedDelta(event.stream_id || '', persistedDelta);
    if (persistedDelta <= 0) recordDrop(event.stream_id || '', classifyDrop(event as never, { duplicate }) || 'unaccounted');
  };
  controller?.setDiagnosticsHook?.(hook);
  let detached = false;
  return {
    armed: true,
    events,
    eventsOfType(name) { return events.filter((event) => event.message === name); },
    flowCounts() { return snapshotCounts() as never; },
    detach() {
      if (detached) return;
      detached = true;
      detachSink();
      controller?.setDiagnosticsHook?.(null);
    },
  };
}
