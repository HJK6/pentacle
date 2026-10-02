'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { installRenderer } = require('./helpers/renderer_chat');

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

function openQuestion(overrides = {}) {
  return {
    notification_id: 'n-open', producer: 'agent_question.v1', state: 'open',
    title: 'Card', body: 'Body', created_at: '2026-10-02T00:00:00Z',
    answer_to_stream_id: 'local:bound-seat',
    question: { question_id: 'q-open', producer_stream_id: 'local:bound-seat', state: 'open',
      response_mode: 'single_choice', options: [{ label: 'OK', value: 'OK' }] },
    ...overrides,
  };
}

test('a renderer that loads after the daemon snapshot still learns open question cards', async () => {
  const calls = [];
  const { context } = installRenderer({
    ccOverrides: {
      notificationList: async (args) => {
        calls.push(args);
        return { notifications: [
          openQuestion({ surfaced_to_stream_id: 'composite:assistant' }),
          { notification_id: 'n-other', producer: 'memory-cadence', state: 'open' },
        ] };
      },
    },
  });
  for (let i = 0; i < 6; i += 1) await flush();

  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [{ states: ['open'] }]);
  const ids = (stream) => vm.runInContext(
    `getOpenQuestionsForStream(${JSON.stringify(stream)}).map(n => n.question.question_id)`, context);
  // No chat has been opened: the card is known for its surface and its producer.
  assert.deepEqual([...ids('composite:assistant')], ['q-open']);
  assert.deepEqual([...ids('local:bound-seat')], ['q-open']);
  assert.deepEqual([...ids('local:unrelated-seat')], []);
});

test('boot hydration tolerates a missing or failing notification list', async () => {
  const failing = installRenderer({
    ccOverrides: { notificationList: async () => { throw new Error('offline'); } },
  });
  const absent = installRenderer();
  for (let i = 0; i < 6; i += 1) await flush();
  for (const { context } of [failing, absent]) {
    assert.equal(vm.runInContext("getOpenQuestionsForStream('local:bound-seat').length", context), 0);
  }
});
