import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { normalizeWorkLanesInventory, LANE_HUE_KEYS, workLaneGoalClasses, workLaneGoalRoad } from '../src/services/workLanes.ts';

const goal = { goal_id: 'g_' + 'a'.repeat(24), title: 'Finish the fixture', state: 'in_progress',
  done: 1, total: 2, rank: 17, icon: 'cube', target_date: '2026-11-30' };
const raw = { lane_id: 'wl-test', title: 'Fixture', state: 'paused', hue: 'chartreuse', goals: [goal],
  goals_total: 9, default_goal: { state: 'not_started', done: 0, total: 65 }, detail_revision: 'b'.repeat(64),
  members: [{ spec_id: 'spec_demo__fixture', goal_id: goal.goal_id }], members_total: 65,
  members_next: 'spec_demo__fixture' };
const lane = (value: unknown) => normalizeWorkLanesInventory({ lanes: [value] })!.lanes[0];

test('goal projection, full revision, hue and paged attribution survive normalization', () => {
  const value = lane(raw);
  assert.deepEqual(value.goals, [goal]);
  assert.equal(value.goals_total, 9);
  assert.deepEqual(value.default_goal, raw.default_goal);
  assert.equal(value.detail_revision, raw.detail_revision);
  assert.equal(value.hue, 'chartreuse');
  assert.equal(value.progress!.members[0].goal_id, goal.goal_id);
  assert.equal(value.progress!.members_total, 65);
  assert.equal(value.progress!.members_next, 'spec_demo__fixture');
});

test('optional older frames and invalid hues stay sparse without inferred goal icons', () => {
  const old = lane({ lane_id: 'wl-old', state: 'paused', title: 'Mobile release' });
  assert.deepEqual(old.goals, []);
  assert.equal(old.default_goal, null);
  assert.equal(old.detail_revision, null);
  assert.equal(old.hue, null);
  const value = lane({ ...raw, hue: 'unknown', detail_revision: 'short', goals: [
    { ...goal, icon: null }, { ...goal, goal_id: 'g_' + 'c'.repeat(24), icon: undefined },
  ] });
  assert.equal(value.hue, null);
  assert.equal(value.detail_revision, null);
  assert.equal(value.goals![0].icon, null);
  assert.equal(value.goals![1].icon, null);
});

test('malformed goals cannot introduce false progress, rank, date or enum values', () => {
  for (const patch of [{ done: -1 }, { done: 3 }, { total: 2.5 }, { rank: true },
    { total: 2147483648 }, { goal_id: 'not-a-goal' }, { state: 'completed' }]) {
    assert.deepEqual(lane({ ...raw, goals: [{ ...goal, ...patch }] }).goals, []);
  }
  assert.equal(lane({ ...raw, goals: [{ ...goal, target_date: '2026-02-30', icon: 'unknown' }] }).goals![0].target_date, undefined);
  assert.equal(lane({ ...raw, goals: [{ ...goal, icon: 'unknown' }] }).goals![0].icon, null);
});

test('hue keys preserve the single palette order', () => {
  const table = JSON.parse(readFileSync(new URL('../lane_hues.json', import.meta.url), 'utf8'));
  assert.deepEqual([...LANE_HUE_KEYS], table.palette.map((cell: { key: string }) => cell.key));
});

test('shared daemon fixture preserves complete goal wire fields on both surfaces', () => {
  const fixture = JSON.parse(readFileSync(new URL('./fixtures/work-lanes-inventory.json', import.meta.url), 'utf8')).goals_v1;
  for (const raw of [fixture.inventory_frame.lanes[0], fixture.show_projection]) {
    const value = lane(raw);
    assert.deepEqual(value.goals, raw.goals);
    assert.equal(value.goals_total, raw.goals_total);
    assert.equal(value.detail_revision, raw.detail_revision);
    assert.equal(value.hue, raw.hue);
    assert.equal(value.progress!.members[0].goal_id, raw.members[0].goal_id);
  }
});

test('lane progress keys normalize and decode through the one class reader', () => {
  const second = { ...goal, goal_id: 'g_' + 'c'.repeat(24) };
  const value = lane({ ...raw, goals: [goal, second], goal_classes: 'wbm', goal_counts: [2, 1, 4, 0],
    progress_at: '2026-11-10T10:00:00Z', state_at: '2026-11-09T08:00:00Z' });
  assert.deepEqual(workLaneGoalClasses(value), {
    goals: { [goal.goal_id]: 'working', [second.goal_id]: 'blocked' }, default_goal: 'moving' });
  assert.deepEqual(value.goal_counts, { moving: 2, blocked: 1, queued: 4, done: 0 });
  assert.equal(value.progress_at, '2026-11-10T10:00:00Z');
  assert.equal(value.state_at, '2026-11-09T08:00:00Z');
  // A dropped goal drops its letter; the default goal may be absent.
  const dropped = lane({ ...raw, goals: [{ ...goal, done: -1 }, second], goal_classes: 'dq-' });
  assert.deepEqual(workLaneGoalClasses(dropped), { goals: { [second.goal_id]: 'queued' }, default_goal: null });
  for (const bad of ['wb', 'wbx', 'w-m', 7]) {
    assert.equal(lane({ ...raw, goals: [goal, second], goal_classes: bad }).goal_classes, null);
  }
  assert.equal(lane({ ...raw, goal_counts: [1, 2, 3] }).goal_counts, null);
});

test('older frames without the progress keys stay sparse', () => {
  const value = lane(raw);
  for (const key of ['goal_classes', 'goal_counts', 'progress_at', 'state_at', 'recent']) assert.equal(key in value, false);
  assert.equal(workLaneGoalClasses(value), null);
  const member = value.progress!.members[0];
  for (const key of ['progress_at', 'summary_line', 'estimate_first', 'unblocks', 'recent']) assert.equal(key in member, false);
  assert.equal('moving' in value.default_goal!, false);
});

test('detail goal, member and trail keys normalize', () => {
  const entries = [
    { at: '2026-11-10T10:00:00Z', spec_id: 'spec_demo__a', kind: 'note', text: 'Assemble the span.' },
    { at: '2026-11-10T09:00:00Z', spec_id: 'spec_demo__a', kind: 'checks', count: 2 },
    { at: '2026-11-10T08:00:00Z', spec_id: 'spec_demo__b', kind: 'done' },
    { at: '2026-11-10T07:00:00Z', spec_id: 'spec_demo__b', kind: 'blocked', text: null },
    { at: '2026-11-10T06:00:00Z', spec_id: 'spec_demo__b', kind: 'unknown' },
    { at: '2026-11-10T05:00:00Z', spec_id: 'spec_demo__b', kind: 'checks', count: 0 },
  ];
  const detail = { moving: 1, blocked: 2, working: true, left_h: 3.5, progress_at: '2026-11-10T10:00:00Z' };
  const value = lane({ ...raw, goals: [{ ...goal, ...detail }], default_goal: { ...raw.default_goal, ...detail, left_h: null },
    recent: entries, selected_goal: { ...goal, ...detail, summary_line: 'A finite outcome.', recent: entries },
    members: [{ spec_id: 'spec_demo__a', progress_at: null, summary_line: 'Readable summary.',
      estimate_first: { p25: 1, p75: 3, median: 2 }, unblocks: [{ spec_id: 'spec_demo__b', title: 'B' }, { title: 'x' }],
      recent: entries.slice(0, 2) }] });
  assert.deepEqual(value.goals![0], { ...goal, ...detail });
  assert.equal(value.default_goal!.left_h, null);
  assert.deepEqual(value.recent, entries.slice(0, 4));
  assert.equal(value.selected_goal!.summary_line, 'A finite outcome.');
  assert.deepEqual(value.selected_goal!.recent, entries.slice(0, 4));
  const member = value.progress!.members[0];
  assert.equal(member.progress_at, null);
  assert.equal(member.summary_line, 'Readable summary.');
  assert.deepEqual(member.estimate_first, { p25: 1, p75: 3, median: 2 });
  assert.deepEqual(member.unblocks, [{ spec_id: 'spec_demo__b', title: 'B' }]);
  assert.deepEqual(member.recent, entries.slice(0, 2));
  assert.equal(lane({ ...raw, goals: [{ ...goal, left_h: -1 }] }).goals![0].left_h, null);
});

test('shared daemon fixture classes decode on both surfaces', () => {
  const fixture = JSON.parse(readFileSync(new URL('./fixtures/work-lanes-inventory.json', import.meta.url), 'utf8')).goals_v1;
  for (const raw of [fixture.inventory_frame.lanes[0], fixture.show_projection]) {
    const value = lane(raw);
    assert.equal(value.goal_classes, raw.goal_classes);
    const classes = workLaneGoalClasses(value)!;
    assert.equal(Object.keys(classes.goals).length, raw.goals.length);
    assert.deepEqual(value.goal_counts, { moving: raw.goal_counts[0], blocked: raw.goal_counts[1],
      queued: raw.goal_counts[2], done: raw.goal_counts[3] });
  }
});


test('goal road aligns positions and decodes all named classes', () => {
  const value = lane({ ...raw, goals: [{ ...goal, pos: 4 }], goals_total: 5, goal_road: 'dwmbq' });
  assert.equal(value.goal_road, 'dwmbq');
  assert.equal(value.goals![0].pos, 4);
  assert.deepEqual(workLaneGoalRoad(value), ['done', 'working', 'moving', 'blocked', 'queued']);
  assert.deepEqual(workLaneGoalRoad(lane({ ...raw, goals: [], goals_total: 0, goal_road: '' })), []);
  assert.equal(workLaneGoalRoad(lane(raw)), null);
});

test('invalid road or positions discard only road alignment', () => {
  const second = { ...goal, goal_id: 'g_' + 'c'.repeat(24) };
  const good = { ...raw, goals: [{ ...goal, pos: 0 }, { ...second, pos: 1 }], goals_total: 2, goal_road: 'wq' };
  for (const patch of [
    { goal_road: 'w' }, { goal_road: 'w\n' }, { goal_road: 'wx' }, { goal_road: null }, { goal_road: 2 },
    { goals: [goal, { ...second, pos: 1 }] },
    { goals: [{ ...goal, pos: 1 }, { ...second, pos: 1 }] },
    { goals: [{ ...goal, pos: 2 }, { ...second, pos: 1 }] },
    { goals: [{ ...goal, pos: -1 }, { ...second, pos: 1 }] },
    { goals: [{ ...goal, pos: 0.5 }, { ...second, pos: 1 }] },
    { goals: [{ ...goal, pos: true }, { ...second, pos: 1 }] },
  ]) {
    const value = lane({ ...good, ...patch });
    assert.equal(value.goal_road, null);
    assert.equal(value.goals!.length, 2);
    assert.ok(value.goals!.every((g) => !Object.hasOwn(g, 'pos')));
  }
  // Validation uses every raw record, even a goal dropped for unrelated bad fields.
  const dropped = lane({ ...good, goals: [{ ...goal, done: -1, pos: 0 }, { ...second, pos: 1 }] });
  assert.equal(dropped.goal_road, 'wq');
  assert.equal(dropped.goals![0].pos, 1);
  const invalidDropped = lane({ ...good, goals: [{ ...goal, done: -1 }, { ...second, pos: 1 }] });
  assert.equal(invalidDropped.goal_road, null);
});

test('ten-goal shared fixture preserves complete road with eight selected records', () => {
  const fixture = JSON.parse(readFileSync(new URL('./fixtures/work-lanes-inventory.json', import.meta.url), 'utf8')).goal_road_v1;
  for (const raw of [fixture.inventory_frame.lanes[0], fixture.show_projection]) {
    const value = lane(raw);
    assert.equal(value.goal_road!.length, 10);
    assert.deepEqual(value.goals, raw.goals);
    assert.equal(workLaneGoalRoad(value)!.length, 10);
  }
});
