import type { PentacleStreamState } from '../types/pentacle';
import { normalizePentacleHost } from './pentacleHosts';

// Platform-neutral host configuration seam.
//
// The chat-model selectors need two pieces of hostderived information:
//   - the display ORDER of hosts (`getHostOrder`)
//   - a host's display THEME, of which the selectors only read `.label`
//     (`getHostTheme`)
//
// The DATA behind both (which hosts exist, their declared order, their
// labels/colors) is platform-sourced — in pentacle-mobile it comes from
// Expo config via `expo-constants`. To keep this package free of any
// Expo/React-Native/DOM dependency, the platform host injects a provider via
// `setHostConfigProvider`, mirroring the `setTelemetrySink` seam in
// `utils/telemetry`. The selectors call the package-level `getHostOrder` /
// `getHostTheme` below, which delegate to the injected provider.

export type HostTheme = {
  label: string;
  color: string;
  accent?: string;
  surface?: string;
  border?: string;
  header?: string;
};

export type HostConfigProvider = {
  getHostOrder: (state?: HostOrderStateInput) => string[];
  getHostTheme: (host: string) => HostTheme;
};

type HostOrderStateInput = Pick<PentacleStreamState, 'hosts' | 'sessions' | 'machineStats'>;

// Safe default that reproduces prior behavior when no platform config data is
// available (mirrors `config/local` when `Constants.expoConfig.extra` is
// undefined): there is no configured order or configured themes, so the order
// is derived purely from the live stream state and themes fall back to a
// titlecased host label. This is non-throwing and never silently changes a
// configured result, because once the host installs a provider it takes over
// entirely.
function titlecaseHost(id: string): string {
  const value = String(id || '').trim();
  if (!value) return 'Unknown';
  return value
    .split(/[-_\s.]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase())
    .join(' ');
}

const defaultProvider: HostConfigProvider = {
  getHostOrder(state) {
    const seen = new Set<string>();
    const ordered: string[] = [];
    const push = (value: string) => {
      const host = normalizePentacleHost(value);
      if (!host || seen.has(host)) return;
      seen.add(host);
      ordered.push(host);
    };
    if (state) {
      Object.values(state.hosts || {}).forEach((host) => push(host.host));
      state.sessions?.forEach((session) => push(session.host));
      Object.values(state.machineStats || {}).forEach((stats) => push(stats.host || ''));
    }
    return ordered;
  },
  getHostTheme(host) {
    const hostId = normalizePentacleHost(host);
    return { label: titlecaseHost(hostId), color: '' };
  },
};

let provider: HostConfigProvider = defaultProvider;

export function setHostConfigProvider(next: HostConfigProvider | null | undefined) {
  provider = next || defaultProvider;
}

export function getHostOrder(state?: HostOrderStateInput): string[] {
  return provider.getHostOrder(state);
}

export function getHostTheme(host: string): HostTheme {
  return provider.getHostTheme(host);
}
