'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { outcomeBeforeRead, resolveBeforeOutcome } = require('./e2e/lib/next_lanes_event_order');

test('reconciliation order accepts tied clock ticks only in recorded append order', () => {
  for (const type of ['notification.resolve:reply_lost', 'notification.resolve:failed']) {
    const outcome = { type, questionId: 'q', at: 960.2 };
    const read = { type: 'prompt.list:start', sequence: 4, at: 960.2 };
    assert.equal(outcomeBeforeRead([outcome, read], type, 'q', 4), true, 'ordered tie');
    assert.equal(outcomeBeforeRead([read, outcome], type, 'q', 4), false, 'reversed tie');
    assert.equal(outcomeBeforeRead([outcome, read], type, 'different', 4), false, 'wrong question');
    assert.equal(outcomeBeforeRead([outcome, read], type, 'q', 5), false, 'wrong read');
    assert.equal(outcomeBeforeRead([{...outcome, at: 961}, read], type, 'q', 4), false, 'backward clock');
    const start = {type: 'notification.resolve:start', questionId: 'q', at: 960.2};
    assert.equal(resolveBeforeOutcome([start, outcome], type, 'q'), true, 'resolve ordered tie');
    assert.equal(resolveBeforeOutcome([outcome, start], type, 'q'), false, 'reversed resolve');
  }
});
