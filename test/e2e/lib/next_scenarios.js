'use strict';
const path = require('path');
function isolated(run) { return async ctx => { try { await run(ctx); } finally { await ctx.session.send('Emulation.clearDeviceMetricsOverride'); await ctx.session.send('Page.navigate', { url: ctx.url }); await ctx.session.waitFor("document.readyState === 'complete' && !!document.getElementById('session-list')", { timeoutMs: ctx.timeoutMs }); } }; }
async function visit(ctx, width = 1440) {
  await ctx.session.send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false });
  await ctx.session.send('Page.navigate', { url: ctx.url + 'next/' });
  await ctx.session.waitFor("!!window.cc && !!window.PentacleChatStore && document.querySelectorAll('.rail-button').length === 4", { timeoutMs: ctx.timeoutMs });
  await ctx.session.eval("document.documentElement.dataset.theme='dark'; localStorage.setItem('pentacle.next.theme','dark'); true");
  await ctx.session.waitFor("window.cc.getChatStreamState().then(s => s.connected === true && (s.sessions || []).length > 0)", { timeoutMs: ctx.timeoutMs });
}
async function homeRail(ctx) {
  const { session, report } = ctx;
  for (const width of [1440, 1010, 440]) {
    await visit(ctx, width);
    const info = await session.eval(`({names:Array.from(document.querySelectorAll('.rail-button')).map(x=>x.getAttribute('aria-label')), text:document.querySelector('.rail').innerText, input:!!document.querySelector('.rail input'), overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth, assistant:document.querySelector('.pane .thread-inner').dataset.streamId})`);
    report.ok(`rail has four icon-only controls at ${width}`, JSON.stringify(info.names) === JSON.stringify(['Assistant Fixture','Chats','Limits and machines','Settings']) && !info.text.trim() && !info.input && !info.overflow && info.assistant === 'local:web-gate-assistant', info);
    if (width === 1440) await session.screenshot(path.join(report.dir, 'next-home.png'));
    if (width === 440) await session.screenshot(path.join(report.dir, 'next-phone-home.png'));
  }
  await session.send('Emulation.clearDeviceMetricsOverride');
}
async function chatCard(ctx) {
  const { session, report } = ctx; await visit(ctx);
  await session.click('[aria-label="Chats"]');
  await session.waitFor(`!!document.querySelector('.popover [data-stream-id="local:web-gate-1"]')`, { timeoutMs: ctx.timeoutMs });
  await session.click('.popover [data-stream-id="local:web-gate-1"]');
  await session.waitFor("!document.querySelector('.card').hidden && document.querySelector('.card-title').textContent.length>0", { timeoutMs: ctx.timeoutMs });
  report.ok('opening a seeded chat displays its card beside Assistant', await session.eval("!document.querySelector('.card').hidden && !document.querySelector('.pane').hidden"));
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
}
module.exports = { homeRail: isolated(homeRail), chatCard: isolated(chatCard) };
async function newChat(ctx) {
  const { session, report } = ctx; await visit(ctx);
  await session.eval(`(() => {
    window.__nextRequests=[]; window.__pentacleNextAdmitted=[]; window.__nextReply={ok:false,error:'Fixture refusal'};
    window.cc.chatSpawnCatalog=async()=>({ok:true,catalog:{catalog_version:'fixture',profiles:{desktop_manual:{claude:['fixture-model','low'],codex:['fixture-codex','low']}},models:{claude:{'fixture-model':{efforts:['low']}},codex:{'fixture-codex':{efforts:['low']}}},account_status:{accounts:[{host:'local',provider:'claude',alias:'ours',username:'fixture@example.test',active:true,enabled:true,health:'healthy'}]}}});
    window.cc.chatSpawnV2=async req=>{window.__nextRequests.push(req);const reply=window.__nextReply;if(reply.ok&&reply.streamId)window.__pentacleNextAdmitted.push(reply.streamId);return reply}; return true;
  })()`);
  await session.click('[aria-label="Chats"]'); await session.click('[aria-label="New chat"]');
  await session.waitFor("!!document.querySelector('[data-testid=\"set-provider-claude\"]')", { timeoutMs: ctx.timeoutMs });
  await session.click('[data-testid="set-provider-claude"]'); await session.click('[data-testid="set-model-fixture-model"]'); await session.click('[data-testid="set-account-ours"]');
  await session.click('[data-testid="start-chat"]'); await session.waitFor("document.querySelector('.start-error')?.textContent === 'Fixture refusal'", { timeoutMs: ctx.timeoutMs });
  report.ok('a resolved refusal retains the form and displays its reason', await session.eval("!!document.querySelector('[data-testid=\"start-chat\"]') && window.__nextRequests.length === 1"));
  await session.eval(`window.__nextReply={ok:true,type:'spawn.ok',state:'queued',streamId:'local:not-listed',stream_id:'local:not-listed',request_id:'fixture',spawn_request_id:'fixture',queue_handle:'fixture'}; true`);
  await session.click('[data-testid="start-chat"]'); await session.waitFor("document.querySelector('[data-testid=\"start-chat\"]')?.textContent === 'Starting'", { timeoutMs: ctx.timeoutMs });
  await session.eval("document.querySelector('[data-testid=\"start-chat\"]').click(),true");
  report.ok('queued admission holds the disabled form and records its stream', await session.eval("document.querySelector('[data-testid=\"start-chat\"]').disabled && window.__pentacleNextAdmitted.includes('local:not-listed') && window.__nextRequests.length === 2"));
  await session.click('.card [aria-label="Close"]'); await session.click('[aria-label="Chats"]'); await session.click('[aria-label="New chat"]');
  report.ok('leaving and reopening shows a fresh form', await session.eval("!!document.querySelector('[data-testid=\"set-provider-claude\"]') && !document.querySelector('.start-error')"));
  await session.screenshot(path.join(report.dir, 'next-new-chat.png'));
  await session.click('[data-testid="set-provider-claude"]'); await session.click('[data-testid="set-model-fixture-model"]'); await session.click('[data-testid="set-account-ours"]');
  await session.eval(`window.__nextReply={ok:true,type:'spawn.ok',state:'queued',streamId:'local:web-gate-1',stream_id:'local:web-gate-1',request_id:'fixture2',spawn_request_id:'fixture2',queue_handle:'fixture2'}; true`);
  await session.click('[data-testid="start-chat"]');
  await session.waitFor("!document.querySelector('.card').hidden && !!document.querySelector('.card [data-next-composer]')", { timeoutMs: ctx.timeoutMs });
  report.ok('an admitted listed session opens its ordinary chat and is recorded', await session.eval("window.__pentacleNextAdmitted.includes('local:web-gate-1') && !!document.querySelector('.card [aria-label=\"Terminal\"]')"));
  await session.click('[aria-label="Chats"]'); await session.click('[aria-label="New chat"]'); await session.click('[data-testid="set-provider-claude"]'); await session.click('[data-testid="set-model-fixture-model"]'); await session.click('[data-testid="set-account-ours"]');
  await session.eval(`window.__nextReply={ok:true,streamId:'local:next-empty',session:{stream_id:'local:next-empty',session_name:'next-empty',host:'local',provider:'claude',title:'New fixture chat',state:'ready'}}; true`);
  await session.click('[data-testid="start-chat"]'); await session.waitFor("window.__pentacleNextAdmitted.includes('local:next-empty') && !!document.querySelector('.card [data-next-composer]')",{timeoutMs:ctx.timeoutMs});
  report.ok('an ordinary ready reply opens the empty session and records its ID',await session.eval("document.querySelector('.card-title').textContent==='New fixture chat' && !document.querySelector('.card .thread-inner .slot-chat-row') && !document.querySelector('.new-chat-host:not([hidden])')"));
}
module.exports.newChat = isolated(newChat);
async function limitsSettings(ctx) {
  const { session, report } = ctx; await visit(ctx);
  await session.click('[aria-label="Limits and machines"]');
  report.ok('limits popover opens with usage rows', await session.eval("!document.querySelector('.popover').hidden && document.querySelector('.popover').textContent.includes('Claude')"));
  await session.send('Input.dispatchKeyEvent', { type:'keyDown', key:'Escape', code:'Escape' });
  report.ok('Escape closes the popover', await session.eval("document.querySelector('.popover').hidden"));
  await session.click('[aria-label="Limits and machines"]'); await session.click('.stage');
  report.ok('clicking away closes the popover', await session.eval("document.querySelector('.popover').hidden"));
  await session.click('[aria-label="Settings"]'); await session.screenshot(path.join(report.dir, 'next-settings.png')); await session.click('[aria-label="light"]');
  const light = await session.eval("getComputedStyle(document.body).backgroundColor");
  await session.click('[aria-label="cosmic"]');
  const cosmic = await session.eval("(() => {const label=document.createElement('span');label.className='status-age';document.body.append(label);const result={bg:getComputedStyle(document.body).backgroundColor,title:getComputedStyle(document.querySelector('.card-title')).fontFamily,body:getComputedStyle(document.body).fontFamily,label:getComputedStyle(label).fontFamily,lit:getComputedStyle(document.querySelector('.rail-button.on')).color,idle:getComputedStyle(document.querySelector('[aria-label=Chats]')).color};label.remove();return result})()");
  report.ok('light and cosmic themes change the page and cosmic type', light !== cosmic.bg && cosmic.bg !== 'rgba(0, 0, 0, 0)' && cosmic.title.includes('Rajdhani') && !cosmic.body.includes('Rajdhani') && cosmic.label.includes('JetBrains Mono') && cosmic.lit !== cosmic.bg && cosmic.idle !== cosmic.bg, { light, cosmic });
  await session.send('Page.reload'); await session.waitFor("document.documentElement.dataset.theme === 'cosmic'", { timeoutMs: ctx.timeoutMs });
  report.ok('theme persists across reload', true);
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
  const { session, report, tmux } = ctx; await visit(ctx);
  const name = `ptest-web-next-${process.pid}-${Date.now().toString(36)}`;
  tmux(['new-session','-d','-s',name,'sh','-c','stty raw -echo; exec cat']);
  try {
    await session.eval(`(() => {const original=window.cc.createPty;window.__nextPtyReady=false;window.cc.createPty=async(...args)=>{const pane=await original(...args);window.__nextPtyReady=!!pane;return pane};return true})()`);
    await session.click('[aria-label="Chats"]'); await session.click('.popover [data-stream-id="local:web-gate-survivor"]');
    report.ok('a session without a status card has no status button', await session.eval("document.querySelector('.card [aria-label=\"Status\"]').hidden"));
    await session.click('.card [aria-label="Close"]');
    await session.eval(`(() => { const store=window.PentacleChatStore; const rows=store.getState().sessions.map(s=>s.stream_id==='local:web-gate-1'?{...s,session_name:${JSON.stringify(name)},status_card:{goal:'Fixture goal',plan:[{text:'First',status:'done'},{text:'Second',status:'active'}],update:'Fixture update',updated_at:new Date().toISOString()}}:s);store.applyFrame({type:'session.inventory',sessions:rows});return true;})()`);
    await session.click('[aria-label="Chats"]'); await session.click('.popover [data-stream-id="local:web-gate-1"]');
    await session.click('.card [aria-label="Terminal"]');
    await session.waitFor("!!document.querySelector('.terminal .xterm-screen')", { timeoutMs:ctx.timeoutMs });
    await session.waitFor("window.__nextPtyReady === true", { timeoutMs:ctx.timeoutMs });
    await session.eval("window.cc.writePty(0, 'NEXT_TERMINAL_PROOF\\r'), true");
    await session.waitFor("document.querySelector('.terminal').textContent.includes('NEXT_TERMINAL_PROOF')", { timeoutMs:ctx.timeoutMs });
    report.ok('the new card displays a real tmux terminal', true);
    await session.screenshot(path.join(report.dir, 'next-terminal.png'));
    await session.click('.card [aria-label="Status"]');
    report.ok('status replaces terminal and retains composer', await session.eval("!document.querySelector('.status').hidden && document.querySelector('.terminal').hidden && document.querySelector('.status').textContent.includes('Fixture goal') && document.querySelector('.status').textContent.includes('Fixture update') && !!document.querySelector('.card [data-next-composer]')"));
    await session.screenshot(path.join(report.dir, 'next-status.png'));
    await session.send('Emulation.setDeviceMetricsOverride', { width:440, height:956, deviceScaleFactor:1, mobile:false }); await session.screenshot(path.join(report.dir, 'next-phone-status.png'));
    await session.click('.card [aria-label="Close"]');
  } finally { try { tmux(['kill-session','-t',`=${name}`]); } catch (_) {} }
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
    await session.click('.card [data-next-voice-cancel]'); report.ok('cancel discards the take',await session.eval("!document.querySelector('.card [data-next-voice-pending]') && window.__nextVoice.sends.length===0"));
    await session.send('Runtime.evaluate',{expression:"document.querySelector('.card [data-next-mic]').click()",userGesture:true}); await session.waitFor("!!document.querySelector('.card [data-next-voice-bars]')",{timeoutMs:ctx.timeoutMs}); await cdp.sleep(1100);
    await session.click('.card [data-next-voice-send]'); await session.waitFor("!!document.querySelector('[data-next-voice-pending]') && !!window.__nextVoice.release",{timeoutMs:ctx.timeoutMs});
    report.ok('sending take shows 22 bars and Sending',await session.eval("document.querySelector('[data-next-voice-pending]').textContent.includes('Sending') && document.querySelectorAll('[data-next-voice-pending] [data-next-voice-bar]').length === 22"));
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
    if(width===440){await session.screenshot(path.join(report.dir, 'next-phone-chat.png'));await session.click('[aria-label="Chats"]');await session.screenshot(path.join(report.dir, 'next-phone-chats.png'));await session.click('.popover [data-stream-id="local:web-gate-survivor"]');await session.click('.card [aria-label="Close"]');report.ok('phone closes newest card to Assistant',await session.eval("!document.querySelector('.pane').hidden && document.querySelector('.card').hidden"));}
  }
  await session.send('Emulation.clearDeviceMetricsOverride');
}
module.exports.narrowPhone=isolated(narrowPhone);
