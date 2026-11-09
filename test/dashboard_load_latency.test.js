'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { installRenderer } = require('./helpers/renderer_chat');
const STREAM = 'host-a:fixture-session';
const catalogApi = require('../renderer/dashboards/catalog_loader');
const flush = () => new Promise(resolve => setImmediate(resolve));

async function boot(t) {
  const h = installRenderer();
  t.after(() => h.dom.window.close());
  await flush(); await flush();
  const run = code => vm.runInContext(code, h.context);
  run(`CONFIG.chatStream.hostMap.local = 'host-a'; CONFIG.hostNames.local = 'host-a';
    ensureSlotChatSurface(0);
    state.slots[0] = {name:'fixture-session',displayName:'Fixture',hostId:'local'};
    state.slotViewModes[0] = 'chat'; state.chatStream.connected = true;
    state.chatStream.sessions = [{stream_id:'${STREAM}',host:'host-a',provider:'claude',
      session_id:'fixture-session',session_name:'fixture-session',name:'fixture-session',title:'Fixture',working:false}];
    renderSlotChat(0);`);
  await flush();
  return { ...h, run };
}

test('ten inventories coalesce automatic asset and question reads across duplicate slots', async t => {
  const { dom, run } = await boot(t);
  const assets = [], questions = [];
  dom.window.cc.assetList = args => new Promise(resolve => assets.push({ args, resolve }));
  dom.window.cc.promptList = args => new Promise(resolve => questions.push({ args, resolve }));
  run(`state.slots[1] = {...state.slots[0]}; state.slotViewModes[1] = 'chat';
    for (let i=0;i<10;i++) applyChatStreamPayload({type:'session.inventory',sessions:state.chatStream.sessions});`);
  assert.deepEqual({assets:assets.length,questions:questions.length}, {assets:1,questions:1}, 'one automatic read of each kind per viewer while pending');
  assets[0].resolve({ assets: [] }); questions[0].resolve({ questions: [] });
  await flush();
  assert.equal(assets.length, 2, 'one trailing dirty refresh');
  assert.equal(questions.length, 2, 'one trailing dirty hydration');
  assets[1].resolve({ assets: [] }); questions[1].resolve({ questions: [] });
  await flush();
  assert.equal(assets.length, 2); assert.equal(questions.length, 2);
});

test('hidden chat ingests pushes, defers automatic reads and catches up once on return', async t => {
  const { dom, run } = await boot(t);
  let assets = 0, questions = 0;
  dom.window.cc.assetList = async () => { assets++; return { assets: [] }; };
  dom.window.cc.promptList = async () => { questions++; return { questions: [] }; };
  run(`state.currentView = 'dashboards';
    for (let i=0;i<10;i++) applyChatStreamPayload({type:'session.inventory',sessions:state.chatStream.sessions});
    applyChatStreamPayload({type:'asset.update',asset_id:'pushed',content_type:'report',title:'Push',session_key:{host:'host-a',session_name:'fixture-session',stream_id:'${STREAM}'}});
    applyChatStreamPayload({type:'notification',notification:{notification_id:'hidden-question',producer:'agent_question.v1',state:'open',question:{question_id:'hidden-question',producer_stream_id:'${STREAM}',state:'open'}}});`);
  assert.equal(assets, 0); assert.equal(questions, 0);
  assert.equal(run(`visibleAssetsForSlot(0).some(x=>x.asset_id==='pushed')`), true);
  assert.equal(run(`getOpenQuestionsForStream('${STREAM}').length`), 1);
  run(`switchView('chats')`);
  await flush();
  assert.equal(assets, 1); assert.equal(questions, 1);
});

test('slot reassignment rejects old owner data and a different viewer can fetch immediately', async t => {
  const { dom, run } = await boot(t);
  const assets = [], questions = [];
  dom.window.cc.assetList = args => new Promise(resolve => assets.push({ args, resolve }));
  dom.window.cc.promptList = args => new Promise(resolve => questions.push({ args, resolve }));
  run(`refetchAssetSnapshotsForActiveSlots();
    state.slotGen[0]++;
    state.slots[0] = {hostId:'local',name:'other'};
    state.chatStream.sessions.push({stream_id:'host-a:other',host:'host-a',session_name:'other'});
    refetchAssetSnapshotsForActiveSlots();`);
  assert.equal(assets.length, 2); assert.equal(questions.length, 2);
  assert.equal(assets[1].args.stream_id, 'host-a:other');
  assets[0].resolve({assets:[{asset_id:'late',stream_id:STREAM,content_type:'report'}]});
  questions[0].resolve({questions:[{notification_id:'late',question_id:'late',producer_stream_id:STREAM,state:'open'}]});
  await flush();
  assert.equal(run(`visibleAssetsForSlot(0).length`), 0);
  assert.equal(run(`Object.keys(state.durableQuestionNotificationsById).length`), 0);
  assets[1].resolve({assets:[]}); questions[1].resolve({questions:[]}); await flush();
});

test('warm catalog stays usable across navigation during a pending revalidation', async t => {
  const { dom, context, run } = await boot(t);
  const catalog = { schema_version:1,catalog_version:'v1',package:{repo:'example',commit:'a'.repeat(40)},requires:{host_api:2},boards:[{id:'local-board',name:'Local',kind:'built-in'}] };
  let reads = 0, release;
  dom.window.cc.assetList = async args => { if (!args.spec_id) return {assets:[]}; reads++; if (reads > 1) await new Promise(r => { release = r; }); return { assets:[{asset_id:'dashboard-catalog',content_type:'dashboard-catalog',stream_id:STREAM}] }; };
  dom.window.cc.assetGet = async () => ({ asset:{body:JSON.stringify(catalog)} });
  dom.window.visibleDashboards = () => dom.window.DASHBOARDS;
  context.testLoader = catalogApi.createLoader({root:dom.window});
  run(`catalogClient = testLoader; CONFIG.dashboards = {catalogSpecId:'example__catalog'};
    builtinDashboardImplementations.push({id:'local-board',mount(node){node.textContent='Usable local content';return {};}});
    switchView('dashboards');`);
  await flush();
  assert.match(dom.window.document.querySelector('#dashboard-content').textContent, /Usable local content/);
  run(`switchView('chats'); switchView('dashboards');`);
  await flush();
  assert.equal(dom.window.document.querySelectorAll('#dashboard-list [data-dashboard-id]').length, 1, 'cached catalog visible before transport settles');
  assert.match(dom.window.document.querySelector('#dashboard-content').textContent, /Usable local content/);
  assert.match(dom.window.document.querySelector('[data-testid="dashboard-catalog-cached"]').textContent, /refresh/i);
  run(`switchView('chats'); switchView('dashboards');`);
  await flush();
  assert.equal(reads, 2, 'navigation joins one list/get chain');
  const before = dom.window.document.querySelector('#dashboard-content').lastChild;
  release(); await flush();
  assert.equal(dom.window.document.querySelector('#dashboard-content').lastChild, before, 'unchanged catalog does not remount');
});

test('binding and reconnect epochs reject late questions and assets without clearing new pending reads', async t => {
  const { dom, run } = await boot(t);
  const assets = [], questions = [];
  dom.window.cc.assetList = args => new Promise(resolve => assets.push({ args, resolve }));
  dom.window.cc.promptList = args => new Promise(resolve => questions.push({ args, resolve }));
  run(`refetchAssetSnapshotsForActiveSlots();
    applyChatStreamPayload({type:'assistant.binding.changed'});`);
  assert.equal(assets.length, 2); assert.equal(questions.length, 2);
  assets[0].resolve({assets:[{asset_id:'late',title:'Late',content_type:'report',stream_id:'${STREAM}'}]});
  questions[0].resolve({questions:[{notification_id:'late',question_id:'late',producer_stream_id:STREAM,state:'open',envelope:{prompt:'Late'}}]});
  await flush();
  assert.equal(run(`visibleAssetsForSlot(0).length`), 0);
  assert.equal(run(`Object.keys(state.durableQuestionNotificationsById).length`), 0);
  run(`applyChatStreamPayload({type:'session.inventory',sessions:state.chatStream.sessions});`);
  assert.equal(assets.length, 2); assert.equal(questions.length, 2);
  run(`applyChatStreamState({connected:false}); applyChatStreamState({connected:true});`);
  assert.equal(assets.length, 3); assert.equal(questions.length, 3);
  assets[1].resolve({assets:[]}); questions[1].resolve({questions:[]});
  await flush();
  assert.equal(assets.length, 3); assert.equal(questions.length, 3);
  assets[2].resolve({assets:[]}); questions[2].resolve({questions:[]});
  await flush();
});

test('native reconnect without a disconnected edge starts a new read epoch and rejects late old replies', async t => {
  const { dom, run } = await boot(t);
  const assets = [], questions = [];
  dom.window.cc.assetList = args => new Promise(resolve => assets.push({args,resolve}));
  dom.window.cc.promptList = args => new Promise(resolve => questions.push({args,resolve}));
  run(`refetchAssetSnapshotsForActiveSlots();
    applyChatStreamPayload({type:'__reconnect',generation:1,next_generation:2});`);
  const dispatch = {assets:assets.length,questions:questions.length};
  assets[0].resolve({assets:[{asset_id:'old-epoch',stream_id:STREAM,content_type:'report'}]});
  questions[0].resolve({questions:[{notification_id:'old-epoch',question_id:'old-epoch',producer_stream_id:STREAM,state:'open'}]});
  await flush();
  const observed = {connected:run('state.chatStream.connected'),dispatch,
    oldAsset:run(`visibleAssetsForSlot(0).some(a=>a.asset_id==='old-epoch')`),
    oldQuestion:run(`getOpenQuestionsForStream('${STREAM}').some(q=>q.notification_id==='old-epoch')`)};
  for (const pending of assets.slice(1)) pending.resolve({assets:[]});
  for (const pending of questions.slice(1)) pending.resolve({questions:[]});
  await flush();
  assert.deepEqual(observed,{connected:true,dispatch:{assets:2,questions:2},oldAsset:false,oldQuestion:false});
});

test('native reconnect invalidates catalog eligibility and preserves hidden chat deferral', async t => {
  const { dom, context, run } = await boot(t);
  const catalog = {schema_version:1,catalog_version:'v1',package:{repo:'example',commit:'a'.repeat(40)},requires:{host_api:2},boards:[]};
  const loader = catalogApi.createLoader({root:dom.window,storage:null,cc:{
    assetList:async()=>({assets:[{asset_id:'dashboard-catalog',content_type:'dashboard-catalog',stream_id:STREAM}]}),
    assetGet:async()=>({asset:{body:catalog}}),
  }});
  await loader.refresh('example__catalog'); context.testLoader = loader;
  let assets=0,questions=0;
  dom.window.cc.assetList = async()=>{assets++;return {assets:[]};};
  dom.window.cc.promptList = async()=>{questions++;return {questions:[]};};
  run(`catalogClient=testLoader; CONFIG.dashboards={catalogSpecId:'example__catalog'};
    state.currentView='dashboards';
    applyChatStreamPayload({type:'__reconnect',generation:1,next_generation:2});`);
  const cachedStillEligible = !!loader.peek('example__catalog');
  const hidden = {assets,questions};
  // Leave before the asynchronous dashboard entry can write DOM. The request
  // eligibility assertion above is independent of view/render readiness.
  run(`switchView('chats')`); await flush();
  assert.equal(cachedStillEligible,false);
  assert.deepEqual(hidden,{assets:0,questions:0});
  assert.equal(assets,1,'only the visible chat catch-up starts a read');
  assert.equal(questions,1);
});

test('automatic read errors release ownership and retry only on a later invalidation', async t => {
  const { dom, run } = await boot(t);
  let assets = 0, questions = 0;
  dom.window.cc.assetList = async () => { assets++; throw new Error('offline'); };
  dom.window.cc.promptList = async () => { questions++; throw new Error('offline'); };
  run('refetchAssetSnapshotsForActiveSlots()');
  await flush(); await flush();
  assert.equal(assets, 1); assert.equal(questions, 1);
  run('refetchAssetSnapshotsForActiveSlots()'); await flush();
  assert.equal(assets, 2); assert.equal(questions, 2);
});

test('question pushes and resolution tombstones survive an older hydration snapshot', async t => {
  const { dom, run } = await boot(t);
  let release;
  dom.window.cc.promptList = () => new Promise(resolve => { release = resolve; });
  run(`ensureDurableQuestionsHydrated('${STREAM}', true);
    indexDurableQuestionNotification({notification_id:'resolved',producer:'agent_question.v1',state:'answered',question:{question_id:'resolved',producer_stream_id:'${STREAM}',state:'answered'}});
    indexDurableQuestionNotification({notification_id:'new',producer:'agent_question.v1',state:'open',question:{question_id:'new',producer_stream_id:'${STREAM}',state:'open'}});`);
  release({producer_stream_ids:[STREAM],questions:[{notification_id:'resolved',question_id:'resolved',producer_stream_id:STREAM,state:'open',envelope:{prompt:'Old'}}]});
  await flush();
  assert.equal(run(`state.durableQuestionNotificationsById.resolved.state`), 'answered');
  assert.equal(run(`getOpenQuestionsForStream('${STREAM}').some(q=>q.notification_id==='new')`), true);
});

test('reparenting while hidden clears old membership and rejects an old pending snapshot', async t => {
  const { dom, run } = await boot(t);
  let release;
  dom.window.cc.assetList = () => new Promise(resolve => { release = resolve; });
  run(`state.chatStream.sessions.push({stream_id:'host-a:child',parent_stream_id:'${STREAM}',visibility:'hidden'});
    reconcileChatReadScopes();
    refetchAssetSnapshotsForActiveSlots();
    state.currentView='dashboards';
    applyChatStreamPayload({type:'session.inventory',sessions:state.chatStream.sessions.map(s=>s.stream_id==='host-a:child'?{...s,parent_stream_id:'host-a:other'}:s)});`);
  release({scope_stream_ids:[STREAM,'host-a:child'],assets:[{asset_id:'old-child',stream_id:'host-a:child',content_type:'report'}]});
  await flush();
  assert.equal(run(`visibleAssetsForSlot(0).length`), 0);
  assert.deepEqual([...run(`state.chatStream.assets['${STREAM}'].scopeStreamIds`)], [STREAM]);
});
