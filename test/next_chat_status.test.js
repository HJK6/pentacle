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

edge('elapsed fallback is seeded once, ticks, and discards the seed on idle or replacement', () => {
  const {workingClock} = status;
  const clock=workingClock();
  const row={stream_id:'fixture:a',working:true,working_label:'7s'};
  assert.equal(clock.elapsed(row,null,'idle',null,0), '7s');
  assert.equal(clock.elapsed(row,null,'idle',null,2200), '9s');
  assert.equal(clock.elapsed(row,null,'idle',64000,2200), '1m 04s');
  assert.equal(clock.elapsed({...row,working:false},null,'idle',null,2300), '');
  assert.equal(clock.elapsed(row,null,'idle',null,3500), '7s');
  clock.retain(new Set());
  assert.equal(clock.elapsed(row,null,'idle',null,5500), '7s');
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
