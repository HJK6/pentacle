'use strict';
const { fixtureRequest } = require('./closed_chat_scenario');
const { reloadDashboardPage } = require('./dashboard_scenario');
// These use the gate's owned producer and the production notification resolver.
async function prepare(ctx, suffix, count) {
  const {session,fixture,runtime,report}=ctx;
  if(!fixture || !runtime.fixtureTokens) throw new Error('isolated question producer required');
  await session.waitFor(`window.focusStreamId(${JSON.stringify(fixture.streamId)}) === true`);
  await session.click('#header-0 [data-mode="chat"]');
  const questions=[];
  for(let i=0;i<count;i++){
    const questionId=`web-${suffix}-${i}`;
    const response=await fixtureRequest(runtime,fixture.streamId,{type:'prompt.ask',actions:[{kind:'yes_no',action_id:'a0',label:'Keep',choice:true,value:{schema_version:1,question_id:questionId,answer:'keep'}}],envelope:{schema_version:1,question_id:questionId,dedup_key:questionId,producer_stream_id:fixture.streamId,title:`Question ${i+1}`,body:'Choose a fixture result',response_mode:'single_choice',allow_custom:true,options:[{label:'Keep',value:'keep'}]}});
    report.ok(`${suffix} fixture ${i+1} created`,response.ok===true,response);
    questions.push(response.question);
  }
  await session.waitFor(`!!document.querySelector('#cell-0 .slot-chat-question-open')`);
  await session.click('#cell-0 .slot-chat-question-open');
  await session.waitFor(`!!document.querySelector('[data-notification-id="${questions[0].notification_id}"]')`);
  return questions;
}
async function questionPartialSend(ctx){
 const {session,fixture,runtime,report,cdp,timeoutMs}=ctx;
 const [first,second]=await prepare(ctx,'partial-send',2);
 await session.click('.desktop-question-portal .slot-chat-question-option[data-option="1"]');
 const control=await session.eval(`(()=>{const b=document.querySelector('.slot-chat-question-submit');return {label:b.textContent,disabled:b.disabled}})()`);
 report.ok('one completed card enables Send 1 of 2',control.label==='Send 1 of 2'&&!control.disabled,control);
 await session.click('.slot-chat-question-submit');
 await session.waitFor(`!!document.querySelector('[data-notification-id="${second.notification_id}"]')`);
 const a=await fixtureRequest(runtime,fixture.streamId,{type:'prompt.status',question_id:first.question_id});
 const b=await fixtureRequest(runtime,fixture.streamId,{type:'prompt.status',question_id:second.question_id});
 report.ok('partial send answers only the complete card and keeps the deck open',a.question.state==='answered'&&b.question.state==='open',{first:a.question.state,second:b.question.state});
 await session.click('.slot-chat-question-skip');
 await session.waitFor(`!document.querySelector('.desktop-question-portal')`);
 await reloadDashboardPage({session,cdp,timeoutMs});
}
async function questionSkip(ctx){
 const {session,fixture,runtime,report,cdp,timeoutMs}=ctx;
 const [question]=await prepare(ctx,'skip',1);
 await session.click('.slot-chat-question-skip');
 await session.waitFor(`!document.querySelector('[data-notification-id="${question.notification_id}"]')`);
 const delivery=await fixtureRequest(runtime,fixture.streamId,{type:'notification.await',notification_id:question.notification_id,timeout:1});
 const status=await fixtureRequest(runtime,fixture.streamId,{type:'prompt.status',question_id:question.question_id});
 const expected=await session.eval('window.PentacleChatCore.QUESTION_SKIP_TEXT');
 report.ok('Skip is an ordinary answered prompt with the shared reserved text',status.question?.state==='answered'&&(status.question.answer?.text||status.question.answer?.custom_text)===expected,{state:status.question?.state,answer:status.question?.answer});
 report.ok('producer await readback receives the skip answer',delivery.ok===true&&(delivery.answer?.text||delivery.answer?.custom_text)===expected,delivery);
 await session.waitFor(`!document.querySelector('.desktop-question-portal')`);
 await reloadDashboardPage({session,cdp,timeoutMs});
}
module.exports={questionPartialSend,questionSkip};
