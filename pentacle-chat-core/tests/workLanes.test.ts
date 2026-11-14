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
  LANE_ICON_KEYS,
} from '../src/index.ts';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/work-lanes-inventory.json', import.meta.url), 'utf8'));

test('optional and retired checkpoint policy fields survive the existing normalizer', () => {
  const cell = fixture.spec_details_v1;
  const raw = structuredClone(cell.show_projection);
  raw.members[0].review_text = 'F12 reproduced; review current evidence.';
  raw.members[0].retired_policy_text = 'Prior released field.';
  raw.members[0].progress_text = 'Fixed v2.3; 12 checks pass.';
  raw.visibility_counts = { working: 1, blocked: null, complete: { source: false } };
  const lane = normalizeWorkLanesInventory({ ...cell.inventory_frame, lanes: [raw] })!.lanes[0];
  const member = lane.progress!.members[0];
  assert.equal(member.review_text, raw.members[0].review_text);
  assert.equal(member.retired_policy_text, 'Prior released field.');
  assert.equal(member.progress_text, 'Fixed v2.3; 12 checks pass.');
  assert.deepEqual(lane.visibility_counts, raw.visibility_counts);
});

test('lane icon keys match the frozen ordered keyword table', () => {
  const table = JSON.parse(readFileSync(new URL('../lane_icons.json', import.meta.url), 'utf8'));
  assert.deepEqual([...LANE_ICON_KEYS].sort(), table.icons.map((icon: { key: string }) => icon.key).sort());
});

test('structured spec fields survive normalization and older frames default safely', () => {
  const cell = fixture.spec_details_v1;
  const lane = normalizeWorkLanesInventory(cell.inventory_frame)!.lanes[0];
  assert.equal(lane.icon, 'phone');
  const member = lane.progress!.members[0];
  for (const key of ['worked', 'lead_seat', 'status_line', 'next_line']) {
    assert.deepEqual((member as Record<string, unknown>)[key], cell.inventory_frame.lanes[0].members[0][key]);
  }
  assert.equal(member.status_line, 'Tests pass. Review pending');
  assert.equal('blocked_line' in member, false);
  assert.equal(member.lead_seat?.active_step, 'Review the result.');
  assert.equal(member.criteria, undefined);
  assert.equal(member.transitions, undefined);
  const detail = normalizeWorkLanesInventory({ ...cell.inventory_frame, lanes: [cell.show_projection] })!.lanes[0].progress!.members[0];
  assert.deepEqual(detail.criteria, cell.show_projection.members[0].criteria);
  assert.deepEqual(detail.transitions, cell.show_projection.members[0].transitions);
  assert.equal(detail.blocked_line, null);
  const old = normalizeWorkLanesInventory(fixture.inventory_frame)!.lanes[0];
  assert.equal(old.icon, null);
});

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

test('tap target never falls back to the primary assistant, a hidden worker or a new generation', () => {
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
  return { host: 'home', session_name: 'assistant', ...wire.event } as PentacleEvent;
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
  state = { ...state, sessions: [{ stream_id: 'home:assistant', host: 'home', provider: 'composite', session_name: 'assistant',
    last_event_at: '2026-10-07T19:00:00.000Z' } as never] };
  for (const wire of fixture.lane_update_events) state = applyPentacleEvent(state, event(wire));
  // Live broadcast + history replay of the same publication must not double up.
  state = applyPentacleEvent(state, { ...event(fixture.lane_update_events[1]), daemon_seq: 1043 });
  const detail = selectSessionDetail(state, 'home:assistant', { visibleCount: 50 })!;
  const rows = detail.transcriptItems.filter((item) => item.publishKind === 'lane_update');
  assert.equal(new Set(rows.map((r) => r.messageId)).size, rows.length);
  assert.equal(rows.length, 6);
  assert.deepEqual(rows.map((r) => r.laneUpdate?.kind).sort(),
    ['lane_blocked', 'lane_completed', 'lane_started', 'lane_unblocked', 'major_decision', 'milestone']);
  const blocked = rows.find((r) => r.laneUpdate?.kind === 'lane_blocked')!;
  assert.equal(blocked.text, 'Waiting for operator deploy window');
});

test('v1 consumer accepts additive member facts with unchanged state, order and counts', () => {
  for (const cell of fixture.progress_v2) {
    const inv = applyWorkLanesFrame(emptyWorkLanesInventory(), cell.frame);
    assert.deepEqual(inv.counts, cell.frame.counts, cell.name);
    assert.deepEqual(selectWorkLanes(inv).map((lane) => [lane.lane_id, lane.state]),
      cell.frame.lanes.map((lane: {lane_id: string; state: string}) => [lane.lane_id, lane.state]), cell.name);
  }
});

// Lanes 3D (spec_pentacle__lanes_3d_web_dashboard_2026_10): the additive progress
// facts the daemon already sends survive normalization, so web consumers can show
// member specs and remaining-work estimates.
test('progress facts and members survive normalization in wire order', () => {
  for (const cell of fixture.progress_v2) {
    const inv = applyWorkLanesFrame(emptyWorkLanesInventory(), cell.frame);
    assert.deepEqual(inv.work_index, cell.frame.work_index ?? null, cell.name);
    selectWorkLanes(inv).forEach((lane, i) => {
      const wire = cell.frame.lanes[i];
      const progress = lane.progress!;
      assert.ok(progress, `${cell.name}: progress present`);
      for (const key of ['members_total', 'no_spec_reason', 'items_total', 'items_completed', 'items_dropped', 'items_open',
        'items_unresolved', 'ac_checked', 'ac_total', 'ac_members', 'open_estimate_h', 'open_estimated', 'estimate_complete', 'freshness_at']) {
        assert.deepEqual((progress as Record<string, unknown>)[key], wire[key] ?? null, `${cell.name}: ${key}`);
      }
      assert.deepEqual(progress.members.map((m) => m.spec_id), wire.members.map((m: { spec_id: string }) => m.spec_id), cell.name);
      progress.members.forEach((member, j) => {
        const raw = wire.members[j];
        for (const key of ['title', 'status', 'terminal', 'ac_checked', 'ac_total', 'estimate', 'status_text', 'next_action_text'])
          assert.deepEqual((member as Record<string, unknown>)[key], raw[key] ?? null, `${cell.name}: member ${key}`);
        assert.equal(member.estimate_exempt, raw.estimate_exempt === true);
        assert.equal(member.observation?.quality ?? null, raw.observation?.quality ?? null);
      });
    });
  }
});

test('a v1 lane without progress facts normalizes progress to null; malformed members are dropped', () => {
  const inv = applyWorkLanesFrame(emptyWorkLanesInventory(), fixture.inventory_frame);
  assert.ok(selectWorkLanes(inv).every((lane) => lane.progress === null));
  assert.equal(inv.work_index, null);
  const frame = JSON.parse(JSON.stringify(fixture.progress_v2[0].frame));
  frame.lanes[0].members = [null, { title: 'no id' }, { spec_id: 'spec_demo__ok', title: 'Ok', terminal: 'bogus', estimate: { p25: 'x' } }];
  const lane = selectWorkLanes(applyWorkLanesFrame(emptyWorkLanesInventory(), frame))[0];
  assert.deepEqual(lane.progress!.members.map((m) => m.spec_id), ['spec_demo__ok']);
  assert.equal(lane.progress!.members[0].terminal, null);
  assert.equal(lane.progress!.members[0].estimate, null);
});

test('park record is additive and malformed values never drop a lane', () => {
  for (const [raw, expected] of [
    [{ at: '2026-01-01T00:00:00Z', reason: 'Later' }, { at: '2026-01-01T00:00:00Z', reason: 'Later' }],
    [{ at: null, reason: '' }, { at: null, reason: null }],
    ['parked', undefined], [undefined, undefined],
  ]) {
    const frame = { lanes: [{ lane_id: 'parked', state: 'paused', parked: raw }] };
    const lane = normalizeWorkLanesInventory(frame)!.lanes[0];
    assert.equal(lane.state, 'paused');
    assert.deepEqual(lane.parked, expected);
    assert.equal(Object.hasOwn(lane, 'parked'), expected !== undefined);
  }
  const cell = fixture.parked_v1;
  assert.deepEqual(normalizeWorkLanesInventory(cell.inventory_frame)!.lanes[0].parked,
    cell.inventory_frame.lanes[0].parked);
});
