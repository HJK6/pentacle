'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const candidate = path.join(__dirname, '../renderer/next/chat_status.js');
const status = fs.existsSync(candidate) ? require(candidate) : null;
// The pinned base exercises the same U1-U5 expectations via the real UI in
// next_fluid_shell; these supplementary tests cover pure edge cases at tip.
const edge = (name, fn) => test(name, {skip: !status && 'pure edge case: pinned-base behavior exercised via Next shell'}, fn);

edge('ordinary elapsed carries the authoritative start and resets across turn, idle and generation', () => {
  const {workingClock} = status;
  const clock = workingClock();
  const row = {stream_id:'fixture:a', session_generation:'a', working:true};
  const turn = {phase:'pending', optimisticId:'send-a', sentAt:1};
  assert.equal(clock.elapsed({...row, working:false}, null, turn, 7000, 0), '', 'pending alone has no duration');
  assert.equal(clock.elapsed({...row, working_label:'7s'}, null, turn, null, 0), '7s');
  assert.equal(clock.elapsed(row, null, turn, null, 2200), '9s');
  assert.equal(clock.elapsed(row, null, turn, 64000, 2200), '1m 04s');
  const confirmed = {...turn, phase:'working', firstServerEventAt:2300};
  assert.equal(clock.elapsed(row, null, confirmed, null, 3200), '1m 05s');
  assert.equal(clock.elapsed(row, null, {...confirmed, phase:'idle', endedAt:3300}, null, 3300), '0s');
  assert.equal(clock.elapsed({...row, working:false}, null, undefined, null, 3500), '');
  assert.equal(clock.elapsed(row, null, undefined, null, 5500), '0s');
  assert.equal(clock.elapsed(row, null, undefined, null, 9500), '4s');
  assert.equal(clock.elapsed({...row, session_generation:'b'}, null, undefined, null, 9600), '0s');
  clock.retain(new Set());
  assert.equal(clock.elapsed({...row, session_generation:'b'}, null, undefined, null, 11000), '0s');
});

edge('a running time counts from its first report and ignores heartbeat jitter, but follows a real correction', () => {
  const {workingClock} = status;
  const clock = workingClock();
  const row = {stream_id:'fixture:a', session_generation:'a', working:true};
  assert.equal(clock.elapsed(row, null, undefined, 478600, 100000), '7m 58s');
  assert.equal(clock.untilNextSecond(100000), 400, 'the next step lands on the whole second');
  // Heartbeats restate the start up to 0.4 s either way; the shown second keeps its own pace.
  assert.equal(clock.elapsed(row, null, undefined, 483100 + 400, 104500), '8m 03s');
  assert.equal(clock.elapsed(row, null, undefined, 488600 - 400, 110000), '8m 08s');
  assert.equal(clock.untilNextSecond(110000), 400);
  assert.equal(clock.elapsed(row, null, undefined, null, 111400), '8m 10s', 'a missing report keeps counting');
  assert.equal(clock.elapsed(row, null, undefined, 4000, 120000), '4s', 'a new turn restarts it');
  const composite = {stream_id:'fixture:c', session_kind:'assistant_composite', working:true};
  assert.equal(clock.elapsed(composite, null, undefined, 9000, 1000), '9s');
  assert.equal(clock.elapsed(composite, null, undefined, 10300, 2000), '10s', 'the assistant time is steady too');
  assert.equal(clock.elapsed(composite, null, undefined, null, 3000), '', 'and still has no fallback');
  assert.equal(workingClock().untilNextSecond(0), null, 'nothing running needs no wake');
});

edge('seen marks key by stream, survive temporary absence and reload, collapse identical messages and drop explicit closes', () => {
  const {createSeenMarks} = status;
  const a={stream_id:'f:a',last_kind:'ASSIST',last_text:'a'};
  const b={stream_id:'f:b',last_kind:'ASSIST',last_text:'b'};
  const comp={stream_id:'f:c',session_kind:'assistant_composite',last_kind:'NOTICE',last_text:'c'};
  const storage=new Map();
  const options={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)};
  let marks=createSeenMarks(options);
  marks.update([],new Set(),true,true);
  marks.update([a,b,comp],new Set(),true,true);
  assert.ok(!marks.unread(a) && !marks.unread(comp));
  marks.update([a,comp],new Set(),true,true);
  marks.update([a,b,comp],new Set(),true,true);
  assert.equal(marks.unread({...b}),false);
  const newComp={...comp,last_text:'something new'};
  assert.equal(marks.unread(newComp),true);
  assert.equal(marks.unread({stream_id:'f:d',last_kind:'',last_text:''}),false);
  assert.equal(marks.unread({stream_id:'f:d',last_kind:'USER',last_text:'mine'}),false);
  marks=createSeenMarks(options);
  assert.equal(marks.unread(newComp),true);
  marks.update([{...a,status:'closed'},b,comp],new Set(),true,true);
  assert.equal(marks.unread(a),true,'same identity after a confirmed close has no old mark');
  marks.forget(b.stream_id);
  assert.equal(marks.unread(b),true);
  marks=createSeenMarks({getItem(){throw Error();},setItem(){throw Error();}});
  marks.update([a,b,comp],new Set(),true,true);
  assert.ok(!marks.unread(a) && !marks.unread(comp));
});

edge('send states have exactly the approved words, glyph and queued-only gate', () => {
  const {sendLine}=status;
  for (const [state,label,glyph] of [['sending','Sending','stream'],['sent','Sent','check'],
    ['queued','Queued','chevron'],['cancelled','Cancelled',null],['failed','Failed to send',null],
    ['indeterminate','Delivery uncertain',null]]) {
    assert.deepEqual(sendLine(state,true),{label,glyph,sendNow:state==='queued'});
    assert.deepEqual(sendLine(state,false),{label,glyph,sendNow:false});
  }
});

edge('one shared presentation module keeps legacy labels and exact configured protection', () => {
  const {modelLabel,isProtectedAssistant}=require('../renderer/chat_presentation');
  assert.equal(modelLabel('claude-opus-5-5'),'Opus 5.5');
  assert.equal(modelLabel('gpt-6-sol'),'6 Sol');
  assert.equal(modelLabel('unknown-model'),'unknown-model');
  assert.equal(isProtectedAssistant([{display_name:'assistant'}],'assistant'),false);
  assert.equal(isProtectedAssistant([null,{role:'assistant'}],'assistant'),true);
  assert.equal(isProtectedAssistant([{session_kind:'assistant_composite'}],''),true);
});

edge('assistant composite suppresses a known model and effort in the shared formatter', () => {
  const {modelAndEffort}=status;
  const metadata={model:'claude-sonnet-5',effort:'low',effective_model:'claude-opus-5-5',effective_effort:'high'};
  assert.equal(modelAndEffort({...metadata,session_kind:'session'}),'Opus 5.5 · high');
  assert.equal(modelAndEffort({...metadata,session_kind:'assistant_composite'}),'');
});
