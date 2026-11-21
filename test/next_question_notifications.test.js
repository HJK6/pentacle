'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createQuestionNotifications } = require('../renderer/next/question_notifications');

test('snapshot and live updates merge, and late hydration cannot resurrect an answer', () => {
  let changes = 0;
  const index = createQuestionNotifications(() => changes++);
  const question = { notification_id: 'n1', producer: 'agent_question.v1', state: 'open', question: { question_id: 'q1', producer_stream_id: 'host:child', state: 'open' } };
  index.frame({ type: 'snapshot', notifications: [question] });
  index.frame({ type: 'notification', notification: { notification_id: 'n1', state: 'answered', question: { state: 'answered' } } });
  index.ingest([question]);
  assert.equal(index.values().length, 1);
  assert.equal(index.values()[0].state, 'answered');
  assert.equal(index.values()[0].question.question_id, 'q1');
  assert.equal(changes, 2);
});

test('ignores unrelated notifications and malformed frames', () => {
  const index = createQuestionNotifications();
  index.frame({ type: 'notification', notification: { id: 'other', producer: 'other' } });
  index.frame(null);
  assert.deepEqual(index.values(), []);
});
