// Unit tests for the desktop chat session-reliability view-state
// (public_contract, scope 1+2).
//
// Pure logic: bounded paging window, earlier-page growth + termination, prepend
// anchoring, near-bottom/pinned detection, unread accounting, and per-slot
// isolation. Bundled to CJS by scripts/run-tests.js.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  INITIAL_TRANSCRIPT_ROWS,
  TRANSCRIPT_PAGE_ROWS,
  NEAR_BOTTOM_PX,
  createSlotReliabilityState,
  distanceFromBottom,
  isNearBottom,
  hasEarlierHistory,
  earlierPageSize,
  visibleCountForEarlierPage,
  anchorScrollTopAfterPrepend,
  newestRowId,
  countTrailingUnread,
  reconcileOnRender,
  markCaughtUp,
} from '../renderer/src/chat_reliability_view';

const ids = (n: number, base = 0): string[] =>
  Array.from({ length: n }, (_, i) => `row-${base + i}`);

test('initial state mounts exactly 16 rows, pinned, no unread', () => {
  assert.equal(INITIAL_TRANSCRIPT_ROWS, 16);
  assert.equal(TRANSCRIPT_PAGE_ROWS, 48);
  const s = createSlotReliabilityState();
  assert.equal(s.visibleCount, 16);
  assert.equal(s.pinnedToBottom, true);
  assert.equal(s.unreadCount, 0);
  assert.equal(s.lastSeenRowId, null);
});

test('earlier page adds at most 48 and terminates at history start', () => {
  const s = createSlotReliabilityState();
  // 200 older rows held above the initial 16 window.
  assert.equal(hasEarlierHistory(200), true);
  assert.equal(earlierPageSize(200), 48);
  let vc = visibleCountForEarlierPage(s, 200);
  assert.equal(vc, 16 + 48); // one full page

  // A final short page: only 5 older rows remain.
  assert.equal(earlierPageSize(5), 5);
  vc = visibleCountForEarlierPage({ ...s, visibleCount: vc }, 5);
  assert.equal(vc, 16 + 48 + 5);

  // History start reached: nothing remains, affordance ends, window unchanged.
  assert.equal(hasEarlierHistory(0), false);
  assert.equal(earlierPageSize(0), 0);
  assert.equal(visibleCountForEarlierPage({ ...s, visibleCount: vc }, 0), vc);
});

test('earlier page never grows past what the store holds', () => {
  const s = { ...createSlotReliabilityState(), visibleCount: 16 };
  // Only 30 older rows remain — a page reveals 30, not 48.
  assert.equal(visibleCountForEarlierPage(s, 30), 46);
});

test('near-bottom / pinned detection uses a 32px threshold', () => {
  assert.equal(NEAR_BOTTOM_PX, 32);
  const atBottom = { scrollTop: 968, scrollHeight: 1000, clientHeight: 32 };
  assert.equal(distanceFromBottom(atBottom), 0);
  assert.equal(isNearBottom(atBottom), true);

  const within = { scrollTop: 940, scrollHeight: 1000, clientHeight: 32 }; // 28px up
  assert.equal(isNearBottom(within), true);

  const scrolledUp = { scrollTop: 500, scrollHeight: 1000, clientHeight: 32 }; // 468px up
  assert.equal(distanceFromBottom(scrolledUp), 468);
  assert.equal(isNearBottom(scrolledUp), false);
});

test('prepend anchoring pushes scrollTop down by the exact height gained', () => {
  // Was scrolled to top of a 1000px list; prepend grows it to 1600px.
  assert.equal(anchorScrollTopAfterPrepend(1000, 0, 1600), 600);
  // Mid-list position is preserved by the same delta.
  assert.equal(anchorScrollTopAfterPrepend(1000, 250, 1600), 850);
  // Never negative.
  assert.equal(anchorScrollTopAfterPrepend(1600, 0, 1000), 0);
});

test('unread = rows strictly after the seen watermark', () => {
  const list = ids(10); // row-0 .. row-9
  assert.equal(newestRowId(list), 'row-9');
  assert.equal(countTrailingUnread(list, 'row-9'), 0); // caught up
  assert.equal(countTrailingUnread(list, 'row-6'), 3); // row-7,8,9
  assert.equal(countTrailingUnread(list, null), 0); // never seen → 0
  assert.equal(countTrailingUnread(list, 'row-999'), 0); // watermark scrolled out → 0
  assert.equal(newestRowId([]), null);
});

test('reconcile: pinned follows tail and clears unread', () => {
  const s = { ...createSlotReliabilityState(), lastSeenRowId: 'row-3', unreadCount: 4, pinnedToBottom: false };
  const next = reconcileOnRender(s, ids(10), /* nearBottom */ true);
  assert.equal(next.pinnedToBottom, true);
  assert.equal(next.unreadCount, 0);
  assert.equal(next.lastSeenRowId, 'row-9');
});

test('reconcile: unpinned preserves position and accumulates unread', () => {
  const s = { ...createSlotReliabilityState(), pinnedToBottom: false, lastSeenRowId: 'row-4' };
  const next = reconcileOnRender(s, ids(12), /* nearBottom */ false);
  assert.equal(next.pinnedToBottom, false);
  assert.equal(next.lastSeenRowId, 'row-4'); // watermark held
  assert.equal(next.unreadCount, 7); // row-5..row-11
});

test('reconcile: empty transcript resets to a clean pinned baseline', () => {
  const s = { ...createSlotReliabilityState(), pinnedToBottom: false, unreadCount: 5, lastSeenRowId: 'row-1' };
  const next = reconcileOnRender(s, [], false);
  assert.equal(next.pinnedToBottom, true);
  assert.equal(next.unreadCount, 0);
  assert.equal(next.lastSeenRowId, null);
});

test('markCaughtUp pins, clears unread, re-baselines the watermark', () => {
  const s = { ...createSlotReliabilityState(), pinnedToBottom: false, unreadCount: 9, lastSeenRowId: 'row-2' };
  const next = markCaughtUp(s, ids(15));
  assert.equal(next.pinnedToBottom, true);
  assert.equal(next.unreadCount, 0);
  assert.equal(next.lastSeenRowId, 'row-14');
});

test('per-slot isolation: two states evolve independently', () => {
  const a = markCaughtUp(createSlotReliabilityState(), ids(20));
  const b = reconcileOnRender(createSlotReliabilityState(), ids(5), false);
  const aPaged = { ...a, visibleCount: visibleCountForEarlierPage(a, 100) };
  assert.equal(aPaged.visibleCount, 64);
  assert.equal(a.lastSeenRowId, 'row-19');
  assert.equal(a.pinnedToBottom, true); // a caught up
  // b is untouched by a's paging / seen watermark and evolved on its own inputs.
  assert.equal(b.visibleCount, 16);
  assert.equal(b.lastSeenRowId, null); // null watermark held while unpinned
  assert.equal(b.pinnedToBottom, false); // b was reconciled unpinned
  assert.equal(b.unreadCount, 0); // null watermark ⇒ unattributable ⇒ 0
});
