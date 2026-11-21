'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createCcHandlers, createCollector } = require('../main/cc_handlers');
const { buildCc } = require('../renderer/web_cc');

function fakeTransport(reply) {
  const calls = [];
  return {
    calls,
    call(method, ...args) {
      calls.push({ method, args });
      return Promise.resolve(reply);
    },
    fire() {},
    on() {},
  };
}

test('window.cc workLanesShow is one additive pass-through with the agreed arguments', async () => {
  const reply = { type: 'work_lanes.show.ok', detail_revision: 'a'.repeat(64), projection: { goals: [] } };
  const transport = fakeTransport(reply);
  const cc = buildCc(transport, {
    clipboard: { writeText() {}, readText: () => '' },
    chatPopoutContext: null,
    reload() {},
  });

  const result = await cc.workLanesShow('lane_fixture', {
    goalId: 'goal_fixture', membersAfter: 'spec_fixture__a', membersLimit: 32,
  });

  assert.deepEqual(transport.calls, [{
    method: 'chat-stream:work-lanes-show',
    args: ['lane_fixture', { goalId: 'goal_fixture', membersAfter: 'spec_fixture__a', membersLimit: 32 }],
  }]);
  assert.strictEqual(result, reply);
});

test('host handler passes the daemon reply through and leaves failures rejected', async () => {
  const reply = { type: 'work_lanes.show.ok', detail_revision: 'b'.repeat(64) };
  const seen = [];
  const client = {
    async workLanesShow(laneId, options) {
      seen.push({ laneId, options });
      return reply;
    },
  };
  const table = {};
  createCcHandlers({ CONFIG: { chatStream: {} }, chatStreamClient: client, harness: true })
    .register(createCollector(table));
  const handler = table['chat-stream:work-lanes-show'].handler;
  const options = { goalId: 'goal_fixture', membersAfter: 'spec_fixture__b', membersLimit: 1 };

  assert.strictEqual(await handler(null, 'lane_fixture', options), reply);
  assert.deepEqual(seen, [{ laneId: 'lane_fixture', options }]);

  const failure = { type: 'work_lanes.show.error', error_code: 'work_lane_not_found' };
  client.workLanesShow = async () => { throw failure; };
  await assert.rejects(handler(null, 'missing_lane', {}), error => error === failure);
});
