'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createWakeDelivery } = require('../renderer/wake_delivery');
const row = (id='bart:current', extra={}) => ({ stream_id:id, host:'bart', role:'assistant', ...extra });
function fixture() {
  const f = { config:{features:{mic:true,assistantRole:'assistant'},mic:{wakeTargetHost:'bart'},chatStream:{}},
    status:{mode:'on',wake:{enabled:true,generation:'one',pending_count:1}},
    state:{connected:true,sessions:[row()]},claims:[{id:'a',generation:'one',text:'hello'}], sends:[], apiCalls:[], notes:[] };
  f.api = async (method,path) => {
    f.apiCalls.push(path);
    return path==='/status' ? structuredClone(f.status) : {claim:f.claims.shift() || null};
  };
  f.make = () => createWakeDelivery({config:f.config, getState:async()=>structuredClone(f.state),
    api:(...args)=>f.api(...args),sendTurn:(...args)=>f.sends.push(args),onStatus:text=>f.notes.push(text)});
  f.helper=f.make(); return f;
}
test('one send for concurrent/repeated polling, identical text in distinct captures remains valid',async()=>{
  const f=fixture();
  await Promise.all([f.helper.tick(f.status),f.helper.tick(f.status)]);
  await f.helper.tick(f.status);
  assert.deepEqual(f.sends,[['bart:current','hello']]);
  f.claims.push({id:'b',generation:'one',text:'hello'});
  await f.helper.tick(f.status);
  assert.equal(f.sends.length,2);
});
test('no wake work consumes nothing',async()=>{
  const f=fixture(); f.status.wake.pending_count=0;
  await f.helper.tick(f.status); assert.equal(f.apiCalls.length,0);
});
for(const variant of ['disconnected','none','multiple','closed','wrong-role','wrong-host','unconfigured','snapshot-disabled','mic-disabled']) {
  test(`${variant} defers without claiming`,async()=>{
    const f=fixture();
    if(variant==='disconnected') f.state.connected=false;
    if(variant==='none') f.state.sessions=[];
    if(variant==='multiple') f.state.sessions.push(row('bart:second'));
    if(variant==='closed') f.state.sessions[0].closed_at='today';
    if(variant==='wrong-role') f.state.sessions[0].role='Assistant';
    if(variant==='wrong-host') f.state.sessions[0].host='other';
    if(variant==='unconfigured') delete f.config.mic.wakeTargetHost;
    if(variant==='snapshot-disabled') f.config.chatStream.snapshot=false;
    if(variant==='mic-disabled') f.config.features.mic=false;
    await f.helper.tick(f.status); assert.equal(f.apiCalls.length,0); assert.equal(f.sends.length,0);
  });
}
test('handoff while claiming holds once, then sends to fresh unique assistant regardless of focus',async()=>{
  const f=fixture(); const original=f.api;
  f.api=async(...args)=>{const result=await original(...args); if(args[1]==='/wake/claim') f.state.sessions=[row('bart:new')]; return result;};
  await f.helper.tick(f.status); assert.equal(f.sends.length,0);
  f.status.wake.pending_count=0;
  await f.helper.tick(f.status);
  assert.deepEqual(f.sends,[['bart:new','hello']]);
  assert.equal(f.apiCalls.filter(x=>x==='/wake/claim').length,1);
});
test('Off during claim cancels work even if an old On status returns',async()=>{
  const f=fixture(); const original=f.api;
  f.api=async(...args)=>{const result=await original(...args); if(args[1]==='/wake/claim') f.helper.cancel(); return result;};
  await f.helper.tick(f.status); await f.helper.tick(f.status);
  assert.equal(f.sends.length,0);
});
test('Off then On generation change cancels held work',async()=>{
  const f=fixture(); const original=f.api;
  f.api=async(...args)=>{const result=await original(...args); if(args[1]==='/wake/claim') f.state.connected=false; return result;};
  await f.helper.tick(f.status);
  f.state.connected=true; f.status.wake.generation='two'; f.status.wake.pending_count=0;
  await f.helper.tick(f.status); assert.equal(f.sends.length,0);
});
test('last service readback Off prevents send',async()=>{
  const f=fixture(); const original=f.api;
  f.api=async(...args)=>{const result=await original(...args); if(args[1]==='/status') result.mode='off'; return result;};
  await f.helper.tick(f.status); assert.equal(f.sends.length,0);
});
test('uncertain send is never retried with a second sendTurn',async()=>{
  const f=fixture(); let count=0;
  f.helper=createWakeDelivery({config:f.config,getState:async()=>f.state,api:(...args)=>f.api(...args),sendTurn:()=>{count++;throw Error('lost response');},onStatus:()=>{}});
  await f.helper.tick(f.status); await f.helper.tick(f.status);
  assert.equal(count,1);
});
test('fresh On lifecycle enables wake after initial Off observation',async()=>{
  const f=fixture();
  await f.helper.tick({mode:'off',wake:{enabled:true,generation:'off'}});
  await f.helper.tick(f.status);
  assert.equal(f.sends.length,1);
});
test('claimed message waits for reconnection without a second claim',async()=>{
  const f=fixture(); const original=f.api;
  f.api=async(...args)=>{const result=await original(...args);if(args[1]==='/wake/claim') f.state.connected=false;return result;};
  await f.helper.tick(f.status);
  f.state={connected:true,sessions:[row('bart:new')]};
  await f.helper.tick(f.status);
  assert.deepEqual(f.sends,[['bart:new','hello']]);
  assert.equal(f.apiCalls.filter(p=>p==='/wake/claim').length,1);
});
test('lost claim response never reconstructs or replays its text',async()=>{
  const f=fixture(); const original=f.api;
  f.api=async(...args)=>{const result=await original(...args);return args[1]==='/wake/claim' ? null : result;};
  await f.helper.tick(f.status); await f.helper.tick(f.status);
  assert.equal(f.sends.length,0);
  assert.match(f.notes.join(' '),/unconfirmed/);
});
