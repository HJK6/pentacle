import assert from 'node:assert/strict';
import test from 'node:test';

import {
  clampQuestionPageIndex,
  questionPageCount,
  questionPageQuestion,
  type PentacleQuestion,
} from '../src/index.ts';

function multiQuestion(): PentacleQuestion {
  return {
    question_key: 'qkey',
    prompt: 'Answer these',
    options: [],
    multi: true,
    questions: [
      { index: 0, header: 'First', prompt: 'Pick first', options: [{ index: 1, label: 'A' }] },
      { index: 1, header: 'Second', prompt: 'Pick second', options: [{ index: 1, label: 'B' }] },
    ],
  } as PentacleQuestion;
}

test('question pager counts single and multi-question payloads', () => {
  assert.equal(questionPageCount({ prompt: 'one', options: [] }), 1);
  assert.equal(questionPageCount(multiQuestion()), 2);
  assert.equal(questionPageCount(null), 0);
});

test('question pager clamps page indexes', () => {
  assert.equal(clampQuestionPageIndex(-2, 2), 0);
  assert.equal(clampQuestionPageIndex(9, 2), 1);
  assert.equal(clampQuestionPageIndex(Number.NaN, 2), 0);
});

test('question pager returns a single visible item while preserving question identity', () => {
  const page = questionPageQuestion(multiQuestion(), 1);
  assert.equal((page as PentacleQuestion & { question_key?: string }).question_key, 'qkey');
  assert.equal(page.questions?.length, 1);
  assert.equal(page.questions?.[0]?.header, 'Second');
});
