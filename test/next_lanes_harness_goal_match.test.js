'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { matchesOptionalGoalId } = require('./e2e/lib/next_lanes_harness');

test('omitted goalId is a wildcard while explicit null matches only lane-level reads', () => {
  assert.equal(matchesOptionalGoalId({}, null), true);
  assert.equal(matchesOptionalGoalId({}, 'fixture-goal'), true);

  assert.equal(matchesOptionalGoalId({ goalId: null }, null), true);
  assert.equal(matchesOptionalGoalId({ goalId: null }, undefined), true);
  assert.equal(matchesOptionalGoalId({ goalId: null }, 'fixture-goal'), false);

  assert.equal(matchesOptionalGoalId({ goalId: 'fixture-goal' }, 'fixture-goal'), true);
  assert.equal(matchesOptionalGoalId({ goalId: 'fixture-goal' }, 'other-goal'), false);
  assert.equal(matchesOptionalGoalId({ goalId: 'fixture-goal' }, null), false);
});
