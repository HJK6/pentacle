// Synthetic-only harness telemetry for local renderer tests.

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
  } = {},
): HarnessHandle {
  const armed = options.force === true || isHarnessArmed(options.env, options.config);
  if (!armed) return INERT;

  const events: CapturedTelemetry[] = [{
    subsystem: 'harness',
    message: 'harness:armed',
    data: { surface: 'desktop', mode: 'synthetic' },
  }];
  const counts = new Map<string, StreamFlowCounts>();
  const hook: ChatStoreDiagnosticsHook = ({ event, persistedDelta }) => {
    const streamId = typeof event.stream_id === 'string' ? 'fixture-stream' : 'fixture-unknown';
    const prior = counts.get(streamId) || { inbound: 0, persisted: 0, dropped: 0 };
    counts.set(streamId, {
      inbound: prior.inbound + 1,
      persisted: prior.persisted + Math.max(0, persistedDelta),
      dropped: prior.dropped + (persistedDelta > 0 ? 0 : 1),
    });
  };
  controller?.setDiagnosticsHook?.(hook);
  let detached = false;
  return {
    armed: true,
    events,
    eventsOfType(name) { return events.filter((event) => event.message === name); },
    flowCounts() { return new Map(counts); },
    detach() {
      if (detached) return;
      detached = true;
      controller?.setDiagnosticsHook?.(null);
    },
  };
}
