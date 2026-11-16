'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { TODAY, FakeHousehold, rpcError, fakeClock, flush } = require('./fixtures/household_support');
const source=()=>require('../renderer/household/store');
function boot() { const clock=fakeClock(), server=new FakeHousehold(); const store=source().createHouseholdStore(clock); store.configure({household:server.handle}); return {clock,server,store,state:()=>store.getState()}; }
const MUTATIONS=[
  ['addItem','household.item.add',s=>s.addItem('grocery','Synthetic new item'),0],
  ['removeItem','household.item.remove',s=>s.removeItem(11),0],
  ['addEvent','household.event.add',s=>s.addEvent({date:TODAY,time:null,title:'Synthetic new event',who:'self'}),0],
  ['removeEvent','household.event.remove',s=>s.removeEvent(102),0],
  ['checkItem','household.item.done',s=>s.checkItem(11),5000],
];
function unknown(server,clock,verb,when,code) {
  if (arguments.length < 5) code = 'unknown_outcome';
  server.overrides.set(verb,async(fields,fake)=>{ if(when==='now') await fake.apply(verb,fields); else if(typeof when==='number') clock.setTimeout(()=>fake.apply(verb,fields),when); return rpcError(code); });
}
function failures(server,count) { server.overrides.set('household.snapshot',(fields,fake)=>count-- > 0 ? rpcError('unavailable') : fake.apply('household.snapshot',fields)); }
test('mobile refresh vector: one read, exact month fields, honest per-attempt result and recovery',async()=>{
  const {server,store,state}=boot(); assert.equal(state().status,'loading');
  assert.deepEqual(await store.refresh(),{ok:true,server_now:`${TODAY}T23:39:00Z`});
  assert.deepEqual(server.reads(),[{verb:'household.snapshot',fields:{}}]);
  await store.load('2054-11'); assert.deepEqual(server.reads().at(-1).fields,{month:'2054-11'});
  await store.refresh(); assert.deepEqual(server.reads().at(-1).fields,{month:'2054-11'});
  for(const code of ['unavailable','unauthorized','timed_out','disconnected','invalid_range']) {
    server.overrides.set('household.snapshot',()=>rpcError(code)); assert.deepEqual(await store.refresh(),{error:code});
    assert.equal(state().status,code==='unauthorized'?'unauthorized':'unavailable');
  }
  server.overrides.clear(); await store.refresh(); assert.equal(state().status,'ready');
});
test('module exports one stable store; subscriptions detach without canceling the pending check',async()=>{
  assert.equal(source().householdStore,source().householdStore);
  const {clock,server,store}=boot(); let pushes=0; const listener=()=>pushes++; const off=store.attach(listener);
  await store.refresh(); store.checkItem(11); const before=pushes; off(); store.detach(listener);
  await clock.advance(5000); assert.equal(pushes,before); assert.deepEqual(server.mutations(),[{verb:'household.item.done',fields:{item_id:11}}]);
  assert.equal(server.reads().length,2); assert.ok(!store.getState().snapshot.lists.grocery.some(i=>i.id===11));
});
test('mobile check vector: exactly one done at 5000ms, no early mutation, confirmed readback',async()=>{
  const {clock,server,store}=boot(); await store.refresh(); store.checkItem(11);
  await clock.advance(4999); assert.deepEqual(server.mutations(),[]); await clock.advance(1);
  assert.deepEqual(server.mutations(),[{verb:'household.item.done',fields:{item_id:11}}]); assert.equal(server.reads().length,2);
  await clock.advance(30000); assert.equal(server.mutations().length,1);
});
test('mobile undo and check-undo-check vectors restart the independent five-second window',async()=>{
  const {clock,server,store}=boot(); await store.refresh(); store.checkItem(11); await clock.advance(2000); store.checkItem(11);
  await clock.advance(60000); assert.deepEqual(server.mutations(),[]);
  store.checkItem(11); await clock.advance(2000); store.checkItem(11); await clock.advance(1000); store.checkItem(11);
  await clock.advance(4999); assert.equal(server.mutations().length,0); await clock.advance(1); assert.equal(server.mutations().length,1);
});
test('mobile independent item check deadlines',async()=>{
  const {clock,server,store}=boot(); await store.refresh(); store.checkItem(11); await clock.advance(1000); store.checkItem(12);
  await clock.advance(4000); assert.deepEqual(server.mutations().map(c=>c.fields.item_id),[11]);
  await clock.advance(1000); assert.deepEqual(server.mutations().map(c=>c.fields.item_id),[11,12]);
});
test('mobile add vectors: trim, ignore blank and same-list duplicate only',async()=>{
  const {server,store}=boot(); await store.refresh();
  for(const text of ['', '   ', '\t\n','Synthetic grocery 10']) assert.equal(await store.addItem('grocery',text),'ignored');
  assert.deepEqual(server.mutations(),[]); await store.addItem('tasks','Synthetic grocery 10'); await store.addItem('grocery','  Synthetic trimmed  ');
  assert.deepEqual(server.mutations().map(c=>c.fields),[{list:'tasks',label:'Synthetic grocery 10'},{list:'grocery',label:'Synthetic trimmed'}]);
});
test('all five mutations construct exact whitelisted fields, rejecting poison by omission',async()=>{
  const {clock,server,store}=boot(); await store.refresh();
  await store.addItem('tasks','Synthetic plain'); await store.addEvent({date:TODAY,time:'09:00',title:'Synthetic event',who:'partner',scope:'shared',priority:'hi',due_date:TODAY,created_by:'assistant',position:1});
  await store.removeItem(11); await store.removeEvent(102); store.checkItem(12); await clock.advance(5000);
  assert.equal(server.mutations().length,5); assert.deepEqual(server.mutations()[1].fields,{date:TODAY,time:'09:00',title:'Synthetic event',who:'partner'});
});
for(const [name,verb,run,settle] of MUTATIONS) {
  test(`mobile confirmed ${name}: exactly one mutation/readback`,async()=>{
    const {clock,server,store}=boot(); await store.refresh(); run(store); await clock.advance(settle);
    assert.equal(server.mutations().length,1); assert.equal(server.reads().length,2);
  });
  for(const code of ['unknown_outcome','timed_out','disconnected',undefined]) test(`mobile unknown ${name}/${code}: read now and +6s, never resubmit`,async()=>{
    const {clock,server,store,state}=boot(); await store.refresh(); unknown(server,clock,verb,'never',code); run(store); await clock.advance(settle);
    assert.equal(server.mutations().length,1); assert.equal(server.reads().length,2); assert.equal(state().notice.text,"Couldn't confirm — checking again"); assert.equal(state().unresolved,1);
    await clock.advance(5999); assert.equal(server.reads().length,2); await clock.advance(1); assert.equal(server.reads().length,3); assert.equal(state().notice.text,'Not saved'); assert.equal(state().unresolved,0);
    await clock.advance(120000); assert.equal(server.reads().length,3); assert.equal(server.mutations().length,1);
  });
  for(const when of ['now',3000]) test(`mobile unknown ${name} commit ${when}: resolves silently only after delayed fresh read`,async()=>{
    const {clock,server,store,state}=boot(); await store.refresh(); unknown(server,clock,verb,when); run(store); await clock.advance(settle);
    assert.equal(state().unresolved,1); await clock.advance(6000); assert.equal(state().unresolved,0); assert.equal(state().notice,null);
    await clock.advance(120000); assert.equal(server.mutations().length,1); assert.equal(server.reads().length,3);
  });
  for(const code of ['invalid_request','invalid_range','not_found','forbidden','unavailable','unauthorized']) test(`mobile definite refusal ${name}/${code}: no readback`,async()=>{
    const {clock,server,store,state}=boot(); await store.refresh(); server.overrides.set(verb,()=>rpcError(code)); run(store); await clock.advance(settle+120000);
    assert.equal(server.mutations().length,1); assert.equal(server.reads().length,1); assert.equal(state().notice.text,'Not saved'); assert.equal(state().unresolved,0);
    if(['unauthorized','unavailable'].includes(code)) assert.equal(state().status,code);
  });
}
test('mobile slow read vector: six seconds starts after immediate read completes',async()=>{
  const {clock,server,store}=boot(); await store.refresh(); unknown(server,clock,'household.item.add','never'); let slow=true;
  server.overrides.set('household.snapshot',(fields,fake)=>{if(!slow)return fake.apply('household.snapshot',fields);slow=false;return new Promise(resolve=>clock.setTimeout(()=>resolve(fake.apply('household.snapshot',fields)),4000));});
  store.addItem('grocery','Synthetic delayed'); await clock.advance(9999); assert.equal(server.reads().length,2); await clock.advance(1); assert.equal(server.reads().length,3);
});
for(const [when,n,expected] of [['now',2,'saved'],['never',3,'not_saved']]) test(`mobile failed read never decides ${when}/${n}`,async()=>{
  const {clock,server,store,state}=boot(); await store.refresh(); unknown(server,clock,'household.item.add',when); failures(server,n); let result;
  store.addItem('grocery','Synthetic delayed').then(r=>result=r); await clock.advance((n-1)*6000);
  assert.equal(result,undefined); assert.equal(state().unresolved,1); assert.equal(state().notice.text,"Couldn't confirm — checking again");
  await clock.advance(6000); assert.equal(result,expected); assert.equal(state().unresolved,0); assert.equal(server.mutations().length,1);
});
test('mobile removed row remains hidden while failed readbacks retry, even with no subscribers',async()=>{
  const {clock,server,store,state}=boot(); await store.refresh(); unknown(server,clock,'household.item.remove','never'); failures(server,2);
  store.removeItem(11); await clock.advance(6000); assert.equal(state().hiddenItems[11],true); await clock.advance(6000);
  assert.equal(state().hiddenItems[11],undefined); assert.equal(state().notice.text,'Not saved');
});
test('refresh settles while an independent unknown readback loop remains unresolved',async()=>{
  const {clock,server,store,state}=boot(); await store.refresh(); unknown(server,clock,'household.item.add','now'); failures(server,10);
  store.addItem('grocery','Synthetic unresolved'); await flush(); const before=server.reads().length;
  assert.deepEqual(await store.refresh(),{error:'unavailable'}); assert.equal(server.reads().length,before+1); assert.equal(state().unresolved,1);
  assert.equal(clock.timers.size,1);
});
test('isUnknown classifies bridge timeouts/disconnects and absent codes only',()=>{
  for(const error of [rpcError('unknown_outcome'),rpcError('timed_out'),rpcError('disconnected'),new Error('Synthetic disconnect'),undefined]) assert.equal(source().isUnknown(error),true);
  for(const code of ['unavailable','unauthorized','forbidden','invalid_request','invalid_range','not_found']) assert.equal(source().isUnknown(rpcError(code)),false);
});

test('web-only pre-open disconnect fails refresh fast and keeps mutation readback unresolved until reconnect',async()=>{
  const {clock,server,store,state}=boot(); await store.refresh();
  store.configure({household:async()=>{const error=new Error('Synthetic disconnected'); error.code='disconnected'; throw error;}});
  assert.deepEqual(await store.refresh(),{error:'disconnected'});
  let result; store.addItem('grocery','Synthetic disconnected item').then(value=>result=value); await clock.advance(6000);
  assert.equal(result,undefined); assert.equal(state().unresolved,1); assert.equal(state().notice.text,"Couldn't confirm — checking again");
  store.configure({household:server.handle}); await clock.advance(6000);
  assert.equal(result,'not_saved'); assert.equal(state().unresolved,0); assert.equal(server.mutations().length,0);
});
test('repeated removes while hidden send once; removal supersedes an unsent pending check',async()=>{
  const {clock,server,store}=boot(); await store.refresh(); store.checkItem(11);
  const first=store.removeItem(11), second=store.removeItem(11); assert.equal(await second,'ignored'); await first;
  await clock.advance(10000); assert.deepEqual(server.mutations(),[{verb:'household.item.remove',fields:{item_id:11}}]);
});
test('ordinary month loads are latest-request-wins, including a stale failed reply',async()=>{
  const {server,store,state}=boot(); await store.refresh(); const pending=[];
  server.overrides.set('household.snapshot',(fields,fake)=>new Promise(resolve=>pending.push({fields,fake,resolve})));
  const old=store.load('2054-11'), next=store.load('2054-12');
  pending[1].resolve(await pending[1].fake.apply('household.snapshot',pending[1].fields)); await next;
  pending[0].resolve(await pending[0].fake.apply('household.snapshot',pending[0].fields)); await old;
  assert.equal(state().snapshot.month,'2054-12'); assert.equal(state().month,'2054-12');
  const older=store.load('2055-01'), newer=store.load('2055-02');
  pending[3].resolve(await pending[3].fake.apply('household.snapshot',pending[3].fields)); await newer;
  pending[2].resolve(rpcError('unavailable')); await older;
  assert.equal(state().snapshot.month,'2055-02'); assert.equal(state().status,'ready');
});
for(const [verb,run] of [['household.event.add',s=>s.addEvent({date:'2055-02-03',time:null,title:'Synthetic locked add',who:'self'})],['household.event.remove',s=>s.removeEvent(1200)]]) {
  for(const when of ['now','never']) test(`event month lock ${verb}/${when} survives detach and releases after fresh resolution`,async()=>{
    const {clock,server,store,state}=boot(); server.snapshot.events.push({id:1200,date:'2055-02-03',time:null,title:'Synthetic locked event',who:'self',scope:'private',created_by:'app'});
    await store.load('2055-02'); unknown(server,clock,verb,when); const off=store.attach(()=>{}); let outcome;
    run(store).then(value=>outcome=value); await flush(); off(); assert.equal(state().monthLocked,true);
    const reads=server.reads().length; assert.deepEqual(await store.load('2055-03'),{error:'month_locked'}); assert.equal(server.reads().length,reads); assert.equal(state().month,'2055-02');
    store.attach(()=>{}); await clock.advance(6000); assert.equal(state().monthLocked,false); assert.equal(outcome,when==='now'?'saved':'not_saved');
    assert.ok(server.reads().every(call=>call.fields.month==='2055-02'));
  });
}
test('event month lock begins before request settles, blocks repeated events, releases on definite refusal',async()=>{
  const {server,store,state}=boot(); await store.refresh(); let release;
  server.overrides.set('household.event.add',()=>new Promise(resolve=>release=resolve));
  const value={date:TODAY,time:null,title:'Synthetic deferred event',who:'self'};
  const write=store.addEvent(value); assert.equal(state().monthLocked,true); assert.equal(await store.addEvent(value),'ignored');
  release(rpcError('invalid_request')); assert.equal(await write,'failed'); assert.equal(state().monthLocked,false);
});
for(const when of ['now','never']) test(`cross-month add pins submitted day before send, drops older read, stays selected (${when})`,async()=>{
  const {clock,server,store,state}=boot(); await store.refresh(); let oldRead;
  server.overrides.set('household.snapshot',(fields,fake)=>fields.month==='2055-01' ? new Promise(resolve=>oldRead=()=>fake.apply('household.snapshot',fields).then(resolve)) : fake.apply('household.snapshot',fields));
  const superseded=store.load('2055-01'); unknown(server,clock,'household.event.add',when);
  let beforeSend;
  const original=server.overrides.get('household.event.add'); server.overrides.set('household.event.add',(fields,fake)=>{ beforeSend={month:state().month,date:state().eventDate,locked:state().monthLocked}; return original(fields,fake); });
  let result; store.addEvent({date:'2055-02-03',time:null,title:'Synthetic cross-month',who:'self'}).then(value=>result=value); await flush();
  assert.deepEqual(beforeSend,{month:'2055-02',date:'2055-02-03',locked:true});
  assert.equal(state().snapshot.month,'2055-02'); await oldRead(); await superseded; assert.equal(state().snapshot.month,'2055-02');
  await clock.advance(6000); assert.equal(result,when==='now'?'saved':'not_saved');
  assert.equal(state().month,'2055-02'); assert.equal(state().eventDate,'2055-02-03'); assert.equal(state().monthLocked,false);
  const mutationIndex=server.calls.findIndex(call=>call.verb==='household.event.add');
  assert.ok(server.calls.slice(mutationIndex+1).filter(call=>call.verb==='household.snapshot').every(call=>call.fields.month==='2055-02'));
});
test('event removal pins the existing row date even when it comes from the always-present today rail',async()=>{
  const {server,store,state}=boot(); await store.load('2055-01'); let observed;
  server.overrides.set('household.event.remove',(fields,fake)=>{observed={month:state().month,date:state().eventDate};return fake.apply('household.event.remove',fields);});
  assert.equal(await store.removeEvent(102),'ok'); assert.deepEqual(observed,{month:'2054-10',date:TODAY});
  assert.equal(state().snapshot.month,'2054-10'); assert.equal(state().eventDate,TODAY);
});
