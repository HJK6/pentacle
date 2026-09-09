// Pure helpers for recovering optimistic sends after a websocket reconnect.
// The local types are the small public adapter contract required by this file;
// no private package or source tree is imported at runtime or for typing.

export type OptimisticSendStatus = 'queued' | 'dispatched' | 'indeterminate' | 'acked' | 'echoed' | 'failed';

export type OptimisticSendState = {
  optimistic_id: string;
  request_id: string;
  status: OptimisticSendStatus;
  created_at: number;
  queued_at?: number;
  socket_generation?: number;
};

export type PentacleStreamState = {
  optimisticSends?: Record<string, OptimisticSendState>;
  optimisticByRequestId?: Record<string, string>;
  [key: string]: unknown;
};

export const RECONNECT_REPLAY_MAX_AGE_MS = 30 * 60 * 1000;

export function eligibleReconnectReplayOptimisticIds(
  state: PentacleStreamState,
  generation: number,
  now: number = Date.now(),
  maxAgeMs: number = RECONNECT_REPLAY_MAX_AGE_MS,
): string[] {
  return Object.values(state.optimisticSends ?? {})
    .filter((send) => (
      send.socket_generation === generation
      && (send.status === 'queued' || send.status === 'dispatched' || send.status === 'indeterminate')
      && now - send.created_at <= maxAgeMs
    ))
    .sort((a, b) => (a.queued_at ?? a.created_at) - (b.queued_at ?? b.created_at))
    .map((send) => send.optimistic_id);
}

export function markDaemonRestartSurvivorsIndeterminate(
  state: PentacleStreamState,
  generation: number,
): PentacleStreamState {
  let changed = false;
  const optimisticSends = { ...(state.optimisticSends ?? {}) };
  for (const [optimisticId, send] of Object.entries(optimisticSends)) {
    if (send.socket_generation !== generation
      || (send.status !== 'queued' && send.status !== 'dispatched')) continue;
    optimisticSends[optimisticId] = { ...send, status: 'indeterminate' };
    changed = true;
  }
  return changed ? { ...state, optimisticSends } : state;
}

export function rotateOptimisticSendRequestId(
  state: PentacleStreamState,
  optimisticId: string,
  newRequestId: string,
): PentacleStreamState {
  const send = state.optimisticSends?.[optimisticId];
  if (!send || send.request_id === newRequestId) return state;
  const optimisticSends = {
    ...(state.optimisticSends ?? {}),
    [optimisticId]: { ...send, request_id: newRequestId },
  };
  const optimisticByRequestId = { ...(state.optimisticByRequestId ?? {}) };
  delete optimisticByRequestId[send.request_id];
  optimisticByRequestId[newRequestId] = optimisticId;
  return { ...state, optimisticSends, optimisticByRequestId };
}
