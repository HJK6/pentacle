import type { OptimisticSendState, PentacleEvent } from '../types/pentacle';

// Live reconcile window: a server USER echo arriving within this many ms of an
// optimistic send's created_at reconciles it. Mirrors the mobile store's and
// desktop controller's historical OPTIMISTIC_RECONCILE_WINDOW_MS.
export const OPTIMISTIC_RECONCILE_WINDOW_MS = 60_000;

/**
 * Parse a server event's wall-clock time (ms since epoch). Falls back to
 * Date.now() when the timestamp is missing/unparseable — byte-identical to the
 * `serverEventTime`/`eventTime` helpers previously triplicated across the
 * mobile store, desktop controller, and reducer.
 */
export function serverEventTime(event: PentacleEvent): number {
  const parsed = Date.parse(String(event.timestamp || ''));
  return Number.isFinite(parsed) ? parsed : Date.now();
}

export function parseServerEventTimeStrict(event: PentacleEvent): number | null {
  const parsed = Date.parse(String(event.timestamp || ''));
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Pure optimistic-send ↔ server-USER-echo matcher. Returns true when the given
 * server event is the daemon's echo of this optimistic send:
 *   - event.kind === 'USER' (case-insensitive)
 *   - NOT client_origin (i.e. a true server-origin echo, not our own visual row)
 *   - same stream_id
 *   - same text
 *   - |parseServerEventTimeStrict(event) - send.created_at| <= windowMs
 *
 * Status gating (queued/dispatched/acked/indeterminate/echoed) is the caller's
 * responsibility — call sites scan their optimisticSends map and apply it
 * before invoking this predicate. `windowMs` is passed explicitly so the live
 * reconcile path and the snapshot reconcile path can each supply their own
 * window constant while sharing one matcher.
 */
export function optimisticMatchesServerUser(
  send: Pick<OptimisticSendState, 'stream_id' | 'text' | 'created_at' | 'optimistic_id'>,
  serverEvent: PentacleEvent,
  windowMs: number,
): boolean {
  if (String(serverEvent.kind || '').toUpperCase() !== 'USER') return false;
  if (serverEvent.client_origin === true) return false;
  if (serverEvent.stream_id !== send.stream_id) return false;
  if (serverEvent.optimistic_id) return serverEvent.optimistic_id === send.optimistic_id;
  if (serverEvent.text !== send.text) return false;
  const parsedTime = parseServerEventTimeStrict(serverEvent);
  return parsedTime !== null && Math.abs(parsedTime - send.created_at) <= windowMs;
}
