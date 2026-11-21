'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const record = require('./fixtures/next_lanes_answered.json');
const { hasStoredAnswer, latestAnsweredRecord, storedAnswerText } = require('../renderer/next/questions');

test('redacted live answered record retains the daemon shape and resolves its stored choice', () => {
  assert.equal(record.state, 'answered');
  assert.equal(hasStoredAnswer(record), true);
  assert.equal(latestAnsweredRecord([record]), record);
  assert.equal(storedAnswerText(record), 'Fixture choice');
});
