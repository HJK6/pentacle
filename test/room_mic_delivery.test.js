'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createWakeDelivery } = require('../renderer/wake_delivery');

test('claimed room input delivers the service conversation header once across binding handoff', async () => {
  let target = 'workstation:old';
  const calls = [], sends = [], turns = [];
  const status = {mode:'on', wake:{enabled:true,generation:'generation',pending_count:1}};
  const delivery = createWakeDelivery({
    config:{features:{mic:true},chatStream:{}},
    getState:async()=>({connected:true,sessions:[{stream_id:target,session_generation:'seat',status:'open',pane_status:'pane_alive'}]}),
    getBinding:async()=>({ok:true,source:'durable',stream_id:target,generation:'seat'}),
    api:async(method,path)=>{
      calls.push(path);
      if(path==='/status') return status;
      target='workstation:new';
      return {claim:{id:'capture',generation:'generation',conversation_id:'conversation-one',text:'What time is it?'}};
    },
    sendTurn:(...args)=>{sends.push(args);return 'optimistic-one';},
    onRoomMicTurn:turn=>turns.push(turn),
  });
  await delivery.tick(status);
  status.wake.pending_count=0;
  await delivery.tick(status);
  await delivery.tick(status);
  assert.deepEqual(sends,[['workstation:new','[pentacle-input {"origin":"room_mic","conversation_id":"conversation-one"}]\n\nWhat time is it?']]);
  assert.equal(calls.filter(path=>path==='/wake/claim').length,1);
  assert.equal(turns.length,1);
  assert.equal(turns[0].conversationId,'conversation-one');
  assert.equal(turns[0].optimisticId,'optimistic-one');
});
test('a legacy claim without a service-issued conversation id stays held',async()=>{
  const status={mode:'on',wake:{enabled:true,generation:'g',pending_count:1}};
  let sends=0,claims=0;
  const delivery=createWakeDelivery({config:{features:{mic:true,assistantRole:'assistant'},mic:{wakeTargetHost:'workstation'},chatStream:{}},
    getState:async()=>({connected:true,sessions:[{stream_id:'workstation:seat',host:'workstation',role:'assistant',status:'open',pane_status:'pane_alive'}]}),
    api:async(method,path)=>path==='/status'?status:(claims++,{claim:{id:'capture',generation:'g',text:'hello'}}),
    sendTurn:()=>sends++});
  await delivery.tick(status);await delivery.tick(status);
  assert.equal(sends,0);assert.equal(claims,1);
});
