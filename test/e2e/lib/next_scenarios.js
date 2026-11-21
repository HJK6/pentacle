'use strict';
const path = require('path');
function isolated(run) { return async ctx => { try { await run(ctx); } finally { await ctx.session.send('Emulation.clearDeviceMetricsOverride'); await ctx.session.send('Page.navigate', { url: ctx.url }); await ctx.session.waitFor("document.readyState === 'complete' && !!document.getElementById('session-list')", { timeoutMs: ctx.timeoutMs }); } }; }
async function visit(ctx, width = 1440) {
  await ctx.session.send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false });
  await ctx.session.send('Page.navigate', { url: ctx.url + 'next/' });
  await ctx.session.waitFor("!!window.cc && !!window.PentacleChatStore && document.querySelectorAll('.rail-button').length === 6", { timeoutMs: ctx.timeoutMs });
  await ctx.session.eval("document.documentElement.dataset.theme='dark'; localStorage.setItem('pentacle.next.theme','dark'); true");
  await ctx.session.waitFor("window.cc.getChatStreamState().then(s => s.connected === true && (s.sessions || []).length > 0)", { timeoutMs: ctx.timeoutMs });
}
async function homeRail(ctx) {
  const { session, report } = ctx;
  for (const width of [1440, 1010, 440]) {
    await visit(ctx, width);
    const expectedCount = await session.eval(`(async () => {
      const reply = await window.cc.promptList({ open: true });
      if (!reply?.ok || !Array.isArray(reply.questions)) throw new Error('home rail open-question read failed');
      const open = reply.questions.filter(question => question.state === 'open');
      if (open.some(question => !question.question_id)) throw new Error('open question has no ID');
      return new Set(open.map(question => question.question_id)).size;
    })()`);
    const expectedLabel = expectedCount ? `Questions (${expectedCount})` : 'Questions';
    const expectedNames = ['Assistant Fixture', 'Lanes', 'Chats', expectedLabel, 'Limits and machines', 'Settings'];
    await session.waitFor(`document.querySelectorAll('.rail-button')[3]?.dataset.count === ${JSON.stringify(String(expectedCount))}`,
      { timeoutMs: ctx.timeoutMs, label: `home rail hydrates the daemon's ${expectedCount} open questions at ${width}` });
    const info = await session.eval(`(() => {
      const rail = document.querySelector('.rail');
      const buttons = [...rail.querySelectorAll('.rail-button')];
      return {
        names: buttons.map(button => button.getAttribute('aria-label')),
        counts: buttons.filter(button => button.hasAttribute('data-count')).map(button => [button.getAttribute('aria-label'), button.dataset.count]),
        text: rail.innerText, input: !!rail.querySelector('input'),
        overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
        assistant: document.querySelector('.pane .thread-inner').dataset.streamId,
        lit: rail.querySelector('.brand').classList.contains('on'),
        chatLit: rail.querySelector('[aria-label=Chats]').classList.contains('on'),
      };
    })()`);
    report.ok(`rail has six icon-only controls at ${width}`, JSON.stringify(info.names) === JSON.stringify(expectedNames)
      && JSON.stringify(info.counts) === JSON.stringify([[expectedLabel, String(expectedCount)]])
      && !info.text.trim() && !info.input && !info.overflow && info.assistant === 'local:web-gate-assistant' && info.lit && !info.chatLit,
      { ...info, expectedCount });
    if (width === 1440) await session.screenshot(path.join(report.dir, 'next-home.png'));
    if (width === 440) await session.screenshot(path.join(report.dir, 'next-phone-home.png'));
  }
  await session.send('Emulation.clearDeviceMetricsOverride');
}
async function chatCard(ctx) {
  const { session, report } = ctx; await visit(ctx);
  await session.click('[aria-label="Chats"]');
  report.ok('open chats popover lights its rail control', await session.eval("document.querySelector('[aria-label=Chats]').classList.contains('open')"));
  await session.waitFor(`!!document.querySelector('.popover [data-stream-id="local:web-gate-1"]')`, { timeoutMs: ctx.timeoutMs });
  await session.click('.popover [data-stream-id="local:web-gate-1"]');
  await session.waitFor("!document.querySelector('.card').hidden && document.querySelector('.card-title').textContent.length>0", { timeoutMs: ctx.timeoutMs });
  report.ok('opening a seeded chat displays its card beside Assistant', await session.eval("!document.querySelector('.card').hidden && !document.querySelector('.pane').hidden"));
  report.ok('the visible chat lights the chats control', await session.eval("document.querySelector('[aria-label=Chats]').classList.contains('on')"));
  report.ok('the two chat panes share the stage evenly', await session.eval("Math.abs(document.querySelector('.pane').getBoundingClientRect().width-document.querySelector('.card').getBoundingClientRect().width)<20"));
  await session.click('[aria-label="Full screen"]'); report.ok('full screen hides Assistant', await session.eval("document.querySelector('.pane').hidden"));
  await session.screenshot(path.join(report.dir, 'next-full.png'));
  await session.send('Input.dispatchKeyEvent', { type:'keyDown', key:'Escape', code:'Escape' });
  report.ok('Escape returns to the split view', await session.eval("!document.querySelector('.pane').hidden"));
  await session.click('[aria-label="Full screen"]'); await session.click('.rail .brand');
  report.ok('logo exits full screen', await session.eval("!document.querySelector('.pane').hidden && !document.querySelector('.card').hidden"));
  await session.click('[aria-label="Chats"]'); await session.click('.popover [data-stream-id="local:web-gate-survivor"]');
  report.ok('a second chat replaces the first', await session.eval("document.querySelector('.card .thread-inner').dataset.streamId === 'local:web-gate-survivor'"));
  await session.screenshot(path.join(report.dir, 'next-chat.png'));
  await session.click('[aria-label="Close"]'); report.ok('closing the card returns to Assistant', await session.eval("document.querySelector('.card').hidden && !document.querySelector('.pane').hidden"));
  await session.click('[aria-label="Chats"]'); report.ok('closing the card leaves the session in the list', await session.eval("!!document.querySelector('.popover [data-stream-id=\"local:web-gate-survivor\"]')"));
  report.ok('closing the card does not close its daemon session', await session.eval("window.cc.getChatStreamState().then(s=>(s.sessions||[]).some(x=>x.stream_id==='local:web-gate-survivor' && x.status!=='closed' && !x.closed_at))"));
}
module.exports = { homeRail: isolated(homeRail), chatCard: isolated(chatCard) };
async function newChat(ctx) {
  const { session, report } = ctx; await visit(ctx);
  const catalogFlag = path.join(ctx.runtime.fixtureScratch, 'next-catalog-fixture'); require('fs').writeFileSync(catalogFlag, 'enabled');
  try {
  await session.eval(`(() => {
    window.__nextRequests=[]; window.__pentacleNextAdmitted=[]; window.__nextReply={ok:false,error:'Fixture refusal'};
    window.cc.chatSpawnV2=async req=>{window.__nextRequests.push(req);const reply=window.__nextReply;if(reply.ok&&reply.streamId)window.__pentacleNextAdmitted.push(reply.streamId);return reply}; return true;
  })()`);
  await session.click('[aria-label="Chats"]'); await session.click('[aria-label="New chat"]');
  await session.waitFor("!!document.querySelector('[data-testid=\"set-provider-claude\"]')", { timeoutMs: ctx.timeoutMs });
  report.ok('provider tiles show catalog counts and Start is disabled', await session.eval("document.querySelector('[data-testid=\"set-provider-claude\"]').textContent.includes('1 model') && document.querySelector('[data-testid=\"set-provider-codex\"]').textContent.includes('1 model') && document.querySelector('[data-testid=\"start-chat\"]').disabled"));
  await session.click('[data-testid="set-provider-claude"]'); await session.click('[data-testid="set-model-fixture-model"]'); await session.click('[data-testid="set-account-ours"]');
  report.ok('account uses the current-web alias fallback and permits Start', await session.eval("document.querySelector('[data-testid=\"set-account-ours\"]').textContent==='ours' && !document.querySelector('[data-testid=\"start-chat\"]').disabled"));
  await session.click('[data-testid="start-chat"]'); await session.waitFor("document.querySelector('.start-error')?.textContent === 'Fixture refusal'", { timeoutMs: ctx.timeoutMs });
  report.ok('a resolved refusal retains the choices and displays its reason', await session.eval("document.querySelector('[data-testid=\"set-provider-claude\"]').getAttribute('aria-checked')==='true' && document.querySelector('[data-testid=\"set-model-fixture-model\"]').getAttribute('aria-checked')==='true' && document.querySelector('[data-testid=\"set-account-ours\"]').getAttribute('aria-checked')==='true' && window.__nextRequests.length === 1"));
  await session.eval(`window.__nextReply={ok:true,type:'spawn.ok',state:'queued',streamId:'local:not-listed',stream_id:'local:not-listed',request_id:'fixture',spawn_request_id:'fixture',queue_handle:'fixture'}; true`);
  await session.click('[data-testid="start-chat"]'); await session.waitFor("document.querySelector('[data-testid=\"start-chat\"]')?.textContent === 'Starting'", { timeoutMs: ctx.timeoutMs });
  await session.eval("document.querySelector('[data-testid=\"start-chat\"]').click(),true");
  report.ok('queued admission holds the disabled form and records its stream', await session.eval("document.querySelector('[data-testid=\"start-chat\"]').disabled && window.__pentacleNextAdmitted.includes('local:not-listed') && window.__nextRequests.length === 2"));
  await session.click('.card [aria-label="Close"]'); await session.click('[aria-label="Chats"]'); await session.click('[aria-label="New chat"]');
  await session.waitFor("!!document.querySelector('[data-testid=\"set-provider-claude\"]')", { timeoutMs: ctx.timeoutMs });
  report.ok('leaving and reopening shows a fresh form', await session.eval("!!document.querySelector('[data-testid=\"set-provider-claude\"]') && !document.querySelector('.start-error')"));
  await session.screenshot(path.join(report.dir, 'next-new-chat.png'));
  await session.click('[data-testid="set-provider-claude"]'); await session.click('[data-testid="set-model-fixture-model"]'); await session.click('[data-testid="set-account-ours"]');
  await session.eval(`window.__nextReply={ok:true,type:'spawn.ok',state:'queued',streamId:'local:web-gate-1',stream_id:'local:web-gate-1',request_id:'fixture2',spawn_request_id:'fixture2',queue_handle:'fixture2'}; true`);
  await session.click('[data-testid="start-chat"]');
  await session.waitFor("!document.querySelector('.card').hidden && !!document.querySelector('.card [data-next-composer]')", { timeoutMs: ctx.timeoutMs });
  report.ok('an admitted listed session opens its ordinary chat and is recorded', await session.eval("window.__pentacleNextAdmitted.includes('local:web-gate-1') && !!document.querySelector('.card [aria-label=\"Terminal\"]')"));
  await session.click('[aria-label="Chats"]'); await session.click('[aria-label="New chat"]'); await session.waitFor("!!document.querySelector('[data-testid=\"set-provider-claude\"]')", { timeoutMs: ctx.timeoutMs }); await session.click('[data-testid="set-provider-claude"]'); await session.click('[data-testid="set-model-fixture-model"]'); await session.click('[data-testid="set-account-ours"]');
  await session.eval(`window.__nextReply={ok:true,streamId:'local:web-gate-empty',session:{...window.PentacleChatStore.getState().sessions.find(s=>s.stream_id==='local:web-gate-empty'),state:'ready'}}; true`);
  await session.click('[data-testid="start-chat"]'); await session.waitFor("window.__pentacleNextAdmitted.includes('local:web-gate-empty') && !!document.querySelector('.card [data-next-composer]')",{timeoutMs:ctx.timeoutMs});
  report.ok('an ordinary ready reply opens the empty session and records its ID',await session.eval("!!document.querySelector('.card-title').textContent && !document.querySelector('.card .thread-inner .slot-chat-row') && !document.querySelector('.new-chat-host:not([hidden])')"));
  } finally { require('fs').rmSync(catalogFlag, { force:true }); }
}
module.exports.newChat = isolated(newChat);
async function limitsSettings(ctx) {
  const { session, report } = ctx; const statsFlag = path.join(ctx.runtime.fixtureScratch, 'next-stats-fixture'); require('fs').writeFileSync(statsFlag, 'enabled');
  try { await visit(ctx);
  await session.click('[aria-label="Limits and machines"]');
  report.ok('limits popover opens with usage rows and lights its control', await session.eval("!document.querySelector('.popover').hidden && document.querySelector('.popover').textContent.includes('Claude') && document.querySelector('[aria-label=\"Limits and machines\"]').classList.contains('open')"));
  await session.waitFor("!!document.querySelector('[data-machine-host=\"fixture-machine\"]')", { timeoutMs: ctx.timeoutMs });
  report.ok('machine CPU, RAM and disk figures use separate mock table columns', await session.eval("(() => {const row=document.querySelector('[data-machine-host=\"fixture-machine\"]');const cells=[...row.querySelectorAll('[role=cell]')].map(x=>x.textContent);return JSON.stringify(cells)===JSON.stringify(['24%','25%','40%'])&&!!document.querySelector('.limits-view h3')})()"));
  await session.screenshot(path.join(report.dir,'next-repair-stats-1440.png'));
  await session.send('Emulation.setDeviceMetricsOverride', {width:440,height:956,deviceScaleFactor:1,mobile:false});
  await session.waitFor("window.innerWidth===440", {timeoutMs:ctx.timeoutMs});
  report.ok('phone stats table does not overflow', await session.eval("document.documentElement.scrollWidth===document.documentElement.clientWidth && !!document.querySelector('[data-machine-host=\"fixture-machine\"]')"));
  await session.screenshot(path.join(report.dir,'next-repair-stats-440.png'));
  await session.send('Emulation.setDeviceMetricsOverride', {width:1440,height:900,deviceScaleFactor:1,mobile:false});
  await session.waitFor("window.innerWidth===1440", {timeoutMs:ctx.timeoutMs});
  await session.send('Input.dispatchKeyEvent', { type:'keyDown', key:'Escape', code:'Escape' });
  report.ok('Escape closes the popover', await session.eval("document.querySelector('.popover').hidden"));
  await session.click('[aria-label="Limits and machines"]'); await session.click('.stage');
  report.ok('clicking away closes the popover', await session.eval("document.querySelector('.popover').hidden"));
  await session.click('[aria-label="Settings"]'); report.ok('settings presents three tiles and the selected theme', await session.eval("document.querySelector('.theme-heading').textContent==='Theme' && document.querySelectorAll('.theme-choice').length===3 && document.querySelector('[aria-label=dark]').getAttribute('aria-pressed')==='true'")); await session.screenshot(path.join(report.dir, 'next-settings.png')); await session.click('[aria-label="light"]');
  const light = await session.eval("getComputedStyle(document.body).backgroundColor");
  await session.click('[aria-label="cosmic"]');
  const cosmic = await session.eval("(() => {const label=document.createElement('span');label.className='status-age';document.body.append(label);const result={bg:getComputedStyle(document.body).backgroundColor,title:getComputedStyle(document.querySelector('.card-title')).fontFamily,body:getComputedStyle(document.body).fontFamily,label:getComputedStyle(label).fontFamily,lit:getComputedStyle(document.querySelector('.rail-button.on')).color,idle:getComputedStyle(document.querySelector('[aria-label=Chats]')).color};label.remove();return result})()");
  report.ok('light and cosmic themes change the page and cosmic type', light !== cosmic.bg && cosmic.bg !== 'rgba(0, 0, 0, 0)' && cosmic.title.includes('Rajdhani') && !cosmic.body.includes('Rajdhani') && cosmic.label.includes('JetBrains Mono') && cosmic.lit !== cosmic.bg && cosmic.idle !== cosmic.bg, { light, cosmic });
  await session.send('Page.reload'); await session.waitFor("document.documentElement.dataset.theme === 'cosmic'", { timeoutMs: ctx.timeoutMs });
  report.ok('theme persists across reload', true);
  } finally { require('fs').rmSync(statsFlag, { force:true }); }
}
module.exports.limitsSettings = isolated(limitsSettings);
async function composer(ctx) {
  const { session, report, cdp } = ctx; await visit(ctx);
  await session.click('[aria-label="Chats"]'); await session.click('.popover [data-stream-id="local:web-gate-1"]');
  await session.waitFor("!!document.querySelector('.card [data-next-message]')", { timeoutMs: ctx.timeoutMs });
  await session.eval(`(() => { window.__nextSends=[]; window.PentacleChatStore.setSendBridge(async args => { window.__nextSends.push(args); if(window.__nextSends.length===1) await new Promise(resolve=>window.__releaseNextSend=resolve); return {ok:true}; }); const e=document.querySelector('.card [data-next-message]'); e.value='draft to keep'; e.dispatchEvent(new Event('input',{bubbles:true})); return true; })()`);
  report.ok('typing replaces the microphone with send', await session.eval("document.querySelector('.card [data-next-mic]').hidden && !document.querySelector('.card [data-next-send]').hidden"));
  await session.click('[aria-label="Close"]'); await session.click('[aria-label="Chats"]'); await session.click('.popover [data-stream-id="local:web-gate-1"]');
  report.ok('the draft survives closing and reopening', await session.eval("document.querySelector('.card [data-next-message]').value === 'draft to keep'"));
  const shiftPass = await session.eval(`(() => { const e=document.querySelector('.card [data-next-message]');const k=new KeyboardEvent('keydown',{key:'Enter',shiftKey:true,bubbles:true,cancelable:true}); return e.dispatchEvent(k); })()`);
  report.ok('Shift+Enter remains a line break', shiftPass);
  await session.eval(`(() => { const e=document.querySelector('.card [data-next-message]');e.value='x'.repeat(10000);e.dispatchEvent(new Event('input',{bubbles:true}));return true;})()`);
  report.ok('long text stays within the 160 px cap', await session.eval("document.querySelector('.card [data-next-message]').getBoundingClientRect().height <= 160"));
  await session.eval(`(() => { const e=document.querySelector('.card [data-next-message]'); e.value='a\\nb'; e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true})); return true; })()`);
  await session.waitFor("window.__nextSends.length === 1", { timeoutMs: ctx.timeoutMs });
  report.ok('Enter sends through the shared store', await session.eval("window.__nextSends[0].streamId === 'local:web-gate-1' && window.__nextSends[0].text === 'a\\nb'"));
  report.ok('the pending send is visible in the transcript', await session.eval("!!document.querySelector('.card .slot-chat-send-status.is-sending') || !!document.querySelector('.card .slot-chat-send-status.is-queued')"));
  await session.eval('window.__releaseNextSend(),true');
  await session.eval(`(() => {const data=Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a1sAAAAASUVORK5CYII='),c=>c.charCodeAt(0));const dt=new DataTransfer();dt.items.add(new File([data],'fixture.png',{type:'image/png'}));const input=document.querySelector('.card [data-next-file-input]');input.files=dt.files;input.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
  await session.waitFor("!!document.querySelector('.card .next-composer-attachment')",{timeoutMs:ctx.timeoutMs});
  await session.click('.card [data-next-send]'); await session.waitFor("window.__nextSends.length===2",{timeoutMs:ctx.timeoutMs});
  report.ok('attachment uploads and is included in the outbound turn',await session.eval("window.__nextSends[1].attachments.length === 1 && !!window.__nextSends[1].attachments[0].key"));
  await cdp.sleep(100);
}
module.exports.composer = isolated(composer);
async function terminalStatus(ctx) {
  const { session, report, tmux, cdp } = ctx; await visit(ctx);
  const name = `ptest-web-next-${process.pid}-${Date.now().toString(36)}`;
  tmux(['new-session','-d','-s',name,'sh','-c','stty raw -echo; exec cat']);
  let ptyOutput = '';
  const observeFrame = raw => {
    try {
      const event = JSON.parse(raw);
      if (event.method !== 'Network.webSocketFrameReceived') return;
      const message = JSON.parse(event.params.response.payloadData);
      if (message.event === 'pty:data' && message.args?.[0] === 0) ptyOutput += message.args[1];
    } catch (_) { /* Ignore non-JSON and unrelated WebSocket frames. */ }
  };
  try {
    await session.send('Network.enable'); session.ws.on('message', observeFrame);
    await session.eval(`(() => {const original=window.cc.createPty;window.__nextPtyReady=false;window.cc.createPty=async(...args)=>{const pane=await original(...args);window.__nextPtyReady=!!pane;return pane};return true})()`);
    await session.click('[aria-label="Chats"]'); await session.click('.popover [data-stream-id="local:web-gate-survivor"]');
    report.ok('a session without a status card has no status button', await session.eval("document.querySelector('.card [aria-label=\"Status\"]').hidden"));
    await session.click('.card [aria-label="Close"]');
    await session.eval(`(() => { const store=window.PentacleChatStore; const rows=store.getState().sessions.map(s=>s.stream_id==='local:web-gate-1'?{...s,session_name:${JSON.stringify(name)},status_card:{goal:'Fixture goal',plan:[{text:'First',status:'done'},{text:'Second',status:'active'}],update:'Fixture update',updated_at:new Date().toISOString()}}:s);store.applyFrame({type:'session.inventory',sessions:rows});return true;})()`);
    await session.click('[aria-label="Chats"]'); await session.click('.popover [data-stream-id="local:web-gate-1"]');
    await session.click('.card [aria-label="Terminal"]');
    await session.waitFor("!!document.querySelector('.terminal .xterm-screen')", { timeoutMs:ctx.timeoutMs });
    await session.waitFor("window.__nextPtyReady === true", { timeoutMs:ctx.timeoutMs });
    if (process.env.PENTACLE_NEXT_REQUIRE_WEBGL === '1') {
      report.ok('terminal uses an active WebGL renderer', await session.eval("[...document.querySelectorAll('.terminal canvas')].some(canvas => !!canvas.getContext('webgl2'))"));
    }
    const waitForOutput = async (predicate, label) => {
      const deadline = Date.now() + ctx.timeoutMs;
      while (!predicate() && Date.now() < deadline) await cdp.sleep(50);
      if (!predicate()) throw new Error(`PTY ${label} timed out`);
    };
    await waitForOutput(() => ptyOutput.length > 0, 'attach output');
    await session.eval("window.cc.writePty(0, 'NEXT_TERMINAL_PROOF\\r'), true");
    await waitForOutput(() => ptyOutput.includes('NEXT_TERMINAL_PROOF'), 'sentinel output');
    report.ok('the new card displays a real tmux terminal', true);
    report.ok('terminal is inset and the composer is hidden', await session.eval("document.querySelector('.card .composer-host').hidden && getComputedStyle(document.querySelector('.terminal')).borderRadius === '14px' && parseFloat(getComputedStyle(document.querySelector('.terminal')).marginLeft) === 12"));
    await session.screenshot(path.join(report.dir, 'next-terminal.png'));
    await session.click('.card [aria-label="Status"]');
    report.ok('status replaces terminal and retains composer', await session.eval("!document.querySelector('.status').hidden && document.querySelector('.terminal').hidden && document.querySelector('.status').textContent.includes('Fixture goal') && document.querySelector('.status').textContent.includes('Fixture update') && document.querySelector('.status').textContent.includes('Step 2 of 2') && document.querySelector('.status').textContent.includes('updated') && document.querySelectorAll('.status li').length===2 && !!document.querySelector('.card [data-next-composer]')"));
    await session.screenshot(path.join(report.dir, 'next-status.png'));
    await session.send('Emulation.setDeviceMetricsOverride', { width:440, height:956, deviceScaleFactor:1, mobile:false }); await session.screenshot(path.join(report.dir, 'next-phone-status.png'));
    await session.click('.card [aria-label="Close"]');
  } finally { session.ws.off('message', observeFrame); try { tmux(['kill-session','-t',`=${name}`]); } catch (_) {} }
}
module.exports.terminalStatus = isolated(terminalStatus);
async function voice(ctx) {
  const {session, report, cdp} = ctx; await visit(ctx);
  await session.click('[aria-label="Chats"]'); await session.click('.popover [data-stream-id="local:web-gate-1"]');
  await session.eval(`(() => { window.__nextVoice={media:[],sends:[]}; const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices); const transcribe=window.cc.chatTranscribeBlob; window.__nextVoice.restore=async()=>{window.__nextVoice.release?.();navigator.mediaDevices.getUserMedia=original;window.cc.chatTranscribeBlob=transcribe;for(const m of window.__nextVoice.media){m.osc.stop();m.stream.getTracks().forEach(t=>t.stop());await m.context.close()}}; navigator.mediaDevices.getUserMedia=async()=>{const context=new AudioContext();await context.resume();const osc=context.createOscillator(),gain=context.createGain(),dest=context.createMediaStreamDestination();gain.gain.value=.05;osc.connect(gain).connect(dest);osc.start();setTimeout(()=>gain.gain.value=.9,500);window.__nextVoice.media.push({context,osc,stream:dest.stream});return dest.stream};window.cc.chatTranscribeBlob=async payload=>{await new Promise(resolve=>window.__nextVoice.release=resolve);return transcribe(payload)};window.cc.chatOutboundCommit=async()=>({ok:false,error_code:'unsupported'});window.PentacleChatStore.setSendBridge(async args=>{window.__nextVoice.sends.push(args);return {ok:true}});return true})()`);
  try {
    await session.send('Runtime.evaluate',{expression:"document.querySelector('.card [data-next-mic]').click()",userGesture:true});
    await session.waitFor("!!document.querySelector('.card [data-next-voice-bars]')",{timeoutMs:ctx.timeoutMs});
    await cdp.sleep(1350);
    const sample=await session.eval("({bars:[...document.querySelectorAll('.card [data-next-voice-bars] .next-voice-bar')].map(x=>x.style.height),time:document.querySelector('.card [data-next-voice-time]').textContent})");
    report.ok('live waveform varies and timer advances',sample.bars.length>1&&sample.bars.length<=110&&new Set(sample.bars).size>1&&sample.time!=='0:00',sample);
    await cdp.sleep(7000); report.ok('live waveform retains at most 110 bars', await session.eval("document.querySelectorAll('.card [data-next-voice-bars] .next-voice-bar').length===110"));
    await session.click('.card [data-next-voice-cancel]'); report.ok('cancel discards the take',await session.eval("!document.querySelector('.card [data-next-voice-pending]') && window.__nextVoice.sends.length===0"));
    await session.send('Runtime.evaluate',{expression:"document.querySelector('.card [data-next-mic]').click()",userGesture:true}); await session.waitFor("!!document.querySelector('.card [data-next-voice-bars]')",{timeoutMs:ctx.timeoutMs}); await cdp.sleep(1100);
    await session.click('.card [data-next-voice-send]'); await session.waitFor("!!document.querySelector('[data-next-voice-pending]') && !!window.__nextVoice.release",{timeoutMs:ctx.timeoutMs});
    report.ok('sending take shows 22 bars, duration and Sending',await session.eval("document.querySelector('[data-next-voice-pending]').textContent.includes('Sending') && document.querySelectorAll('[data-next-voice-pending] [data-next-voice-bar]').length === 22 && document.querySelector('[data-next-voice-duration]').textContent !== '0:00'"));
    require('fs').writeFileSync(path.join(ctx.runtime.fixtureScratch, 'asr-release'), 'release');
    await session.eval('window.__nextVoice.release(),true'); await session.waitFor("window.__nextVoice.sends.length===1",{timeoutMs:ctx.timeoutMs});
    report.ok('transcribed voice reaches the shared send bridge',await session.eval("window.__nextVoice.sends[0].text.includes('Hermetic server transcript')"));
    await session.send('Runtime.evaluate',{expression:"document.querySelector('.card [data-next-mic]').click()",userGesture:true}); await session.waitFor("!!document.querySelector('.card [data-next-voice-bars]')",{timeoutMs:ctx.timeoutMs});
    await session.click('[aria-label="Chats"]'); await session.click('.popover [data-stream-id="local:web-gate-survivor"]');
    await session.waitFor("window.__nextVoice.media.at(-1).stream.getTracks().every(t=>t.readyState==='ended')",{timeoutMs:ctx.timeoutMs});
    report.ok('changing chats stops and discards an in-progress take', await session.eval("window.__nextVoice.sends.length===1 && !document.querySelector('[data-next-voice-pending]')"));
  } finally { await session.eval('window.__nextVoice.restore()'); require('fs').rmSync(path.join(ctx.runtime.fixtureScratch, 'asr-release'), { force: true }); }
}
module.exports.voice=isolated(voice);
async function narrowPhone(ctx) {
  const {session,report}=ctx;
  for (const width of [1010,702,440]) {
    await visit(ctx,width); await session.click('[aria-label="Chats"]'); await session.click('.popover [data-stream-id="local:web-gate-1"]');
    const result=await session.eval("({overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth,assistant:!document.querySelector('.pane').hidden,full:getComputedStyle(document.querySelector('.fullscreen')).display})");
    report.ok(`chat layout fits at ${width}`,!result.overflow && (width>599 || (!result.assistant&&result.full==='none')),result);
    if(width===440){await session.screenshot(path.join(report.dir, 'next-phone-chat.png'));await session.click('[aria-label="Chats"]');await session.screenshot(path.join(report.dir, 'next-phone-chats.png'));await session.click('.popover [data-stream-id="local:web-gate-survivor"]');await session.click('.card [aria-label="Close"]');report.ok('phone closes newest card to Assistant',await session.eval("!document.querySelector('.pane').hidden && document.querySelector('.card').hidden"));await session.click('[aria-label="Chats"]');await session.click('.popover [data-stream-id="local:web-gate-1"]');await session.click('.rail .brand');report.ok('phone logo returns to the assistant',await session.eval("!document.querySelector('.pane').hidden && document.querySelector('.card').hidden"));}
  }
  await session.send('Emulation.clearDeviceMetricsOverride');
}
module.exports.narrowPhone=isolated(narrowPhone);
async function lanesQuestions(ctx) {
  const { session, report } = ctx;
  const fixture = require('../../../pentacle-chat-core/tests/fixtures/work-lanes-inventory.json').goals_v1;
  const lane = fixture.inventory_frame.lanes[0];
  const member = { ...fixture.show_projection.members[0], title:'Fixture release spec' };
  lane.members[0].title = member.title;
  const notice = { notification_id:'next-fixture-notice', producer:'agent_question.v1', state:'open', title:'Fixture decision', body:'Choose an answer', actions:[{kind:'yes_no'}], question:{question_id:'next-fixture-question',state:'open',producer_stream_id:'local:web-gate-1',spec_id:member.spec_id,response_mode:'single_choice',options:[{label:'Keep',value:'keep'},{label:'Change',value:'change'}]} };
  const capture = await session.send('Page.addScriptToEvaluateOnNewDocument', { source:`(() => { const Native=window.WebSocket; window.WebSocket=class extends Native { constructor(...args) { super(...args); window.__fixtureSocket=this; } }; })()` });
  try {
    await visit(ctx);
    await session.eval(`(() => {
      const lane=${JSON.stringify(lane)}, projection=${JSON.stringify(fixture.show_projection)}, member=${JSON.stringify(member)};
      window.cc.workLanesShow=async (_lane, options={}) => ({type:'work_lanes.show.ok',lane:{lane_id:lane.lane_id,members:[member.spec_id]},projection:{...projection,lane_id:lane.lane_id,members:[member],members_total:1,members_next:null,...(options.goalId?{selected_goal:lane.goals.find(g=>g.goal_id===options.goalId)}:{})},members:[member],members_total:1,members_next:null,detail_revision:lane.detail_revision});
      window.__fixtureAnswers=[];
      window.cc.notificationResolve=async (...args)=>{window.__fixtureAnswers.push(args);return {ok:true};};
      window.cc.promptList=async()=>({ok:true,questions:window.__fixtureAnswers.length?[{question_id:'next-fixture-question',state:'answered',answered_at:'2026-11-21T00:00:00Z',answer:{selections:['keep']},envelope:{spec_id:member.spec_id,options:[{label:'Keep',value:'keep'}]}}]:[]});
      const emit=frame=>window.__fixtureSocket.dispatchEvent(new MessageEvent('message',{data:JSON.stringify({event:'chat-stream:frame',args:[frame]})}));
      emit(${JSON.stringify(fixture.inventory_frame)}); emit({type:'notification',notification:${JSON.stringify(notice)}}); return true;
    })()`);
    await session.waitFor("document.querySelector('.next-lanes-row-title')?.textContent==='Mobile release' && document.querySelector('[data-count=\"1\"]')", { timeoutMs:ctx.timeoutMs });
    report.ok('an unversioned lane push reaches the card and one question reaches the rail', await session.eval("document.querySelector('.next-lanes-row-title').textContent==='Mobile release' && document.querySelector('[data-count=\"1\"]')!==null"));
    report.ok('desktop lanes card keeps the mock width', await session.eval("Math.abs(document.querySelector('.lanes-card').getBoundingClientRect().width-344)<2"));
    await session.screenshot(path.join(report.dir,'next-lanes-fixture.png'));
    await session.click('.next-lanes-row-open'); await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level==='lane' && !!document.querySelector('.next-lanes-stop-open')", {timeoutMs:ctx.timeoutMs});
    await session.screenshot(path.join(report.dir,'next-lane-fixture.png'));
    await session.click('.next-lanes-stop-open'); await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level==='goal'", {timeoutMs:ctx.timeoutMs});
    await session.screenshot(path.join(report.dir,'next-goal-fixture.png'));
    await session.click('.next-lanes-back'); await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level==='lane'", {timeoutMs:ctx.timeoutMs});
    await session.click('.next-lanes-back'); await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level==='all'", {timeoutMs:ctx.timeoutMs});
    report.ok('back navigation returns from goal to lane and all lanes', true);
    await session.click('.next-lanes-close');
    await session.click('[aria-label="Questions (1)"]');
    report.ok('question list identifies its lane and goal', await session.eval("document.querySelector('.next-question-heading-link')?.textContent.includes('Mobile release') && document.querySelector('.next-question-heading-link')?.textContent.includes('Finish fixture 0')"));
    await session.screenshot(path.join(report.dir,'next-questions-fixture.png'));
    await session.click('.next-question-heading-link');
    await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level==='spec'", { timeoutMs:ctx.timeoutMs });
    report.ok('question heading reopens lanes at its spec and question card', await session.eval("!document.querySelector('.lanes-card').hidden && !!document.querySelector('.next-question-card')"));
    report.ok('spec back control names its goal', await session.eval("document.querySelector('.next-lanes-back')?.textContent.includes('Finish fixture 0')"));
    await session.screenshot(path.join(report.dir,'next-spec-fixture.png'));
    await session.send('Emulation.setDeviceMetricsOverride', {width:440,height:956,deviceScaleFactor:1,mobile:false});
    await session.waitFor("window.innerWidth===440 && document.querySelector('.pane').hidden", {timeoutMs:ctx.timeoutMs});
    report.ok('phone spec occupies the stage without horizontal overflow', await session.eval("!document.querySelector('.lanes-card').hidden && document.querySelector('.pane').hidden && document.documentElement.scrollWidth===document.documentElement.clientWidth"));
    await session.screenshot(path.join(report.dir,'next-phone-spec-fixture.png'));
    await session.click('.next-question-card .next-question-option');
    await session.click('.next-question-card .next-question-send');
    await session.waitFor("document.querySelector('.next-question-card')?.textContent.includes('Answer sent')", {timeoutMs:ctx.timeoutMs});
    report.ok('one answer is sent and the receipt lowers the rail count', await session.eval("window.__fixtureAnswers.length===1 && window.__fixtureAnswers[0][2].selections[0]==='keep' && document.querySelector('[aria-label=Questions]')?.dataset.count==='0'"));
    await session.screenshot(path.join(report.dir,'next-answered-fixture.png'));
    await session.waitFor("document.querySelector('.next-lanes-scroll')?.textContent.includes('You said') && document.querySelector('.next-lanes-scroll')?.textContent.includes('Keep')", {timeoutMs:ctx.timeoutMs});
    report.ok('spec reads the stored answer back', await session.eval("document.querySelector('.next-lanes-scroll').textContent.includes('You said')"));
    await session.click('.next-lanes-back'); await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level==='goal'", {timeoutMs:ctx.timeoutMs});
    await session.click('.next-lanes-member-title'); await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level==='spec' && document.querySelector('.next-lanes-scroll').textContent.includes('You said')", {timeoutMs:ctx.timeoutMs});
    report.ok('returning to the spec retains its stored answer', await session.eval("document.querySelector('.next-lanes-scroll').textContent.includes('Keep') && window.__fixtureAnswers.length===1"));
  } finally { await session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier:capture.identifier }); }
}
module.exports.lanesQuestions=isolated(lanesQuestions);
async function scrollAndFocus(ctx) {
  const {session,report}=ctx;
  for (const width of [1440,440]) {
    await visit(ctx,width);
    const baseline=await session.eval(`(() => {const input=document.querySelector('.pane .next-composer-input'), outer=input.closest('.next-composer'), scroller=document.querySelector('.pane .thread'); const filler=document.createElement('div');filler.style.height='2000px';filler.style.flex='none';filler.textContent='Scroll fixture';scroller.append(filler);scroller.scrollTop=500;const before=getComputedStyle(outer).boxShadow;input.focus();return {outline:getComputedStyle(input).outlineStyle,before,after:getComputedStyle(outer).boxShadow,scrollbar:getComputedStyle(scroller).scrollbarWidth,width:getComputedStyle(scroller,'::-webkit-scrollbar').width,overflow:scroller.scrollHeight>scroller.clientHeight};})()`);
    report.ok(`composer focus uses the outer cue at ${width}`,baseline.outline==='none' && baseline.before!==baseline.after,baseline);
    report.ok(`scrollbar is thin and styled at ${width}`,baseline.overflow && baseline.scrollbar==='thin' && baseline.width==='7px',baseline);
    await session.screenshot(path.join(report.dir,`next-repair-scroll-focus-${width}.png`));
  }
}
module.exports.scrollAndFocus=isolated(scrollAndFocus);
async function newChatParity(ctx) {
  const {session,report}=ctx;
  const catalog={catalog_version:'parity-fixture',profiles:{desktop_manual:{claude:['allowed-claude','low'],codex:['allowed-codex','low'],cursor:['auto','default'],gemini:['hidden-gemini','default']}},
    models:{claude:{'allowed-claude':{efforts:['low','high']},'hidden-claude':{efforts:['low']}},codex:{'allowed-codex':{efforts:['low','medium']},'hidden-codex':{efforts:['low']}},cursor:{auto:{efforts:['default']}},gemini:{'hidden-gemini':{efforts:['default']}}},
    available_models:{claude:{'allowed-claude':{efforts:['low','high']}},codex:{'allowed-codex':{efforts:['low','medium']}},cursor:{auto:{efforts:['default']}}},
    account_status:{accounts:[{host:'local',provider:'claude',alias:'fixture-account',active:true,enabled:true,health:'healthy'}],defaults:[{host:'local',provider:'claude',alias:'fixture-account'}]}};
  const install=`window.cc.chatSpawnCatalog=async()=>({ok:true,catalog:${JSON.stringify(catalog)}});window.cc.chatSpawnV2=async()=>{throw Error('unexpected spawn')};true`;
  for(const width of [1440,440]) {
    await session.send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
    await session.send('Page.navigate',{url:ctx.url});
    await session.waitFor("!!window.cc && !!document.querySelector('#btn-new')",{timeoutMs:ctx.timeoutMs});
    await session.eval("new Promise(resolve=>setTimeout(resolve,500))");
    await session.eval(install);
    if(width<600) await session.click('#sidebar-toggle');
    await session.click('#btn-new');
    const machines=await session.eval("[...document.querySelectorAll('#new-session-location [data-loc]')].map(x=>({id:x.dataset.loc,label:x.querySelector('.new-session-option-label').textContent.trim()}))");
    await session.click('#new-session-location [data-loc=local]');
    await session.waitFor("document.querySelectorAll('#spawn-provider option').length===3 && /^(allowed-|auto)/.test(document.querySelector('#spawn-model option')?.value||'')",{timeoutMs:ctx.timeoutMs});
    const expected=await session.eval("[...document.querySelectorAll('#spawn-provider option')].map(x=>x.value)");
    const oldChoices={};
    for(const provider of expected){
      await session.eval(`(()=>{const e=document.querySelector('#spawn-provider');e.value=${JSON.stringify(provider)};e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
      oldChoices[provider]=await session.eval("({models:[...document.querySelectorAll('#spawn-model option')].map(x=>x.value),efforts:[...document.querySelectorAll('#spawn-effort option')].map(x=>x.value),accounts:[...document.querySelectorAll('#spawn-account option')].map(x=>x.value)})");
    }
    await session.screenshot(path.join(report.dir,`current-repair-choices-${width}.png`));
    await visit(ctx,width); await session.eval(install);
    await session.click('[aria-label="Chats"]'); await session.click('[aria-label="New chat"]');
    await session.waitFor("!!document.querySelector('[data-set=provider]')",{timeoutMs:ctx.timeoutMs});
    const actual=await session.eval("[...document.querySelectorAll('[data-set=provider]')].map(x=>x.dataset.val)");
    report.ok(`new chat providers match the current web at ${width}`,JSON.stringify(actual)===JSON.stringify(expected),{actual,expected});
    for(const provider of expected){
      await session.click(`[data-testid="set-provider-${provider}"]`);
      const models=await session.eval("[...document.querySelectorAll('[data-set=model]')].map(x=>x.dataset.val)");
      await session.click(`[data-testid="set-model-${oldChoices[provider].models[0]}"]`);
      const next=await session.eval("({efforts:[...document.querySelectorAll('[data-set=effort]')].map(x=>x.dataset.val),accounts:[...document.querySelectorAll('[data-set=account]')].map(x=>x.dataset.val),machines:[...document.querySelectorAll('[data-set=host]')].map(x=>({id:x.dataset.val,label:x.textContent.trim()}))})");
      report.ok(`new chat ${provider} choices match the current web at ${width}`,JSON.stringify(models)===JSON.stringify(oldChoices[provider].models)&&JSON.stringify(next.efforts)===JSON.stringify(oldChoices[provider].efforts)&&JSON.stringify(next.accounts)===JSON.stringify(oldChoices[provider].accounts)&&JSON.stringify(next.machines)===JSON.stringify(machines),{models,next,expected:oldChoices[provider],machines});
    }
    await session.screenshot(path.join(report.dir,`next-repair-choices-${width}.png`));
  }
}
module.exports.newChatParity=isolated(newChatParity);
