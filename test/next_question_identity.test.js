'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const { createQuestionNotifications } = require('../renderer/next/question_notifications');
const { buildQuestionPlacement, createQuestions } = require('../renderer/next/questions');
const { applyWorkLanesFrame, emptyWorkLanesInventory } = require('../pentacle-chat-core/src/services/workLanes.ts');

const notice = (id) => ({ notification_id: `notice-${id}`, producer: 'agent_question.v1', state: 'open', title: `Question ${id}`, question: { question_id: id, state: 'open', producer_stream_id: 'local:chat' } });
const tick = () => new Promise(resolve => setImmediate(resolve));

test('open notifications keep their count and own title while durable identity loads, then join spec by question ID', async () => {
  let resolveList;
  const index = createQuestionNotifications(() => {}, { promptList: () => new Promise(resolve => { resolveList = resolve; }) });
  index.ingest([notice('q1')]);
  await tick();
  const lane = { lane_id: 'lane', title: 'Lane', detail_revision: 'r1', goals: [{ goal_id: 'goal', title: 'Goal' }], progress: { members: [{ spec_id: 'spec', goal_id: 'goal', title: 'Spec' }], members_total: 1 } };
  const project = () => buildQuestionPlacement({ inventory: { lanes: [lane] }, notifications: index.values(), sessions: [{ stream_id: 'local:chat', title: 'Chat' }] });
  assert.equal(project().questions.length, 1);
  assert.equal(project().questions[0].placement.kind, 'unplaced');
  resolveList({ ok: true, questions: [{ question_id: 'q1', state: 'open', spec_id: 'spec' }] });
  await tick();
  assert.equal(project().questions[0].placement.kind, 'lane_goal');
  assert.equal(project().questions[0].placement.goalId, 'goal');
  assert.deepEqual(project().requestedMembers, []);
});

test('same-revision cached goal and member replies remain usable during refresh failure', () => {
  const lane = { lane_id: 'lane', title: 'Lane', detail_revision: 'r1', goals: [], progress: { members: [], members_total: 2 } };
  const reads = { getRead(_id, kind) { return { status: 'failed', detailRevision: 'r1', reply: { projection: { members: [{ spec_id: 'spec', goal_id: 'goal' }], goals: [{ goal_id: 'goal', title: 'Goal' }] } } }; } };
  const item = notice('q1'); item.question.spec_id = 'spec';
  const result = buildQuestionPlacement({ inventory: { lanes: [lane] }, notifications: [item], laneReads: reads });
  assert.equal(result.questions[0].placement.kind, 'lane_goal');
});

test('an unresolved question offers Retry after a failed refresh even when cached members remain available', () => {
  const lane = { lane_id: 'lane', title: 'Lane', detail_revision: 'r1', goals: [], progress: { members: [], members_total: 2 } };
  const reads = { getRead() { return { status: 'failed', detailRevision: 'r1', reply: { projection: { members: [{ spec_id: 'other' }], goals: [] } } }; } };
  const item = notice('q1'); item.question.spec_id = 'missing';
  const result = buildQuestionPlacement({ inventory: { lanes: [lane] }, notifications: [item], laneReads: reads });
  assert.equal(result.questions[0].placement.kind, 'unplaced');
  assert.deepEqual(result.questions[0].placement.retryReads, [{ laneId: 'lane', kind: 'members' }]);
});

test('a production-normalized complete preview places its question without a members read', () => {
  const goalId = 'g_0123456789abcdef01234567';
  const frame = { type: 'work_lanes.inventory', lanes: [{ lane_id: 'lane', title: 'Lane', state: 'active',
    goals: [{ goal_id: goalId, title: 'Goal', rank: 0, state: 'in_progress', done: 0, total: 1 }], goals_total: 1,
    members: [{ spec_id: 'spec', goal_id: goalId, title: 'Spec' }], members_total: 1 }] };
  const inventory = applyWorkLanesFrame(emptyWorkLanesInventory(), frame);
  assert.equal(inventory.lanes[0].progress.members[0].goal_id, goalId);
  const item = notice('q1'); item.question.spec_id = 'spec';
  const result = buildQuestionPlacement({ inventory, notifications: [item] });
  assert.equal(result.questions[0].placement.kind, 'lane_goal');
  assert.deepEqual(result.requestedMembers, []);
});

test('confirmed no-spec identity may use a listed chat heading; lookup failure never guesses', async () => {
  let reply = { ok: false };
  const index = createQuestionNotifications(() => {}, { promptList: async () => reply });
  index.ingest([notice('q1')]);
  await tick();
  const project = () => buildQuestionPlacement({ notifications: index.values(), sessions: [{ stream_id: 'local:chat', visibility: 'default', session_name: 'Chat' }] });
  assert.equal(project().questions[0].placement.kind, 'unplaced');
  reply = { ok: true, questions: [{ question_id: 'q1', state: 'open', spec_id: null }] };
  await index.refreshIdentities();
  assert.equal(project().questions[0].placement.kind, 'chat');
});

test('a failed identity lookup exposes Retry without hiding the question or answer form', async () => {
  let calls = 0;
  let control;
  const dom = new JSDOM('<div id="list"></div>');
  const index = createQuestionNotifications(() => control?.update({ notifications: index.values() }), {
    promptList: async () => ++calls === 1 ? { ok: false } : { ok: true, questions: [{ question_id: 'q1', state: 'open', spec_id: null }] },
  });
  control = createQuestions({ document: dom.window.document, retryQuestionIdentity: () => index.refreshIdentities() });
  control.mountList(dom.window.document.querySelector('#list'));
  index.ingest([notice('q1')]);
  await tick();
  assert.equal(control.getSnapshot().openCount, 1);
  const retry = dom.window.document.querySelector('.next-question-retry');
  assert.ok(retry);
  assert.ok(dom.window.document.querySelector('.next-question-form'));
  retry.click();
  await tick();
  assert.equal(calls, 2);
  assert.equal(dom.window.document.querySelector('.next-question-retry'), null);
  control.dispose();
  dom.window.close();
});

test('an open notification omitted by the identity read stays unplaced and can be retried', async () => {
  const index = createQuestionNotifications(() => {}, { promptList: async () => ({ ok: true, questions: [] }) });
  index.ingest([notice('q1')]);
  await tick();
  const result = buildQuestionPlacement({ notifications: index.values(), sessions: [{ stream_id: 'local:chat', visibility: 'default', title: 'Chat' }] });
  assert.equal(result.questions[0].placement.kind, 'unplaced');
  assert.equal(result.questions[0].placement.retryIdentity, true);
});

test('a late identity reply cannot reopen an answered notification', async () => {
  let resolveList;
  const index = createQuestionNotifications(() => {}, { promptList: () => new Promise(resolve => { resolveList = resolve; }) });
  index.ingest([notice('q1')]);
  await tick();
  index.frame({ type: 'notification', notification: { notification_id: 'notice-q1', state: 'answered', question: { state: 'answered' } } });
  resolveList({ ok: true, questions: [{ question_id: 'q1', state: 'open', spec_id: 'spec' }] });
  await tick();
  assert.equal(buildQuestionPlacement({ notifications: index.values() }).questions.length, 0);
});

test('a new notification arriving during a read is resolved by a follow-up read', async () => {
  const requests = [];
  const index = createQuestionNotifications(() => {}, { promptList: () => new Promise(resolve => requests.push(resolve)) });
  index.ingest([notice('q1')]);
  await tick();
  index.ingest([notice('q2')]);
  requests[0]({ ok: true, questions: [{ question_id: 'q1', state: 'open', spec_id: 'spec-1' }] });
  await tick();
  assert.equal(requests.length, 2);
  assert.equal(index.values().find(item => item.question.question_id === 'q2').question_identity_pending, true);
  requests[1]({ ok: true, questions: [{ question_id: 'q2', state: 'open', spec_id: 'spec-2' }] });
  await tick();
  assert.equal(index.values().find(item => item.question.question_id === 'q2').question.spec_id, 'spec-2');
});

test('actual serialized notifier shape is joined to its durable record before placement', async () => {
  const notification = require('./fixtures/next_question_notification.json');
  assert.equal(Object.hasOwn(notification.question, 'spec_id'), false);
  const id = notification.question.question_id;
  const index = createQuestionNotifications(() => {}, { promptList: async () => ({ ok: true, questions: [{ question_id: id, state: 'open', spec_id: 'spec_gate__next_lanes__07' }] }) });
  index.ingest([notification]);
  await tick();
  const lane = { lane_id: 'lane', title: 'Lane', goals: [{ goal_id: 'goal', title: 'Goal' }], progress: { members: [{ spec_id: 'spec_gate__next_lanes__07', goal_id: 'goal' }], members_total: 1 } };
  const result = buildQuestionPlacement({ inventory: { lanes: [lane] }, notifications: index.values() });
  assert.equal(result.questions[0].placement.kind, 'lane_goal');
});
