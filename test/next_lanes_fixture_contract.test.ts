import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { delimiter, resolve } from 'node:path';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import {
  LANE_HUE_KEYS,
  applyWorkLanesFrame,
  emptyWorkLanesInventory,
} from 'pentacle-chat-core';
import * as F from './e2e/lib/next_lanes_fixtures.js';

const PROJECTOR_ORACLE = `
import json, sys, types
try:
    import yaml
except ModuleNotFoundError:
    # The projection path does not parse YAML. Keep the oracle runnable in the
    # source-only test environment where the optional parser dependency is absent.
    yaml = types.ModuleType("yaml")
    yaml.SafeLoader = type("SafeLoader", (), {})
    yaml.BaseLoader = type("BaseLoader", (), {})
    yaml.AliasEvent = type("AliasEvent", (), {})
    yaml.load = lambda *args, **kwargs: None
    sys.modules["yaml"] = yaml
from work_lanes_projection import page_lane_detail, project_lane
value = json.load(sys.stdin)
projected = project_lane(value["lane"], None, value["now_iso"], index_available=True,
                         all_members=True, sessions_by_stream=value["sessions_by_stream"],
                         questions_by_spec=value["questions_by_spec"], worked_by_spec=value["worked_by_spec"])
page = page_lane_detail(value["shown"], projected, sessions_by_stream=value["sessions_by_stream"],
                        questions_by_spec=value["questions_by_spec"])
goal_page = page_lane_detail(value["shown"], projected, goal_filter=value["goal_filter"],
                             sessions_by_stream=value["sessions_by_stream"],
                             questions_by_spec=value["questions_by_spec"])
print(json.dumps({"projection": projected, "page": page, "goal_page": goal_page}, separators=(",", ":")))
`;

function runDaemonProjector(input: unknown): { projection: any; page: any; goal_page: any } {
  const paths = [resolve(process.cwd(), 'services/chat-stream-v2'), resolve(process.cwd(), 'services'), process.env.PYTHONPATH]
    .filter((value): value is string => Boolean(value));
  const result = spawnSync(process.env.PYTHON || 'python3', ['-c', PROJECTOR_ORACLE], {
    input: JSON.stringify(input), encoding: 'utf8', timeout: 30_000, maxBuffer: 8 * 1024 * 1024,
    env: { ...process.env, PYTHONPATH: paths.join(delimiter) },
  });
  assert.equal(result.status, 0, result.stderr || result.error?.message || 'daemon projector oracle exited without status');
  return JSON.parse(result.stdout) as { projection: any; page: any; goal_page: any };
}

function daemonInputs(lane: any, nowIso: string, goalFilter: string) {
  const sessionsByStream: Record<string, any> = {};
  const questionsBySpec: Record<string, any> = {};
  for (const member of lane.members) {
    if (member.open_questions?.count) questionsBySpec[member.spec_id] = member.open_questions;
    for (const seat of member.seats || []) {
      sessionsByStream[seat.stream_id] = {
        ...seat,
        status: 'open',
        online: true,
        working: seat.activity === 'working',
        assigned_spec_id: member.spec_id,
        status_card: null,
      };
    }
  }
  const goals = Object.fromEntries(lane.goals.map((goal: any) => {
    const source = lane.goalSources[goal.goal_id];
    return [source.epicId, {
      facts: { ...goal, epic_id: source.epicId, _detail: source.detail }, quality: 'fresh', source_hash: source.revision,
    }];
  }));
  const projectorLane = {
    ...lane.storedLane,
    _members: lane.members,
    _goals: goals,
    _qualifies: true,
    _work_index_available: true,
    _detail_revision: lane.detailRevision,
  };
  return {
    lane: projectorLane,
    now_iso: nowIso,
    sessions_by_stream: sessionsByStream,
    questions_by_spec: questionsBySpec,
    worked_by_spec: lane.workedBySpec,
    goal_filter: goalFilter,
    shown: {
      lane: lane.storedLane,
      events: lane.events,
      updates: lane.updates,
      _projection_row: projectorLane,
      _work_index: lane.workIndex,
      _trail_events: lane.trailEvents || [],
      _unblocks: {},
    },
  };
}

test('pageLaneDetail source runs in a bare bridge VM without fixture-module closures', () => {
  const lane = F.makeL11({ previewQuestionIds: ['next_lanes__vm-preview'], includeFinishedEarlier: true });
  const options = { membersLimit: 5, nowIso: '2026-11-20T12:00:00Z', goalId: lane.goals[0].goal_id };
  const serialized = runInNewContext(`(${F.pageLaneDetail.toString()})`);
  assert.equal(typeof serialized, 'function');
  const local = F.pageLaneDetail(lane, options);
  const inVm = serialized(lane, options);
  assert.deepEqual(JSON.parse(JSON.stringify(inVm)), JSON.parse(JSON.stringify(local)));
});

test('flat next-lanes preview survives the production inventory normalizer', () => {
  const questionId = 'next_lanes__contract-preview';
  const offPreviewQuestionId = 'next_lanes__contract-off-preview';
  const lane = F.makeL11({ previewQuestionIds: [questionId], offPreviewQuestionIds: [offPreviewQuestionId] });
  const raw = F.inventoryFrame([lane]);
  const rawLane = raw.lanes[0];

  const adoptionKey = 'spec:next_lanes_fixture_l11';
  const expectedLaneId = `wl-${createHash('sha256').update(`local:web-gate-assistant\0${adoptionKey}`).digest('hex').slice(0, 24)}`;
  assert.equal(rawLane.lane_id, expectedLaneId);
  assert.equal(lane.storedLane.adoption_key, adoptionKey);
  assert.ok(LANE_HUE_KEYS.includes(rawLane.hue));
  assert.equal('progress' in rawLane, false, 'daemon wire keeps progress fields flat');
  assert.ok(Array.isArray(rawLane.members));
  assert.equal(rawLane.members_total, 40);
  assert.equal(rawLane.members.length, F.INLINE_MEMBERS);
  assert.ok(rawLane.members.some(member => member.spec_id === lane.questionSpecIds.preview));
  assert.equal(rawLane.goal_road, 'bbwmqwwbwbw');
  assert.equal(rawLane.activity, 'working');
  assert.deepEqual(rawLane.members.map(member => member.seats_total), [0, 0, 1, 0, 0, 1, 1, 0]);
  assert.deepEqual(rawLane.members.map(member => member.seats.length), rawLane.members.map(member => member.seats_total));
  const rawPreview = rawLane.members.find(member => member.spec_id === lane.questionSpecIds.preview);
  assert.equal(rawPreview?.goal_id,
    lane.members.find(member => member.spec_id === lane.questionSpecIds.preview)?.goal_id);
  assert.deepEqual(rawPreview?.open_questions, { count: 1, first_question_id: questionId });

  const normalized = applyWorkLanesFrame(emptyWorkLanesInventory(), raw);
  const normalizedLane = normalized.lanes.find(row => row.lane_id === lane.inventory.lane_id);
  assert.ok(normalizedLane);
  assert.equal(normalizedLane.progress?.members_total, 40);
  assert.equal(normalizedLane.progress?.members.length, F.INLINE_MEMBERS);
  const normalizedPreview = normalizedLane.progress?.members.find(member => member.spec_id === lane.questionSpecIds.preview);
  assert.equal(normalizedPreview?.goal_id, lane.members.find(member => member.spec_id === lane.questionSpecIds.preview)?.goal_id);
  assert.deepEqual(normalizedPreview?.open_questions, { count: 1, first_question_id: questionId });

  const replacement = F.makeL11({ offPreviewQuestionIds: [offPreviewQuestionId] });
  const replacementRaw = F.inventoryFrame([replacement]).lanes[0];
  assert.equal(replacementRaw.members.length, F.INLINE_MEMBERS);
  assert.equal(replacementRaw.members.at(-1).spec_id, replacement.questionSpecIds.offPreview);
  assert.deepEqual(replacementRaw.members.at(-1).open_questions, { count: 1, first_question_id: offPreviewQuestionId });
  const replacementNormalized = applyWorkLanesFrame(emptyWorkLanesInventory(), { type: 'work_lanes.inventory', lanes: [replacementRaw] });
  assert.equal(replacementNormalized.lanes[0].progress?.members.at(-1)?.spec_id, replacement.questionSpecIds.offPreview);
  assert.equal(replacementNormalized.lanes[0].progress?.members.at(-1)?.open_questions.first_question_id, offPreviewQuestionId);
});

test('page detail fixtures preserve production paging, positions, and goal projection shape', () => {
  const lane = F.makeL11({ includeFinishedEarlier: true });
  lane.events = [{ event_id: 42, operation: 'adopt', actor_stream_id: 'fixture:operator' }];
  lane.updates = [{ update_id: 'fixture-update-42', kind: 'lane_started', summary: 'Synthetic start.' }];
  const first = F.pageLaneDetail(lane, { membersLimit: F.PAGE_SIZE });
  assert.equal(first.type, 'work_lanes.show.ok');
  assert.equal(first.members.length, F.PAGE_SIZE);
  assert.equal(first.members_total, 41);
  assert.equal(first.members_next, first.members.at(-1)?.spec_id);
  assert.equal(first.projection.members_total, first.members_total);
  assert.equal(first.projection.members_next, first.members_next);
  assert.equal(first.projection.goals_total, 11);
  assert.equal(first.lane.members.length, F.PAGE_SIZE);
  assert.deepEqual(first.lane.members, first.members.map(member => member.spec_id));
  assert.equal('goals' in first.lane, false, 'stored lane response does not duplicate projected goals');
  assert.deepEqual(first.events, lane.events);
  assert.deepEqual(first.updates, lane.updates);
  assert.equal(first.work_index.available, true);
  assert.equal(first.detail_revision, F.REVISION_A);
  assert.ok(first.members[0].criteria?.length);
  assert.ok(first.members[0].transitions);
  assert.ok(first.projection.members[0].criteria?.length, 'legacy projection retains criteria');
  assert.equal('blocked_line' in first.projection.members[0], false);
  assert.equal(first.explicit_member_positions[first.members[0].spec_id], lane.storedLane.members.indexOf(first.members[0].spec_id));

  const second = F.pageLaneDetail(lane, { membersLimit: F.PAGE_SIZE, membersAfter: first.members_next });
  assert.equal(second.members.length, 9);
  assert.equal(second.members_next, null);
  assert.deepEqual([...first.members, ...second.members].map(member => member.spec_id),
    [...lane.members].sort((a, b) => a.spec_id.localeCompare(b.spec_id)).map(member => member.spec_id));

  const selectedGoalId = lane.goals[7].goal_id;
  const goalReply = F.pageLaneDetail(lane, { membersLimit: 5, goalId: selectedGoalId });
  assert.ok(goalReply.members.length > 0);
  assert.ok(goalReply.members.every(member => member.goal_id === selectedGoalId));
  assert.equal(goalReply.projection.selected_goal.goal_id, selectedGoalId);
  assert.equal(goalReply.projection.selected_goal.source.epic_id, lane.goalSources[selectedGoalId].epicId);
  assert.equal(goalReply.projection.selected_goal.source.revision, lane.goalSources[selectedGoalId].revision);
  assert.equal(goalReply.projection.members_total, goalReply.members_total);
  assert.equal(goalReply.projection.members_next, goalReply.members_next);
  assert.equal(goalReply.members_total, lane.members.filter(member => member.goal_id === selectedGoalId).length);
  assert.equal(typeof goalReply.projection.selected_goal.summary_text, 'string');
  assert.equal(typeof goalReply.projection.selected_goal.summary_line, 'string');
  assert.equal(typeof goalReply.projection.selected_goal.goal_text, 'string');
  assert.ok(goalReply.projection.selected_goal.visibility_counts.complete.source);
  assert.ok(Array.isArray(goalReply.projection.selected_goal.recent));
  assert.equal(first.projection.default_goal.left_h, null, 'completed-only default goal has no remaining estimate population');
});

test('flat fixture detail page differentially matches the daemon projector', () => {
  const template = F.makeL11();
  const completedGoalId = template.goals[0].goal_id;
  const completedSpecIds = template.members.filter(member => member.goal_id === completedGoalId).map(member => member.spec_id);
  const lane = F.makeL11({ includeFinishedEarlier: true, completedSpecIds });
  const member = lane.members.find(row => row.status === 'blocked');
  assert.ok(member, 'fixture has an unfinished blocked member for computed detail evidence');
  member.status_text = 'The synthetic dependency remains blocked.';
  member.next_action_text = 'Check the synthetic dependency again.';
  member.blockers_text = 'Waiting for the synthetic prerequisite.';
  member.progress_at = '2026-10-01T00:00:00Z';
  member._progress_at = '2026-11-01T00:00:00Z';
  member.latest_event = { kind: 'stale_fixture_event', at: '2026-11-02T00:00:00Z' };
  member._latest_event = { kind: 'reported_done', at: '2026-11-05T00:00:00Z' };
  member.worked = { elapsed_h: 999, censored: true, active_h: 999, seats: 0, qa_rounds: 0, as_of: '2026-11-01T00:00:00Z' };
  lane.workedBySpec[member.spec_id] = {
    elapsed_h: 4.5, censored: false, active_h: 3.5, seats: 1, qa_rounds: 0, as_of: '2026-11-20T12:00:00Z',
  };

  const nowIso = '2026-11-20T12:00:00Z';
  const actual = runDaemonProjector(daemonInputs(lane, nowIso, member.goal_id));
  const fixture = F.pageLaneDetail(lane, { nowIso });
  const detailKeys = ['worked', 'lead_seat', 'status_line', 'next_line', 'blocked_line', 'progress_at',
    'latest_event', 'status_behind', 'criteria', 'transitions', 'seats', 'seats_total', 'open_questions', 'recent'];
  assert.deepEqual(fixture.members.map(row => row.spec_id), actual.page.members.map(row => row.spec_id));
  for (const expected of actual.page.members) {
    const projected = fixture.members.find(row => row.spec_id === expected.spec_id);
    assert.ok(projected);
    assert.deepEqual(Object.fromEntries(detailKeys.map(key => [key, projected[key]])),
      Object.fromEntries(detailKeys.map(key => [key, expected[key]])), `member projector parity: ${expected.spec_id}`);
  }
  assert.deepEqual(fixture.projection.goals, actual.projection.goals);
  assert.deepEqual(fixture.projection.default_goal, actual.projection.default_goal);
  assert.equal(fixture.projection.default_goal.left_h, null);
  assert.equal(fixture.projection.goals.find(goal => goal.goal_id === completedGoalId).left_h, null);
  assert.equal(fixture.projection.goal_road, actual.projection.goal_road);
  assert.equal(fixture.projection.activity, actual.projection.activity);
  assert.deepEqual(fixture.projection.recent, actual.page.projection.recent);
  assert.equal(fixture.members_total, actual.page.members_total);
  assert.equal(fixture.members_next, actual.page.members_next);
  const selectedGoalFixture = F.pageLaneDetail(lane, { nowIso, goalId: member.goal_id });
  assert.deepEqual(selectedGoalFixture.members.map(row => row.spec_id), actual.goal_page.members.map(row => row.spec_id));
  assert.deepEqual(selectedGoalFixture.projection.selected_goal, actual.goal_page.projection.selected_goal,
    'selected goal visibility, source prose, and trail match the daemon page projector');
  assert.deepEqual(selectedGoalFixture.projection.selected_goal.recent, actual.goal_page.projection.selected_goal.recent);
  const actualMemberRecent = actual.page.members.find(row => row.spec_id === member.spec_id)?.recent;
  assert.deepEqual(fixture.members.find(row => row.spec_id === member.spec_id)?.recent, actualMemberRecent);
  const computed = fixture.members.find(row => row.spec_id === member.spec_id);
  assert.deepEqual(computed.worked, lane.workedBySpec[member.spec_id]);
  assert.equal(computed.progress_at, member._progress_at);
  assert.deepEqual(computed.latest_event, member._latest_event);
  assert.equal(computed.status_behind, true);
});

test('deprecated-only unplaced members omit the daemon default goal', () => {
  const deprecated = F.makeMember('spec_gate__next_lanes_deprecated_loose', null, {
    status: 'deprecated', terminal: 'deprecated', seats: [],
  });
  const lane = F.makeOrdinaryLane({ laneId: F.fixtureLaneId('deprecated-loose'), noGoal: true, members: [deprecated] });
  const raw = F.inventoryFrame([lane]).lanes[0];
  const detail = F.pageLaneDetail(lane);
  assert.equal('default_goal' in raw, false);
  assert.equal('default_goal' in detail.projection, false);
});

test('65-lane fixture counts and truncation are derived before the production cap', () => {
  const rows = Array.from({ length: 65 }, (_, index) => F.makeOrdinaryLane({
    laneId: F.fixtureLaneId(`contract-cap-${String(index).padStart(2, '0')}`),
    title: `Fixture cap lane ${String(index).padStart(2, '0')}`,
    state: index === 0 ? 'blocked' : index === 64 ? 'paused' : 'active',
  }));
  rows.forEach((lane, index) => {
    lane.inventory.updated_at = new Date(Date.parse('2026-11-20T12:00:00Z') + index * 1000).toISOString().replace('.000Z', 'Z');
  });
  const raw = F.inventoryFrame(rows, { open: 65, truncated: true });
  assert.equal(raw.counts.open, 65);
  assert.equal(raw.counts.blocked, 1);
  assert.equal(raw.counts.active, 63);
  assert.equal(raw.counts.paused, 1);
  assert.equal(raw.truncated, true);
  assert.equal(raw.lanes.length, 64);
  assert.equal(raw.lanes[0].state, 'blocked');
  assert.equal(raw.lanes.some(row => row.state === 'paused'), false);
  const expected = [...rows].sort((a, b) => (a.inventory.state === 'blocked' ? 0 : a.inventory.state === 'active' ? 1 : 2)
    - (b.inventory.state === 'blocked' ? 0 : b.inventory.state === 'active' ? 1 : 2)
    || String(b.inventory.updated_at).localeCompare(String(a.inventory.updated_at))
    || a.inventory.lane_id.localeCompare(b.inventory.lane_id)).slice(0, 64).map(lane => lane.inventory.lane_id);
  assert.deepEqual(raw.lanes.map(row => row.lane_id), expected);
  assert.equal(raw.lanes[1].lane_id, rows[63].inventory.lane_id, 'newest active updated_at sorts before older active rows');
  assert.ok(raw.lanes[1].updated_at > raw.lanes[2].updated_at);

  const normalized = applyWorkLanesFrame(emptyWorkLanesInventory(), raw);
  assert.equal(normalized.counts.open, 65);
  assert.equal(normalized.counts.active, 63);
  assert.equal(normalized.counts.paused, 1);
  assert.equal(normalized.counts.blocked, 1);
  assert.equal(normalized.truncated, true);
  assert.equal(normalized.lanes.length, 64);
});

test('L65 omits inline road positions and keeps its preview question tied to the sent goal', () => {
  const questionId = 'next_lanes__contract-l65';
  const lane = F.makeL65({ questionId });
  const raw = F.inventoryFrame([lane]);
  const rawLane = raw.lanes[0];
  assert.equal(rawLane.goals_total, 65);
  assert.equal('goal_road' in rawLane, false);
  assert.ok(rawLane.goals.every(goal => !('pos' in goal)));
  assert.equal(rawLane.members[0].status, 'blocked');
  assert.equal(rawLane.members[1].status, 'blocked');
  const questionMember = rawLane.members.find(member => member.spec_id === lane.questionSpecId);
  assert.ok(questionMember);
  assert.equal(questionMember.open_questions.first_question_id, questionId);
  assert.ok(rawLane.goals.some(goal => goal.goal_id === questionMember.goal_id));
  const zuluIndex = lane.goals.findIndex(goal => goal.title === 'Zulu fixture tie');
  const alphaIndex = lane.goals.findIndex(goal => goal.title === 'Alpha fixture tie');
  assert.ok(zuluIndex >= 0 && alphaIndex > zuluIndex, 'equal-rank full goal set sorts ties by goal id');
  assert.ok(lane.goals[zuluIndex].goal_id < lane.goals[alphaIndex].goal_id);

  const normalized = applyWorkLanesFrame(emptyWorkLanesInventory(), raw).lanes[0];
  assert.equal(normalized.goals_total, 65);
  assert.equal(normalized.goal_road, null);
  assert.equal(normalized.progress?.members.find(member => member.spec_id === lane.questionSpecId)?.open_questions?.first_question_id,
    questionId);
});
