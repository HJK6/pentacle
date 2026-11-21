'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const F = require('./e2e/lib/next_lanes_fixtures');
const { applyWorkLanesFrame, emptyWorkLanesInventory } = require('../pentacle-chat-core/src/services/workLanes.ts');
const { createLaneReads } = require('../renderer/next/lane_reads');
const { buildQuestionPlacement } = require('../renderer/next/questions');

function normalize(lanes) { return applyWorkLanesFrame(emptyWorkLanesInventory(), F.inventoryFrame(lanes)); }
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise,resolve}; }

test('E9 moved preview excludes the member and current placement ignores stale A', async () => {
  const qid = 'next_lanes__e9-revision-moved';
  const { l11A, l11B, movedLane, movedSpecId } = F.e9MovedRevisionFixture(qid);
  const stateB = normalize([l11B, movedLane]);
  const normalizedMoved = stateB.lanes.find(lane => lane.lane_id === movedLane.inventory.lane_id);
  assert.ok(normalizedMoved.progress.members_total > normalizedMoved.progress.members.length);
  assert.equal(normalizedMoved.progress.members.some(member => member.spec_id === movedSpecId), false,
    'the destination preview must omit the moved question member');
  const target = movedLane.members.find(member => member.spec_id === movedSpecId);
  assert.equal(target.open_questions.first_question_id, qid);
  assert.equal(target.goal_id, movedLane.goals[0].goal_id);

  const calls = [];
  const reads = createLaneReads({show: (laneId, options) => {
    const item = deferred(); calls.push({laneId, options, ...item}); return item.promise;
  }});
  const stateA = normalize([l11A]);
  reads.updateInventory(stateA);
  const membersA = reads.requestMembers(l11A.inventory.lane_id);
  reads.updateInventory(stateB);
  const notification = {notification_id:'n', state:'open', producer:'agent_question.v1', title:'Moved',
    question:{question_id:qid, spec_id:movedSpecId, state:'open', response_mode:'single_choice', options:[{label:'Yes',value:'yes'}]}};
  const place = laneReads => buildQuestionPlacement({inventory:stateB, notifications:[notification], laneReads});
  const pending = place(reads);
  assert.equal(pending.questions[0].placement.kind, 'unplaced');
  assert.ok(pending.requestedMembers.includes(movedLane.inventory.lane_id));
  const movedPending = reads.requestMembers(movedLane.inventory.lane_id);
  const a = calls.find(row => row.laneId === l11A.inventory.lane_id);
  a.resolve(F.pageLaneDetail(l11A, {membersLimit:32}));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(place(reads).questions[0].placement.kind, 'unplaced', 'late A cannot place the question');
  const staleReply = F.pageLaneDetail(l11A, {membersLimit:32});
  const mutant = { getRead(laneId, kind) {
    if (laneId === l11A.inventory.lane_id && kind === 'members') return {status:'held', detailRevision:F.REVISION_B, reply:staleReply};
    return reads.getRead(laneId, kind);
  }};
  assert.equal(place(mutant).questions[0].placement.laneId, l11A.inventory.lane_id, 'mutation exposes stale-A leak');
  const b = calls.filter(row => row.laneId === l11A.inventory.lane_id).at(-1);
  b.resolve(F.pageLaneDetail(l11B, {membersLimit:32}));
  await new Promise(resolve => setImmediate(resolve));
  const bNext = calls.filter(row => row.laneId === l11A.inventory.lane_id).at(-1);
  if (bNext !== b) bNext.resolve(F.pageLaneDetail(l11B, {membersLimit:32, membersAfter:b.options.membersAfter || l11B.members[31].spec_id}));
  await membersA;
  assert.notEqual(place(reads).questions[0].placement.laneId, l11A.inventory.lane_id);
  const destination = calls.find(row => row.laneId === movedLane.inventory.lane_id);
  destination.resolve(F.pageLaneDetail(movedLane, {membersLimit:32}));
  await movedPending;
  const placement = place(reads).questions[0].placement;
  assert.equal(placement.kind, 'lane_goal');
  assert.equal(placement.laneId, movedLane.inventory.lane_id);
  assert.equal(placement.goalId, movedLane.goals[0].goal_id);
});
