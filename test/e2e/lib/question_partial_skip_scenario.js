'use strict';
const { fixtureRequest } = require('./closed_chat_scenario');
const { reloadDashboardPage } = require('./dashboard_scenario');
// These use the gate's owned producer and the production notification resolver.
async function prepare(ctx, suffix, count, ownedQuestionIds) {
  const {session,fixture,runtime,report}=ctx;
  if(!fixture || !runtime.fixtureTokens) throw new Error('isolated question producer required');
  await session.waitFor(`window.focusStreamId(${JSON.stringify(fixture.streamId)}) === true`);
  await session.click('#header-0 [data-mode="chat"]');
  const questions=[];
  for(let i=0;i<count;i++){
    const questionId=`web-${suffix}-${i}`;
    // Retain the attempted id even if the durable create's reply is lost.
    ownedQuestionIds.push(questionId);
    const response=await fixtureRequest(runtime,fixture.streamId,{type:'prompt.ask',actions:[{kind:'yes_no',action_id:'a0',label:'Keep',choice:true,value:{schema_version:1,question_id:questionId,answer:'keep'}}],envelope:{schema_version:1,question_id:questionId,dedup_key:questionId,producer_stream_id:fixture.streamId,title:`Question ${i+1}`,body:'Choose a fixture result',response_mode:'single_choice',allow_custom:true,options:[{label:'Keep',value:'keep'}]}});
    questions.push(response.question);
    report.ok(`${suffix} fixture ${i+1} created`,response.ok===true,response);
  }
  await session.waitFor(`!!document.querySelector('#cell-0 .slot-chat-question-open')`);
  await session.click('#cell-0 .slot-chat-question-open');
  // The renderer mounts one active card, with a dot per entry only for a
  // multi-question deck. Verify every owned card through that real pager.
  const portal=`#desktop-question-portal-${fixture.streamId.replace(/[^a-zA-Z0-9_-]/g, '-')}`;
  const ready=index=>`!!document.querySelector(${JSON.stringify(`${portal} [data-notification-id="${questions[index].notification_id}"]`)}) && document.querySelectorAll(${JSON.stringify(`${portal} .desktop-question-dot`)}).length === ${count > 1 ? count : 0}`;
  await session.waitFor(ready(0));
  for(let i=1;i<questions.length;i++){
    await session.click(`${portal} .desktop-question-dot:nth-child(${i+1})`);
    await session.waitFor(ready(i));
  }
  if(questions.length>1){
    await session.click(`${portal} .desktop-question-dot:nth-child(1)`);
    await session.waitFor(ready(0));
  }
  return questions;
}

async function withQuestions(ctx, suffix, count, run) {
  const ownedQuestionIds=[];
  let primaryError;
  try {
    await run(await prepare(ctx,suffix,count,ownedQuestionIds));
  } catch(error) {
    primaryError=error;
    throw error;
  } finally {
    const errors=[];
    const status=async questionId=>{
      const result=await fixtureRequest(ctx.runtime,ctx.fixture.streamId,{type:'prompt.status',question_id:questionId});
      if(result.error_code==='question_not_found' && result.ok===false) return null;
      if(result.ok!==true || result.type!=='prompt.status.ok' || result.question?.question_id!==questionId || result.question?.producer_stream_id!==ctx.fixture.streamId) throw new Error(`question cleanup status failed: ${questionId}`);
      return result.question;
    };
    for(const questionId of ownedQuestionIds){
      try {
        let question=await status(questionId);
        if(!question) continue;
        if(question.state==='open'){
          const cancelled=await fixtureRequest(ctx.runtime,ctx.fixture.streamId,{type:'prompt.cancel',question_id:questionId});
          // An answer may win between status and cancellation. Re-read that
          // specific race; all other cancellation errors remain failures.
          if(cancelled.ok!==true && cancelled.error_code!=='question_terminal_state') throw new Error(`question cleanup cancel failed: ${questionId}`);
          question=await status(questionId);
        }
        if(!question || !['answered','dismissed','expired'].includes(question.state)) throw new Error(`question cleanup remained open: ${questionId}`);
      } catch(error) { errors.push(error); }
    }
    try { await reloadDashboardPage(ctx); } catch(error) { errors.push(error); }
    if(errors.length){
      if(primaryError){
        // Preserve the original Error identity/message/stack and fail the
        // scenario on that primary cause, while retaining cleanup evidence.
        try { primaryError.cleanupErrors=errors; } catch {}
        try { ctx.report.note('question fixture cleanup failed: '+errors.map(error=>error.message).join('; ')); } catch {}
      } else {
        throw new AggregateError(errors,'question fixture cleanup failed: '+errors.map(error=>error.message).join('; '));
      }
    }
  }
}
async function questionPartialSend(ctx){
 const {session,fixture,runtime,report,cdp,timeoutMs}=ctx;
 return withQuestions(ctx,'partial-send',2,async([first,second])=>{
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
 });
}
async function questionSkip(ctx){
 const {session,fixture,runtime,report,cdp,timeoutMs}=ctx;
 return withQuestions(ctx,'skip',1,async([question])=>{
 await session.click('.slot-chat-question-skip');
 await session.waitFor(`!document.querySelector('[data-notification-id="${question.notification_id}"]')`);
 const delivery=await fixtureRequest(runtime,fixture.streamId,{type:'notification.await',notification_id:question.notification_id,timeout:1});
 const status=await fixtureRequest(runtime,fixture.streamId,{type:'prompt.status',question_id:question.question_id});
 const expected=await session.eval('window.PentacleChatCore.QUESTION_SKIP_TEXT');
 report.ok('Skip is an ordinary answered prompt with the shared reserved text',status.question?.state==='answered'&&(status.question.answer?.text||status.question.answer?.custom_text)===expected,{state:status.question?.state,answer:status.question?.answer});
 report.ok('producer await readback receives the skip answer',delivery.ok===true&&(delivery.answer?.text||delivery.answer?.custom_text)===expected,delivery);
 await session.waitFor(`!document.querySelector('.desktop-question-portal')`);
 });
}
module.exports={questionPartialSend,questionSkip};
