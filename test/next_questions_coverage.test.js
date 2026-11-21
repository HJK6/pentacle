'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const { createQuestions } = require('../renderer/next/questions');

function notice(id, specId, overrides = {}) {
  const question = {
    question_id: id,
    state: 'open',
    spec_id: specId,
    producer_stream_id: 'fixture-chat',
    response_mode: 'single_choice',
    options: [{ label: 'Keep', value: 'keep' }, { label: 'Change', value: 'change' }],
    ...overrides.question,
  };
  const { question: _ignored, ...notificationOverrides } = overrides;
  return {
    notification_id: `notice-${id}`,
    producer: 'agent_question.v1',
    state: 'open',
    title: `Question ${id}`,
    body: `Prompt ${id}`,
    actions: [{ kind: 'yes_no' }],
    ...notificationOverrides,
    question,
  };
}

function member(specId, goalId = null, title = `Spec ${specId}`) {
  return { spec_id: specId, goal_id: goalId, title };
}

function lane(id, members, overrides = {}) {
  return {
    lane_id: id,
    title: `Lane ${id}`,
    icon: 'flask',
    detail_revision: 'rev-a',
    members,
    members_total: members.length,
    goals: [],
    goals_total: 0,
    ...overrides,
  };
}

function inventory(lanes, truncated = false) {
  return { lanes, truncated, counts: { open: lanes.length } };
}

function heldReply(laneId, members = [], goals = []) {
  return {
    type: 'work_lanes.show.ok',
    lane: { lane_id: laneId },
    projection: { lane_id: laneId, members, goals, members_total: members.length },
  };
}

function readStore(initial = {}) {
  const states = new Map(Object.entries(initial));
  const requested = [];
  const retries = [];
  const listeners = new Set();
  const key = (laneId, kind, goalId = '') => `${laneId}:${kind}:${goalId || ''}`;
  return {
    requested,
    retries,
    getRead(laneId, kind, goalId) {
      return states.get(key(laneId, kind, goalId)) || { status: 'idle' };
    },
    requestMembers(laneId) {
      requested.push([laneId, 'members']);
      states.set(key(laneId, 'members'), { status: 'loading' });
    },
    requestGoals(laneId) {
      requested.push([laneId, 'goals']);
      states.set(key(laneId, 'goals'), { status: 'loading' });
    },
    retry(laneId, kind, goalId) {
      retries.push([laneId, kind, goalId]);
      states.set(key(laneId, kind, goalId), { status: 'loading' });
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    setRead(laneId, kind, value, goalId) {
      states.set(key(laneId, kind, goalId), value);
      for (const listener of listeners) listener();
    },
  };
}

function mounts() {
  const dom = new JSDOM('<!doctype html><html><body><div id="list"></div><div id="lane"></div><div id="goal"></div><div id="spec"></div><div id="said"></div></body></html>');
  return { dom, document: dom.window.document };
}

async function flush(turns = 8) {
  for (let i = 0; i < turns; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

test('U4: a question before the first inventory frame stays unplaced, then moves into the lane when that frame arrives', () => {
  const q = notice('late-frame', 'spec-late');
  const { document, dom } = mounts();
  const reads = readStore();
  const questions = createQuestions({
    document,
    notifications: [q],
    sessions: [{ stream_id: 'fixture-chat', visibility: 'default', title: 'Synthetic chat' }],
    laneReads: reads,
  });
  questions.mountList(document.getElementById('list'));

  let item = questions.getSnapshot().list[0];
  assert.equal(item.placement.kind, 'unplaced');
  assert.deepEqual(item.placement.retryReads, []);
  assert.equal(document.querySelector('.next-question-heading-title').textContent, 'Question late-frame');
  assert.equal(document.querySelector('.next-question-chat-link'), null);

  questions.update({ inventory: inventory([lane('late-lane', [member('spec-late')])]) });
  item = questions.getSnapshot().list[0];
  assert.equal(item.placement.kind, 'lane');
  assert.equal(document.querySelector('.next-question-heading-link .next-question-lane-title').textContent, 'Lane late-lane');
  assert.deepEqual(reads.requested, []);

  questions.dispose();
  dom.window.close();
});

test('U4 and U5: a complete eight-member preview places a lane-only question without a read and draws its lane and spec cards', () => {
  const members = Array.from({ length: 8 }, (_, index) => member(`spec-${index + 1}`));
  members[3] = member('spec-lane-only', null, 'Synthetic no-goal spec');
  const reads = readStore();
  const { document, dom } = mounts();
  const questions = createQuestions({
    document,
    inventory: inventory([lane('eight-member-lane', members, { members_total: 8 })]),
    notifications: [notice('lane-only', 'spec-lane-only')],
    laneReads: reads,
  });
  questions.mountList(document.getElementById('list'));
  questions.mountCards(document.getElementById('lane'), { level: 'lane', laneId: 'eight-member-lane', noGoal: true });
  questions.mountCards(document.getElementById('spec'), { level: 'spec', specId: 'spec-lane-only' });

  const item = questions.getSnapshot().list[0];
  assert.equal(item.placement.kind, 'lane');
  assert.equal(document.querySelector('.next-question-heading-link .next-question-lane-title').textContent, 'Lane eight-member-lane');
  assert.equal(document.querySelector('.next-question-goal-title'), null);
  assert.equal(document.querySelector('#lane .next-question-card').dataset.questionId, 'lane-only');
  assert.equal(document.querySelector('#spec .next-question-card').dataset.questionId, 'lane-only');
  assert.deepEqual(reads.requested, []);

  questions.dispose();
  dom.window.close();
});

test('U4 and U5: a previewed member with an unsent goal waits for the goal read, shows Retry on failure, then has lane and goal cards', () => {
  const reads = readStore();
  const laneValue = lane('goal-lane', [member('spec-goal', 'goal-hidden', 'Synthetic spec')], {
    goals_total: 1,
    members_total: 1,
  });
  const { document, dom } = mounts();
  const questions = createQuestions({
    document,
    inventory: inventory([laneValue]),
    notifications: [notice('goal-question', 'spec-goal')],
    laneReads: reads,
  });
  questions.mountList(document.getElementById('list'));

  let item = questions.getSnapshot().list[0];
  assert.equal(item.placement.kind, 'unplaced');
  assert.deepEqual(reads.requested, [['goal-lane', 'goals']]);
  assert.deepEqual(item.placement.retryReads, []);
  reads.setRead('goal-lane', 'goals', { status: 'failed', error: 'synthetic read failure' });
  item = questions.getSnapshot().list[0];
  assert.equal(item.placement.kind, 'unplaced');
  assert.deepEqual(item.placement.retryReads, [{ laneId: 'goal-lane', kind: 'goals' }]);
  assert.equal(document.querySelector('.next-question-heading-title').textContent, 'Question goal-question');
  assert.ok(document.querySelector('.next-question-retry'));
  document.querySelector('.next-question-retry').click();
  assert.deepEqual(reads.retries, [['goal-lane', 'goals', undefined]]);

  reads.setRead('goal-lane', 'goals', {
    status: 'held',
    detailRevision: 'rev-a',
    reply: heldReply('goal-lane', [], [{ goal_id: 'goal-hidden', title: 'Synthetic goal' }]),
  });
  item = questions.getSnapshot().list[0];
  assert.equal(item.placement.kind, 'lane_goal');
  assert.equal(item.placement.goalTitle, 'Synthetic goal');
  assert.equal(document.querySelector('.next-question-heading-link .next-question-lane-title').textContent, 'Lane goal-lane');
  assert.equal(document.querySelector('.next-question-goal-title').textContent, 'Synthetic goal');
  assert.equal(document.querySelector('.next-question-retry'), null);

  questions.mountCards(document.getElementById('lane'), { level: 'lane', laneId: 'goal-lane' });
  questions.mountCards(document.getElementById('goal'), { level: 'goal', laneId: 'goal-lane', goalId: 'goal-hidden' });
  assert.equal(document.querySelector('#lane .next-question-card').dataset.questionId, 'goal-question');
  assert.equal(document.querySelector('#goal .next-question-card').dataset.questionId, 'goal-question');

  questions.dispose();
  dom.window.close();
});

test('U4: a truncated frame with every lane read held and no match leaves the spec unplaced, with its own title and no Retry', () => {
  const first = lane('held-lane-a', [member('spec-preview-a')], {
    detail_revision: 'rev-a',
    members_total: 9,
  });
  const second = lane('held-lane-b', [], {
    detail_revision: 'rev-b',
    members_total: 4,
    state: 'paused',
    parked: { reason: 'synthetic fixture' },
  });
  const reads = readStore({
    'held-lane-a:members:': {
      status: 'held', detailRevision: 'rev-a',
      reply: heldReply('held-lane-a', [member('spec-preview-a')]),
    },
    'held-lane-b:members:': {
      status: 'held', detailRevision: 'rev-b',
      reply: heldReply('held-lane-b', []),
    },
  });
  const { document, dom } = mounts();
  const questions = createQuestions({
    document,
    inventory: inventory([first, second], true),
    notifications: [notice('outside-all-lanes', 'spec-nowhere')],
    sessions: [{ stream_id: 'fixture-chat', visibility: 'default', title: 'Synthetic chat' }],
    laneReads: reads,
  });
  questions.mountList(document.getElementById('list'));

  const item = questions.getSnapshot().list[0];
  assert.equal(item.placement.kind, 'unplaced');
  assert.deepEqual(item.placement.retryReads, []);
  assert.equal(item.placement.retryLaneIds.length, 0);
  assert.equal(document.querySelector('.next-question-heading-title').textContent, 'Question outside-all-lanes');
  assert.equal(document.querySelector('.next-question-chat-link'), null);
  assert.equal(document.querySelector('.next-question-retry'), null);
  assert.deepEqual(reads.requested, []);

  questions.dispose();
  dom.window.close();
});

test('U4: Retry searches the one failed lane after held reads in other lanes find no member', () => {
  const failedLane = lane('retry-lane', [member('spec-preview')], { members_total: 12 });
  const heldLane = lane('already-read-lane', [member('another-preview')], { members_total: 12 });
  const reads = readStore({
    'retry-lane:members:': { status: 'failed', error: 'synthetic failure' },
    'already-read-lane:members:': {
      status: 'held', detailRevision: 'rev-a',
      reply: heldReply('already-read-lane', [member('another-preview')]),
    },
  });
  const { document, dom } = mounts();
  const questions = createQuestions({
    document,
    inventory: inventory([failedLane, heldLane]),
    notifications: [notice('retry-found', 'spec-retry-found')],
    laneReads: reads,
  });
  questions.mountList(document.getElementById('list'));

  let item = questions.getSnapshot().list[0];
  assert.equal(item.placement.kind, 'unplaced');
  assert.deepEqual(item.placement.retryReads, [{ laneId: 'retry-lane', kind: 'members' }]);
  document.querySelector('.next-question-retry').click();
  assert.deepEqual(reads.retries, [['retry-lane', 'members', undefined]]);

  reads.setRead('retry-lane', 'members', {
    status: 'held',
    detailRevision: 'rev-a',
    reply: heldReply('retry-lane', [member('spec-retry-found')]),
  });
  item = questions.getSnapshot().list[0];
  assert.equal(item.placement.kind, 'lane');
  assert.equal(item.placement.laneId, 'retry-lane');
  assert.equal(document.querySelector('.next-question-retry'), null);
  assert.deepEqual(reads.requested, []);

  questions.dispose();
  dom.window.close();
});

test('U4: changing detail_revision discards a held member read and a superseded reply cannot place the question in its former lane', () => {
  const revisionA = lane('changing-lane', [member('spec-preview')], {
    detail_revision: 'rev-a',
    members_total: 2,
  });
  const reads = readStore({
    'changing-lane:members:': {
      status: 'held', detailRevision: 'rev-a',
      reply: heldReply('changing-lane', [member('spec-moved')]),
    },
  });
  const { document, dom } = mounts();
  const questions = createQuestions({
    document,
    inventory: inventory([revisionA]),
    notifications: [notice('moved-question', 'spec-moved')],
    laneReads: reads,
  });
  questions.mountList(document.getElementById('list'));
  assert.equal(questions.getSnapshot().list[0].placement.kind, 'lane');
  assert.equal(questions.getSnapshot().list[0].placement.laneId, 'changing-lane');

  questions.update({
    inventory: inventory([lane('changing-lane', [member('spec-preview')], {
      detail_revision: 'rev-b',
      members_total: 2,
    })]),
  });
  let item = questions.getSnapshot().list[0];
  assert.equal(item.placement.kind, 'unplaced');
  assert.equal(item.placement.retryReads.length, 0);

  reads.setRead('changing-lane', 'members', {
    status: 'held',
    detailRevision: 'rev-a',
    reply: heldReply('changing-lane', [member('spec-moved')]),
  });
  item = questions.getSnapshot().list[0];
  assert.equal(item.placement.kind, 'unplaced');
  assert.equal(item.placement.laneId, undefined);
  assert.equal(document.querySelector('.next-question-heading-title').textContent, 'Question moved-question');
  assert.equal(document.querySelector('.next-question-retry'), null);

  questions.dispose();
  dom.window.close();
});

test('U5 mock journey checks: the Questions count shows four waiting, same-spec questions page together, and a chat heading has no question mark', () => {
  const q1 = notice('shared-one', 'spec-shared');
  const q2 = notice('shared-two', 'spec-shared');
  const q3 = notice('listed-chat', null, { question: { producer_stream_id: 'fixture-chat' } });
  const q4 = notice('unplaced-fourth', 'spec-not-listed');
  const duplicateQ1 = { ...q1, notification_id: 'notice-shared-one-again' };
  const { document, dom } = mounts();
  const questions = createQuestions({
    document,
    inventory: inventory([lane('shared-lane', [member('spec-shared')])]),
    notifications: [q1, q2, q3, q4, duplicateQ1],
    sessions: [{ stream_id: 'fixture-chat', visibility: 'default', title: 'Synthetic chat' }],
  });
  questions.mountList(document.getElementById('list'));
  questions.mountCards(document.getElementById('spec'), { level: 'spec', specId: 'spec-shared' });

  const snapshot = questions.getSnapshot();
  assert.equal(snapshot.openCount, 4);
  assert.equal(snapshot.list.length, 4);
  assert.equal(document.querySelectorAll('.next-question-list-item').length, 4);
  const chatHeading = [...document.querySelectorAll('.next-question-chat-link')][0];
  assert.equal(chatHeading.textContent, 'Synthetic chat');
  assert.equal(chatHeading.textContent.includes('?'), false);
  assert.equal(chatHeading.querySelector('.next-question-chat-mark'), null);

  assert.equal(document.querySelector('#spec .next-question-card').dataset.questionId, 'shared-one');
  assert.equal(document.querySelector('#spec .next-question-card-label').textContent, 'Question 1 of 2');
  document.querySelector('#spec .next-question-next').click();
  assert.equal(document.querySelector('#spec .next-question-card').dataset.questionId, 'shared-two');
  assert.equal(document.querySelector('#spec .next-question-card-label').textContent, 'Question 2 of 2');

  questions.dispose();
  dom.window.close();
});

test('U5: an acknowledge-only question sends the acknowledge action with its synthetic option', async () => {
  const calls = [];
  const q = notice('ack-only', 'spec-ack', {
    actions: [],
    question: { response_mode: 'ack', options: [] },
  });
  const { document, dom } = mounts();
  const questions = createQuestions({
    document,
    notifications: [q],
    cc: {
      notificationResolve: async (...args) => { calls.push(args); return { ok: true }; },
    },
    receiptMs: 5000,
  });
  questions.mountList(document.getElementById('list'));

  const option = document.querySelector('.next-question-option');
  assert.equal(option.textContent, 'Acknowledge');
  option.click();
  document.querySelector('.next-question-send').click();
  await flush();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1], 'ack');
  assert.deepEqual(calls[0][2], { selections: ['ack'], submit: true });
  assert.equal(document.querySelector('.next-question-sent-answer').textContent, 'Acknowledge');

  questions.dispose();
  dom.window.close();
});
