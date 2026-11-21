import assert from 'node:assert/strict';
import test from 'node:test';
import { readableAge, readableByActivity, readableGoalClass, readableLastActivity, readableMapLanes, readableHours, readableLaneClass, readableLeadGoals, readableSeatCount, readableSentence, readableTrouble } from '../src/services/readableLanes';
import type { WorkLane, WorkLaneGoalProgress, WorkLaneMember, WorkLaneSeat } from '../src/services/workLanes';
import type { LaneClass } from '../src/services/readableLanes';

type ReadableLane = Omit<WorkLane, 'goal_road' | 'goal_classes' | 'goal_counts' | 'default_goal'> & {
  goal_road?: string;
  goal_classes?: string;
  goal_counts?: readonly number[];
  default_goal?: WorkLaneGoalProgress;
};
const goal = { state: 'in_progress' as const, done: 0, total: 2 };
const lane = (extra: Partial<ReadableLane> = {}) => ({ state: 'active', title: 'Lane', goals: [], ...extra } as ReadableLane);
const fixtureMember = (value: Partial<WorkLaneMember> & Pick<WorkLaneMember, 'spec_id'>) => value as WorkLaneMember;
const seat = (activity: WorkLaneSeat['activity']) => ({ stream_id: 's', role: null, host: null, visibility: null, parent_stream_id: null, activity, last_activity: null });

test('goal classes prefer the inventory letter and otherwise derive detail or legacy state', () => {
  assert.equal(readableGoalClass(goal, 'b'), 'blocked');
  assert.equal(readableGoalClass({ ...goal, total: 0, done: 0, moving: 0, blocked: 0, working: false }), 'queued');
  assert.equal(readableGoalClass({ ...goal, done: 2, working: true }), 'done');
  assert.equal(readableGoalClass({ ...goal, moving: 1, blocked: 0, working: true }), 'working');
  assert.equal(readableGoalClass({ ...goal, moving: 1, blocked: 0, working: false }), 'moving');
  assert.equal(readableGoalClass({ ...goal, moving: 0, blocked: 1, working: false }), 'blocked');
  assert.equal(readableGoalClass(goal), 'moving');
  assert.equal(readableGoalClass({ ...goal, moving: 0, blocked: 1, working: false }, undefined, true), 'moving');
});
test('lane classes use uncapped counts and lead goals use class letters and rank', () => {
  const goals = Array.from({ length: 8 }, (_, rank) => ({ ...goal, goal_id: `${rank}`, title: `${rank}`, rank }));
  const item = lane({ goals, goal_counts: [1, 9, 2, 0], goal_classes: 'bbbbbbbm-' });
  assert.equal(readableLaneClass(item), 'moving');
  assert.deepEqual(readableLeadGoals(item).map(x => x.goal.goal_id), ['7']);
  assert.equal(readableLaneClass(lane({ goal_counts: [0, 1, 0, 0], goal_classes: '-' })), 'idle');
  assert.equal(readableLaneClass(lane({ goal_counts: [0, 0, 3, 1], goal_classes: '-' })), 'idle');
  assert.equal(readableLaneClass(lane({ state: 'paused', goal_counts: [1, 0, 0, 0] })), 'paused');
});
test('age and hours floor and round without inventing missing values', () => {
  const now = Date.parse('2026-11-10T12:00:00Z');
  assert.equal(readableAge(null, now), null);
  assert.equal(readableAge('invalid', now), null);
  assert.equal(readableAge('2026-11-10T11:59:30Z', now), 'now');
  assert.equal(readableAge('2026-11-09T12:00:00Z', now), '1d');
  assert.equal(readableHours(.9), '<1h left');
  assert.equal(readableHours(1.5), '~2h left');
  assert.equal(readableHours(null), null);
});
test('sentences and seat counts respect missing data and truncated lists', () => {
  const member = fixtureMember({ spec_id: 'a', status: 'blocked', status_line: 'Working', blocked_line: 'approval', open_questions: { count: 1, first_question_id: null } });
  assert.equal(readableSentence(member), 'Blocked on approval');
  assert.equal(readableSentence({ ...member, blocked_line: null }), null);
  assert.equal(readableSentence({ ...member, status: 'in_progress' }), 'Waiting on your answer.');
  assert.equal(readableSeatCount({ ...member, seats: [seat('working')], seats_total: 2 }), '1+ on it now');
  assert.equal(readableSeatCount({ ...member, seats: [seat('idle')], seats_total: 2 }), null);
});
test('trouble uses the p75 threshold and newest stage transition', () => {
  const member = fixtureMember({ spec_id: 'a', status: 'needs_qa', estimate_first: { p25: 1, median: 2, p75: 3 }, worked: { active_h: 3, elapsed_h: 3, censored: false, seats: 1, qa_rounds: 1, as_of: '' }, transitions: [{ status: 'needs_qa', at: '2026-11-08T12:00:00Z' }] });
  const now = Date.parse('2026-11-10T12:00:00Z');
  assert.deepEqual(readableTrouble(member, now), ['In review 2d']);
  assert.deepEqual(readableTrouble({ ...member, worked: { ...member.worked!, active_h: 3.1, qa_rounds: 2 } }, now), ['1.1h over estimate', '2 review rounds', 'In review 2d']);
  assert.deepEqual(readableTrouble(fixtureMember({ spec_id: 'a' }), now), []);
});

test('missing classes fall back to legacy state and empty goals stay queued', () => {
  assert.equal(readableGoalClass({ ...goal, total: 0, working: true }, 'w'), 'queued');
  assert.equal(readableLaneClass(lane({ goals: [{ ...goal, goal_id: 'g', title: 'G', rank: 1 }] })), 'moving');
  assert.equal(readableLaneClass(lane({ goals: [{ ...goal, goal_id: 'g', title: 'G', rank: 1, state: 'not_started' }] })), 'idle');
});
test('trouble thresholds require all inputs and a full two days', () => {
  const now = Date.parse('2026-11-10T12:00:00Z');
  const member = fixtureMember({ spec_id: 's', status: 'in_progress', transitions: [{ status: 'in_progress', at: '2026-11-08T12:00:01Z' }], worked: { active_h: 9, elapsed_h: null, censored: false, seats: 0, qa_rounds: 1, as_of: '' } });
  assert.deepEqual(readableTrouble(member, now), []);
  assert.equal(readableSeatCount({ ...member, seats: [seat('working'), seat('working')], seats_total: 2 }), '2 on it now');
  assert.equal(readableHours(0), '<1h left');
  assert.equal(readableAge('2026-11-10T11:00:01Z', now), '59m');
});

test('display boundaries floor ages and round hours without inventing missing inputs', () => {
  const now = Date.parse('2026-11-10T12:00:00Z');
  for (const [offset, expected] of [[59_999, 'now'], [60_000, '1m'], [3_600_000, '1h'], [86_400_000, '1d']] as const)
    assert.equal(readableAge(new Date(now - offset).toISOString(), now), expected);
  for (const [hours, expected] of [[0.99, '<1h left'], [1, '~1h left'], [1.49, '~1h left'], [1.5, '~2h left']] as const)
    assert.equal(readableHours(hours), expected);
  assert.equal(readableHours(NaN), null);
});

test('a moving default goal uses the lane title and sorts last', () => {
  const item = lane({ goal_counts: [3, 0, 0, 0], goal_classes: 'mmw',
    goals: [{ ...goal, goal_id: 'later', title: 'Later', rank: 5 }, { ...goal, goal_id: 'earlier', title: 'Earlier', rank: 1 }],
    default_goal: { ...goal } });
  assert.deepEqual(readableLeadGoals(item).map(x => x.goal.title), ['Lane', 'Earlier']);
});

test('last activity is the card age stamp: state_at when paused, otherwise progress_at', () => {
  const l = lane({ state_at: '2026-11-14T10:00:00Z', progress_at: '2026-11-14T12:00:00Z' });
  assert.equal(readableLastActivity(l, 'paused'), '2026-11-14T10:00:00Z');
  for (const cls of ['idle', 'working', 'moving'] as const) assert.equal(readableLastActivity(l, cls), '2026-11-14T12:00:00Z');
});
test('activity order: most recent first, missing or bad stamps last, ties keep list order', () => {
  const rows = [['a', '2026-11-14T10:00:00Z'], ['b', null], ['c', '2026-11-14T12:00:00Z'], ['d', 'not a date'], ['e', '2026-11-14T10:00:00Z'], ['f', undefined]] as const;
  assert.deepEqual(readableByActivity([...rows], r => r[1]).map(r => r[0]), ['c', 'a', 'e', 'b', 'd', 'f']);
  assert.deepEqual(readableByActivity([], () => null), []);
});
test('map lanes: up to six busy lanes inside in list order, quiet lanes by last activity, parked left out', () => {
  const at = (h: number | null) => h === null ? null : new Date(Date.UTC(2026, 10, 14, 12) - h * 3600000).toISOString();
  const entry = (id: string, cls: LaneClass, h: number | null, question = false) => ({ id, cls, question, lane: lane({ lane_id: id, progress_at: at(h), state_at: at(h === null ? null : h + 100) }) });
  const lanes = [entry('m1', 'moving', 9), entry('p1', 'paused', 1), entry('w1', 'working', 5), entry('i1', 'idle', 50), entry('x', 'parked', 0), entry('p2', 'paused', null), entry('i2', 'idle', 2)];
  const map = readableMapLanes(lanes);
  assert.deepEqual(map.inner.map(l => l.id), ['m1', 'w1']);
  assert.deepEqual(map.extra, []);
  // p1 is stamped by state_at (101 h ago), so the idle lanes stamped by progress_at come first.
  assert.deepEqual(map.quiet.map(l => l.id), ['i2', 'i1', 'p1', 'p2']);
});
test('map lanes: more than six busy lanes choose questions, then working, then moving, each by last activity', () => {
  const at = (h: number) => new Date(Date.UTC(2026, 10, 14, 12) - h * 3600000).toISOString();
  const entry = (id: string, cls: LaneClass, h: number, question = false) => ({ id, cls, question, lane: lane({ lane_id: id, progress_at: at(h) }) });
  const lanes = [entry('m-old', 'moving', 30), entry('m-new', 'moving', 1), entry('w-old', 'working', 20), entry('q-moving', 'moving', 40, true),
    entry('w-new', 'working', 2), entry('m-mid', 'moving', 10), entry('q-working', 'working', 3, true), entry('m-tie-a', 'moving', 15), entry('m-tie-b', 'moving', 15)];
  const map = readableMapLanes(lanes);
  assert.deepEqual(map.inner.map(l => l.id), ['q-working', 'q-moving', 'w-new', 'w-old', 'm-new', 'm-mid']);
  assert.deepEqual(map.extra.map(l => l.id), ['m-tie-a', 'm-tie-b', 'm-old']);
  assert.deepEqual(map.quiet, []);
});

import * as readable from '../src/services/readableLanes';
const external = (state: 'working' | 'queued' | 'review', label = 'Dot') => ({ label, ref: `packet-${state}`, state, verified_at: '2026-11-10T10:00:00Z' });

test('external lane activity changes only working and preserves parked/paused precedence', () => {
  assert.equal(readableLaneClass(lane({ activity: 'working' })), 'working');
  assert.equal(readableLaneClass(lane({ activity: 'working', state: 'paused' })), 'paused');
  assert.equal(readableLaneClass(lane({ activity: 'working', parked: { at: null, reason: null } })), 'parked');
  for (const activity of ['waiting_on_dot', 'waiting_on_you', 'blocked', 'idle', null, undefined] as const) {
    for (const goal_classes of ['q-', 'm-', 'w-']) {
      assert.equal(readableLaneClass(lane({ activity, goal_classes })), readableLaneClass(lane({ goal_classes })));
    }
  }
});

test('lane words use the external label, keep paused words and suppress parked words', () => {
  for (const activity of ['working', 'waiting_on_dot', 'waiting_on_you', 'blocked', 'idle', null, undefined] as const) {
    for (const label of [undefined, 'Scout']) {
      const item = lane({ activity, ...(label ? { external: { label, working: 0, queued: 1, review: 0, verified_at: '2026-11-10T10:00:00Z' } } : {}) });
      const expected = activity === 'waiting_on_dot' ? `waiting on ${label ?? 'Dot'}` : activity === 'blocked' ? 'blocked' : null;
      assert.equal(readable.readableLaneWord(item), expected);
      assert.equal(readable.readableLaneWord({ ...item, state: 'paused' }), expected);
      assert.equal(readable.readableLaneWord({ ...item, parked: { at: null, reason: null } }), null);
    }
  }
});

test('external item work respects completed and open-question precedence and only working work counts as an agent', () => {
  const member = fixtureMember({ spec_id: 'a', status: 'backlog', seats: [], external: [external('working')] });
  assert.equal(readable.readableItemKind(member), 'working');
  assert.equal(readable.readableItemKind({ ...member, status: 'completed' }), 'done');
  assert.equal(readable.readableItemKind({ ...member, open_questions: { count: 1, first_question_id: 'q' } }), 'needs');
  for (const state of ['queued', 'review'] as const) {
    assert.equal(readable.readableItemKind({ ...member, external: [external(state)] }), 'later');
    assert.equal(readableSeatCount({ ...member, external: [external(state)] }), null);
  }
});

test('a working external entry is one more agent on it now, exactly like a working seat', () => {
  const seat = (activity: 'working' | 'idle') => ({ stream_id: `s-${activity}`, role: null, host: null, visibility: null, parent_stream_id: null, activity, last_activity: null });
  const member = fixtureMember({ spec_id: 'a', status: 'in_progress', seats: [], external: [external('working')] });
  assert.equal(readableSeatCount(member), '1 on it now');
  assert.equal(readableSeatCount({ ...member, seats: [seat('working')] }), '2 on it now');
  assert.equal(readableSeatCount({ ...member, seats: [seat('idle')] }), '1 on it now');
  assert.equal(readableSeatCount({ ...member, seats: [seat('working')], seats_total: 3 }), '2+ on it now');
  assert.equal(readableSeatCount({ ...member, external: [external('working'), external('working')] }), '2 on it now');
  assert.equal(readableSeatCount({ ...member, external: [] }), null);
  assert.equal(readableSeatCount(fixtureMember({ spec_id: 'a' })), null);
  assert.equal('readableExternalLine' in readable, false);
});

test('queued-only sentence appears without moving backlog, while blocked/question/status prose wins otherwise', () => {
  const member = fixtureMember({ spec_id: 'a', status: 'backlog', status_line: 'Lead status.', external: [external('queued')] });
  assert.equal(readableSentence(member), 'Waiting for Dot');
  assert.equal(readable.readableItemKind(member), 'later');
  assert.equal(readableSentence({ ...member, status: 'blocked', blocked_line: 'approval' }), 'Blocked on approval');
  assert.equal(readableSentence({ ...member, status: 'blocked' }), null);
  assert.equal(readableSentence({ ...member, open_questions: { count: 1, first_question_id: null } }), 'Waiting on your answer.');
  for (const state of ['working', 'review'] as const) assert.equal(readableSentence({ ...member, external: [...member.external!, external(state)] }), 'Lead status.');
  assert.equal(readableSentence({ ...member, external: [] }), 'Lead status.');
});
