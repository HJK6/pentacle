'use strict';
const { fixtureRequest } = require('./closed_chat_scenario');

// Exercise the complete renderer adapter and durable resolution using only the
// gate's isolated producer. Live asking-seat delivery is qualified separately.
async function questionFreeText({ session, report, fixture, runtime, cdp }) {
  if (!fixture || !runtime.fixtureTokens) {
    report.note('no isolated question producer: free-text scenario excluded');
    return;
  }
  await session.waitFor("document.readyState === 'complete' && typeof window.focusStreamId === 'function'");
  await session.waitFor('window.cc.getChatStreamState().then(s => s.connected === true)');
  await session.eval(`window.focusStreamId(${JSON.stringify(fixture.streamId)})`);
  await session.waitFor(`!!document.querySelector('#header-0 [data-mode="chat"]')`);
  await session.click('#header-0 [data-mode="chat"]');
  for (const [index, mode, allowCustom] of [[0, 'free_text', true], [1, 'free_text', false], [2, 'single_choice', true], [3, 'multi_choice', true]]) {
    const questionId = `web-free-text-${index}`;
    const options = mode === 'free_text' ? [] : [{ label: 'Alpha', value: 'alpha' }, { label: 'Beta', value: 'beta' }];
    const asked = await fixtureRequest(runtime, fixture.streamId, {
      type: 'prompt.ask', envelope: { schema_version: 1, question_id: questionId,
        title: 'Gate input', body: 'Describe the result', dedup_key: questionId,
        producer_stream_id: fixture.streamId, response_mode: mode, allow_custom: allowCustom, options },
      actions: options.map((option, i) => ({ kind: 'yes_no', action_id: `a${i}`, label:option.label, choice:i===0,
        value:{schema_version:1,question_id:questionId,answer:option.value} })),
    });
    report.ok(`isolated ${mode} question created (${allowCustom})`, asked.ok === true, asked.error);
    const id = asked.question.notification_id;
    const card = `[data-notification-id="${id}"]`;
    await session.waitFor(`!!document.querySelector('#cell-0 .slot-chat-question-open')`);
    await session.click('#cell-0 .slot-chat-question-open');
    await session.waitFor(`!!document.querySelector(${JSON.stringify(card)})`);
    if (options.length) {
      await session.click(`${card} [data-option="1"]`);
      await session.click(`${card} .slot-chat-question-custom`);
    }
    const state = await session.eval(`(() => { const c=document.querySelector(${JSON.stringify(card)}), t=c.querySelector('.slot-chat-question-freetext'); return {visible:!t.hidden && t.getBoundingClientRect().height>0,label:t.getAttribute('aria-label'),disabled:c.querySelector('.slot-chat-question-submit').disabled}; })()`);
    report.ok(`${mode} has visible labeled editable answer`, state.visible && !!state.label && state.disabled, state);
    await session.type(`${card} .slot-chat-question-freetext`, '  ');
    report.ok(`${mode} whitespace cannot submit`, await session.eval(`document.querySelector(${JSON.stringify(card+' .slot-chat-question-submit')}).disabled`));
    // A real transport reconnect must retain the editable draft and question.
    await session.type(`${card} .slot-chat-question-freetext`, `answer-${index}`);
    await session.send('Network.enable');
    await session.send('Network.emulateNetworkConditions', {offline:true,latency:0,downloadThroughput:0,uploadThroughput:0});
    await cdp.sleep(200);
    await session.send('Network.emulateNetworkConditions', {offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1});
    await session.waitFor(`document.querySelector(${JSON.stringify(card+' .slot-chat-question-freetext')})?.value === 'answer-${index}'`);
    await session.waitFor('window.cc.getChatStreamState().then(s => s.connected === true)');
    await cdp.sleep(500);
    await session.eval(`(() => { window.__questionResolutions=[]; const original=window.cc.notificationResolve; window.cc.notificationResolve=async(...args)=>{const result=await original(...args);window.__questionResolutions.push({args,result});return result;}; const b=document.querySelector(${JSON.stringify(card+' .slot-chat-question-submit')}); b.click(); b.click(); })()`);
    await cdp.sleep(500);
    const submissions = await session.eval('window.__questionResolutions');
    report.ok(`${mode} duplicate click produces one accepted resolution`, submissions.length === 1 && submissions[0].result.ok === true,
      { count:submissions.length, ok:submissions[0]?.result.ok, error:submissions[0]?.result.error });
    const status = await fixtureRequest(runtime, fixture.streamId, { type:'prompt.status', question_id:questionId });
    report.ok(`${mode} durable answer resolved once to its producer`, status.question?.state === 'answered' && status.question?.producer_stream_id === fixture.streamId,
      { state:status.question?.state, answer:status.question?.answer, producer:status.question?.producer_stream_id });
    await session.waitFor(`!document.querySelector(${JSON.stringify(card)})`);
    await session.eval('window.__questionOldDocument = true');
    await session.send('Page.reload');
    await session.waitFor("!window.__questionOldDocument && document.readyState==='complete' && typeof window.focusStreamId==='function'");
    await session.eval(`window.focusStreamId(${JSON.stringify(fixture.streamId)})`);
    await session.waitFor(`!!document.querySelector('#header-0 [data-mode="chat"]')`);
    await session.click('#header-0 [data-mode="chat"]');
    await session.waitFor(`!document.querySelector(${JSON.stringify(card)})`);
    report.ok(`${mode} answered card stays retired after reload`, true);
  }
}
module.exports = { questionFreeText };
