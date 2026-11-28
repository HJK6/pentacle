'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createLaneReads } = require('../renderer/next/lane_reads');

const LANE_ID = 'lane_fixture';
const GOAL_ID = 'goal_fixture';
const REV_A = 'a'.repeat(64);
const REV_B = 'b'.repeat(64);
const REV_C = 'c'.repeat(64);

function lane(revision = REV_A, extra = {}) {
  return { lane_id: LANE_ID, detail_revision: revision, goals: [], goals_total: 0, ...extra };
}

function inventory(item) {
  return { lanes: item ? [item] : [] };
}

function member(specId) {
  return { spec_id: specId, status: 'in_progress', summary_line: `Summary ${specId}` };
}

function reply({ revision = REV_A, goals = [], goalsTotal = goals.length, members = [], total = members.length,
  next = null, goalId = null, laneId = LANE_ID } = {}) {
  const summaryMembers = members.map(({ spec_id }) => ({ spec_id }));
  return {
    type: 'work_lanes.show.ok',
    lane: { lane_id: laneId, members: members.map(item => item.spec_id) },
    projection: {
      lane_id: laneId,
      goals,
      goals_total: goalsTotal,
      members: summaryMembers,
      members_total: total,
      members_next: next,
      ...(goalId ? { selected_goal: { goal_id: goalId, title: 'Fixture goal' } } : {}),
    },
    members,
    members_total: total,
    members_next: next,
    detail_revision: revision,
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test('goals read requests one member and accepts only a complete identified projection', async () => {
  const goals = Array.from({ length: 11 }, (_, i) => ({ goal_id: `goal_${i}`, title: `Goal ${i}` }));
  const calls = [];
  const reads = createLaneReads({ show: async (...args) => { calls.push(args); return reply({ goals }); } });
  reads.updateInventory(inventory(lane()));

  const state = await reads.requestGoals(LANE_ID);

  assert.deepEqual(calls, [[LANE_ID, { membersLimit: 1 }]]);
  assert.equal(state.status, 'held');
  assert.equal(state.reply.projection.goals.length, 11);
  assert.equal(state.detailRevision, REV_A);
});

test('goals reads reject mismatched lane identities and malformed detail revisions', async () => {
  let calls = 0;
  const reads = createLaneReads({ show: async () => {
    calls += 1;
    if (calls === 1) return { ...reply(), lane: { lane_id: 'lane_other' } };
    if (calls === 2) return { ...reply(), projection: { ...reply().projection, lane_id: 'lane_other' } };
    if (calls === 3) return reply({ revision: 'not-a-sha256' });
    return reply();
  } });
  reads.updateInventory(inventory(lane()));

  for (let attempt = 1; attempt <= 3; attempt += 1) {
    await reads.requestGoals(LANE_ID);
    assert.equal(reads.getRead(LANE_ID, 'goals').status, 'failed');
    await reads.retry(LANE_ID, 'goals');
    if (attempt < 3) assert.equal(reads.getRead(LANE_ID, 'goals').status, 'failed');
  }
  assert.equal(reads.getRead(LANE_ID, 'goals').status, 'held');
  assert.equal(calls, 4);
});

test('incomplete goal replies stay failed and Retry recovers without treating them as empty', async () => {
  let calls = 0;
  const reads = createLaneReads({ show: async () => {
    calls += 1;
    if (calls === 1) return reply({ goals: [{ goal_id: 'goal_fixture' }], goalsTotal: 2, revision: REV_A });
    if (calls === 2) return reply({ goals: [{ goal_id: 'goal_fixture' }], revision: REV_A });
    throw { error_code: 'work_lane_not_found' };
  } });
  reads.updateInventory(inventory(lane()));

  await reads.requestGoals(LANE_ID);
  assert.equal(reads.getRead(LANE_ID, 'goals').status, 'failed', 'a partial goal list is not a valid empty answer');
  assert.equal(reads.getRead(LANE_ID, 'goals').reply, null);
  await reads.retry(LANE_ID, 'goals');
  assert.equal(reads.getRead(LANE_ID, 'goals').status, 'held');
  const held = reads.getRead(LANE_ID, 'goals').reply;
  await reads.retry(LANE_ID, 'goals');
  assert.equal(reads.getRead(LANE_ID, 'goals').status, 'failed', 'daemon failures stay explicit');
  assert.strictEqual(reads.getRead(LANE_ID, 'goals').reply, held, 'failed refresh retains current held data');
  assert.equal(calls, 3);
});

test('members read pages at 32, validates cursors, and holds a complete aggregate that also serves goals', async () => {
  const members = Array.from({ length: 33 }, (_, i) => member(`spec_fixture__${String(i).padStart(2, '0')}`));
  const goals = [{ goal_id: GOAL_ID, title: 'Fixture goal' }];
  const calls = [];
  const reads = createLaneReads({ show: async (laneId, options) => {
    calls.push([laneId, options]);
    if (calls.length === 1) return reply({ goals, members: members.slice(0, 32), total: 33,
      next: members[31].spec_id });
    return reply({ goals, members: members.slice(32), total: 33 });
  } });
  reads.updateInventory(inventory(lane()));

  const state = await reads.requestMembers(LANE_ID);

  assert.deepEqual(calls, [
    [LANE_ID, { membersLimit: 32 }],
    [LANE_ID, { membersLimit: 32, membersAfter: members[31].spec_id }],
  ]);
  assert.equal(state.status, 'held');
  assert.equal(state.reply.members.length, 33);
  assert.equal(state.reply.projection.members.length, 33);
  assert.equal(state.reply.members_total, 33);
  assert.equal(state.reply.members_next, null);
  assert.strictEqual(reads.getRead(LANE_ID, 'goals').reply, state.reply);
  assert.equal((await reads.requestGoals(LANE_ID)).reply, state.reply);
  assert.equal(calls.length, 2, 'a held members read supplies the complete goals');
});

test('goal read filters every page and holds the selected goal with its complete members', async () => {
  const first = member('spec_fixture__a');
  const second = member('spec_fixture__b');
  const calls = [];
  const reads = createLaneReads({ show: async (laneId, options) => {
    calls.push([laneId, options]);
    return calls.length === 1
      ? reply({ goals: [{ goal_id: GOAL_ID }], members: [first], total: 2, next: first.spec_id, goalId: GOAL_ID })
      : reply({ goals: [{ goal_id: GOAL_ID }], members: [second], total: 2, goalId: GOAL_ID });
  } });
  reads.updateInventory(inventory(lane()));

  const state = await reads.requestGoal(LANE_ID, GOAL_ID);

  assert.deepEqual(calls, [
    [LANE_ID, { membersLimit: 32, goalId: GOAL_ID }],
    [LANE_ID, { membersLimit: 32, goalId: GOAL_ID, membersAfter: first.spec_id }],
  ]);
  assert.equal(state.status, 'held');
  assert.deepEqual(state.reply.members.map(item => item.spec_id), [first.spec_id, second.spec_id]);
  assert.equal(state.reply.projection.selected_goal.goal_id, GOAL_ID);
});

test('bad cursors, duplicate IDs, and count mismatches fail visibly; Retry can recover', async () => {
  let calls = 0;
  const a = member('spec_fixture__a');
  const b = member('spec_fixture__b');
  const reads = createLaneReads({ show: async () => {
    calls += 1;
    if (calls === 1) return reply({ members: [a], total: 2, next: 'spec_fixture__z' });
    if (calls === 2) return reply({ members: [a], total: 2, next: a.spec_id });
    if (calls === 3) return reply({ members: [a], total: 2 });
    if (calls === 4) return reply({ members: [a, b], total: 3 });
    return reply({ members: [a, b], total: 2 });
  } });
  reads.updateInventory(inventory(lane()));

  await reads.requestMembers(LANE_ID);
  assert.equal(reads.getRead(LANE_ID, 'members').status, 'failed');
  assert.equal(reads.getRead(LANE_ID, 'members').reply, null);
  await reads.retry(LANE_ID, 'members');
  assert.equal(reads.getRead(LANE_ID, 'members').status, 'failed', 'repeated page IDs are rejected');
  await reads.retry(LANE_ID, 'members');
  assert.equal(reads.getRead(LANE_ID, 'members').status, 'failed', 'a short aggregate is not held');
  await reads.retry(LANE_ID, 'members');
  assert.equal(reads.getRead(LANE_ID, 'members').status, 'held');
  assert.equal(reads.getRead(LANE_ID, 'members').reply.members.length, 2);
  assert.equal(calls, 5);
});

test('a page revision change restarts once from page one and a second change fails', async () => {
  const a = member('spec_fixture__a');
  const b = member('spec_fixture__b');
  let call = 0;
  const reads = createLaneReads({ show: async () => {
    call += 1;
    if (call === 1) return reply({ members: [a], total: 2, next: a.spec_id, revision: REV_A });
    if (call === 2) return reply({ members: [b], total: 2, revision: REV_B });
    if (call === 3) return reply({ members: [a], total: 2, next: a.spec_id, revision: REV_B });
    return reply({ members: [b], total: 2, revision: REV_C });
  } });
  reads.updateInventory(inventory(lane(REV_A)));

  await reads.requestMembers(LANE_ID);

  assert.equal(call, 4, 'after one restart, a second page revision change fails');
  assert.equal(reads.getRead(LANE_ID, 'members').status, 'failed');
  assert.equal(reads.getRead(LANE_ID, 'members').reply, null);
});

test('an in-flight reply for a superseded frame is discarded and re-read at once', async () => {
  const requests = [];
  const reads = createLaneReads({ show: () => {
    const item = deferred();
    requests.push(item);
    return item.promise;
  } });
  reads.updateInventory(inventory(lane(REV_A)));
  const pending = reads.requestMembers(LANE_ID);
  assert.equal(requests.length, 1);

  reads.updateInventory(inventory(lane(REV_B)));
  assert.equal(requests.length, 2);
  assert.deepEqual(reads.getRead(LANE_ID, 'members'), {
    status: 'loading', reply: null, detailRevision: null, error: null,
  });
  requests[0].resolve(reply({ revision: REV_A, members: [member('spec_fixture__old')] }));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(reads.getRead(LANE_ID, 'members').reply, null, 'old detail is never exposed at the new revision');

  requests[1].resolve(reply({ revision: REV_B, members: [member('spec_fixture__new')] }));
  const state = await pending;
  assert.equal(state.status, 'held');
  assert.equal(state.detailRevision, REV_B);
  assert.deepEqual(state.reply.members.map(item => item.spec_id), ['spec_fixture__new']);
});

test('a goals demand coalesces into the restarted in-flight B members read', async () => {
  const requests = [];
  const reads = createLaneReads({ show: (laneId, options) => {
    const item = deferred();
    requests.push({ laneId, options, ...item });
    return item.promise;
  } });
  reads.updateInventory(inventory(lane(REV_A)));
  const members = reads.requestMembers(LANE_ID);
  reads.updateInventory(inventory(lane(REV_B)));
  const goals = reads.requestGoals(LANE_ID);
  assert.deepEqual(requests.map(row => row.options), [{ membersLimit: 32 }, { membersLimit: 32 }]);
  assert.equal(reads.getRead(LANE_ID, 'goals').status, 'loading');
  requests[0].resolve(reply({ revision: REV_A, goals: [{goal_id: 'old'}] }));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(reads.getRead(LANE_ID, 'goals').reply, null);
  requests[1].resolve(reply({ revision: REV_B, goals: [{ goal_id: GOAL_ID, title: 'Fixture goal' }] }));
  const [membersResult, goalsResult] = await Promise.all([members, goals]);
  assert.equal(membersResult.detailRevision, REV_B);
  assert.equal(goalsResult.detailRevision, REV_B);
  assert.strictEqual(reads.getRead(LANE_ID, 'goals').reply, membersResult.reply);
  assert.equal(requests.length, 2);
});

test('a mismatched reply waits hidden for its exact frame; any other frame restarts the read', async () => {
  const calls = [];
  const reads = createLaneReads({ show: async () => {
    calls.push(calls.length + 1);
    return calls.length === 1
      ? reply({ revision: REV_B, members: [member('spec_fixture__b')] })
      : reply({ revision: REV_C, members: [member('spec_fixture__c')] });
  } });
  reads.updateInventory(inventory(lane(REV_A)));
  const pending = reads.requestMembers(LANE_ID);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(reads.getRead(LANE_ID, 'members').status, 'loading');
  assert.equal(reads.getRead(LANE_ID, 'members').reply, null);

  reads.updateInventory(inventory(lane(REV_C)));
  const state = await pending;
  assert.equal(calls.length, 2);
  assert.equal(state.status, 'held');
  assert.equal(state.detailRevision, REV_C);
  assert.deepEqual(state.reply.members.map(item => item.spec_id), ['spec_fixture__c']);
});

test('a matching revision frame holds a previously hidden candidate', async () => {
  const reads = createLaneReads({ show: async () => reply({ revision: REV_B, members: [member('spec_fixture__b')] }) });
  reads.updateInventory(inventory(lane(REV_A)));
  const pending = reads.requestMembers(LANE_ID);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(reads.getRead(LANE_ID, 'members').reply, null);

  reads.updateInventory(inventory(lane(REV_B)));
  const state = await pending;
  assert.equal(state.status, 'held');
  assert.equal(state.detailRevision, REV_B);
});

test('a presence-only inventory change keeps held data without a read until it is ten minutes old', async () => {
  let clock = 0;
  let calls = 0;
  const reads = createLaneReads({ now: () => clock, show: async () => {
    calls += 1;
    return reply({ members: [member(`spec_fixture__${calls}`)] });
  } });
  reads.updateInventory(inventory(lane(REV_A)));
  await reads.requestMembers(LANE_ID);
  await reads.requestGoals(LANE_ID);
  const original = reads.getRead(LANE_ID, 'members').reply;
  assert.equal(calls, 1, 'the members read also serves goals');

  // Inventory pushes carry presence and working counts, which detail_revision excludes.
  for (let push = 0; push < 20; push++) {
    clock += 30 * 1000;
    reads.updateInventory(inventory(lane(REV_A, { working: push % 2, last_activity: String(clock) })));
  }
  assert.equal(calls, 1, 'twenty presence pushes over ten minutes read nothing');
  assert.equal(reads.getRead(LANE_ID, 'members').status, 'held');
  assert.strictEqual(reads.getRead(LANE_ID, 'members').reply, original);

  // Worked time is also outside the revision, so held data older than ten minutes refreshes once.
  clock += 1000;
  reads.updateInventory(inventory(lane(REV_A, { working: 3 })));
  assert.equal(calls, 2);
  assert.strictEqual(reads.getRead(LANE_ID, 'members').reply, original, 'the refresh keeps showing held data');
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(reads.getRead(LANE_ID, 'members').reply.members.map(item => item.spec_id), ['spec_fixture__2']);
  reads.updateInventory(inventory(lane(REV_A, { working: 4 })));
  assert.equal(calls, 2);

  // A new detail revision still reads at once.
  clock += 1000;
  reads.updateInventory(inventory(lane(REV_B)));
  assert.equal(calls, 3);
});

test('a failed read waits for Retry, a new revision or ten minutes, not for presence pushes', async () => {
  let clock = 0;
  let calls = 0;
  const reads = createLaneReads({ now: () => clock, show: async () => { calls += 1; throw new Error('refused'); } });
  reads.updateInventory(inventory(lane(REV_A)));
  await reads.requestGoals(LANE_ID);
  assert.equal(reads.getRead(LANE_ID, 'goals').status, 'failed');
  for (let push = 0; push < 20; push++) {
    clock += 30 * 1000;
    reads.updateInventory(inventory(lane(REV_A, { working: push })));
  }
  assert.equal(calls, 1, 'presence pushes do not retry a failed read');
  clock += 1000;
  reads.updateInventory(inventory(lane(REV_A, { working: 99 })));
  assert.equal(calls, 2, 'ten minutes on, the next push tries again');
  await new Promise(resolve => setImmediate(resolve));
  reads.updateInventory(inventory(lane(REV_B)));
  assert.equal(calls, 3, 'a new revision tries again at once');
});

test('same-revision entry changes refresh while preserving held data; failure keeps data and Retry recovers', async () => {
  const refreshed = deferred();
  let calls = 0;
  let clock = 0;
  const reads = createLaneReads({ now: () => clock, show: async () => {
    calls += 1;
    if (calls === 1) return reply({ members: [member('spec_fixture__a')] });
    if (calls === 2) return refreshed.promise;
    return reply({ members: [member('spec_fixture__b')] });
  } });
  reads.updateInventory(inventory(lane(REV_A)));
  await reads.requestMembers(LANE_ID);
  const original = reads.getRead(LANE_ID, 'members').reply;

  clock += 10 * 60 * 1000 + 1000;
  reads.updateInventory(inventory(lane(REV_A, { activity: 'working' })));
  const loading = reads.getRead(LANE_ID, 'members');
  assert.equal(loading.status, 'loading');
  assert.strictEqual(loading.reply, original);
  refreshed.reject(new Error('temporary read failure'));
  await new Promise(resolve => setImmediate(resolve));
  const failed = reads.getRead(LANE_ID, 'members');
  assert.equal(failed.status, 'failed');
  assert.strictEqual(failed.reply, original);

  await reads.retry(LANE_ID, 'members');
  assert.equal(reads.getRead(LANE_ID, 'members').status, 'held');
  assert.deepEqual(reads.getRead(LANE_ID, 'members').reply.members.map(item => item.spec_id), ['spec_fixture__b']);
  assert.equal(calls, 3);
});

test('held data is dropped on a new detail revision and on lane removal', async () => {
  let calls = 0;
  const reads = createLaneReads({ show: async (laneId) => {
    calls += 1;
    return reply({ revision: calls === 1 ? REV_A : REV_B, members: [member(`spec_fixture__${calls}`)] });
  } });
  reads.updateInventory(inventory(lane(REV_A)));
  await reads.requestMembers(LANE_ID);

  reads.updateInventory(inventory(lane(REV_B)));
  assert.equal(reads.getRead(LANE_ID, 'members').reply, null);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(reads.getRead(LANE_ID, 'members').status, 'held');
  assert.equal(reads.getRead(LANE_ID, 'members').detailRevision, REV_B);

  reads.updateInventory(inventory(null));
  assert.deepEqual(reads.getRead(LANE_ID, 'members'), {
    status: 'idle', reply: null, detailRevision: null, error: null,
  });
});

test('subscribers are notified for loading, held, failed, and retry transitions', async () => {
  let calls = 0;
  const reads = createLaneReads({ show: async () => {
    calls += 1;
    if (calls === 1) throw new Error('offline');
    return reply({ members: [] });
  } });
  reads.updateInventory(inventory(lane()));
  let notifications = 0;
  const unsubscribe = reads.subscribe(() => { notifications += 1; });

  await reads.requestMembers(LANE_ID);
  assert.equal(reads.getRead(LANE_ID, 'members').status, 'failed');
  await reads.retry(LANE_ID, 'members');
  assert.equal(reads.getRead(LANE_ID, 'members').status, 'held');
  assert.ok(notifications >= 4);
  unsubscribe();
  const before = notifications;
  reads.updateInventory(inventory(lane(REV_B)));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(notifications, before);
});

test('presence pushes do not restart an in-flight paged read', async () => {
  const calls = [];
  const reads = createLaneReads({ show: (_laneId, options) => {
    const item = deferred(); calls.push({ options, ...item }); return item.promise;
  } });
  reads.updateInventory(inventory(lane(REV_A, { working: 0 })));
  const pending = reads.requestMembers(LANE_ID);
  const first = member('spec_fixture__a');
  calls[0].resolve(reply({ members: [first], total: 2, next: first.spec_id }));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, 2);
  for (let push = 1; push <= 4; push++) reads.updateInventory(inventory(lane(REV_A, { working: push })));
  assert.equal(calls.length, 2, 'unchanged detail does not start page one again');
  calls[1].resolve(reply({ members: [member('spec_fixture__b')], total: 2 }));
  const state = await pending;
  assert.equal(state.status, 'held');
  assert.deepEqual(state.reply.members.map(item => item.spec_id), ['spec_fixture__a', 'spec_fixture__b']);
});

test('presence pushes preserve a reply waiting for its matching revision', async () => {
  let calls = 0;
  const reads = createLaneReads({ show: async () => { calls++; return reply({ revision: REV_B, members: [member('spec_fixture__b')] }); } });
  reads.updateInventory(inventory(lane(REV_A, { working: 0 })));
  const pending = reads.requestMembers(LANE_ID);
  await new Promise(resolve => setImmediate(resolve));
  reads.updateInventory(inventory(lane(REV_A, { working: 1 })));
  assert.equal(calls, 1, 'unchanged detail continues waiting');
  reads.updateInventory(inventory(lane(REV_B, { working: 2 })));
  assert.equal((await pending).detailRevision, REV_B);
  assert.equal(calls, 1);
});
