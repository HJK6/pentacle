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

test('old tab page-one replays join or reuse one daemon request per revision and browser session', async () => {
  const A = 'a'.repeat(64), B = 'b'.repeat(64);
  let revision = A, fingerprint = 0;
  const requests = [];
  const client = {
    snapshot: () => ({ work_lanes: { lanes: [{ lane_id: 'lane_fixture', detail_revision: revision, working: fingerprint }] } }),
    workLanesShow: (laneId, options) => new Promise((resolve, reject) => requests.push({laneId, options, resolve, reject})),
  };
  const table = {};
  createCcHandlers({ CONFIG: {}, chatStreamClient: client }).register(createCollector(table));
  const show = table['chat-stream:work-lanes-show'].handler;
  const firstTab = { sender: {} }, secondTab = { sender: {} };
  const pageOne = { membersLimit: 32 }, pageTwo = { membersLimit: 32, membersAfter: 'spec_fixture__a' };
  const result = rev => ({type:'work_lanes.show.ok',detail_revision:rev});
  const first = show(firstTab, 'lane_fixture', pageOne);
  const joined = show(firstTab, 'lane_fixture', pageOne);
  assert.equal(requests.length, 1, 'an in-flight duplicate joins');
  requests[0].resolve(result(A));
  assert.deepEqual(await first, await joined);
  for (let i=0; i<4; i++) { fingerprint++; await show(firstTab, 'lane_fixture', pageOne); }
  assert.equal(requests.length, 1, 'fingerprint-only pushes cannot restart page one');
  const nextPage = show(firstTab, 'lane_fixture', pageTwo);
  assert.equal(requests.length, 2, 'a different page is distinct'); requests[1].resolve(result(A)); await nextPage;
  const otherSession = show(secondTab, 'lane_fixture', pageOne);
  assert.equal(requests.length, 3, 'another browser has its own cache'); requests[2].resolve(result(A)); await otherSession;
  revision = B;
  const changed = show(firstTab, 'lane_fixture', pageOne);
  assert.equal(requests.length, 4, 'a changed detail revision sends again'); requests[3].resolve(result(B)); await changed;
  assert.equal((await show(firstTab, 'lane_fixture', pageOne)).detail_revision, B);
});

test('failed shows are not cached and an old in-flight result cannot replace a newer revision', async () => {
  const A = 'a'.repeat(64), B = 'b'.repeat(64); let revision=A;
  const requests=[];
  const client={ snapshot:()=>({work_lanes:{lanes:[{lane_id:'lane_fixture',detail_revision:revision}]}}),
    workLanesShow:()=>new Promise((resolve,reject)=>requests.push({resolve,reject})) };
  const table={}; createCcHandlers({CONFIG:{},chatStreamClient:client}).register(createCollector(table));
  const show=table['chat-stream:work-lanes-show'].handler, event={sender:{}};
  const old=show(event,'lane_fixture',{}); revision=B;
  const fresh=show(event,'lane_fixture',{}); assert.equal(requests.length,2);
  requests[1].resolve({type:'work_lanes.show.ok',detail_revision:B}); await fresh;
  requests[0].resolve({type:'work_lanes.show.ok',detail_revision:A}); await old;
  assert.equal((await show(event,'lane_fixture',{})).detail_revision,B);
  const failed=show(event,'other_lane',{}); requests[2].reject(new Error('offline'));
  await assert.rejects(failed,/offline/);
  const retried=show(event,'other_lane',{}); assert.equal(requests.length,4); requests[3].resolve({type:'work_lanes.show.ok',detail_revision:B}); await retried;
});

test('old tab restart storm during a paged read does not reread page one', async () => {
  const revision = 'a'.repeat(64); let fingerprint = 0; const calls = []; let finishPage;
  const client = {
    snapshot: () => ({work_lanes:{lanes:[{lane_id:'lane_fixture', detail_revision:revision, working:fingerprint}]}}),
    workLanesShow: (laneId, options) => {
      calls.push({laneId, options});
      if (options.membersAfter) return new Promise(resolve => { finishPage = resolve; });
      return Promise.resolve({type:'work_lanes.show.ok', detail_revision:revision});
    },
  };
  const table={}; createCcHandlers({CONFIG:{},chatStreamClient:client}).register(createCollector(table));
  const show=table['chat-stream:work-lanes-show'].handler, event={sender:{}};
  await show(event,'lane_fixture',{membersLimit:32});
  const second = show(event,'lane_fixture',{membersLimit:32,membersAfter:'spec_fixture__a'});
  for (let i=0;i<4;i++) { fingerprint++; await show(event,'lane_fixture',{membersLimit:32}); }
  assert.equal(calls.length,2,'the two pages are the only daemon shows');
  finishPage({type:'work_lanes.show.ok',detail_revision:revision}); await second;
});

test('unknown revision joins in-flight requests but never reuses completed replies, and counts pass-throughs', async t => {
  const requests=[], logs=[]; let now=100000;
  t.mock.method(Date,'now',()=>now);
  t.mock.method(console,'info',(...args)=>logs.push(args));
  const client={snapshot:()=>({work_lanes:{lanes:[]}}),workLanesShow:()=>new Promise(resolve=>requests.push(resolve))};
  const table={}; createCcHandlers({CONFIG:{},chatStreamClient:client}).register(createCollector(table));
  const show=table['chat-stream:work-lanes-show'].handler,event={sender:{}};
  const first=show(event,'missing_lane',{membersLimit:32});
  const joined=show(event,'missing_lane',{membersLimit:32});
  assert.equal(requests.length,1,'unknown revisions still join in flight');
  requests[0]({type:'work_lanes.show.ok',detail_revision:'a'.repeat(64)}); await Promise.all([first,joined]);
  const second=show(event,'missing_lane',{membersLimit:32}); assert.equal(requests.length,2,'completed unknown reply is not reused');
  requests[1]({type:'work_lanes.show.ok',detail_revision:'a'.repeat(64)}); await second;
  assert.equal(logs.length,1,'no more than one counter line per minute');
  now+=60000;
  const third=show(event,'missing_lane',{membersLimit:32}); requests[2]({type:'work_lanes.show.ok',detail_revision:'a'.repeat(64)}); await third;
  assert.equal(logs.length,2);
  assert.equal(logs[1][0].count,2,'the counter includes pass-throughs since the last line');
});

test('omitted and explicit default page size share one cache entry', async () => {
  const revision = 'a'.repeat(64); let calls=0;
  const client={ snapshot:()=>({work_lanes:{lanes:[{lane_id:'lane_fixture',detail_revision:revision}]}}),
    workLanesShow:async()=>{calls++;return {type:'work_lanes.show.ok',detail_revision:revision};} };
  const table={}; createCcHandlers({CONFIG:{},chatStreamClient:client}).register(createCollector(table));
  const show=table['chat-stream:work-lanes-show'].handler,event={sender:{}};
  await show(event,'lane_fixture',{});
  await show(event,'lane_fixture',{membersLimit:32});
  assert.equal(calls,1,'the daemon defaults an omitted page size to 32');
});
