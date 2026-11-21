'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { revisionRequestsCoalesce } = require('./e2e/lib/next_lanes_revision_contract');
test('revision B re-asks members and coalesces goals while the members read is loading', () => {
  const a = {laneId:'lane', membersLimit:32, revision:'A'};
  const b = {laneId:'lane', membersLimit:32, revision:'B'};
  const unrelated = {laneId:'other', membersLimit:32, revision:'B'};
  assert.equal(revisionRequestsCoalesce([a, b, unrelated], 'lane', 'A', 'B'), true);
  assert.equal(revisionRequestsCoalesce([a, unrelated], 'lane', 'A', 'B'), false);
  assert.equal(revisionRequestsCoalesce([a, b, {laneId:'lane', membersLimit:1, revision:'B'}], 'lane', 'A', 'B'), false);
});
