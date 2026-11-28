'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const clientSingleton = require('../main/chat_stream_client');

test('workLanesShow maps options to one daemon request and preserves success and failure', async () => {
  const client = new clientSingleton.constructor();
  const requests = [];
  const detail = {
    type: 'work_lanes.show.ok',
    lane: { lane_id: 'lane_fixture' },
    projection: { lane_id: 'lane_fixture', goals: [], goals_total: 0 },
    detail_revision: 'a'.repeat(64),
  };
  client.sendCommand = async (...args) => { requests.push(args); return detail; };

  const options = { goalId: 'goal_fixture', membersAfter: 'spec_fixture__a', membersLimit: 1 };
  const reply = await client.workLanesShow('lane_fixture', options);
  assert.deepEqual(requests[0], [{ type: 'work_lanes.show', lane_id: 'lane_fixture', goal_id: 'goal_fixture',
    members_after: 'spec_fixture__a', members_limit: 1 }, 'work_lanes.show', { rawResponse: true, timeoutMs: 120000 }]);
  assert.strictEqual(reply, detail);

  const failure = { type: 'work_lanes.show.error', error_code: 'work_lane_not_found' };
  client.sendCommand = async (...args) => { requests.push(args); throw failure; };
  await assert.rejects(client.workLanesShow('missing_lane'), error => error === failure);
  assert.deepEqual(requests[1], [{ type: 'work_lanes.show', lane_id: 'missing_lane' },
    'work_lanes.show', { rawResponse: true, timeoutMs: 120000 }]);
});
