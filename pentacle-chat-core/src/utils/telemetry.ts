import {
  TELEMETRY_BUG_REF,
  TELEMETRY_EVENT_BUG_REFS,
  type TelemetryEvent,
} from './telemetryEvents';

export type TelemetryPayload = {
  subsystem: string;
  message: TelemetryEvent;
  bug_ref: string;
  data: Record<string, unknown>;
};

export type TelemetrySink = (payload: TelemetryPayload) => void;

function subsystemFor(name: TelemetryEvent) {
  if (String(name).startsWith('chat.compose.') || String(name).startsWith('chat.session.')) {
    return 'chat_surface';
  }
  return String(name).split(':')[0] || 'app';
}

// Production telemetry is opt-in. Harness and development callers install a
// sink explicitly; keeping the default inert avoids payload serialization on
// every render and stream event.
const defaultTelemetrySink: TelemetrySink = () => {};

let sink: TelemetrySink = defaultTelemetrySink;

export function setTelemetrySink(next: TelemetrySink | null | undefined) {
  sink = next || defaultTelemetrySink;
}

export function teeTelemetrySink(listener: TelemetrySink): () => void {
  const previousSink = sink;
  let active = true;
  const teeSink: TelemetrySink = (payload) => {
    previousSink(payload);
    if (active) listener(payload);
  };
  sink = teeSink;
  return () => {
    if (!active) return;
    active = false;
    if (sink === teeSink) {
      sink = previousSink;
    }
  };
}

export function logTelemetry(name: TelemetryEvent, data: Record<string, unknown> = {}) {
  sink({
    subsystem: subsystemFor(name),
    message: name,
    bug_ref: TELEMETRY_EVENT_BUG_REFS[name] || TELEMETRY_BUG_REF,
    data,
  });
}
