import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  applyWorkLanesFrame,
  emptyWorkLanesInventory,
  initialPentacleStreamState,
  laneUpdateFromEvent,
  normalizeWorkLanesInventory,
  selectOpenLaneCount,
  selectSessionDetail,
  selectWorkLanes,
  workLaneEtaLabel,
  workLaneTapTarget,
  applyPentacleEvent,
  type PentacleEvent,
} from '../src/index.ts';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/work-lanes-inventory.json', import.meta.url), 'utf8'));

test('inventory frame: server order, counts.open header, done never present', () => {
  const inv = applyWorkLanesFrame(emptyWorkLanesInventory(), fixture.inventory_frame);
  assert.deepEqual(selectWorkLanes(inv).map((lane) => lane.lane_id), fixture.expected.order);
  assert.equal(selectOpenLaneCount(inv), fixture.expected.header_count);
  assert.equal(inv.counts.open, 4);
  assert.deepEqual(inv.counts, fixture.inventory_frame.counts);
  assert.equal(inv.truncated, false);
  assert.equal(selectWorkLanes(inv).some((lane) => (lane.state as string) === 'done'), false);
  for (const lane of selectWorkLanes(inv)) assert.ok(fixture.expected.header_states.includes(lane.state));
});

test('hello/list_sessions work_lanes field is the same frame as the push frame', () => {
  const viaPush = applyWorkLanesFrame(emptyWorkLanesInventory(), fixture.inventory_frame);
  const viaHello = applyWorkLanesFrame(emptyWorkLanesInventory(), { type: 'snapshot', ...fixture.hello_field });
  const viaList = applyWorkLanesFrame(emptyWorkLanesInventory(), { type: 'list_sessions', ...fixture.hello_field });
  assert.deepEqual(viaHello, viaPush);
  assert.deepEqual(viaList, viaPush);
});

test('a frame without lane data leaves the inventory untouched; clients never reorder', () => {
  const inv = applyWorkLanesFrame(emptyWorkLanesInventory(), fixture.inventory_frame);
  assert.equal(applyWorkLanesFrame(inv, { type: 'session.inventory', sessions: [] }), inv);
  const shuffled = { ...fixture.inventory_frame, lanes: [...fixture.inventory_frame.lanes].reverse() };
  assert.deepEqual(selectWorkLanes(applyWorkLanesFrame(inv, shuffled)).map((l) => l.lane_id),
    [...fixture.expected.order].reverse());
});

test('header count comes from counts.open, not from the (possibly truncated) lanes array', () => {
  const frame = { ...fixture.inventory_frame, lanes: fixture.inventory_frame.lanes.slice(0, 1), truncated: true,
    counts: { open: 70, active: 30, paused: 30, blocked: 10 } };
  const inv = applyWorkLanesFrame(emptyWorkLanesInventory(), frame);
  assert.equal(selectOpenLaneCount(inv), 70);
  assert.equal(inv.truncated, true);
});

test('malformed lanes are dropped, done lanes are never admitted to lanes[]', () => {
  const frame = { ...fixture.inventory_frame, lanes: [
    ...fixture.inventory_frame.lanes, { lane_id: 'x', state: 'done' }, { state: 'active' }, null, 'junk',
  ] };
  const inv = normalizeWorkLanesInventory(frame)!;
  assert.deepEqual(inv.lanes.map((l) => l.lane_id), fixture.expected.order);
  assert.equal(normalizeWorkLanesInventory({ type: 'work_lanes.inventory' }), null);
});

test('tap target per visible_chat.available matches the fixture', () => {
  const inv = applyWorkLanesFrame(emptyWorkLanesInventory(), fixture.inventory_frame);
  for (const lane of selectWorkLanes(inv)) {
    assert.deepEqual(workLaneTapTarget(lane), fixture.expected.tap[lane.lane_id], lane.lane_id);
  }
});

test('tap target never falls back to Bart, a hidden worker or a new generation', () => {
  const inv = applyWorkLanesFrame(emptyWorkLanesInventory(), fixture.inventory_frame);
  const unavailable = selectWorkLanes(inv).find((l) => l.visible_chat.available === 'unavailable')!;
  const target = workLaneTapTarget(unavailable);
  assert.equal(target.action, 'unavailable');
  assert.equal('stream_id' in target, false);
  const noPointer = { ...unavailable, visible_chat: { ...unavailable.visible_chat, stream_id: '' , available: 'open' as const } };
  assert.equal(workLaneTapTarget(noPointer).action, 'unavailable');
});

test('eta_stale renders "ETA stale" instead of late Xm, per fixture', () => {
  const inv = applyWorkLanesFrame(emptyWorkLanesInventory(), fixture.inventory_frame);
  const now = Date.parse('2026-10-07T19:01:00Z');
  for (const lane of selectWorkLanes(inv)) {
    const label = workLaneEtaLabel(lane, now);
    assert.equal(label.stale, fixture.expected.eta_stale[lane.lane_id], lane.lane_id);
    if (label.stale) assert.equal(label.text, 'ETA stale');
    else assert.match(label.text, /^~\d+(m|h)/);
    assert.doesNotMatch(label.text, /late/);
  }
});

test('lane with no lead and no eta has an empty eta label', () => {
  const inv = applyWorkLanesFrame(emptyWorkLanesInventory(), fixture.inventory_frame);
  const lane = { ...selectWorkLanes(inv)[0], lead: null };
  assert.deepEqual(workLaneEtaLabel(lane, Date.now()), { text: '', stale: false });
});

function event(wire: any): PentacleEvent {
  return { host: 'bart', session_name: 'assistant', ...wire.event } as PentacleEvent;
}

test('lane_update events parse raw.lane_update for every kind; plain events do not', () => {
  const kinds = fixture.lane_update_events.map((wire: any) => laneUpdateFromEvent(event(wire))?.kind);
  assert.deepEqual(kinds, ['lane_started', 'lane_blocked', 'lane_unblocked', 'lane_completed', 'major_decision', 'milestone']);
  const blocked = laneUpdateFromEvent(event(fixture.lane_update_events[1]))!;
  assert.equal(blocked.lane_id, 'wl-blocked-0001');
  assert.equal(blocked.summary, 'Waiting for operator deploy window');
  assert.equal(blocked.update_id, 'lane-update:wl-blocked-0001:req-block-1');
  assert.equal(laneUpdateFromEvent({ ...event(fixture.lane_update_events[0]), publish_kind: 'prose' } as PentacleEvent), null);
  assert.equal(laneUpdateFromEvent({ ...event(fixture.lane_update_events[0]), raw: {} } as PentacleEvent), null);
  assert.equal(laneUpdateFromEvent({ ...event(fixture.lane_update_events[0]),
    raw: { lane_update: { ...fixture.lane_update_events[0].event.raw.lane_update, kind: 'paused' } } } as PentacleEvent), null);
});

test('transcript rows for lane_update carry laneUpdate; legacy text stays the summary; dedupe by message_id', () => {
  let state = { ...initialPentacleStreamState };
  state = { ...state, sessions: [{ stream_id: 'bart:assistant', host: 'bart', provider: 'composite', session_name: 'assistant',
    last_event_at: '2026-10-07T19:00:00.000Z' } as never] };
  for (const wire of fixture.lane_update_events) state = applyPentacleEvent(state, event(wire));
  // Live broadcast + history replay of the same publication must not double up.
  state = applyPentacleEvent(state, { ...event(fixture.lane_update_events[1]), daemon_seq: 1043 });
  const detail = selectSessionDetail(state, 'bart:assistant', { visibleCount: 50 })!;
  const rows = detail.transcriptItems.filter((item) => item.publishKind === 'lane_update');
  assert.equal(new Set(rows.map((r) => r.messageId)).size, rows.length);
  assert.equal(rows.length, 6);
  assert.deepEqual(rows.map((r) => r.laneUpdate?.kind).sort(),
    ['lane_blocked', 'lane_completed', 'lane_started', 'lane_unblocked', 'major_decision', 'milestone']);
  const blocked = rows.find((r) => r.laneUpdate?.kind === 'lane_blocked')!;
  assert.equal(blocked.text, 'Waiting for operator deploy window');
});
