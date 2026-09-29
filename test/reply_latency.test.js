'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {createWakeDelivery}=require('../renderer/wake_delivery');
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
test('delivery read rounds fit the capture latency budget',async()=>{
 const status={mode:'on',wake:{enabled:true,generation:'g',pending_count:1}};
 const seat={stream_id:'workstation:seat',session_generation:'seat',status:'open',pane_status:'pane_alive'};
 let claimedAt,sentAt;
 const started=performance.now();
 const delivery=createWakeDelivery({config:{features:{mic:true},chatStream:{}},
  getState:async()=>{await sleep(80);return {connected:true,sessions:[seat]};},
  getBinding:async()=>{await sleep(80);return {ok:true,source:'durable',stream_id:seat.stream_id,generation:'seat'};},
  api:async(method,path)=>{await sleep(80);if(path==='/status')return status;claimedAt=performance.now();return {claim:{id:'c',generation:'g',conversation_id:'conversation',text:'Hello'}};},
  sendTurn:()=>{sentAt=performance.now();return 'optimistic';}});
 await delivery.tick(status);
 assert.ok(claimedAt-started<300);
 assert.ok(sentAt-started<300,`read rounds took ${sentAt-started} ms`);
});

test('provider root records actual delivery once without turning receipts into delivery',async()=>{
 const {createRoomMicTurns,roomMicHeader}=require('../renderer/room_mic_turns');
 const calls=[];
 const tracker=createRoomMicTurns({api:async(...args)=>calls.push(args)});
 const text=roomMicHeader('c','Hello',{sentences_per_line:3,words_per_line:50,characters_per_line:400,helper:'bart-say'});
 assert.ok(text.includes('"sentences_per_line":3'));
 tracker.register({streamId:'workstation:seat',conversationId:'c',optimisticId:'o',requestId:'r',text});
 const root={stream_id:'workstation:seat',daemon_seq:1,kind:'USER',text,request_id:'r',timestamp:'2026-09-29T07:00:00Z',raw:{transport:'send-receipt'}};
 tracker.observe({type:'chat.event',event:root});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(calls.length,0);
 root.daemon_seq=2;root.raw.transport='codex-rollout';
 tracker.observe({type:'chat.event',event:root});
 tracker.observe({type:'chat.event',event:root});
 await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(calls,[['POST','/conversation/timing',{conversation_id:'c',stage:'delivered_at',at:Date.parse(root.timestamp)/1000}]]);
});
