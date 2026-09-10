// Synthetic-only desktop harness. It emits bounded fixture beacons and is
// inert unless explicitly enabled for a local test run.

import {
  attachChatHarnessTelemetry,
  isHarnessArmed,
  type ChatStoreControllerLike,
  type HarnessHandle as ChatHarnessHandle,
  type HarnessConfigLike,
} from './chat_harness_telemetry';

export type HarnessBeacon = {
  name: string;
  ts: number;
  seq: number;
  streamId?: string;
  slot?: number;
  host?: string;
  provider?: string;
  data?: Record<string, unknown>;
};

export type BeaconFields = {
  streamId?: string;
  slot?: number;
  host?: string;
  provider?: string;
  data?: Record<string, unknown>;
};

export type DesktopHarnessHandle = {
  readonly armed: boolean;
  readonly events: ReadonlyArray<HarnessBeacon>;
  eventsOfType(name: string): HarnessBeacon[];
  since(seq: number): HarnessBeacon[];
  emit(name: string, fields?: BeaconFields): void;
  readonly chat: ChatHarnessHandle;
  clear(): void;
  detach(): void;
};

let armed = false;
let seq = 0;
const RING_MAX = 5000;
const beacons: HarnessBeacon[] = [];

function safeLabel(value: unknown, fallback: string): string {
  if (typeof value !== 'string' || !value) return fallback;
  if (/\/|\\|@|token|secret|password|10\.\d+\.\d+\.\d+|\.local\b/i.test(value)) return fallback;
  return value.slice(0, 80);
}

function safeData(data: Record<string, unknown> | undefined): Record<string, unknown> | undefined {
  if (!data || typeof data !== 'object') return undefined;
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(data)) {
    if (!/^[a-zA-Z][a-zA-Z0-9_]*$/.test(key)) continue;
    if (typeof value === 'string') result[key] = safeLabel(value, '<fixture>');
    else if (typeof value === 'number' || typeof value === 'boolean' || value === null) result[key] = value;
  }
  return Object.keys(result).length ? result : undefined;
}

export function emitHarness(name: string, fields: BeaconFields = {}): void {
  if (!armed) return;
  const beacon: HarnessBeacon = {
    name: safeLabel(name, 'fixture:event'),
    ts: Date.now(),
    seq: ++seq,
    ...(fields.streamId !== undefined ? { streamId: 'fixture-stream' } : {}),
    ...(fields.slot !== undefined && Number.isFinite(fields.slot) ? { slot: fields.slot } : {}),
    ...(fields.host !== undefined ? { host: safeLabel(fields.host, 'local') } : {}),
    ...(fields.provider !== undefined ? { provider: safeLabel(fields.provider, 'fixture-provider') } : {}),
    ...(safeData(fields.data) ? { data: safeData(fields.data) } : {}),
  };
  beacons.push(beacon);
  if (beacons.length > RING_MAX) beacons.shift();
  try { console.log(`[HARNESS] ${JSON.stringify(beacon)}`); } catch {}
}

const INERT_CHAT: ChatHarnessHandle = {
  armed: false,
  events: [],
  eventsOfType: () => [],
  flowCounts: () => new Map(),
  detach: () => {},
};

const INERT: DesktopHarnessHandle = {
  armed: false,
  events: [],
  eventsOfType: () => [],
  since: () => [],
  emit: () => {},
  chat: INERT_CHAT,
  clear: () => {},
  detach: () => {},
};

export function attachDesktopHarness(
  controller: ChatStoreControllerLike,
  options: { env?: Record<string, string | undefined>; config?: HarnessConfigLike; force?: boolean } = {},
): DesktopHarnessHandle {
  const enabled = options.force === true || isHarnessArmed(options.env, options.config);
  if (!enabled) return INERT;
  armed = true;
  seq = 0;
  beacons.length = 0;
  const chat = attachChatHarnessTelemetry(controller, options);
  emitHarness('harness:armed', { data: { surface: 'desktop', mode: 'synthetic' } });
  let detached = false;
  return {
    armed: true,
    get events() { return beacons; },
    eventsOfType(name) { return beacons.filter((beacon) => beacon.name === name); },
    since(afterSeq) { return beacons.filter((beacon) => beacon.seq > afterSeq); },
    emit: emitHarness,
    chat,
    clear() { beacons.length = 0; },
    detach() {
      if (detached) return;
      detached = true;
      chat.detach();
      armed = false;
    },
  };
}
