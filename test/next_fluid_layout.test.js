'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const layout = require('../renderer/next/layout');
const W = 1440;
const fresh = (width = W, rail) => layout.shut(layout.createLayoutState(width, rail), 'lanes', width);
const open = (state, id, width = W) => layout.show(state, 'chat', id, width);
const dash = (state, width = W) => layout.show(state, 'dash', 'melody', width);
const last = layout.markUsed;
const seats = (state, width = W) => layout.visibleSeats?.(state, width) ??
  [...(layout.fits(state, width).assistant ? ['assistant'] : []), ...layout.fits(state, width).cards];
const start = () => open(fresh(), 'a');

test('U1 chat admission uses the active pane, not the newest or a fixed slot', () => {
  const one = start();
  const two = open(one, 'b');
  assert.deepEqual(seats(two), ['chat', 'chat2'], 'the second admission occupies a second visible pane');
  assert.equal(two.chat, 'a');
  assert.equal(two.chat2, 'b');
  let s = open(two, 'a');
  assert.equal(s.last, 'chat');
  assert.equal(s.chat2, 'b');
  s = open(s, 'c'); assert.equal(s.chat2, 'c'); assert.equal(s.chat, 'a');
  s = open(s, 'd'); assert.equal(s.chat, 'd'); assert.equal(s.chat2, 'c');
  s = open(s, '__new__'); assert.equal(s.chat2, '__new__');
  s = open(s, '__new__'); assert.equal(s.chat, 'd');
  s = layout.replaceChat(s, 'chat2', 'created'); assert.equal(s.chat2, 'created');
  assert.equal(open(layout.toggleFullScreen(two, 'chat', W), 'a').solo, 'chat', 'the reference does not move an already-visible chat');
  assert.equal(open(layout.toggleFullScreen(two, 'chat2', W), 'c').solo, null);
  assert.equal(open(last(one, 'assistant'), 'b').chat, 'b');
  assert.deepEqual(seats(open(last(one, 'assistant'), 'b')), ['assistant', 'chat']);
  assert.deepEqual(seats(open(last(dash(one), 'chat'), 'b')), ['chat', 'chat2']);
  assert.equal(open(dash(one), 'b').dash, 'melody');
  assert.deepEqual(seats(open(dash(fresh()), 'a')), ['chat', 'dash']);
  assert.deepEqual(seats(open(last(dash(fresh()), 'assistant'), 'a')), ['assistant', 'chat']);
  const showDash = dash(two);
  assert.equal(showDash.chat, null); assert.equal(showDash.chat2, 'b');
  assert.deepEqual(seats(showDash), ['chat2', 'dash']);
  const linked = layout.follow(two, 'chat2', 'design:example', W);
  assert.equal(linked.chat, null); assert.equal(linked.chat2, 'b');
  assert.deepEqual(seats(linked), ['chat2', 'dash']);
  assert.deepEqual(seats(layout.shut(two, 'chat', W)), ['assistant', 'chat2']);
  assert.deepEqual(seats(layout.shut(two, 'chat2', W)), ['assistant', 'chat']);
  assert.deepEqual(seats(layout.shut(open(layout.toAssistant(one, W), 'b'), 'chat2', W)), ['chat']);
});

test('U2 mark toggles actual stage/overlay, preserves sessions and remembers use', () => {
  const one = start();
  const aside = layout.toAssistant(last(one, 'assistant'), W);
  assert.deepEqual(seats(aside), ['chat'], 'mark hides Assistant, not the open chat');
  assert.equal(aside.last, 'chat');
  const back = layout.toAssistant(aside, W);
  assert.deepEqual(seats(back), ['assistant', 'chat']); assert.equal(back.last, 'assistant');
  assert.deepEqual(seats(layout.toAssistant(fresh(), W)), []);
  assert.deepEqual(seats(layout.toAssistant(layout.toAssistant(fresh(), W), W)), ['assistant']);
  assert.deepEqual(seats(layout.shut(aside, 'chat', W)), ['assistant']);
  const two = open(one, 'b');
  for (const s of [two, dash(one), layout.toggleFullScreen(one, 'chat', W)]) {
    const out = layout.toAssistant(s, W);
    assert.equal(out.overlay, true);
    assert.deepEqual(seats(out), seats(s));
    assert.equal(last(out, 'assistant').last, s.last);
    assert.equal(layout.dismissAssistant(out).overlay, false);
    assert.equal(layout.toAssistant(out, W).overlay, false);
  }
  const kept = layout.keepAssistant(layout.toAssistant(two, W), W);
  assert.equal(kept.chat, null); assert.equal(kept.chat2, 'b');
  const dashKept = layout.keepAssistant(layout.toAssistant(dash(one), W), W);
  assert.equal(dashKept.chat, 'a'); assert.deepEqual(seats(dashKept), ['assistant','dash']);
  assert.equal(layout.keepAssistant(layout.toAssistant(layout.toggleFullScreen(one,'chat',W), W), W).solo, null);
  const underlying = dash(one);
  const afterSolo = layout.keepAssistant(layout.toAssistant(layout.toggleFullScreen(underlying, 'dash', W), W), W);
  assert.deepEqual(seats(afterSolo), seats(underlying), 'Keep only leaves solo when a pair is already behind it');
  assert.equal(layout.toAssistant(open(fresh(640), 'a', 640), 640).overlay, true);
  assert.equal(layout.toAssistant(layout.toAssistant(open(fresh(640, 'top'), 'a', 640), 640), 640).overlay, false);
  const explicit = layout.toAssistant(open(fresh(), 'a'), W);
  const covered = layout.toAssistant(open(explicit, 'b'), W);
  const link = layout.follow(covered, 'assistant', 'design:example', W);
  assert.equal(link.aside, true, 'following from the overlay must not toggle the mark');
  assert.equal(link.overlay, true);
  assert.equal(layout.fits(link, W).assistant, false);
  const phone = layout.toAssistant(open(fresh(440), 'a', 440), 440);
  assert.deepEqual(seats(phone, 440), ['assistant']); assert.equal(phone.chat, null);
});

function geometry(state, key, start) {
  const order = seats(state);
  const sizes = Object.fromEntries(order.map(k => [k, 400]));
  const boxes = Object.fromEntries(order.map((k, i) => [k, { left: 60 + i * 412 }]));
  return { order, sizes, boxes, key, start, bounds: { left: 60, top: 0, edge: W } };
}

test('U3 drag crosses the neighbour midpoint with hysteresis and changes only visible seat order', () => {
  const s = start();
  const d = geometry(s, 'chat', { x: 530, y: 46 });
  // On the base, a pointer journey cannot change the projected order.
  const hold = layout.aimDrag?.(d, 310, 46) ?? d;
  const swap = layout.aimDrag?.(d, 309, 46) ?? d;
  assert.deepEqual(hold.order, ['assistant', 'chat']);
  assert.deepEqual(swap.order, ['chat', 'assistant'], 'left edge must pass midpoint minus eight');
  const back = layout.aimDrag(swap, 530, 46);
  assert.deepEqual(back.order, d.order);
  assert.deepEqual(layout.aimDrag(d, 530, 146).order, d.order);
  const moved = layout.dropSection(s, swap, W);
  assert.deepEqual(seats(moved), ['chat', 'assistant']);
  assert.deepEqual(moved.seats.slice(2), ['chat2','dash','lanes']);
  assert.deepEqual(seats(s), ['assistant','chat'], 'Escape discards the uncommitted drag');
  assert.equal(layout.liftReached(3, 3), false); assert.equal(layout.liftReached(3, 4), true);
});

test('U4 taken-there edges and top, with lane fixed and proportions retained', () => {
  const s = start();
  const d = geometry(s, 'chat', { x: 1399, y: 50 });
  const aim = (g, x, y) => layout.aimDrag?.(g, x, y) ?? { ...g, type: 'slot' };
  assert.equal(aim(d, 1417, 55).type, 'right', 'a deliberate move into right strip takes a half');
  assert.equal(aim(d, 1410, 55).type, 'slot');
  const leftStart = {...d, start: {x: 96, y: 50}};
  assert.equal(aim(leftStart, 90, 50).type, 'slot');
  assert.equal(aim(leftStart, 79, 50).type, 'left');
  const topStart = { ...d, start: {x: 530, y: 24}};
  assert.equal(aim(topStart, 530, 17).type, 'slot');
  assert.equal(aim(topStart, 530, 13).type, 'full');
  assert.equal(aim(d, 75, 15).type, 'full');
  assert.equal(aim({ ...d, key: 'assistant' }, 75, 15).type, 'left');
  assert.equal(layout.dropSection(s, aim(d, 75, 15), W).solo, 'chat');
  const half = layout.dropSection(s, aim(d, 1417, 55), W);
  assert.equal(half.half, 'chat');
  assert.deepEqual(layout.seatWidths(half, ['assistant','chat'], 1368), {assistant: 684, chat: 672});
  const withLanes = layout.show(s, 'lanes', true, W);
  const lane = geometry(withLanes, 'lanes', {x: 950, y: 50});
  const moved = layout.dropSection(withLanes, aim(lane, 75, 15), W);
  assert.equal(seats(moved)[0], 'lanes');
  assert.equal(layout.seatWidths(moved, seats(moved), 1368).lanes, 344);
  const hold = {...half, shares: { assistant: 350, dash: 500 }};
  const sized = layout.seatWidths(hold, ['assistant','chat','dash'], 1800);
  assert.ok(Math.abs(sized.assistant / sized.dash - .7) < 1e-6);
});

test('U5 measured widths persist as shares and reset; no lane boundary', () => {
  const s = start(); const keys = ['assistant','chat'];
  // The legacy grip retains pixels instead of scaling at a different width.
  const sized = layout.resizeBoundary?.({...s, half: 'chat'}, keys, {assistant: 500, chat: 500}, 0, -50) ??
    layout.setAssistantWidth(s, 450, 1012, W);
  const widths = layout.seatWidths?.(sized, keys, 2012) ?? { assistant: sized.assistantWidth };
  assert.equal(widths.assistant, 900, 'a 45% drag survives doubling the movable width');
  assert.equal(sized.half, null);
  const floor = layout.resizeBoundary(s, keys, {assistant: 500, chat: 500}, 0, 900);
  assert.deepEqual(floor.shares, {assistant: 688, chat: 312});
  assert.equal(layout.resizeBoundary(s, keys, {assistant: 500, chat: 500}, 0, -900).shares.assistant, 280);
  assert.equal(layout.resizeBoundary(s, ['assistant','lanes'], {assistant: 500, lanes: 344}, 0, 80), s);
  assert.deepEqual(layout.resetWidths(sized).shares, {});
  assert.equal(layout.resetWidths({...sized, half:'chat'}).half, null);
});

test('U6 stored Top changes room; absent and unknown default to Left', () => {
  const show = rail => seats(open(fresh(640, rail), 'a', 640), 640);
  assert.deepEqual(show('top'), ['assistant','chat'], 'stored Top changes the fit at this exact width');
  assert.deepEqual(show(undefined), ['chat']);
  assert.deepEqual(show('right'), ['chat']);
  assert.equal(layout.setRail(fresh(), 'top').rail, 'top');
  assert.equal(layout.setRail(fresh(640, 'top'), 'typo').rail, 'left');
});
