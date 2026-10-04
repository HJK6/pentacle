'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { roomMicHeader, createRoomMicTurns } = require('../renderer/room_mic_turns');
const flush = () => new Promise(resolve=>setImmediate(resolve));
function fixture() {
  const calls=[];
  const tracker=createRoomMicTurns({api:async(...args)=>{if(args[1]==='/turn-ended') calls.push(args);return {outcome:'suppressed',reason:'silent'};},
    isSystemEndOfTurnEvent:event=>event.kind==='SYSTEM'&&event.raw?.subtype==='turn-summary'});
  const text=roomMicHeader('conversation','hello');
  tracker.register({streamId:'workstation:seat',conversationId:'conversation',optimisticId:'optimistic',requestId:'request',text});
  let seq=0;
  const emit=(kind,raw={},extra={})=>tracker.observe({type:'chat.event',event:{stream_id:'workstation:seat',daemon_seq:++seq,kind,raw,text,...extra}});
  return {calls,tracker,emit,text};
}
test('header serializes opaque data and refuses missing or control-character ids',()=>{
  assert.equal(roomMicHeader(null,'text'),null);
  assert.equal(roomMicHeader('bad\nid','text'),null);
  assert.equal(roomMicHeader('a"b','text'),'[pentacle-input {"origin":"room_mic","conversation_id":"a\\"b"}]\n\ntext');
});
for(const provider of ['codex-rollout','claude-jsonl'])test(`${provider} calls turn-ended exactly once after the corresponding provider final`,async()=>{
  const f=fixture();
  f.emit('ASSIST',{transport:provider,phase:'final_answer',stop_reason:'end_turn'}); // Prior turn.
  f.emit('USER',{transport:provider},{request_id:'request'});
  f.emit('ASSIST',{transport:provider,phase:'commentary',stop_reason:'tool_use'});
  f.emit('WORKING',{working:false});
  await flush();assert.equal(f.calls.length,0);
  f.emit('ASSIST',{transport:provider,phase:'final_answer',stop_reason:'end_turn'});
  f.emit('SYSTEM',{subtype:'turn-summary'});
  f.emit('ASSIST',{transport:provider,phase:'final_answer',stop_reason:'end_turn'});
  await flush();
  assert.deepEqual(f.calls,[['POST','/turn-ended',{conversation_id:'conversation'}]]);
});
test('typed/mobile user roots, receipts, sidechains and history do not end the room request',async()=>{
  const f=fixture();
  f.emit('USER',{transport:'send-receipt'},{request_id:'request'});
  f.emit('ASSIST',{transport:'codex-rollout',phase:'final_answer'});
  f.emit('USER',{transport:'codex-rollout'},{text:'typed message'});
  f.emit('ASSIST',{transport:'codex-rollout',phase:'final_answer'});
  f.tracker.observe({type:'stream_events',events:[{kind:'USER',text:f.text}]});
  f.emit('USER',{transport:'codex-rollout',is_sidechain:true},{request_id:'request'});
  f.emit('ASSIST',{transport:'codex-rollout',phase:'final_answer'});
  await flush();assert.equal(f.calls.length,0);
  f.emit('USER',{transport:'codex-rollout'},{request_id:'request'});
  f.emit('ASSIST',{transport:'codex-rollout',phase:'final_answer',is_sidechain:true});
  await flush();assert.equal(f.calls.length,0);
  f.emit('SYSTEM',{subtype:'turn-summary'});
  await flush();assert.equal(f.calls.length,1);
});
test('offline completion callback cannot fail chat delivery and is never retried',async()=>{
  let calls=0;
  const tracker=createRoomMicTurns({api:async(method,path)=>{if(path==='/turn-ended') calls++;throw Error('offline');}});
  tracker.register({streamId:'workstation:seat',conversationId:'id',optimisticId:'o',text:'tagged'});
  const event={stream_id:'workstation:seat',daemon_seq:1,kind:'USER',text:'tagged',raw:{transport:'codex-rollout'}};
  tracker.observe({type:'chat.event',event});
  const final={...event,daemon_seq:2,kind:'ASSIST',raw:{transport:'codex-rollout',phase:'final_answer'}};
  tracker.observe({type:'chat.event',event:final});
  tracker.observe({type:'chat.event',event:final});
  await flush();assert.equal(calls,1);
});
test('reconnect backfill completes a delivered turn once and ignores older history',async()=>{
  const f=fixture();
  f.tracker.observe({type:'snapshot',events:[
    {stream_id:'workstation:seat',daemon_seq:1,kind:'ASSIST',text:'old',raw:{transport:'codex-rollout',phase:'final_answer'}},
    {stream_id:'workstation:seat',daemon_seq:2,kind:'USER',text:f.text,raw:{transport:'codex-rollout'}},
  ]});
  f.tracker.observe({type:'stream_events',events:[
    {stream_id:'workstation:seat',daemon_seq:3,kind:'ASSIST',text:'done',raw:{transport:'codex-rollout',phase:'final_answer'}},
  ]});
  await flush();assert.equal(f.calls.length,1);
});
test('merged actual distinct-header roots close only the active root; uncertain roots stay open',async()=>{
  const calls=[];
  const tracker=createRoomMicTurns({api:async(...args)=>{if(args[1]==='/turn-ended') calls.push(args);return {outcome:'suppressed',reason:'silent'};},onOutcome:()=>{}});
  const stream_id='workstation:seat';
  const first=roomMicHeader('first','Repeat the request');
  const second=roomMicHeader('second','Repeat the request');
  tracker.register({streamId:stream_id,conversationId:'first',optimisticId:'o1',requestId:'r1',text:first});
  tracker.register({streamId:stream_id,conversationId:'second',optimisticId:'o2',requestId:'r2',text:second});
  tracker.observe({type:'chat.event',event:{stream_id,daemon_seq:1,kind:'USER',text:first,request_id:'r1',raw:{transport:'codex-rollout'}}});
  tracker.observe({type:'chat.event',event:{stream_id,daemon_seq:2,kind:'USER',text:second,request_id:'r2',raw:{transport:'codex-rollout'}}});
  tracker.observe({type:'chat.event',event:{stream_id,daemon_seq:3,kind:'ASSIST_TEXT',text:'First result',raw:{transport:'codex-rollout',phase:'final_answer'}}});
  await flush();
  assert.deepEqual(calls,[['POST','/turn-ended',{conversation_id:'first'}]]);
  tracker.observe({type:'chat.event',event:{stream_id,daemon_seq:4,kind:'ASSIST_TEXT',text:'Second result',raw:{transport:'codex-rollout',phase:'final_answer'}}});
  await flush();
  assert.deepEqual(calls,[['POST','/turn-ended',{conversation_id:'first'}]]); // No authoritative second root/final pair.
});
test('a mismatched authoritative root id never falls back to equal tagged text',async()=>{
  const f=fixture();
  f.emit('USER',{transport:'codex-rollout'},{request_id:'other-request'});
  f.emit('ASSIST',{transport:'codex-rollout',phase:'final_answer'});
  await flush();assert.equal(f.calls.length,0);
});
test('typed first root prevents a merged room root from taking the typed final',async()=>{
 const f=fixture();
 f.emit('USER',{transport:'codex-rollout'},{text:'typed request',request_id:'typed'});
 f.emit('USER',{transport:'codex-rollout'},{request_id:'request'});
 f.emit('ASSIST',{transport:'codex-rollout',phase:'final_answer'});
 await flush();assert.equal(f.calls.length,0);
});
test('separate answer roots may reuse a conversation id and each end once',async()=>{
 const f=fixture();
 f.tracker.register({streamId:'workstation:seat',conversationId:'conversation',optimisticId:'answer-o',requestId:'answer-r',text:roomMicHeader('conversation','answer')});
 f.emit('USER',{transport:'codex-rollout'},{request_id:'request'});
 f.emit('ASSIST',{transport:'codex-rollout',phase:'final_answer'});
 f.emit('USER',{transport:'codex-rollout'},{request_id:'answer-r',text:roomMicHeader('conversation','answer')});
 f.emit('ASSIST',{transport:'codex-rollout',phase:'final_answer'});
 await flush();assert.equal(f.calls.length,2);
});
test('a terminal with another authoritative id never closes the active room root',async()=>{
 const f=fixture();
 f.emit('USER',{transport:'codex-rollout'},{request_id:'request'});
 f.emit('ASSIST',{transport:'codex-rollout',phase:'final_answer'},{request_id:'other'});
 await flush();assert.equal(f.calls.length,0);
});
test('answer_to is included only for an answer turn, after conversation_id',()=>{
  assert.equal(roomMicHeader('conv','ans','','line-9'),'[pentacle-input {"origin":"room_mic","conversation_id":"conv","answer_to":"line-9"}]\n\nans');
  assert.equal(roomMicHeader('conv','msg'),'[pentacle-input {"origin":"room_mic","conversation_id":"conv"}]\n\nmsg');
  assert.equal(roomMicHeader('conv','msg',null,'bad\nid'),'[pentacle-input {"origin":"room_mic","conversation_id":"conv"}]\n\nmsg');
});
