// Pure, DOM-free view state for a bounded desktop transcript.

export const INITIAL_TRANSCRIPT_ROWS = 16;
export const TRANSCRIPT_PAGE_ROWS = 48;
export const NEAR_BOTTOM_PX = 32;

export type SlotReliabilityState = {
  visibleCount: number;
  pinnedToBottom: boolean;
  unreadCount: number;
  lastSeenRowId: string | null;
};

export function createSlotReliabilityState(): SlotReliabilityState {
  return { visibleCount: INITIAL_TRANSCRIPT_ROWS, pinnedToBottom: true, unreadCount: 0, lastSeenRowId: null };
}

export type ScrollMetrics = { scrollTop: number; scrollHeight: number; clientHeight: number };

export function distanceFromBottom(metrics: ScrollMetrics): number {
  return Math.max(0, metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight);
}

export function isNearBottom(metrics: ScrollMetrics, threshold: number = NEAR_BOTTOM_PX): boolean {
  return distanceFromBottom(metrics) <= threshold;
}

export function hasEarlierHistory(remainingCount: number): boolean {
  return Number.isFinite(remainingCount) && remainingCount > 0;
}

export function earlierPageSize(remainingCount: number): number {
  if (!hasEarlierHistory(remainingCount)) return 0;
  return Math.min(TRANSCRIPT_PAGE_ROWS, Math.max(0, Math.floor(remainingCount)));
}

export function visibleCountForEarlierPage(state: SlotReliabilityState, remainingCount: number): number {
  return state.visibleCount + earlierPageSize(remainingCount);
}

export function anchorScrollTopAfterPrepend(
  prevScrollHeight: number,
  prevScrollTop: number,
  nextScrollHeight: number,
): number {
  return Math.max(0, prevScrollTop + (nextScrollHeight - prevScrollHeight));
}

export function newestRowId(rowIds: ReadonlyArray<string>): string | null {
  return rowIds.length ? rowIds[rowIds.length - 1] : null;
}

export function countTrailingUnread(rowIds: ReadonlyArray<string>, lastSeenRowId: string | null): number {
  if (!lastSeenRowId) return 0;
  const index = rowIds.lastIndexOf(lastSeenRowId);
  return index < 0 ? 0 : rowIds.length - 1 - index;
}

export function reconcileOnRender(
  state: SlotReliabilityState,
  rowIds: ReadonlyArray<string>,
  nearBottom: boolean,
): SlotReliabilityState {
  const tail = newestRowId(rowIds);
  if (tail === null) return { ...state, pinnedToBottom: true, unreadCount: 0, lastSeenRowId: null };
  if (nearBottom) return { ...state, pinnedToBottom: true, unreadCount: 0, lastSeenRowId: tail };
  return { ...state, pinnedToBottom: false, unreadCount: countTrailingUnread(rowIds, state.lastSeenRowId) };
}

export function markCaughtUp(state: SlotReliabilityState, rowIds: ReadonlyArray<string>): SlotReliabilityState {
  return { ...state, pinnedToBottom: true, unreadCount: 0, lastSeenRowId: newestRowId(rowIds) };
}
