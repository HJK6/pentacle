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
  const result = rev => ({type:'work_lanes.show.ok',detail_revision:rev,lane:{lane_id:'lane_fixture'},projection:{lane_id:'lane_fixture'}});
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
  requests[1].resolve({type:'work_lanes.show.ok',detail_revision:B,lane:{lane_id:'lane_fixture'},projection:{lane_id:'lane_fixture'}}); await fresh;
  requests[0].resolve({type:'work_lanes.show.ok',detail_revision:A,lane:{lane_id:'lane_fixture'},projection:{lane_id:'lane_fixture'}}); await old;
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
      return Promise.resolve({type:'work_lanes.show.ok', detail_revision:revision, lane:{lane_id:'lane_fixture'}, projection:{lane_id:'lane_fixture'}});
    },
  };
  const table={}; createCcHandlers({CONFIG:{},chatStreamClient:client}).register(createCollector(table));
  const show=table['chat-stream:work-lanes-show'].handler, event={sender:{}};
  await show(event,'lane_fixture',{membersLimit:32});
  const second = show(event,'lane_fixture',{membersLimit:32,membersAfter:'spec_fixture__a'});
  for (let i=0;i<4;i++) { fingerprint++; await show(event,'lane_fixture',{membersLimit:32}); }
  assert.equal(calls.length,2,'the two pages are the only daemon shows');
  finishPage({type:'work_lanes.show.ok',detail_revision:revision,lane:{lane_id:'lane_fixture'},projection:{lane_id:'lane_fixture'}}); await second;
});

test('unknown revision joins in-flight requests but never reuses completed replies, and counts pass-throughs', async t => {
  const requests=[], logs=[]; let now=100000;
  t.mock.method(Date,'now',()=>now);
  t.mock.method(console,'info',(...args)=>logs.push(args));
  const client={snapshot:()=>({work_lanes:{lanes:[]}}),workLanesShow:()=>new Promise(resolve=>requests.push(resolve))};
  const table={}; createCcHandlers({CONFIG:{},chatStreamClient:client,monotonicNow:()=>now}).register(createCollector(table));
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
    workLanesShow:async()=>{calls++;return {type:'work_lanes.show.ok',detail_revision:revision,lane:{lane_id:'lane_fixture'},projection:{lane_id:'lane_fixture'}};} };
  const table={}; createCcHandlers({CONFIG:{},chatStreamClient:client}).register(createCollector(table));
  const show=table['chat-stream:work-lanes-show'].handler,event={sender:{}};
  await show(event,'lane_fixture',{});
  await show(event,'lane_fixture',{membersLimit:32});
  assert.equal(calls,1,'the daemon defaults an omitted page size to 32');
});

test('resolved failures and incomplete detail are never cached', async () => {
  const revision='a'.repeat(64), laneId='lane_fixture'; let calls=0;
  const bad=[null,{type:'work_lanes.show.error',error:'offline'},{type:'work_lanes.show.ok',detail_revision:revision,lane:null},
    {type:'work_lanes.show.ok',detail_revision:revision,lane:{lane_id:laneId},projection:null}];
  const client={snapshot:()=>({work_lanes:{lanes:[{lane_id:laneId,detail_revision:revision}]}}),
    workLanesShow:async()=>{calls++;return bad.shift() ?? (calls===1 ? null : {type:'work_lanes.show.ok',detail_revision:revision,lane:{lane_id:laneId},projection:{lane_id:laneId}})}};
  const table={};createCcHandlers({CONFIG:{},chatStreamClient:client}).register(createCollector(table));
  const show=table['chat-stream:work-lanes-show'].handler,event={sender:{}};
  for(let i=0;i<5;i++) await show(event,laneId,{});
  assert.equal(calls,5,'each bad result permits another daemon request');
  await show(event,laneId,{}); assert.equal(calls,5,'complete detail is cached');
});

test('old-tab replay through the real web bridge isolates stable sender objects', async () => {
  const {createWsBridge}=require('../server/ws_bridge'); const revision='a'.repeat(64);let fingerprint=0,calls=0,finishPage;
  const client={snapshot:()=>({work_lanes:{lanes:[{lane_id:'lane_fixture',detail_revision:revision,working:fingerprint}]}}),
    workLanesShow:(_laneId,options)=>{calls++;const reply={type:'work_lanes.show.ok',detail_revision:revision,lane:{lane_id:'lane_fixture'},projection:{lane_id:'lane_fixture'}};return options.membersAfter ? new Promise(resolve=>{finishPage=()=>resolve(reply);}) : Promise.resolve(reply);}};
  const collector=createCollector();createCcHandlers({CONFIG:{},chatStreamClient:client}).register(collector);
  const bridge=createWsBridge({table:collector.table,logger:{warn(){}}});
  const a={send(){}},b={send(){}};bridge.addSocket(a);bridge.addSocket(b);
  let id=0;const show=(socket,options)=>bridge.handleMessage(socket,JSON.stringify({id:++id,method:'chat-stream:work-lanes-show',args:['lane_fixture',options]}));
  await show(a,{membersLimit:32});const pageTwo=show(a,{membersLimit:32,membersAfter:'spec_fixture__a'});
  for(let i=0;i<4;i++){fingerprint++;await show(a,{membersLimit:32});}
  assert.equal(calls,2,'one session reuses its page while the next page is in flight');
  finishPage(); await pageTwo;
  await show(b,{membersLimit:32});assert.equal(calls,3,'a second browser has a separate sender');
  bridge.removeSocket(a);const reopened={send(){}};bridge.addSocket(reopened);
  await show(reopened,{membersLimit:32});assert.equal(calls,4,'a reopened connection is a new session');
  bridge.closeAll();
});

test('known replies expire thirty minutes after completion while in-flight calls keep joining', async t => {
  let now=0,calls=0,finish; t.mock.method(Date,'now',()=>now); const revision='a'.repeat(64);
  const client={snapshot:()=>({work_lanes:{lanes:[{lane_id:'lane_fixture',detail_revision:revision}]}}),
    workLanesShow:()=>{calls++;return new Promise(resolve=>{finish=resolve;});}};
  const table={};createCcHandlers({CONFIG:{},chatStreamClient:client,monotonicNow:()=>now}).register(createCollector(table));
  const show=table['chat-stream:work-lanes-show'].handler,event={sender:{}},detail={type:'work_lanes.show.ok',detail_revision:revision,lane:{lane_id:'lane_fixture'},projection:{lane_id:'lane_fixture'}};
  const first=show(event,'lane_fixture',{});now=600000;
  const joined=show(event,'lane_fixture',{});assert.equal(calls,1,'pending work remains joined even after five minutes');
  finish(detail);await Promise.all([first,joined]);
  now+=1799999;const retained=show(event,'lane_fixture',{});assert.equal(calls,1,'reply remains reusable just before thirty minutes');await retained;
  now+=1;const refreshed=show(event,'lane_fixture',{});assert.equal(calls,2,'a thirty-minute-old completed reply is refreshed');
  finish(detail);await refreshed;
});

test('per-sender cache keeps only the 64 most recently used completed pages', async () => {
  const revision='a'.repeat(64);let calls=0;
  const client={snapshot:()=>({work_lanes:{lanes:[{lane_id:'lane_fixture',detail_revision:revision}]}}),workLanesShow:async()=>{calls++;return {type:'work_lanes.show.ok',detail_revision:revision,lane:{lane_id:'lane_fixture'},projection:{lane_id:'lane_fixture'}}}};
  const table={};createCcHandlers({CONFIG:{},chatStreamClient:client}).register(createCollector(table));
  const show=table['chat-stream:work-lanes-show'].handler,event={sender:{}};
  for(let i=0;i<64;i++) await show(event,'lane_fixture',{membersAfter:'spec_fixture__'+i});
  await show(event,'lane_fixture',{membersAfter:'spec_fixture__0'});
  await show(event,'lane_fixture',{membersAfter:'spec_fixture__64'});assert.equal(calls,65);
  await show(event,'lane_fixture',{membersAfter:'spec_fixture__0'});assert.equal(calls,65,'recently used page remains');
  await show(event,'lane_fixture',{membersAfter:'spec_fixture__1'});assert.equal(calls,66,'least recently used page was evicted');
});

test('unknown-revision counter flushes trailing calls after one minute without more requests', async t => {
  t.mock.timers.enable({apis:['setTimeout']}); let now=100000; t.mock.method(Date,'now',()=>now);
  const logs=[];t.mock.method(console,'info',(...args)=>logs.push(args));
  const client={snapshot:()=>({work_lanes:{lanes:[]}}),workLanesShow:async()=>({type:'work_lanes.show.ok'})};
  const table={};createCcHandlers({CONFIG:{},chatStreamClient:client,monotonicNow:()=>now}).register(createCollector(table));
  const show=table['chat-stream:work-lanes-show'].handler,event={sender:{}};
  await show(event,'missing_lane',{});await show(event,'missing_lane',{});
  assert.equal(logs.length,1);now+=59999;t.mock.timers.tick(59999);assert.equal(logs.length,1);
  now+=1;t.mock.timers.tick(1);assert.equal(logs.length,2);assert.equal(logs[1][0].count,1);
});

test('the completed-entry cap never evicts an in-flight request', async () => {
  const revision='a'.repeat(64), pending=[]; let calls=0;
  const client={snapshot:()=>({work_lanes:{lanes:[{lane_id:'lane_fixture',detail_revision:revision}]}}),
    workLanesShow:()=>{calls++;return new Promise(resolve=>pending.push(resolve));}};
  const table={};createCcHandlers({CONFIG:{},chatStreamClient:client}).register(createCollector(table));
  const show=table['chat-stream:work-lanes-show'].handler,event={sender:{}},promises=[];
  for(let i=0;i<65;i++) promises.push(show(event,'lane_fixture',{membersAfter:'spec_fixture__'+i}));
  const joined=show(event,'lane_fixture',{membersAfter:'spec_fixture__0'});assert.equal(calls,65);
  const reply={type:'work_lanes.show.ok',detail_revision:revision,lane:{lane_id:'lane_fixture'},projection:{lane_id:'lane_fixture'}};
  pending.forEach(resolve=>resolve(reply));await Promise.all([...promises,joined]);
});


test('wall-clock steps do not change elapsed expiry or counter rate', async t => {
  let wall=1000000, elapsed=0, calls=0;
  t.mock.method(Date,'now',()=>wall); t.mock.timers.enable({apis:['setTimeout']});
  const logs=[]; t.mock.method(console,'info',(...args)=>logs.push(args));
  const rev='a'.repeat(64), lane='lane_fixture';
  const client={snapshot:()=>({work_lanes:{lanes:[{lane_id:lane,detail_revision:rev}]}}),
    workLanesShow:async()=>{calls++;return {type:'work_lanes.show.ok',detail_revision:rev,lane:{lane_id:lane},projection:{lane_id:lane}};}};
  const table={};createCcHandlers({CONFIG:{},chatStreamClient:client,monotonicNow:()=>elapsed}).register(createCollector(table));
  const show=table['chat-stream:work-lanes-show'].handler,event={sender:{}};
  await show(event,lane,{}); wall-=600000; elapsed+=1800000;
  await show(event,lane,{}); assert.equal(calls,2,'elapsed thirty minutes expire despite a backward wall step');
  await show(event,'missing_lane',{}); wall+=3600000; elapsed+=1000;
  await show(event,'missing_lane',{}); assert.equal(logs.length,1,'forward wall step does not bypass rate limit');
  elapsed+=59000;t.mock.timers.tick(59000);assert.equal(logs.length,2,'trailing counter flushes on elapsed time');
});

test('pending page survives eviction of completed pages', async () => {
  const revision='a'.repeat(64), lane='lane_fixture';let calls=0, finish;
  const detail={type:'work_lanes.show.ok',detail_revision:revision,lane:{lane_id:lane},projection:{lane_id:lane}};
  const client={snapshot:()=>({work_lanes:{lanes:[{lane_id:lane,detail_revision:revision}]}}),
    workLanesShow:(_lane,options)=>{calls++;return options.membersAfter==='pending' ? new Promise(resolve=>{finish=resolve;}) : Promise.resolve(detail);}};
  const table={};createCcHandlers({CONFIG:{},chatStreamClient:client}).register(createCollector(table));
  const show=table['chat-stream:work-lanes-show'].handler,event={sender:{}};
  const first=show(event,lane,{membersAfter:'pending'});
  for(let i=0;i<65;i++) await show(event,lane,{membersAfter:'page'+i});
  const joined=show(event,lane,{membersAfter:'pending'});
  assert.equal(calls,66,'pending page is retained through completed-page eviction');
  finish(detail);await Promise.all([first,joined]);
});


test('a timed-out lane show is dropped and a later call reissues', async t => {
  t.mock.timers.enable({apis:['setTimeout']});
  const singleton=require('../main/chat_stream_client');const client=new singleton.constructor();
  const revision='a'.repeat(64), lane='lane_fixture', sent=[];
  client._ws={readyState:1,send:raw=>sent.push(JSON.parse(raw))};client.connected=true;
  client.snapshot=()=>({work_lanes:{lanes:[{lane_id:lane,detail_revision:revision}]}});
  const table={};createCcHandlers({CONFIG:{},chatStreamClient:client}).register(createCollector(table));
  const show=table['chat-stream:work-lanes-show'].handler,event={sender:{}};
  let failure=null; const first=show(event,lane,{}).catch(error=>{failure=error;});
  const joined=show(event,lane,{}).catch(()=>{}); assert.equal(sent.length,1);
  t.mock.timers.tick(120000); await Promise.resolve(); await Promise.resolve();
  assert.equal(failure?.error,'timed_out','joined request expires at the bounded timeout');
  const retried=show(event,lane,{}).catch(()=>{});assert.equal(sent.length,2,'timeout evicts the held entry');
  client._rejectPending('test cleanup');await Promise.all([first,joined,retried]);
});
