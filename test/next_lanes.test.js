'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const { buildLanesViewModel, mountLanesCard } = require('../renderer/next/lanes');
const { createQuestions } = require('../renderer/next/questions');
const { applyWorkLanesFrame, emptyWorkLanesInventory } = require('../pentacle-chat-core/src/services/workLanes.ts');
const liveLaneFixture = require('./fixtures/next_lanes_live_redacted.json');

function goal(goalId, title, rank, extra = {}) {
  return { goal_id: goalId, title, rank, state: 'in_progress', done: 0, total: 2, moving: 1, blocked: 0, working: false, ...extra };
}

function member(specId, goalId = 'goal-a', extra = {}) {
  return { spec_id: specId, goal_id: goalId, title: `Synthetic spec ${specId}`, status: 'ready_for_dev', ...extra };
}

function lane(extra = {}) {
  const goals = [goal('goal-a', 'Synthetic goal', 1)];
  const suppliedProgress = extra.progress || {};
  const previewMembers = Array.isArray(suppliedProgress.members) ? suppliedProgress.members
    : Array.isArray(extra.members) ? extra.members : [];
  const previewTotal = Object.prototype.hasOwnProperty.call(suppliedProgress, 'members_total') ? suppliedProgress.members_total
    : Object.prototype.hasOwnProperty.call(extra, 'members_total') ? extra.members_total : previewMembers.length;
  return {
    lane_id: 'lane-a', title: 'Synthetic lane', icon: 'flask', detail_revision: 'rev-a',
    goals, goals_total: goals.length, goal_road: 'm', goal_classes: 'm', goal_counts: [1, 0, 0, 0],
    members: [], members_total: 0,
    ...extra,
    progress: { ...suppliedProgress, members: previewMembers, members_total: previewTotal },
  };
}

function inventory(lanes) {
  return { lanes, truncated: false, counts: { open: lanes.length } };
}

function heldReply(laneId, goals, members, extra = {}) {
  return {
    type: 'work_lanes.show.ok',
    lane: { lane_id: laneId },
    projection: { lane_id: laneId, goals, members, members_total: members.length, ...extra.projection },
    members,
    members_total: members.length,
    detail_revision: 'rev-a',
  };
}

function readController(reads = {}) {
  const listeners = new Set();
  return {
    ...reads,
    listeners,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    publish() { for (const listener of [...listeners]) listener(); },
  };
}

function notification() {
  return {
    notification_id: 'notice-a', producer: 'agent_question.v1', state: 'open', title: 'Synthetic question',
    question: {
      question_id: 'question-a', state: 'open', producer_stream_id: 'stream-chat', spec_id: 'spec-a',
      response_mode: 'single_choice', options: [{ label: 'Continue', value: 'yes' }],
    },
  };
}

function makeDom() {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  return { dom, root: dom.window.document.getElementById('root'), document: dom.window.document };
}

function roadGoal(laneId, index, letter, overrides = {}) {
  const common = { goal_id: `${laneId}-goal-${index}`, title: `Synthetic goal ${index}`, rank: index + 1, pos: index,
    state: 'in_progress', done: 0, total: 2, moving: 1, blocked: 0, working: false };
  const byClass = letter === 'b' ? { state: 'in_progress', total: 4, blocked: 2, moving: 0, working: false }
    : letter === 'w' ? { state: 'in_progress', moving: 0, blocked: 0, working: true }
      : letter === 'm' ? { state: 'in_progress', moving: 1, blocked: 0, working: false }
        : letter === 'q' ? { state: 'not_started', total: 0, done: 0, moving: 0, blocked: 0, working: false }
          : { state: 'done', total: 2, done: 2, moving: 0, blocked: 0, working: false };
  return { ...common, ...byClass, ...overrides };
}

function goalCountSummary(classes) {
  const counts = { moving: 0, blocked: 0, queued: 0, done: 0 };
  for (const letter of classes) {
    if (letter === 'w' || letter === 'm') counts.moving += 1;
    else if (letter === 'b') counts.blocked += 1;
    else if (letter === 'q') counts.queued += 1;
    else if (letter === 'd') counts.done += 1;
  }
  return counts;
}

function questionSnapshot(laneId, placed) {
  const byLane = new Map([[laneId, placed]]);
  const byGoal = new Map([[laneId, new Map()]]);
  const bySpec = new Map();
  for (const item of placed) {
    const placement = item.placement;
    const list = byGoal.get(laneId).get(placement.goalId) || [];
    list.push(item);
    byGoal.get(laneId).set(placement.goalId, list);
    const specList = bySpec.get(placement.specId) || [];
    specList.push(item);
    bySpec.set(placement.specId, specList);
  }
  return { byLane, byGoal, bySpec, youSaidBySpec: new Map() };
}

function placedQuestion(laneId, goalId, specId, questionId) {
  return { id: questionId, placement: { kind: 'lane_goal', laneId, goalId, specId } };
}

function normalizedPreviewFrame({ membersTotal = 1, members } = {}) {
  const goalId = 'g_0123456789abcdef01234567';
  const preview = members || [{ spec_id: 'synthetic-preview-spec', goal_id: goalId, title: 'Synthetic preview spec',
    status: 'ready_for_dev', open_questions: { count: 1, first_question_id: 'synthetic-preview-question' } }];
  const rawLane = {
    lane_id: 'wl-synthetic-preview', title: 'Synthetic preview lane', state: 'active',
    detail_revision: 'a'.repeat(64), goals: [{ goal_id: goalId, title: 'Synthetic preview goal', rank: 1,
      state: 'in_progress', done: 0, total: 1, moving: 1, blocked: 0, working: false, pos: 0 }],
    goals_total: 1, goal_road: 'm', goal_classes: 'm-', goal_counts: [1, 0, 0, 0], members_total: membersTotal,
    members: preview,
  };
  return applyWorkLanesFrame(emptyWorkLanesInventory(), {
    type: 'work_lanes.inventory', lanes: [rawLane], counts: { open: 1, active: 1, paused: 0, blocked: 0 },
  });
}

test('normalized preview membership drives question marks and spec fallback while lane open still reads detail', () => {
  const normalized = normalizedPreviewFrame();
  const normalizedLane = normalized.lanes[0];
  assert.equal(normalizedLane.members, undefined, 'the core normalizer nests preview membership under progress');
  assert.equal(normalizedLane.progress.members_total, 1);
  assert.equal(normalizedLane.progress.members.length, 1);

  const root = buildLanesViewModel({ inventory: normalized });
  assert.equal(root.active[0].question, true);

  const laneModel = buildLanesViewModel({ inventory: normalized, route: { level: 'lane', laneId: normalizedLane.lane_id } });
  assert.equal(laneModel.stops[0].question, true);
  assert.equal(laneModel.detailHeld, false);

  const specModel = buildLanesViewModel({ inventory: normalized,
    route: { level: 'spec', laneId: normalizedLane.lane_id, goalId: normalizedLane.goals[0].goal_id, specId: 'synthetic-preview-spec' } });
  assert.equal(specModel.member.title, 'Synthetic preview spec');

  let memberRequests = 0;
  const reads = readController({ requestMembers() { memberRequests += 1; } });
  const { dom, root: mountRoot } = makeDom();
  const control = mountLanesCard(mountRoot, { getState: () => ({ inventory: normalized }), reads });
  assert.equal(memberRequests, 0, 'root question evidence needs no full membership read');
  control.openLane(normalizedLane.lane_id);
  assert.equal(memberRequests, 1, 'opening a lane always reads members for complete goals and detail-only facts');
  control.destroy();
  dom.window.close();

  const partial = normalizedPreviewFrame({ membersTotal: 2 });
  let partialRequests = 0;
  const partialReads = readController({ requestMembers() { partialRequests += 1; } });
  const partialDom = makeDom();
  const partialControl = mountLanesCard(partialDom.root, { getState: () => ({ inventory: partial }), reads: partialReads });
  partialControl.openLane(partial.lanes[0].lane_id);
  assert.equal(partialRequests, 1, 'a capped preview still requests the full member detail');
  partialControl.destroy();
  partialDom.dom.window.close();

  const countBelowPreview = normalizedPreviewFrame({ membersTotal: 0, members: [{ spec_id: 'synthetic-finished-preview',
    goal_id: null, title: 'Synthetic finished preview', status: 'completed', terminal: 'completed' }] });
  const finishedPreview = buildLanesViewModel({ inventory: countBelowPreview,
    route: { level: 'lane', laneId: countBelowPreview.lanes[0].lane_id } });
  assert.equal(finishedPreview.finishedEarlierCount, 1, 'a total below preview length is treated as a complete member set');
});

test('current-revision cached member detail remains visible during loading and failed refresh states', () => {
  const normalized = normalizedPreviewFrame({ membersTotal: 2 });
  const item = normalized.lanes[0];
  const completeMembers = [...item.progress.members,
    member('synthetic-preview-spec-2', item.goals[0].goal_id, { status: 'in_progress' })];
  const reply = { ...heldReply(item.lane_id, item.goals, completeMembers), detail_revision: item.detail_revision };

  for (const status of ['loading', 'failed']) {
    const reads = readController({ getRead: (_laneId, kind) => kind === 'members'
      ? { status, reply, detailRevision: item.detail_revision, error: status === 'failed' ? 'Synthetic refresh failure' : null }
      : { status: 'idle', reply: null } });
    const model = buildLanesViewModel({ inventory: normalized, reads, route: { level: 'lane', laneId: item.lane_id } });
    assert.equal(model.detailHeld, true, `${status} keeps current-revision detail available`);
    assert.equal(model.stops[0].members.length, 2);
    assert.equal(model.readStatus, status);
    assert.equal(model.retry, status === 'failed');
  }
});

test('held road goals outside the preview use complete classes and hidden no-road goals use their reply classes', () => {
  const roadGoals = [
    goal('road-a', 'Road A', 1, { pos: 0, moving: 0, blocked: 2, working: false }),
    goal('road-b', 'Road B', 2, { pos: 1, moving: 1, blocked: 0, working: false }),
    goal('road-hidden', 'Road hidden', 3, { pos: 2, moving: 0, blocked: 2, working: false }),
  ];
  const roadLane = lane({ goals: roadGoals.slice(0, 2), goals_total: 3, goal_road: 'bmw', goal_classes: 'bm-' });
  const roadReads = readController({ getRead: () => ({ status: 'held', reply: heldReply(roadLane.lane_id, roadGoals, [], { projection: { goal_classes: 'bmw-' } }) }) });
  const roadModel = buildLanesViewModel({ inventory: inventory([roadLane]), reads: roadReads, route: { level: 'lane', laneId: roadLane.lane_id } });
  assert.deepEqual(roadModel.stops.map(stop => stop.goalClass), ['blocked', 'moving', 'working']);
  assert.deepEqual(roadModel.stops.map(stop => stop.number), [1, 2, 3]);

  const noRoadGoals = [
    goal('wide-a', 'Wide A', 1, { moving: 1, blocked: 0, working: false }),
    goal('wide-b', 'Wide B', 2, { moving: 1, blocked: 0, working: false }),
    goal('wide-hidden', 'Wide hidden', 3, { moving: 1, blocked: 0, working: false }),
    goal('wide-last', 'Wide last', 4, { moving: 1, blocked: 0, working: false }),
  ];
  const noRoadLane = lane({ goals: noRoadGoals.slice(0, 2), goals_total: 65, goal_road: null, goal_classes: 'bm-' });
  const noRoadReads = readController({ getRead: () => ({ status: 'held', reply: heldReply(noRoadLane.lane_id, noRoadGoals, [], { projection: { goal_classes: 'wqbd-' } }) }) });
  const noRoadModel = buildLanesViewModel({ inventory: inventory([noRoadLane]), reads: noRoadReads, route: { level: 'lane', laneId: noRoadLane.lane_id } });
  assert.deepEqual(noRoadModel.stops.map(stop => stop.goal.title), noRoadGoals.map(row => row.title));
  assert.deepEqual(noRoadModel.stops.map(stop => stop.goalClass), ['working', 'queued', 'blocked', 'done']);
  assert.ok(noRoadModel.stops.every(stop => stop.number === null));
});

test('redacted live inventory and capped detail retain production-shaped synthetic goal IDs', () => {
  assert.equal(liveLaneFixture.capture.redacted, true);
  assert.match(liveLaneFixture.capture.raw_sha256.inventory, /^[a-f0-9]{64}$/);
  assert.match(liveLaneFixture.capture.raw_sha256.detail, /^[a-f0-9]{64}$/);
  assert.equal(liveLaneFixture.inventory.lanes.length, liveLaneFixture.capture.open_lane_count);
  const normalizedInventory = applyWorkLanesFrame(emptyWorkLanesInventory(), {
    type: 'work_lanes.inventory', ...liveLaneFixture.inventory,
  });
  assert.equal(normalizedInventory.lanes.length, liveLaneFixture.inventory.lanes.length);
  for (const rawLane of liveLaneFixture.inventory.lanes) {
    const normalizedLane = normalizedInventory.lanes.find(item => item.lane_id === rawLane.lane_id);
    assert.ok(normalizedLane, `normalizer retains ${rawLane.lane_id}`);
    assert.deepEqual(normalizedLane.goals.map(item => item.goal_id), rawLane.goals.map(item => item.goal_id));
    assert.deepEqual(normalizedLane.progress.members.map(item => item.goal_id), rawLane.members.map(item => item.goal_id ?? null));
  }
  const capped = liveLaneFixture.inventory.lanes.filter(item => item.goals_total > item.goals.length);
  assert.equal(capped.length, 1);
  const lane = capped[0];
  const detail = liveLaneFixture.capped_detail;
  assert.equal(lane.lane_id, detail.lane.lane_id);
  assert.equal(lane.goals_total, detail.projection.goals_total);
  assert.equal(lane.goals.length, 8);
  assert.equal(detail.projection.goals.length, 11);
  assert.equal(lane.members_total, detail.members_total);
  assert.equal(detail.members.length, detail.members_total);

  const identifiers = [];
  const goalIds = [];
  const collectIds = value => {
    if (!value || typeof value !== 'object') return;
    if (Array.isArray(value)) return value.forEach(collectIds);
    for (const [key, child] of Object.entries(value)) {
      if (key === 'goal_id' && typeof child === 'string') goalIds.push(child);
      else if (key.endsWith('_id') && typeof child === 'string') identifiers.push(child);
      else collectIds(child);
    }
  };
  collectIds(liveLaneFixture.inventory);
  collectIds(detail);
  assert.ok(identifiers.length > 0);
  assert.ok(identifiers.every(id => id.startsWith('synthetic-')));
  assert.ok(goalIds.length > 0);
  assert.ok(goalIds.every(id => /^g_[0-9a-f]{24}$/.test(id)));

  const normalizedDetail = applyWorkLanesFrame(emptyWorkLanesInventory(), {
    type: 'work_lanes.inventory',
    lanes: [{ ...lane, ...detail.projection }],
  }).lanes[0];
  assert.ok(normalizedDetail);
  assert.deepEqual(normalizedDetail.goals.map(item => item.goal_id), detail.projection.goals.map(item => item.goal_id));
  assert.deepEqual(normalizedDetail.progress.members.map(item => item.goal_id), detail.projection.members.map(item => item.goal_id ?? null));

  let held = false;
  const reads = readController({ getRead: (laneId, kind) => held && laneId === lane.lane_id
    && (kind === 'goals' || kind === 'members')
    ? { status: 'held', reply: detail }
    : { status: 'idle', reply: null } });
  const before = buildLanesViewModel({ inventory: liveLaneFixture.inventory, reads });
  const beforeRow = before.active.concat(before.parked).find(row => row.lane.lane_id === lane.lane_id);
  assert.deepEqual(beforeRow.attention, []);
  assert.equal(beforeRow.more, null);
  const preview = buildLanesViewModel({ inventory: liveLaneFixture.inventory, reads,
    route: { level: 'lane', laneId: lane.lane_id } });
  assert.equal(preview.stops.length, 8);
  assert.ok(preview.more > 0);

  held = true;
  const complete = buildLanesViewModel({ inventory: liveLaneFixture.inventory, reads,
    route: { level: 'lane', laneId: lane.lane_id } });
  assert.equal(complete.detailHeld, true);
  assert.equal(complete.stops.slice(0, 11).length, detail.projection.goals.length);
  assert.equal(complete.more, 0);
});

test('U3 L11: the full road, eight-record preview, forty members and question placement stay observable', () => {
  const road = 'bbwmqwwbwbw';
  const sentPositions = [0, 1, 2, 3, 5, 6, 8, 10];
  const goals = [...road].map((letter, index) => roadGoal('lane-l11', index, letter));
  const allMembers = Array.from({ length: 40 }, (_, index) => {
    const position = index === 7 ? 7 : index === 20 ? 2 : index % goals.length;
    const questions = index === 7 || index === 20
      ? { open_questions: { count: 1, first_question_id: `question-${index}` } }
      : {};
    return member(`l11-spec-${String(index).padStart(2, '0')}`, goals[position].goal_id, questions);
  });
  const item = lane({
    lane_id: 'lane-l11', title: 'Synthetic L11', goals: sentPositions.map(position => goals[position]),
    goals_total: 11, goal_road: road, goal_classes: 'bbwmwwwwd', goal_counts: [6, 4, 1, 0],
    default_goal: { state: 'done', done: 1, total: 1 },
    members: allMembers.slice(0, 8), members_total: 40,
  });
  const memberPages = [allMembers.slice(0, 32), allMembers.slice(32)];
  const joinedMembers = memberPages.flat();
  const projection = { goal_classes: `${road}d`, default_goal: item.default_goal, goals_total: 11 };
  let goalsRead = { status: 'idle', reply: null };
  let membersRead = { status: 'idle', reply: null };
  const reads = readController({
    getRead: (_laneId, kind) => kind === 'goals' ? goalsRead : kind === 'members' ? membersRead : null,
  });
  const noQuestions = buildLanesViewModel({ inventory: inventory([item]), reads });
  assert.deepEqual(noQuestions.active[0].attention, []);
  assert.equal(noQuestions.active[0].more, null);
  assert.equal(noQuestions.active[0].retry, false);

  const laneBeforeRead = buildLanesViewModel({ inventory: inventory([item]), reads, route: { level: 'lane', laneId: item.lane_id } });
  assert.deepEqual(laneBeforeRead.stops.map(stop => stop.goal.title), item.goals.map(goal => goal.title));
  assert.equal(laneBeforeRead.stops.length, 8);
  assert.equal(laneBeforeRead.more, 3);

  goalsRead = { status: 'held', reply: heldReply(item.lane_id, goals, [], { projection }) };
  const heldRoot = buildLanesViewModel({ inventory: inventory([item]), reads });
  assert.equal(heldRoot.active[0].goalsHeld, true);
  assert.equal(heldRoot.active[0].attention.length + heldRoot.active[0].more, 10);
  assert.deepEqual(heldRoot.active[0].attention.map(row => [row.title, row.reason]), [
    [goals[0].title, '2 blocked'], [goals[1].title, '2 blocked'],
  ]);
  assert.equal(heldRoot.active[0].more, 8);

  // A goals-only read drives root attention but the open lane stays on its sent preview.
  const laneAfterGoalsRead = buildLanesViewModel({ inventory: inventory([item]), reads, route: { level: 'lane', laneId: item.lane_id } });
  assert.deepEqual(laneAfterGoalsRead.stops.map(stop => stop.goal.title), item.goals.map(goal => goal.title));
  assert.equal(laneAfterGoalsRead.stops.length, 8);
  assert.equal(laneAfterGoalsRead.more, 3);

  const questions = questionSnapshot(item.lane_id, [
    placedQuestion(item.lane_id, goals[2].goal_id, allMembers[20].spec_id, 'question-off-preview'),
    placedQuestion(item.lane_id, goals[7].goal_id, allMembers[7].spec_id, 'question-preview'),
  ]);
  const placedRoot = buildLanesViewModel({ inventory: inventory([item]), reads, questions });
  assert.deepEqual(placedRoot.active[0].attention.map(row => [row.title, row.reason]), [
    [goals[2].title, 'needs you'], [goals[7].title, 'needs you'],
  ]);
  assert.equal(placedRoot.active[0].more, 8);

  goalsRead = { status: 'failed', reply: null };
  const failedRoot = buildLanesViewModel({ inventory: inventory([item]), reads, questions });
  assert.deepEqual(failedRoot.active[0].attention, []);
  assert.equal(failedRoot.active[0].more, null);
  assert.equal(failedRoot.active[0].retry, true);

  const updated = { ...item, goal_road: `ww${road.slice(2)}`, goal_classes: 'wwwmwwwwd', goal_counts: [8, 2, 1, 0] };
  goalsRead = { status: 'held', reply: heldReply(item.lane_id, goals, [], { projection }) };
  const changedInventory = buildLanesViewModel({ inventory: inventory([updated]), reads });
  assert.deepEqual(changedInventory.active[0].attention.map(row => row.title), [goals[7].title, goals[9].title]);
  assert.equal(changedInventory.active[0].more, 8);

  assert.deepEqual(memberPages.map(page => page.length), [32, 8]);
  membersRead = { status: 'held', reply: heldReply(item.lane_id, goals, joinedMembers, { projection }) };
  const laneHeld = buildLanesViewModel({ inventory: inventory([item]), reads, questions, route: { level: 'lane', laneId: item.lane_id } });
  assert.equal(laneHeld.detailHeld, true);
  assert.deepEqual(laneHeld.stops.map(stop => stop.goal.title), goals.map(goal => goal.title));
  assert.deepEqual(laneHeld.stops.map(stop => stop.number), Array.from({ length: 11 }, (_, index) => index + 1));
  assert.equal(laneHeld.more, 0);
  assert.equal(laneHeld.stops.reduce((count, stop) => count + stop.members.length, 0), 40);
  assert.equal(laneHeld.stops.find(stop => stop.goalId === goals[2].goal_id).question, true);
  assert.equal(laneHeld.stops.find(stop => stop.goalId === goals[7].goal_id).question, true);
});

test('U3 L65: no-road detail preserves daemon order, record classes and the default stop', () => {
  const sentClasses = 'bbwmwwww';
  const completeClasses = `${sentClasses}${'w'.repeat(57)}b`;
  const titles = [];
  const goals = Array.from(completeClasses.slice(0, 65), (letter, index) => {
    const identity = index === 6 ? { goal_id: 'a-goal-zulu', title: 'Zulu', rank: 50 }
      : index === 7 ? { goal_id: 'z-goal-alpha', title: 'Alpha', rank: 50 }
        : { goal_id: `lane-l65-goal-${index}`, title: `Synthetic no-road goal ${index}`, rank: 100 - index };
    const row = roadGoal('lane-l65', index, letter, identity);
    delete row.pos;
    titles.push(row.title);
    return row;
  });
  const defaultGoal = { state: 'in_progress', done: 0, total: 1, moving: 1, blocked: 0, working: false };
  const questionMember = member('l65-question-spec', goals[2].goal_id, {
    open_questions: { count: 1, first_question_id: 'l65-question' },
  });
  const looseMember = member('l65-no-goal-spec', null, { status: 'in_progress' });
  const item = lane({
    lane_id: 'lane-l65', title: 'Synthetic L65', goals: goals.slice(0, 8), goals_total: 65,
    goal_road: null, goal_classes: `${sentClasses}w`, goal_counts: [63, 2, 0, 0], default_goal: defaultGoal,
    members: [questionMember, looseMember], members_total: 2,
  });
  const projection = { goal_classes: completeClasses, goals_total: 65, default_goal: { ...defaultGoal, blocked: 1 } };
  let goalsRead = { status: 'idle', reply: null };
  let membersRead = { status: 'idle', reply: null };
  const reads = readController({
    getRead: (_laneId, kind) => kind === 'goals' ? goalsRead : kind === 'members' ? membersRead : null,
  });
  const question = placedQuestion(item.lane_id, goals[2].goal_id, questionMember.spec_id, 'l65-question');
  const questions = questionSnapshot(item.lane_id, [question]);

  const beforeRoot = buildLanesViewModel({ inventory: inventory([item]), reads, questions });
  assert.deepEqual(beforeRoot.active[0].attention, []);
  assert.equal(beforeRoot.active[0].more, null);
  const beforeLane = buildLanesViewModel({ inventory: inventory([item]), reads, questions, route: { level: 'lane', laneId: item.lane_id } });
  assert.deepEqual(beforeLane.stops.slice(0, 8).map(stop => stop.goal.title), titles.slice(0, 8));
  assert.equal(beforeLane.stops.every(stop => stop.number === null && stop.position === null), true);
  assert.equal(beforeLane.more, 57);

  const detail = heldReply(item.lane_id, goals, [questionMember, looseMember], { projection });
  goalsRead = { status: 'held', reply: heldReply(item.lane_id, goals, [], { projection }) };
  const afterGoalsOnly = buildLanesViewModel({ inventory: inventory([item]), reads, questions, route: { level: 'lane', laneId: item.lane_id } });
  assert.deepEqual(afterGoalsOnly.stops.slice(0, 8).map(stop => stop.goal.title), titles.slice(0, 8));
  assert.equal(afterGoalsOnly.more, 57);
  const afterGoalsRoot = buildLanesViewModel({ inventory: inventory([item]), reads, questions });
  assert.deepEqual(afterGoalsRoot.active[0].attention.map(row => [row.title, row.reason]), [
    [goals[2].title, 'needs you'], [goals[0].title, '2 blocked'],
  ]);
  assert.equal(afterGoalsRoot.active[0].more, 63);
  assert.equal(afterGoalsRoot.active[0].attention.length + afterGoalsRoot.active[0].more, 65,
    'the one question, two blocked goals and remaining active goals fill the 65-goal attention set');

  membersRead = { status: 'held', reply: detail };
  const afterMembers = buildLanesViewModel({ inventory: inventory([item]), reads, questions, route: { level: 'lane', laneId: item.lane_id } });
  assert.deepEqual(afterMembers.stops.slice(0, 65).map(stop => stop.goal.title), titles);
  assert.equal(afterMembers.stops.length, 66);
  assert.deepEqual(afterMembers.stops.map(stop => stop.number), Array(66).fill(null));
  assert.deepEqual(afterMembers.stops.map(stop => stop.position), Array(66).fill(null));
  assert.equal(afterMembers.stops[6].goal.title, 'Zulu');
  assert.equal(afterMembers.stops[7].goal.title, 'Alpha');
  assert.deepEqual(afterMembers.stops.slice(0, 65).map(stop => stop.goalClass), [...completeClasses.slice(0, 65)].map(letter => ({ b: 'blocked', w: 'working', m: 'moving', q: 'queued', d: 'done' })[letter]));
  const noGoalStop = afterMembers.stops[65];
  assert.equal(noGoalStop.isDefault, true);
  assert.equal(noGoalStop.goalClass, 'blocked', 'the held complete classes override the capped inventory default class');
  assert.equal(noGoalStop.members.length, 1);
  assert.equal(noGoalStop.members[0].spec_id, looseMember.spec_id);
  assert.equal(noGoalStop.sections.find(section => section.kind === 'in_progress')?.rows[0]?.member.spec_id, looseMember.spec_id);
});

test('U3 L65: a moved working seat keeps the last root read until the no-road lane is opened', () => {
  const oldClasses = `${'d'.repeat(8)}wm${'d'.repeat(55)}`;
  const movedClasses = `${'d'.repeat(8)}mw${'d'.repeat(55)}`;
  const goals = [...oldClasses].map((letter, index) => {
    const identity = index === 8 ? { goal_id: 'synthetic-unsent-a', title: 'Synthetic unsent A' }
      : index === 9 ? { goal_id: 'synthetic-unsent-b', title: 'Synthetic unsent B' }
        : { goal_id: `synthetic-seat-goal-${index}`, title: `Synthetic seat goal ${index}` };
    const row = roadGoal('lane-seat-move', index, letter, identity);
    delete row.pos;
    return row;
  });
  const movedGoals = [...movedClasses].map((letter, index) => roadGoal('lane-seat-move', index, letter, goals[index]));
  for (const row of movedGoals) delete row.pos;
  const item = lane({ lane_id: 'lane-seat-move', title: 'Synthetic seat lane', goals: goals.slice(0, 8), goals_total: 65,
    goal_road: null, goal_classes: 'd'.repeat(8), goal_counts: [2, 0, 0, 63], members: [], members_total: 2 });
  const frame = inventory([item]);
  const workingSeat = (id, activity) => ({ stream_id: id, role: null, host: null, visibility: null, parent_stream_id: null,
    activity, last_activity: null });
  const oldMembers = [
    member('synthetic-seat-member-a', goals[8].goal_id, { status: 'in_progress', seats: [workingSeat('synthetic-seat-a', 'working')] }),
    member('synthetic-seat-member-b', goals[9].goal_id, { status: 'in_progress', seats: [workingSeat('synthetic-seat-b', 'idle')] }),
  ];
  const movedMembers = [
    member('synthetic-seat-member-a', goals[8].goal_id, { status: 'in_progress', seats: [workingSeat('synthetic-seat-a', 'idle')] }),
    member('synthetic-seat-member-b', goals[9].goal_id, { status: 'in_progress', seats: [workingSeat('synthetic-seat-b', 'working')] }),
  ];
  const goalsRead = { status: 'held', reply: heldReply(item.lane_id, goals, [], { projection: { goal_classes: oldClasses } }) };
  const refreshedReply = heldReply(item.lane_id, movedGoals, movedMembers, { projection: { goal_classes: movedClasses } });
  let membersRead = { status: 'idle', reply: null };
  let memberRequests = 0;
  const reads = readController({
    getRead: (_laneId, kind) => kind === 'goals' ? goalsRead : kind === 'members' ? membersRead : null,
    requestMembers() {
      memberRequests += 1;
      membersRead = { status: 'held', reply: refreshedReply };
      reads.publish();
      return Promise.resolve(membersRead);
    },
  });

  const oldRoot = buildLanesViewModel({ inventory: frame, reads });
  assert.deepEqual(oldRoot.active[0].attention.map(row => row.title), ['Synthetic unsent A', 'Synthetic unsent B']);
  assert.equal(frame.lanes[0], item, 'the inventory frame remains unchanged between detail reads');

  const { dom, root } = makeDom();
  const control = mountLanesCard(root, { getState: () => ({ inventory: frame }), reads });
  control.openLane(item.lane_id);
  const stops = root.querySelectorAll('.next-lanes-stop');
  assert.equal(memberRequests, 1);
  assert.equal(stops.length, 65);
  assert.ok(stops[8].classList.contains('next-lanes-stop-moving'));
  assert.ok(stops[9].classList.contains('next-lanes-stop-working'));
  assert.equal(frame.lanes[0], item, 'the new marks came from the lane read, without an inventory update');
  control.destroy();
  dom.window.close();
});

test('capped no-road lanes keep daemon order, omit positions, and reveal goal rows only with held detail', () => {
  const sentGoals = [goal('goal-first', 'First from daemon', 90), goal('goal-second', 'Second from daemon', 1)];
  const item = lane({ goals: sentGoals, goals_total: 65, goal_road: null, goal_classes: undefined, goal_counts: undefined });
  const reads = readController({ getRead: () => ({ status: 'idle', reply: null }) });
  const before = buildLanesViewModel({ inventory: inventory([item]), reads });
  assert.deepEqual(before.active[0].attention, []);
  assert.equal(before.active[0].capped, true);

  const normalizedCounts = lane({ goal_counts: { moving: 1, blocked: 0, queued: 0, done: 0 }, goal_classes: 'q-' });
  assert.equal(buildLanesViewModel({ inventory: inventory([normalizedCounts]) }).active[0].laneClass, 'moving');

  const fullGoals = [...sentGoals, ...Array.from({ length: 63 }, (_, index) => goal(`goal-extra-${index}`, `Extra ${index}`, 100 - index))];
  reads.getRead = (_laneId, kind) => kind === 'members'
    ? { status: 'held', reply: heldReply(item.lane_id, fullGoals, []) }
    : { status: 'idle', reply: null };
  const detail = buildLanesViewModel({ inventory: inventory([item]), reads, route: { level: 'lane', laneId: item.lane_id } });
  assert.deepEqual(detail.stops.map(stop => stop.goal.title), fullGoals.map(row => row.title));
  assert.ok(detail.stops.every(stop => stop.number === null));
});

test('goal sections use mobile membership, six-row expansion, and the Done fold', () => {
  const nextRows = Array.from({ length: 7 }, (_, index) => member(`next-${index}`, 'goal-a', { status: 'ready_for_dev' }));
  const doneRows = Array.from({ length: 10 }, (_, index) => member(`done-${index}`, 'goal-a', { status: 'completed' }));
  const members = [...nextRows, ...doneRows, member('deprecated', 'goal-a', { status: 'deprecated' })];
  const item = lane({ members: members.slice(0, 8), members_total: members.length });
  const reads = readController({ getRead: () => ({ status: 'held', reply: heldReply(item.lane_id, item.goals, members) }) });
  const route = { level: 'goal', laneId: item.lane_id, goalId: 'goal-a' };
  const initial = buildLanesViewModel({ inventory: inventory([item]), reads, route });
  const next = initial.sections.find(section => section.kind === 'next');
  const done = initial.sections.find(section => section.kind === 'done');
  assert.equal(next.count, 7);
  assert.equal(next.rows.length, 6);
  assert.equal(next.more, 1);
  assert.equal(done.count, 10);
  assert.equal(done.rows.length, 0);
  assert.equal(initial.sections.some(section => section.rows.some(row => row.member.spec_id === 'deprecated')), false);

  const expanded = buildLanesViewModel({ inventory: inventory([item]), reads, route,
    expanded: new Set([`goal:${item.lane_id}:goal-a:next`, `goal:${item.lane_id}:goal-a:done`]) });
  assert.equal(expanded.sections.find(section => section.kind === 'next').rows.length, 7);
  assert.equal(expanded.sections.find(section => section.kind === 'done').rows.length, 8);
  const allDone = buildLanesViewModel({ inventory: inventory([item]), reads, route,
    expanded: new Set([`goal:${item.lane_id}:goal-a:done`, `goal:${item.lane_id}:goal-a:done:all`]) });
  assert.equal(allDone.sections.find(section => section.kind === 'done').rows.length, 10);
});

test('show more reveals all members without adding a collapse control', () => {
  const later = Array.from({ length: 8 }, (_, index) => member(`later-expand-${index}`, 'goal-a', { status: 'backlog' }));
  const done = Array.from({ length: 10 }, (_, index) => member(`done-expand-${index}`, 'goal-a', { status: 'completed' }));
  const item = lane({ members: later.slice(0, 8), members_total: later.length + done.length });
  const reads = readController({ getRead: () => ({ status: 'held', reply: heldReply(item.lane_id, item.goals, [...later, ...done]) }) });
  const { dom, root } = makeDom();
  const control = mountLanesCard(root, { getState: () => ({ inventory: inventory([item]) }), reads });
  control.openGoal(item.lane_id, 'goal-a', 'Synthetic goal');

  const section = () => root.querySelector('.next-lanes-section-later');
  assert.equal(section().querySelectorAll('.next-lanes-member').length, 6);
  section().querySelector('.next-lanes-show-more').click();
  assert.equal(section().querySelectorAll('.next-lanes-member').length, 8);
  assert.equal(section().querySelector('.next-lanes-show-more'), null);

  root.querySelector('.next-lanes-section-done .next-lanes-section-toggle').click();
  const doneSection = () => root.querySelector('.next-lanes-section-done');
  assert.equal(doneSection().querySelectorAll('.next-lanes-member').length, 8);
  doneSection().querySelector('.next-lanes-show-more').click();
  assert.equal(doneSection().querySelectorAll('.next-lanes-member').length, 10);
  assert.equal(doneSection().querySelector('.next-lanes-show-more'), null);
  control.destroy();
  dom.window.close();
});

test('member marks are accessible across mobile kinds and keep a separate question indicator only when needed', () => {
  const workingQuestion = member('mark-working-question', 'goal-a', {
    status: 'blocked', seats: [{ stream_id: 'synthetic-seat', parent_stream_id: null, activity: 'working' }],
  });
  const needs = member('mark-needs', 'goal-a', { status: 'blocked', open_questions: { count: 1, first_question_id: 'mark-question' } });
  const rows = [
    workingQuestion,
    needs,
    member('mark-in-progress', 'goal-a', { status: 'in_progress' }),
    member('mark-blocked', 'goal-a', { status: 'blocked' }),
    member('mark-next', 'goal-a', { status: 'analysis' }),
    member('mark-later', 'goal-a', { status: 'backlog' }),
    member('mark-done', 'goal-a', { status: 'completed' }),
  ];
  const item = lane({ members: rows, members_total: rows.length });
  const reply = heldReply(item.lane_id, item.goals, rows);
  const reads = readController({ getRead: (_laneId, kind) => kind === 'goal'
    ? { status: 'held', reply }
    : { status: 'idle', reply: null } });
  const questions = { getSnapshot: () => questionSnapshot(item.lane_id, [
    placedQuestion(item.lane_id, 'goal-a', workingQuestion.spec_id, 'working-row-question'),
  ]) };
  const { dom, root } = makeDom();
  const control = mountLanesCard(root, { getState: () => ({ inventory: inventory([item]) }), reads, questions });
  control.openGoal(item.lane_id, 'goal-a', 'Synthetic goal');

  const expected = [
    ['mark-working-question', 'working', 'Working'],
    ['mark-needs', 'needs', 'Needs your answer'],
    ['mark-in-progress', 'in_progress', 'In progress'],
    ['mark-blocked', 'blocked', 'Blocked'],
    ['mark-next', 'next', 'Queued next'],
    ['mark-later', 'later', 'Queued later'],
  ];
  for (const [specId, kind, label] of expected) {
    const title = root.querySelector(`.next-lanes-member-title[data-spec-id="${specId}"]`);
    const row = title && title.closest('.next-lanes-member');
    assert.ok(row, `${specId} has a visible row`);
    const mark = row.querySelector('.next-lanes-member-mark');
    assert.ok(mark.classList.contains(`next-lanes-member-mark-${kind}`));
    assert.equal(mark.getAttribute('role'), 'img');
    assert.equal(mark.getAttribute('aria-label'), label);
  }
  assert.deepEqual([...root.querySelectorAll('.next-lanes-section')].map(section => section.classList[1]), [
    'next-lanes-section-working', 'next-lanes-section-needs', 'next-lanes-section-in_progress',
    'next-lanes-section-blocked', 'next-lanes-section-next', 'next-lanes-section-later', 'next-lanes-section-done',
  ]);
  const workingRow = root.querySelector('.next-lanes-member-title[data-spec-id="mark-working-question"]')?.closest('.next-lanes-member');
  assert.equal(workingRow?.querySelector('.next-lanes-question-mark')?.textContent, '?');
  const needsRow = root.querySelector('.next-lanes-member-title[data-spec-id="mark-needs"]')?.closest('.next-lanes-member');
  assert.equal(needsRow?.querySelector('.next-lanes-question-mark'), null, 'Needs already carries the question shape');

  root.querySelector('.next-lanes-section-done .next-lanes-section-toggle').click();
  const doneRow = root.querySelector('.next-lanes-member-title[data-spec-id="mark-done"]')?.closest('.next-lanes-member');
  assert.ok(doneRow?.querySelector('.next-lanes-member-mark-done'));
  assert.equal(doneRow?.querySelector('.next-lanes-member-mark')?.getAttribute('aria-label'), 'Done');
  control.destroy();
  dom.window.close();
});

test('road stop marks preserve working, moving, blocked, done, next and later shapes', () => {
  const road = 'qwmqbd';
  const goals = [...road].map((letter, index) => roadGoal('lane-stop-marks', index, letter));
  const item = lane({ lane_id: 'lane-stop-marks', goals, goals_total: goals.length, goal_road: road,
    goal_classes: road, goal_counts: goalCountSummary(road) });
  const reads = readController({ getRead: () => ({ status: 'idle', reply: null }) });
  const { dom, root } = makeDom();
  const control = mountLanesCard(root, { getState: () => ({ inventory: inventory([item]) }), reads });
  control.openLane(item.lane_id);

  const stops = [...root.querySelectorAll('.next-lanes-stop')];
  assert.equal(stops.length, 6);
  for (const [index, mark] of ['next', 'working', 'moving', 'later', 'blocked', 'done'].entries()) {
    assert.ok(stops[index].classList.contains(`next-lanes-stop-dot-${mark}`), `stop ${index} has ${mark} mark`);
    if (mark === 'next' || mark === 'later') assert.ok(stops[index].classList.contains(`next-lanes-stop-position-${mark}`));
  }
  control.destroy();
  dom.window.close();
});

test('U3 root keeps uncapped blocked and quiet rows compact, folds parked lanes, and shows paused age and truncation', () => {
  const now = Date.parse('2026-11-21T12:00:00Z');
  const blocked = lane({ lane_id: 'lane-blocked-unread', title: 'Synthetic blocked lane',
    goals: [goal('blocked-unread-goal', 'Synthetic blocked goal', 1, { pos: 0, moving: 0, blocked: 1, total: 2, working: false })],
    goals_total: 1, goal_road: 'b', goal_classes: 'b', goal_counts: [0, 1, 0, 0] });
  const quiet = lane({ lane_id: 'lane-quiet', title: 'Synthetic quiet lane', goals: [], goals_total: 0,
    goal_road: '', goal_classes: '', goal_counts: [0, 0, 0, 0] });
  const parked = lane({ lane_id: 'lane-parked', title: 'Synthetic parked lane', parked: { at: null, reason: null },
    goals: [], goals_total: 0, goal_road: '', goal_classes: '', goal_counts: [0, 0, 0, 0] });
  const paused = lane({ lane_id: 'lane-paused', title: 'Synthetic paused lane', state: 'paused',
    state_at: '2026-11-20T12:00:00Z', progress_at: '2026-11-21T11:00:00Z',
    goals: [], goals_total: 0, goal_road: '', goal_classes: '', goal_counts: [0, 0, 0, 0] });
  const frame = { lanes: [blocked, quiet, parked, paused], truncated: true, counts: { open: 6 } };
  const reads = readController({ getRead: () => ({ status: 'idle', reply: null }) });
  const model = buildLanesViewModel({ inventory: frame, reads, now });
  const blockedRow = model.active.find(row => row.lane.lane_id === blocked.lane_id);
  assert.deepEqual(blockedRow.attention.map(row => [row.title, row.reason]), [['Synthetic blocked goal', 'blocked']]);
  assert.equal(blockedRow.goalsHeld, true, 'an uncapped lane needs no detail read');
  assert.equal(model.active.find(row => row.lane.lane_id === paused.lane_id).age, '1d', 'paused age uses state_at');
  assert.equal(model.truncatedMore, 2);
  assert.equal(model.parked.length, 1);

  const { dom, root } = makeDom();
  const control = mountLanesCard(root, { getState: () => ({ inventory: frame }), reads, now: () => now });
  const quietRow = [...root.querySelectorAll('.next-lanes-row')]
    .find(row => row.querySelector('.next-lanes-row-title')?.textContent === quiet.title);
  assert.ok(quietRow);
  assert.equal(quietRow.querySelectorAll('.next-lanes-attention').length, 0, 'quiet lane stays a single title line');
  assert.equal(root.querySelector('.next-lanes-parked-list'), null, 'parked rows start folded');
  assert.equal(root.querySelector('.next-lanes-truncated')?.textContent, '+2 more lanes');
  root.querySelector('.next-lanes-parked-toggle').click();
  assert.ok(root.querySelector('.next-lanes-parked-list .next-lanes-row-title')?.textContent === parked.title);
  control.destroy();
  dom.window.close();
});

test('U3 loose completed work stays in Finished earlier and specs omit absent criteria and recent entries', () => {
  const completed = member('synthetic-finished-earlier', null, { status: 'completed', criteria: [], recent: [] });
  const item = lane({ members: [completed], members_total: 1 });
  const reply = heldReply(item.lane_id, item.goals, [completed]);
  const reads = readController({ getRead: (_laneId, kind) => kind === 'members'
    ? { status: 'held', reply }
    : { status: 'idle', reply: null } });
  const laneModel = buildLanesViewModel({ inventory: inventory([item]), reads, route: { level: 'lane', laneId: item.lane_id } });
  assert.equal(laneModel.finishedEarlierCount, 1);
  assert.equal(laneModel.stops.some(stop => stop.isDefault), false);
  const specModel = buildLanesViewModel({ inventory: inventory([item]), reads,
    route: { level: 'spec', laneId: item.lane_id, goalId: null, specId: completed.spec_id } });
  assert.deepEqual(specModel.criteriaLeft, []);
  assert.deepEqual(specModel.criteriaPassed, []);
  assert.deepEqual(specModel.recent, []);

  const { dom, root } = makeDom();
  const control = mountLanesCard(root, { getState: () => ({ inventory: inventory([item]) }), reads });
  control.openLane(item.lane_id);
  assert.match(root.querySelector('.next-lanes-finished-toggle')?.textContent || '', /Finished earlier · 1/);
  root.querySelector('.next-lanes-finished-toggle').click();
  root.querySelector('.next-lanes-section-done .next-lanes-section-toggle').click();
  root.querySelector('.next-lanes-member-title[data-spec-id="synthetic-finished-earlier"]').click();
  assert.equal(root.querySelector('.next-lanes-criteria-left'), null);
  assert.equal(root.querySelector('.next-lanes-recent'), null);
  control.destroy();
  dom.window.close();
});

test('back follows lane to all, goal to lane, and spec to goal with the goal title', () => {
  const { dom, root } = makeDom();
  const item = lane({ members: [member('spec-a', 'goal-a')], members_total: 1 });
  const reads = readController({ getRead: (_laneId, kind) => kind === 'members'
    ? { status: 'held', reply: heldReply(item.lane_id, item.goals, item.members) }
    : { status: 'idle', reply: null } });
  const control = mountLanesCard(root, { getState: () => ({ inventory: inventory([item]) }), reads });

  control.openLane(item.lane_id);
  assert.equal(control.getRoute().level, 'lane');
  control.back();
  assert.equal(control.getRoute().level, 'all');

  control.openGoal(item.lane_id, 'goal-a', 'Synthetic goal');
  root.querySelector('.next-lanes-member-title').click();
  assert.equal(control.getRoute().level, 'spec');
  assert.equal(root.querySelector('.next-lanes-back')?.textContent, '‹  Synthetic goal');
  control.back();
  assert.equal(control.getRoute().level, 'goal');

  control.openSpec(item.lane_id, 'goal-a', 'spec-a', 'Synthetic spec');
  assert.equal(root.querySelector('.next-lanes-back')?.textContent, '‹  Synthetic goal');
  control.back();
  assert.equal(control.getRoute().level, 'goal');
  control.back();
  assert.equal(control.getRoute().level, 'lane');
  control.back();
  assert.equal(control.getRoute().level, 'all');
  control.destroy();
  dom.window.close();
});

test('every redraw and destroy unmounts lane question and You said hosts before replacing DOM', () => {
  const { dom, root } = makeDom();
  const item = lane({ members: [member('spec-a', 'goal-a', { blocked_line: 'a stored choice' })], members_total: 1 });
  const reads = readController({ getRead: (_laneId, kind) => kind === 'members'
    ? { status: 'held', reply: heldReply(item.lane_id, item.goals, item.members) }
    : { status: 'idle', reply: null } });
  const hosts = [];
  const cleaned = [];
  const question = { id: 'question-a', placement: { kind: 'lane_goal', specId: 'spec-a' } };
  const questions = {
    getSnapshot: () => ({
      byLane: new Map([[item.lane_id, [question]]]),
      byGoal: new Map([[item.lane_id, new Map([['goal-a', [question]]])]]),
      bySpec: new Map([['spec-a', [question]]]),
      youSaidBySpec: new Map(),
    }),
    mountCards(host, args) {
      hosts.push({ host, args, kind: 'cards' });
      return () => { cleaned.push({ host, attached: root.contains(host) }); };
    },
    mountYouSaid(host, specId) {
      hosts.push({ host, specId, kind: 'said' });
      return () => { cleaned.push({ host, attached: root.contains(host) }); };
    },
  };
  const control = mountLanesCard(root, { getState: () => ({ inventory: inventory([item]) }), reads, questions });
  control.openSpec({ laneId: item.lane_id, goalId: 'goal-a', specId: 'spec-a', specTitle: 'Synthetic spec spec-a' });
  assert.deepEqual(hosts.slice(0, 2).map(row => row.kind), ['cards', 'said']);
  assert.equal(root.querySelector('.next-lanes-fact-attention .next-lanes-fact-label')?.textContent, 'Blocked on');

  control.render();
  assert.equal(cleaned.length, 2);
  assert.ok(cleaned.every(row => row.attached), 'redraw cleanup runs while the old hosts remain mounted');
  assert.equal(hosts.length, 4);

  control.destroy();
  assert.equal(cleaned.length, 4);
  assert.ok(cleaned.every(row => row.attached), 'destroy cleanup runs before clearing the lanes root');
  assert.equal(root.childElementCount, 0);
  dom.window.close();
});

test('fresh spec mount hydrates stored answers and keeps Blocked on until reload data arrives', async () => {
  const { dom, root } = makeDom();
  const item = lane({ members: [member('spec-a', 'goal-a', { blocked_line: 'a stored choice', ac_checked: 2, ac_total: 4 })], members_total: 1 });
  const held = heldReply(item.lane_id, item.goals, item.members);
  const reads = readController({ getRead: (_laneId, kind) => kind === 'members'
    ? { status: 'held', reply: held }
    : { status: 'idle', reply: null } });
  const promptCalls = [];
  const questions = createQuestions({
    document: root.ownerDocument,
    inventory: inventory([item]),
    notifications: [notification()],
    laneReads: reads,
    cc: { promptList: async args => {
      promptCalls.push(args);
      return { ok: true, questions: [{ question_id: 'question-a', state: 'answered', answered_at: '2026-11-21T12:00:00Z',
        answer: { selections: ['yes'] }, envelope: { spec_id: 'spec-a', options: [{ label: 'Continue', value: 'yes' }] } }] };
    } },
  });
  let control;
  questions.subscribe(() => control?.render());
  control = mountLanesCard(root, { getState: () => ({ inventory: inventory([item]) }), reads, questions });
  control.openSpec({ laneId: item.lane_id, goalId: 'goal-a', specId: 'spec-a' });
  assert.deepEqual(promptCalls, []);
  assert.equal(root.querySelector('.next-lanes-fact-attention .next-lanes-fact-label')?.textContent, 'Blocked on');

  for (let turn = 0; turn < 8; turn += 1) await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(promptCalls, [{ spec_id: 'spec-a', open: false }]);
  assert.equal(root.querySelector('.next-lanes-fact-attention'), null);
  assert.equal(root.querySelector('.next-question-you-said-label')?.textContent, 'You said');
  assert.equal(root.querySelector('.next-question-you-said-answer')?.textContent, 'Continue');
  const checksBar = root.querySelector('.next-lanes-checks-progress');
  assert.equal(checksBar?.getAttribute('role'), 'img');
  assert.equal(checksBar?.getAttribute('aria-label'), '2 of 4 checks complete');
  assert.equal(checksBar?.querySelector('.next-lanes-checks-progress-done')?.style.width, '50%');
  control.destroy();
  questions.dispose();
  dom.window.close();
});
