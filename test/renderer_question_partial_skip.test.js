'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { installRenderer, mountRaceSlot, STREAM } = require('./helpers/renderer_chat');
const flush = () => new Promise(resolve => setImmediate(resolve));
function fixture(id) {
  return {notification_id:id, producer:'agent_question.v1', state:'open', title:id, answer_to_stream_id:STREAM,
    question:{question_id:id,producer_stream_id:STREAM,state:'open',response_mode:'single_choice',options:[{label:'Keep',value:'keep'}]}};
}
async function setup(pane = null) {
  const h = installRenderer({questionOverride:pane,dismissResult:{ok:true}});
  await flush(); await flush(); mountRaceSlot(h.context);
  vm.runInContext(`indexDurableQuestionNotification(${JSON.stringify(fixture('a-first'))});indexDurableQuestionNotification(${JSON.stringify(fixture('b-second'))});renderSlotChat(0)`,h.context);
  h.dom.window.document.querySelector('.slot-chat-question-open').click();
  return {...h,doc:h.dom.window.document};
}
test('partial durable send resolves only completed cards and advances preserving the next draft',async () => {
  const h=await setup();
  h.doc.querySelectorAll('.desktop-question-dot')[1].click();
  const draft=h.doc.querySelector('.slot-chat-question-note'); draft.value='pending note';draft.dispatchEvent(new h.dom.window.Event('input'));
  h.doc.querySelectorAll('.desktop-question-dot')[0].click();
  h.doc.querySelector('[data-option="1"]').click();
  const button=h.doc.querySelector('.slot-chat-question-submit');
  assert.equal(button.disabled,false); assert.equal(button.textContent,'Send 1 of 2');button.click();
  await flush();await flush();
  assert.equal(h.notificationResolveCalls.length,1);assert.equal(h.notificationResolveCalls[0].notificationId,'a-first');
  assert.ok(h.doc.querySelector('.desktop-question-portal'));
  assert.equal(h.doc.querySelector('.slot-chat-question-card[data-notification-id]').dataset.notificationId,'b-second');
  assert.equal(h.doc.querySelector('.slot-chat-question-note').value,'pending note');
  assert.equal(h.doc.querySelector('.slot-chat-question-submit').textContent,'Send answer');h.dom.window.close();
});
test('Skip sends reserved text once and removes only its current durable card',async()=>{
 const h=await setup();const skip=h.doc.querySelector('.slot-chat-question-skip');assert.ok(skip);skip.click();skip.click();
 await flush();await flush();assert.equal(h.notificationResolveCalls.length,1);
 assert.deepEqual({...h.notificationResolveCalls[0].options},{submit:true,custom_text:'Skipped by operator'});
 assert.equal(h.doc.querySelector('.slot-chat-question-card[data-notification-id]').dataset.notificationId,'b-second');h.dom.window.close();
});
test('partial pane group stays open while a complete durable card is sent',async()=>{
 const pane={question_key:'pane',multi:true,questions:[{index:0,prompt:'One?',options:[{index:1,label:'Yes'}]},{index:1,prompt:'Two?',options:[{index:1,label:'Yes'}]}]};
 const h=await setup(pane);assert.equal(h.doc.querySelector('.slot-chat-question-skip'),null);
 h.doc.querySelector('[data-option="1"]').click();assert.equal(h.doc.querySelector('.slot-chat-question-submit').disabled,true);
 h.doc.querySelectorAll('.desktop-question-dot')[2].click();h.doc.querySelector('[data-option="1"]').click();h.doc.querySelector('.slot-chat-question-submit').click();
 await flush();await flush();assert.equal(h.dismissCalls.length,0);assert.equal(h.notificationResolveCalls.length,1);
 assert.match(h.doc.querySelector('.slot-chat-question-prompt').textContent,/Two/);h.dom.window.close();
});

test('free-text Skip uses text and a failed resolve keeps the page retryable',async()=>{
 const h=installRenderer({questionOverride:null});await flush();await flush();mountRaceSlot(h.context);
 const f=fixture('free-skip');f.question.response_mode='free_text';f.question.options=[];
 vm.runInContext(`indexDurableQuestionNotification(${JSON.stringify(f)});renderSlotChat(0)`,h.context);
 h.doc=h.dom.window.document;h.doc.querySelector('.slot-chat-question-open').click();
 const calls=[];h.dom.window.cc.notificationResolve=async(...args)=>{calls.push(args);return {ok:false,error:'Skip unavailable'};};
 h.dom.window.cc.promptList=async()=>({ok:true,questions:[]});
 h.doc.querySelector('.slot-chat-question-skip').click();await flush();await flush();
 assert.equal(calls.length,1);assert.equal(calls[0][2].text,'Skipped by operator');
 assert.ok(h.doc.querySelector('.desktop-question-portal'));assert.match(h.doc.querySelector('#cell-0').textContent,/Skip unavailable/);
 h.dom.window.close();
});
