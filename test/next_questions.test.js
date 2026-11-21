'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const {
  buildQuestionPlacement,
  createQuestions,
  RECEIPT_MS,
  latestAnsweredRecord,
  storedAnswerText,
  uniqueOpenNotifications,
} = require('../renderer/next/questions');

function notification(id, overrides = {}) {
  return {
    notification_id: `notice-${id}`,
    producer: 'agent_question.v1',
    state: 'open',
    title: `Question ${id}`,
    body: `Prompt ${id}`,
    actions: [{ kind: 'yes_no' }],
    question: {
      question_id: id,
      state: 'open',
      producer_stream_id: 'stream-chat',
      response_mode: 'single_choice',
      options: [{ label: 'Keep', value: 'keep' }, { label: 'Change', value: 'change' }],
    },
    ...overrides,
  };
}

function lane(id, members, goals = [], overrides = {}) {
  return {
    lane_id: id,
    title: `Lane ${id}`,
    icon: 'flask',
    detail_revision: 'rev-1',
    members,
    members_total: members.length,
    goals,
    goals_total: goals.length,
    ...overrides,
  };
}

function member(specId, goalId = null, title = `Spec ${specId}`, extra = {}) {
  return { spec_id: specId, goal_id: goalId, title, ...extra };
}

function inventory(lanes) {
  return { lanes, truncated: false, counts: { open: lanes.length } };
}

function heldReply(laneId, members = [], goals = []) {
  return {
    type: 'work_lanes.show.ok',
    lane: { lane_id: laneId },
    projection: { lane_id: laneId, members, goals, members_total: members.length },
    detail_revision: 'rev-1',
  };
}

function createReads(initial = {}) {
  const states = new Map(Object.entries(initial));
  const requested = [];
  const retries = [];
  const listeners = new Set();
  const key = (laneId, kind, goalId = '') => `${laneId}:${kind}:${goalId || ''}`;
  return {
    requested,
    retries,
    getRead(laneId, kind, goalId) { return states.get(key(laneId, kind, goalId)) || { status: 'idle' }; },
    requestMembers(laneId) { requested.push([laneId, 'members']); states.set(key(laneId, 'members'), { status: 'loading' }); },
    requestGoals(laneId) { requested.push([laneId, 'goals']); states.set(key(laneId, 'goals'), { status: 'loading' }); },
    retry(laneId, kind, goalId) { retries.push([laneId, kind, goalId]); states.set(key(laneId, kind, goalId), { status: 'loading' }); },
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    setRead(laneId, kind, read, goalId) {
      states.set(key(laneId, kind, goalId), read);
      for (const listener of listeners) listener();
    },
  };
}

function docWithMounts() {
  const dom = new JSDOM('<!doctype html><html><body><div id="list"></div><div id="lane"></div><div id="said"></div></body></html>');
  return { dom, document: dom.window.document };
}

function openItem(overrides = {}) {
  const { question: questionOverrides = {}, ...notificationOverrides } = overrides;
  return notification('question-a', {
    ...notificationOverrides,
    question: {
      ...notification('question-a').question,
      spec_id: 'spec-a',
      ...questionOverrides,
    },
  });
}

function answeredRecord(id, answeredAt, selections, options = [
  { label: 'Hold', value: 'hold' }, { label: 'Allow release', value: 'release' },
]) {
  return {
    question_id: id,
    state: 'answered',
    answered_at: answeredAt,
    answer: { selections },
    envelope: { spec_id: 'spec-a', options },
  };
}

async function flush(turns = 8) {
  for (let i = 0; i < turns; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

test('question identity is question_id and duplicate frames count once', () => {
  const older = notification('same-id', { title: 'Earlier title' });
  const newer = notification('same-id', { title: 'Later title' });
  const closed = notification('closed', { state: 'answered' });
  const unique = uniqueOpenNotifications([older, newer, closed]);
  assert.equal(unique.length, 1);
  assert.equal(unique[0].title, 'Later title');
});

test('a question is open only when both notification and embedded question states are open', () => {
  const embeddedAnswered = notification('embedded-answered', {
    state: 'open',
    question: { ...notification('embedded-answered').question, state: 'answered' },
  });
  const notificationAnswered = notification('notification-answered', {
    state: 'answered',
    question: { ...notification('notification-answered').question, state: 'open' },
  });
  const fullyOpen = notification('fully-open', {
    state: 'open',
    question: { ...notification('fully-open').question, state: 'open' },
  });
  assert.deepEqual(uniqueOpenNotifications([embeddedAnswered, notificationAnswered, fullyOpen]).map((item) => item.question.question_id), ['fully-open']);
});

test('questions arriving before inventory stay unplaced and never use the asking chat heading', () => {
  const specQuestion = openItem({ question: { producer_stream_id: 'stream-chat' } });
  const noSpecQuestion = notification('question-no-spec', {
    question: { ...notification('question-no-spec').question, spec_id: undefined, producer_stream_id: 'stream-chat' },
  });
  const { document, dom } = docWithMounts();
  const questions = createQuestions({
    document,
    notifications: [specQuestion, noSpecQuestion],
    sessions: [{ stream_id: 'stream-chat', visibility: 'default', display_name: 'Workspace chat' }],
  });
  const snapshot = questions.getSnapshot();
  assert.equal(snapshot.openCount, 2);
  assert.equal(snapshot.list[0].placement.kind, 'unplaced');
  assert.equal(snapshot.list[1].placement.kind, 'chat');
  questions.mountList(document.getElementById('list'));
  const rows = [...document.querySelectorAll('.next-question-list-item')];
  assert.equal(rows[0].querySelector('.next-question-heading-title').textContent, 'Question question-a');
  assert.equal(rows[0].querySelector('.next-question-chat-link'), null);
  assert.equal(rows[1].querySelector('.next-question-chat-link').textContent, 'Workspace chat');
  questions.dispose();
  dom.window.close();
});

test('a parked lane with no preview question marks is searched for an off-preview member', () => {
  const parked = lane('parked', [member('spec-first')], [{ goal_id:'goal-later', title:'Later goal' }], {
    state:'paused', parked:{reason:'fixture'}, members_total:12, open_questions:{count:0}, activity:null,
  });
  const reads = createReads();
  const q = openItem({ question:{spec_id:'spec-hidden'} });
  const { document, dom } = docWithMounts();
  const questions = createQuestions({ document, inventory:inventory([parked]), notifications:[q], laneReads:reads });
  assert.equal(questions.getSnapshot().list[0].placement.kind, 'unplaced');
  assert.deepEqual(reads.requested, [['parked','members']]);
  reads.setRead('parked','members',{status:'held',detailRevision:'rev-1',reply:heldReply('parked',[member('spec-hidden','goal-later')])});
  assert.equal(questions.getSnapshot().list[0].placement.goalTitle, 'Later goal');
  questions.dispose(); dom.window.close();
});

test('a later complete inventory can place a question without treating an earlier preview as a negative', () => {
  const oldLane = lane('lane-a',[member('spec-existing')],[],{detail_revision:'rev-a'});
  const q = openItem({ question:{spec_id:'spec-arrives'} });
  const { document, dom } = docWithMounts();
  const questions = createQuestions({ document, inventory:inventory([oldLane]), notifications:[q], laneReads:createReads() });
  assert.equal(questions.getSnapshot().list[0].placement.kind, 'unplaced');
  questions.update({inventory:inventory([{...oldLane,detail_revision:'rev-b',members:[...oldLane.members,member('spec-arrives')],members_total:2}])});
  assert.equal(questions.getSnapshot().list[0].placement.kind, 'lane');
  questions.dispose(); dom.window.close();
});

test('placement uses previewed members and waits for held current-revision reads for hidden members and goals', () => {
  const first = lane('lane-a', [member('spec-preview', 'goal-visible')], [
    { goal_id: 'goal-visible', title: 'Visible goal' },
  ]);
  const capped = lane('lane-b', [member('spec-other', null)], [], {
    members_total: 12,
    goals_total: 11,
    open_questions: { count: 0 },
  });
  const reads = createReads();
  const notifications = [
    openItem({ question: { spec_id: 'spec-preview' } }),
    notification('question-hidden', { question: { ...notification('question-hidden').question, spec_id: 'spec-hidden' } }),
  ];
  const result = buildQuestionPlacement({ inventory: inventory([first, capped]), notifications, laneReads: reads });
  assert.equal(result.questions[0].placement.kind, 'lane_goal');
  assert.equal(result.questions[0].placement.goalTitle, 'Visible goal');
  assert.equal(result.questions[1].placement.kind, 'unplaced');
  assert.deepEqual(result.requestedMembers, ['lane-b']);

  const heldMembers = createReads();
  const domData = docWithMounts();
  const questions = createQuestions({
    document: domData.document,
    notifications: [notification('question-hidden', { question: { ...notification('question-hidden').question, spec_id: 'spec-hidden' } })],
    inventory: inventory([capped]),
    laneReads: heldMembers,
  });
  assert.equal(questions.getSnapshot().list[0].placement.kind, 'unplaced');
  heldMembers.setRead('lane-b', 'members', {
    status: 'held', detailRevision: 'rev-1', reply: heldReply('lane-b', [member('spec-hidden', 'goal-hidden')]),
  });
  assert.equal(questions.getSnapshot().list[0].placement.kind, 'unplaced');
  assert.deepEqual(heldMembers.requested.filter(([, kind]) => kind === 'goals'), [['lane-b', 'goals']]);
  heldMembers.setRead('lane-b', 'goals', {
    status: 'held', detailRevision: 'rev-1', reply: heldReply('lane-b', [], [{ goal_id: 'goal-hidden', title: 'Hidden goal' }]),
  });
  const placed = questions.getSnapshot().list[0];
  assert.equal(placed.placement.kind, 'lane_goal');
  assert.equal(placed.placement.goalTitle, 'Hidden goal');
  questions.dispose();
  domData.dom.window.close();
});

test('failed placement reads expose only their matching Retry and a complete no-match stays unplaced', () => {
  const failedLane = lane('lane-failed', [member('spec-preview', null)], [], { members_total: 9 });
  const reads = createReads({
    'lane-failed:members:': { status: 'failed', error: 'read failed' },
  });
  const q = notification('question-unplaced', { question: { ...notification('question-unplaced').question, spec_id: 'spec-elsewhere' } });
  const { document, dom } = docWithMounts();
  const questions = createQuestions({ document, inventory: inventory([failedLane]), notifications: [q], laneReads: reads });
  const item = questions.getSnapshot().list[0];
  assert.equal(item.placement.kind, 'unplaced');
  assert.deepEqual(item.placement.retryReads, [{ laneId: 'lane-failed', kind: 'members' }]);
  questions.mountList(document.getElementById('list'));
  const retry = document.querySelector('.next-question-retry');
  assert.ok(retry);
  retry.click();
  assert.deepEqual(reads.retries, [['lane-failed', 'members', undefined]]);

  reads.setRead('lane-failed', 'members', { status: 'held', detailRevision: 'rev-1', reply: heldReply('lane-failed', [member('spec-preview', null)]) });
  assert.equal(questions.getSnapshot().list[0].placement.kind, 'unplaced');
  assert.deepEqual(questions.getSnapshot().list[0].placement.retryReads, []);
  assert.equal(document.querySelector('.next-question-retry'), null);
  questions.dispose();
  dom.window.close();
});

test('lane and goal headings open the spec, and the same projection feeds lane, goal, and spec cards', () => {
  const q = openItem();
  const data = inventory([lane('lane-a', [member('spec-a', 'goal-a')], [{ goal_id: 'goal-a', title: 'Goal A' }])]);
  const opened = [];
  const { document, dom } = docWithMounts();
  const questions = createQuestions({
    document, inventory: data, notifications: [q],
    onOpenSpec: (id) => opened.push(id),
    renderLaneIcon: (laneValue, doc) => { const icon = doc.createElement('i'); icon.dataset.icon = laneValue.icon; return icon; },
  });
  questions.mountList(document.getElementById('list'));
  const heading = document.querySelector('.next-question-heading-link');
  assert.ok(heading.querySelector('[data-icon="flask"]'));
  assert.equal(heading.querySelector('strong').textContent, 'Lane lane-a');
  assert.equal(heading.querySelector('.next-question-goal-title').textContent, 'Goal A');
  heading.click();
  assert.deepEqual(opened, ['spec-a']);

  questions.mountCards(document.getElementById('lane'), { level: 'lane', laneId: 'lane-a' });
  assert.equal(document.querySelector('#lane .next-question-card').dataset.questionId, 'question-a');
  questions.mountCards(document.createElement('div'), { level: 'goal', laneId: 'lane-a', goalId: 'goal-a' });
  const specMount = document.createElement('div'); document.body.appendChild(specMount);
  questions.mountCards(specMount, { level: 'spec', specId: 'spec-a' });
  assert.equal(specMount.querySelector('.next-question-card').dataset.questionId, 'question-a');
  assert.equal(document.querySelectorAll('.next-question-skip,.next-question-voice').length, 0);
  questions.dispose();
  dom.window.close();
});

test('a no-spec question links to a listed chat only; unlisted and spec-unplaced questions use their own title', () => {
  const listed = notification('question-listed', { question: { ...notification('question-listed').question, spec_id: null, producer_stream_id: 'stream-visible' } });
  const unlisted = notification('question-unlisted', { question: { ...notification('question-unlisted').question, spec_id: null, producer_stream_id: 'stream-hidden' } });
  const specUnplaced = notification('question-spec', { question: { ...notification('question-spec').question, spec_id: 'spec-missing', producer_stream_id: 'stream-visible' } });
  const { document, dom } = docWithMounts();
  const opened = [];
  const questions = createQuestions({
    document,
    notifications: [listed, unlisted, specUnplaced],
    sessions: [{ stream_id: 'stream-visible', visibility: 'default', title: 'Listed synthetic chat' }, { stream_id: 'stream-hidden', visibility: 'hidden', title: 'Hidden synthetic chat' }],
    onOpenChat: (id) => opened.push(id),
  });
  questions.mountList(document.getElementById('list'));
  const rows = [...document.querySelectorAll('.next-question-list-item')];
  assert.equal(rows[0].querySelector('.next-question-chat-link').textContent, 'Listed synthetic chat');
  rows[0].querySelector('.next-question-chat-link').click();
  assert.deepEqual(opened, ['stream-visible']);
  assert.equal(rows[1].querySelector('.next-question-chat-link'), null);
  assert.equal(rows[1].querySelector('.next-question-heading-title').textContent, 'Question question-unlisted');
  assert.equal(rows[2].querySelector('.next-question-chat-link'), null);
  assert.equal(rows[2].querySelector('.next-question-heading-title').textContent, 'Question question-spec');
  questions.dispose();
  dom.window.close();
});

test('one answer is selected at a time across mounts; typed words are preserved, dimmed, and reactivated by focus', async () => {
  const calls = [];
  let promptReads = 0;
  const q = openItem({ question: { allow_custom: true } });
  const { document, dom } = docWithMounts();
  const questions = createQuestions({
    document, inventory: inventory([lane('lane-a', [member('spec-a', null)])]), notifications: [q],
    cc: {
      notificationResolve: async (...args) => { calls.push(args); return { ok: true }; },
      promptList: async () => {
        promptReads += 1;
        return { ok: true, questions: promptReads === 1 ? [] : [{
          question_id: 'question-a', state: 'answered', answered_at: '2026-11-20T10:00:00Z',
          answer: { custom_text: 'write a sentence' }, envelope: { spec_id: 'spec-a', options: [] },
        }] };
      },
    },
    receiptMs: 50,
  });
  questions.mountList(document.getElementById('list'));
  questions.mountCards(document.getElementById('lane'), { level: 'spec', specId: 'spec-a' });
  questions.mountYouSaid(document.getElementById('said'), 'spec-a');
  await flush();
  const list = document.getElementById('list');
  const pick = list.querySelector('[data-option-index="1"]');
  const own = list.querySelector('.next-question-own');
  pick.click();
  assert.equal(document.querySelector('#lane [data-option-index="1"]').getAttribute('aria-checked'), 'true');
  own.value = '  write a sentence  ';
  own.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  assert.equal(pick.getAttribute('aria-checked'), 'false');
  assert.equal(own.classList.contains('is-active'), true);
  pick.click();
  assert.equal(own.value, '  write a sentence  ');
  assert.equal(own.classList.contains('is-dimmed'), true);
  own.focus();
  assert.equal(pick.getAttribute('aria-checked'), 'false');
  assert.equal(own.classList.contains('is-active'), true);
  assert.equal(document.querySelector('.next-question-send').disabled, false);
  document.querySelector('.next-question-send').click();
  await flush();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 'notice-question-a');
  assert.equal(calls[0][1], 'yes_no');
  assert.deepEqual(calls[0][2], { custom_text: 'write a sentence', submit: true });
  assert.equal(questions.getSnapshot().openCount, 0);
  assert.equal(document.querySelector('.next-question-sent-answer').textContent, 'write a sentence');
  await flush();
  assert.equal(document.querySelector('.next-question-you-said-answer').textContent, 'write a sentence');
  questions.dispose();
  dom.window.close();
});

test('multi-choice allows several presets but the answer payload still contains only one answer mode', async () => {
  const calls = [];
  const q = notification('question-multi', {
    question: {
      ...notification('question-multi').question,
      spec_id: 'spec-multi', response_mode: 'multi_choice', allow_custom: false,
      options: [{ label: 'First', value: 'first' }, { label: 'Second', value: 'second' }],
    },
  });
  const { document, dom } = docWithMounts();
  const questions = createQuestions({
    document, notifications: [q], inventory: inventory([]),
    cc: { notificationResolve: async (...args) => { calls.push(args); return { ok: true }; } },
    receiptMs: 50,
  });
  questions.mountList(document.getElementById('list'));
  const options = document.querySelectorAll('.next-question-option');
  options[0].click(); options[1].click();
  assert.equal(options[0].getAttribute('aria-checked'), 'true');
  assert.equal(options[1].getAttribute('aria-checked'), 'true');
  document.querySelector('.next-question-send').click();
  await flush();
  assert.deepEqual(calls[0][2], { selections: ['first', 'second'], submit: true });
  assert.equal(document.querySelector('.next-question-sent-answer').textContent, 'First, Second');
  questions.dispose();
  dom.window.close();
});

test('accepted answers show a 2.6 second receipt, lower the count, hide paging, then advance', async () => {
  assert.equal(RECEIPT_MS, 2600);
  const q1 = notification('question-one', {
    question: { ...notification('question-one').question, spec_id: 'spec-one', options: [{ label: 'Keep', value: 'keep' }] },
  });
  const q2 = notification('question-two', {
    question: { ...notification('question-two').question, spec_id: 'spec-two', options: [{ label: 'Change', value: 'change' }] },
  });
  const data = inventory([lane('lane-a', [member('spec-one', null), member('spec-two', null)])]);
  const { document, dom } = docWithMounts();
  const questions = createQuestions({
    document, inventory: data, notifications: [q1, q2], receiptMs: 35,
    cc: { notificationResolve: async () => ({ ok: true }) },
  });
  questions.mountCards(document.getElementById('lane'), { level: 'lane', laneId: 'lane-a' });
  assert.equal(document.querySelector('.next-question-card-label').textContent, 'Question 1 of 2');
  assert.ok(document.querySelector('.next-question-prev'));
  assert.ok(document.querySelector('.next-question-next'));
  document.querySelector('[data-option-index="1"]').click();
  document.querySelector('.next-question-send').click();
  await flush();
  assert.equal(questions.getSnapshot().openCount, 1);
  assert.equal(document.querySelector('.next-question-sent-label').textContent, 'Answer sent');
  assert.equal(document.querySelector('.next-question-sent-answer').textContent, 'Keep');
  assert.equal(document.querySelector('.next-question-prev'), null);
  assert.equal(document.querySelector('.next-question-next'), null);
  questions.update({ notifications: [q2] });
  assert.equal(document.querySelector('.next-question-card').dataset.questionId, 'question-one');
  assert.equal(document.querySelector('.next-question-sent-label').textContent, 'Answer sent');
  await new Promise((resolve) => setTimeout(resolve, 55));
  assert.equal(document.querySelector('.next-question-card').dataset.questionId, 'question-two');
  assert.equal(questions.getSnapshot().openCount, 1);
  questions.dispose();
  dom.window.close();
});

test('an uncertain send reconciles to open, preserves its draft, and allows one later successful retry', async () => {
  let resolveCalls = 0;
  const promptCalls = [];
  const q = openItem();
  const { document, dom } = docWithMounts();
  const questions = createQuestions({
    document, notifications: [q], inventory: inventory([]), receiptMs: 50,
    cc: {
      notificationResolve: async () => { resolveCalls += 1; if (resolveCalls === 1) throw new Error('reply lost'); return { ok: true }; },
      promptList: async (args) => { promptCalls.push(args); return { ok: true, questions: [{ question_id: 'question-a', state: 'open' }] }; },
    },
  });
  questions.mountList(document.getElementById('list'));
  document.querySelector('[data-option-index="2"]').click();
  document.querySelector('.next-question-send').click();
  await flush(12);
  assert.equal(document.querySelector('.next-question-not-sent').textContent, 'Not sent');
  assert.equal(document.querySelector('[data-option-index="2"]').getAttribute('aria-checked'), 'true');
  assert.deepEqual(promptCalls, [{ spec_id: 'spec-a', open: false }]);
  document.querySelector('.next-question-send').click();
  await flush();
  assert.equal(resolveCalls, 2);
  assert.equal(questions.getSnapshot().openCount, 0);
  assert.equal(document.querySelector('.next-question-sent-answer').textContent, 'Change');
  questions.dispose();
  dom.window.close();
});

test('a committed answer with a lost reply is shown from the stored record exactly once', async () => {
  let resolveCalls = 0;
  const q = openItem();
  const { document, dom } = docWithMounts();
  const record = answeredRecord('question-a', '2026-11-20T12:00:00Z', ['change'], [
    { label: 'Keep', value: 'keep' }, { label: 'Change', value: 'change' },
  ]);
  const questions = createQuestions({
    document, notifications: [q], inventory: inventory([]), receiptMs: 50,
    cc: {
      notificationResolve: async () => { resolveCalls += 1; throw new Error('reply lost after accept'); },
      promptList: async () => ({ ok: true, questions: [record] }),
    },
  });
  questions.mountList(document.getElementById('list'));
  document.querySelector('[data-option-index="2"]').click();
  document.querySelector('.next-question-send').click();
  await flush(12);
  assert.equal(resolveCalls, 1);
  assert.equal(questions.getSnapshot().openCount, 0);
  assert.equal(document.querySelector('.next-question-sent-answer').textContent, 'Change');
  assert.equal(document.querySelector('.next-question-not-sent'), null);
  questions.dispose();
  dom.window.close();
});

test('a lost reply is recorded before the matching reconciliation read even on a tied clock', async () => {
  const { outcomeBeforeRead } = require('./e2e/lib/next_lanes_event_order');
  const q = openItem();
  const { document, dom } = docWithMounts();
  const timeline = [];
  let resolveCalls = 0;
  const record = answeredRecord('question-a', '2026-11-20T12:00:00Z', ['change'], [
    { label: 'Keep', value: 'keep' }, { label: 'Change', value: 'change' },
  ]);
  const questions = createQuestions({
    document, notifications: [q], inventory: inventory([]), receiptMs: 50,
    cc: {
      notificationResolve: async () => {
        resolveCalls += 1;
        timeline.push({ type: 'notification.resolve:reply_lost', questionId: 'question-a', at: 960.2 });
        throw new Error('reply lost after accept');
      },
      promptList: async args => {
        timeline.push({ type: 'prompt.list:start', sequence: 1, at: 960.2, args });
        return { ok: true, questions: [record] };
      },
    },
  });
  questions.mountList(document.getElementById('list'));
  document.querySelector('[data-option-index="2"]').click();
  document.querySelector('.next-question-send').click();
  await flush(12);
  assert.equal(resolveCalls, 1);
  assert.equal(timeline.filter(row => row.type === 'prompt.list:start').length, 1);
  assert.deepEqual(timeline[1].args, { spec_id: 'spec-a', open: false });
  assert.equal(outcomeBeforeRead(timeline, 'notification.resolve:reply_lost', 'question-a', 1), true);
  assert.equal(document.querySelector('.next-question-sent-answer').textContent, 'Change');
  questions.dispose(); dom.window.close();
});

test('You said loads the newest answered record by spec and maps stored option values to labels after reload', async () => {
  const calls = [];
  const records = [
    answeredRecord('question-old', '2026-11-19T10:00:00Z', ['hold']),
    answeredRecord('question-new', '2026-11-20T10:00:00Z', ['release']),
  ];
  const cc = { promptList: async (args) => { calls.push(args); return { ok: true, questions: records }; } };
  const first = docWithMounts();
  const questions = createQuestions({ document: first.document, cc });
  questions.mountYouSaid(first.document.getElementById('said'), 'spec-a');
  await flush();
  assert.deepEqual(calls[0], { spec_id: 'spec-a', open: false });
  assert.equal(first.document.querySelector('.next-question-you-said-label').textContent, 'You said');
  assert.equal(first.document.querySelector('.next-question-you-said-answer').textContent, 'Allow release');
  assert.equal(questions.getSnapshot().youSaidBySpec.get('spec-a').text, 'Allow release');
  questions.dispose(); first.dom.window.close();

  const afterReload = docWithMounts();
  const restored = createQuestions({ document: afterReload.document, cc });
  restored.mountYouSaid(afterReload.document.getElementById('said'), 'spec-a');
  await flush();
  assert.equal(afterReload.document.querySelector('.next-question-you-said-answer').textContent, 'Allow release');
  restored.dispose(); afterReload.dom.window.close();
});

test('a nested durable question has no answer form, and answered records retain a redacted-safe wire shape', () => {
  const q = openItem({ question: { questions: [{ prompt: 'Nested synthetic item' }] } });
  const { document, dom } = docWithMounts();
  const questions = createQuestions({ document, notifications: [q], inventory: inventory([]) });
  questions.mountList(document.getElementById('list'));
  assert.equal(document.querySelector('.next-question-prompt').textContent, 'Prompt question-a');
  assert.equal(document.querySelector('.next-question-unavailable'), null);
  assert.equal(document.querySelector('.next-question-send'), null);
  assert.equal(document.querySelector('.next-question-skip,.next-question-voice'), null);

  const fixture = answeredRecord('synthetic-id', '2026-11-20T10:00:00Z', ['release']);
  assert.equal(latestAnsweredRecord([fixture]).question_id, 'synthetic-id');
  assert.equal(storedAnswerText(fixture), 'Allow release');
  assert.equal(JSON.stringify(fixture).includes('fleet'), false);
  questions.dispose();
  dom.window.close();
});
