'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');

// These fault controls invoke the actual scenarios. Browser/pager correctness
// is proved separately against the genuine renderer, daemon and resolver.
function fixture(options = {}) {
  const rows = new Map();
  const requests = [];
  const notes = [];
  let reloads = 0;
  let creates = 0;
  let statusReads = 0;
  const producer = 'local:owned-question-fixture';
  const injected = options.primary || new Error('injected fixture boundary failure');
  async function fixtureRequest(_runtime, streamId, message) {
    assert.equal(streamId, producer);
    requests.push(message);
    if (message.type === 'prompt.ask') {
      creates += 1;
      if (options.failBeforeCreate === creates) throw injected;
      const qid = message.envelope.question_id;
      const question = { question_id: qid, producer_stream_id: producer,
        notification_id: `owned-${creates}`, state: 'open', answer: null };
      rows.set(qid, question);
      if (options.loseReply === creates) throw injected;
      return { type: 'prompt.ask.ok', ok: true, question };
    }
    const question = rows.get(message.question_id);
    if (message.type === 'prompt.status') {
      statusReads += 1;
      if (!question) return { type: 'prompt.error', ok: false, error_code: 'question_not_found' };
      if (options.statusError) return { type: 'prompt.error', ok: false, error_code: 'question_unauthorized' };
      if (options.reopenForCleanup && statusReads === 2) question.state = 'open';
      return { type: 'prompt.status.ok', ok: true, question: { ...question } };
    }
    if (message.type === 'prompt.cancel') {
      if (options.cancelError === question.question_id) return { type: 'prompt.error', ok: false, error_code: 'question_unauthorized' };
      if (options.answerRace) {
        question.state = 'answered';
        return { type: 'prompt.error', ok: false, error_code: 'question_terminal_state' };
      }
      question.state = 'dismissed';
      return { type: 'prompt.cancel.ok', ok: true, question };
    }
    if (message.type === 'notification.await') return { ok: true, answer: { text: 'SKIP' } };
    throw new Error(`unexpected fixture request: ${message.type}`);
  }
  const session = {
    async waitFor() { return true; },
    async click(selector) {
      if (selector === '.slot-chat-question-submit') rows.values().next().value.state = 'answered';
      if (selector === '.slot-chat-question-skip') {
        for (const row of rows.values()) if (row.state === 'open') {
          row.state = 'answered'; row.answer = { text: 'SKIP' };
        }
      }
      return true;
    },
    async eval(expression) {
      if (expression === 'window.PentacleChatCore.QUESTION_SKIP_TEXT') return 'SKIP';
      return { label: 'Send 1 of 2', disabled: false };
    },
  };
  const report = {
    ok(name, passed) {
      if (options.throwAt === name) throw injected;
      assert.equal(passed, true, name);
    },
    note(message) { notes.push(message); },
  };
  const filename = path.join(__dirname, 'e2e/lib/question_partial_skip_scenario.js');
  const loaded = new Module(filename, module);
  loaded.filename = filename;
  loaded.require = request => {
    if (request === './closed_chat_scenario') return { fixtureRequest };
    if (request === './dashboard_scenario') return { reloadDashboardPage: async () => {
      reloads += 1;
      if (options.reloadError) throw options.reloadError;
    } };
    throw new Error(`unexpected module: ${request}`);
  };
  loaded._compile(fs.readFileSync(filename, 'utf8'), filename);
  return { ...loaded.exports, ctx: { session, report, fixture: { streamId: producer },
    runtime: { fixtureTokens: { [producer]: 'synthetic-fixture-authority' } } }, rows, requests,
    notes, injected, reloads: () => reloads };
}

test('lost durable second-create reply cleans both owned questions', async () => {
  const f = fixture({ loseReply: 2 });
  await assert.rejects(f.questionPartialSend(f.ctx), error => error === f.injected);
  assert.deepEqual([...f.rows.values()].map(row => row.state), ['dismissed', 'dismissed']);
  assert.equal(f.reloads(), 1);
});

test('lost first-create reply is also reconciled without a returned question', async () => {
  const f = fixture({ loseReply: 1 });
  await assert.rejects(f.questionPartialSend(f.ctx), error => error === f.injected);
  assert.equal([...f.rows.values()][0].state, 'dismissed');
  assert.equal(f.reloads(), 1);
});

test('failed creation before commit tolerates only the absent owned id', async () => {
  const f = fixture({ failBeforeCreate: 2 });
  await assert.rejects(f.questionPartialSend(f.ctx), error => error === f.injected);
  assert.equal(f.rows.size, 1);
  assert.equal([...f.rows.values()][0].state, 'dismissed');
  assert.equal(f.injected.cleanupErrors, undefined);
});

test('assertion failure keeps its exact Error and cleans later ids after a cancel error', async () => {
  const primary = new Error('original assertion failure');
  const stack = primary.stack;
  const f = fixture({ primary, throwAt: 'one completed card enables Send 1 of 2',
    cancelError: 'web-partial-send-0' });
  await assert.rejects(f.questionPartialSend(f.ctx), error => error === primary);
  assert.equal(primary.stack, stack);
  assert.equal(primary.message, 'original assertion failure');
  assert.equal(primary.cleanupErrors.length, 1);
  assert.equal(f.rows.get('web-partial-send-1').state, 'dismissed');
  assert.equal(f.reloads(), 1);
  assert.equal(f.notes.length, 1);
});

test('an answer winning the cancel race is re-read as terminal', async () => {
  const f = fixture({ answerRace: true, throwAt: 'partial-send fixture 1 created' });
  await assert.rejects(f.questionPartialSend(f.ctx), error => error === f.injected);
  assert.equal([...f.rows.values()][0].state, 'answered');
  assert.equal(f.injected.cleanupErrors, undefined);
  assert.equal(f.requests.filter(row => row.type === 'prompt.status').length, 2);
});

test('unexpected status errors never authorize cancellation', async () => {
  const f = fixture({ statusError: true, throwAt: 'partial-send fixture 1 created' });
  await assert.rejects(f.questionPartialSend(f.ctx), error => error === f.injected);
  assert.equal(f.requests.filter(row => row.type === 'prompt.cancel').length, 0);
  assert.equal(f.injected.cleanupErrors.length, 1);
  assert.equal(f.reloads(), 1);
});

test('successful partial-send retains answered rows and reloads once', async () => {
  const f = fixture();
  await f.questionPartialSend(f.ctx);
  assert.deepEqual([...f.rows.values()].map(row => row.state), ['answered', 'answered']);
  assert.equal(f.requests.filter(row => row.type === 'prompt.cancel').length, 0);
  assert.equal(f.reloads(), 1);
});

test('single-question skip retains the answered row', async () => {
  const f = fixture();
  await f.questionSkip(f.ctx);
  assert.equal([...f.rows.values()][0].state, 'answered');
  assert.equal(f.requests.filter(row => row.type === 'prompt.cancel').length, 0);
  assert.equal(f.reloads(), 1);
});

test('successful body fails closed on cleanup cancellation failure', async () => {
  const f = fixture({ reopenForCleanup: true, cancelError: 'web-skip-0' });
  await assert.rejects(f.questionSkip(f.ctx), error => error instanceof AggregateError
    && error.errors.length === 1 && /cancel failed/.test(error.errors[0].message));
  assert.equal(f.reloads(), 1);
});

test('successful body also fails on page cleanup failure', async () => {
  const cleanup = new Error('cleanup reload failed');
  const f = fixture({ reloadError: cleanup });
  await assert.rejects(f.questionPartialSend(f.ctx), error => error instanceof AggregateError
    && error.errors[0] === cleanup);
});
