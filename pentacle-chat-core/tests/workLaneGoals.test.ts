import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { normalizeWorkLanesInventory, LANE_HUE_KEYS } from '../src/services/workLanes.ts';

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
