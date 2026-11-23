'use strict';

const path = require('node:path');
const fs = require('node:fs');
const { setTimeout: sleep } = require('node:timers/promises');
const {readPng,thumbSample,delta} = require('./next_settings_pixels');

const A = '[data-pane=assistant]', B = '[data-pane=chat]';
const field = '#next-text-width', slider = '.next-width-slider';
const q = selector => `document.querySelector(${JSON.stringify(selector)})`;
const wait = (ctx, condition, label) => ctx.session.waitFor(condition, {timeoutMs:ctx.timeoutMs,label});
const requireProof = (ctx,label,ok,detail) => {ctx.report.ok(label,!!ok,detail); if (!ok) throw new Error(label);};
const frame = s => s.eval('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))');
const mouse = (s,type,x,y,held=false) => s.send('Input.dispatchMouseEvent',{type,x,y,
  button:type==='mouseMoved'&&!held?'none':'left',buttons:type==='mouseReleased'?0:held||type==='mousePressed'?1:0,
  clickCount:type==='mouseMoved'?0:1});

async function enter(ctx,width=1440) {
  await ctx.session.send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
  await ctx.session.send('Page.navigate',{url:ctx.url+'next/'});
  await wait(ctx,`!!${q('.chats-button')} && window.PentacleChatStore.getState().sessions.some(s=>s.stream_id==='local:web-gate-1')`,'web fixture inventory');
}
async function openChat(ctx) {
  await ctx.session.click('.chats-button');
  await ctx.session.click('.popover [data-stream-id="local:web-gate-1"]');
  await wait(ctx,`${q(B + ' .thread-inner')}?.dataset.streamId==='local:web-gate-1'`,'live fixture chat card');
}
async function setInput(s,selector,value) { if (!await s.type(selector,value)) throw new Error('missing '+selector); }

async function withNext(ctx, width, work, initScript) {
  const {session:s}=ctx;
  const installed = initScript ? await s.send('Page.addScriptToEvaluateOnNewDocument',{source:initScript}) : null;
  let old = null;
  try {
    await enter(ctx,width);
    const keys = ['pentacle.next.read','pentacle.next.mic','pentacle.next.theme'];
    old = await s.eval(`Object.fromEntries(${JSON.stringify(keys)}.map(k=>[k,localStorage.getItem(k)]))`);
    await work();
  } finally {
    if (old) await s.eval(`(() => { for (const [k,v] of Object.entries(${JSON.stringify(old)})) { if (v===null) localStorage.removeItem(k); else localStorage.setItem(k,v); } return true; })()`);
    if (installed) await s.send('Page.removeScriptToEvaluateOnNewDocument',{identifier:installed.identifier});
    await s.send('Emulation.clearDeviceMetricsOverride');
    await s.send('Page.navigate',{url:ctx.url});
    await wait(ctx,"document.readyState==='complete' && !!document.getElementById('session-list')",'restore old web');
  }
}

async function measureWidth(s, wanted) {
  return s.eval(`[...document.querySelectorAll('.stage > :is(.pane,.chat-card):not([hidden])')].map(pane => {
    const thread=pane.querySelector('.thread'), col=pane.querySelector('.thread-inner'), composer=pane.querySelector('.next-composer');
    const style=getComputedStyle(thread), pad=parseFloat(style.paddingLeft)+parseFloat(style.paddingRight);
    const expected=Math.min(${wanted},thread.clientWidth-pad);
    const voice=pane.querySelector('.voice-host'), work=pane.querySelector('.next-working:not([hidden])');
    const c=col.getBoundingClientRect(), f=composer.getBoundingClientRect();
    return {pane:pane.dataset.pane,expected,column:c.width,columnLeft:c.left,
      composerExpected:Math.min(${wanted},pane.clientWidth-(pane.classList.contains('chat-card')||innerWidth<600?32:48)),
      composer:f.width,composerLeft:f.left,voice:voice.getBoundingClientRect().width,
      working:work?.getBoundingClientRect().width ?? null,
      guides:['::before','::after'].every(p=>getComputedStyle(col,p).borderLeftStyle==='dashed')};
  })`);
}
const widthsMatch = rows => rows.every(row => Math.abs(row.column-row.expected)<1 && Math.abs(row.composer-row.composerExpected)<1);

const gripPoint = `(() => {
  const handle=document.querySelector('.grip:not([hidden])');
  if (!handle) return null;
  const r=handle.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  const x=r.left+r.width/2,y=r.top+Math.min(120,r.height/2);
  return document.elementFromPoint(x,y)?.closest('.grip')===handle ? {x,y} : null;
})()`;

async function split(ctx,wide,narrow) {
  const {session:s}=ctx;
  await wait(ctx, `!!${gripPoint}`, 'resize divider is visible and hit-testable');
  const drag = await s.eval(`(() => {
    const handle=${gripPoint}, good=${q(wide)}.getBoundingClientRect(), other=${q(narrow)}.getBoundingClientRect();
    const dir=good.left<other.left?1:-1;
    return {...handle, end:Math.max(6,Math.min(innerWidth-6,handle.x+(980-good.width)*dir))};
  })()`);
  await mouse(s,'mousePressed',drag.x,drag.y);
  await wait(ctx,"document.body.classList.contains('dragging')",'boundary owns the down event');
  await mouse(s,'mouseMoved',drag.end,drag.y,true);
  await mouse(s,'mouseReleased',drag.end,drag.y);
  await wait(ctx,`${q(wide)}.getBoundingClientRect().width>=955 &&
    ${q(narrow)}.getBoundingClientRect().width<500 &&
    !document.body.classList.contains('dragging')`,'actual wide and narrow pane dimensions');
  await frame(s);
}

async function width(ctx) {
  const {session:s}=ctx;
  await withNext(ctx,1440,async () => {
    if (await s.eval("!document.querySelector('[data-pane=lanes]').hidden")) await s.click('button[aria-label=Lanes]');
    await openChat(ctx);
    await s.eval(`(() => {
      const store=window.PentacleChatStore;
      const sessions=store.getState().sessions.map(s=>s.stream_id==='local:web-gate-1'
        ? {...s, working:true, working_label:'7s', status_card:{goal:'Width status fixture'}}
        : s.session_kind==='assistant_composite' ? {...s,working:true} : s);
      store.applyFrame({type:'session.inventory',sessions}); return true;
    })()`);
    await wait(ctx,`!!${q(A+' .next-working:not([hidden])')} && !!${q(B+' .next-working:not([hidden])')}`,'two visible working rows');
    const measure = async (wide,narrow) => {
      await s.click('[aria-label=Settings]');
      await setInput(s,field,'905');
      const rows=await measureWidth(s,905);
      const large=rows.find(r=>r.pane===wide), small=rows.find(r=>r.pane===narrow);
      requireProof(ctx,'transcript, field, voice and working row honor the same cap for '+wide+' wide',rows.length===2 && widthsMatch(rows)
        && Math.abs(large.column-905)<1 && small.column<905
        && rows.every(r=>r.guides && Math.abs(r.voice-r.expected)<1 && r.working!==null &&
          Math.abs(r.working-r.composerExpected)<1),rows);
      await s.eval(`${q(field)}.blur(); true`);
      requireProof(ctx,'blur removes both reading guides', (await measureWidth(s,905)).every(r=>!r.guides));
      await s.click('[aria-label=Settings]');
      return {large,small};
    };

    await split(ctx,A,B);
    const {small:chat} = await measure('assistant','chat');
    requireProof(ctx,'narrow chat text and field have the same width and left edge',
      Math.abs(chat.column-chat.composer)<1 && Math.abs(chat.columnLeft-chat.composerLeft)<1,chat);
    await s.click(B+' [aria-label=Status]');
    await wait(ctx,`!${q(B+' .status')}.hidden && ${q(B+' .status')}.textContent.includes('Width status fixture')`,'narrow status is open');
    const narrowStatus=await s.eval(`({view:${q(B+' .status')}.getBoundingClientRect().width,pane:${q(B)}.getBoundingClientRect().width})`);
    requireProof(ctx,'narrow status view respects its card width',Math.abs(narrowStatus.view-narrowStatus.pane)<1 && narrowStatus.view<905,narrowStatus);
    await s.click(B+' [aria-label=Status]');

    await split(ctx,B,A);
    await measure('chat','assistant');
    await s.click(B+' [aria-label=Status]');
    await wait(ctx,`!${q(B+' .status')}.hidden && ${q(B+' .status')}.textContent.includes('Width status fixture')`,'wide status is open');
    const status = await s.eval(`${q(B+' .status')}.getBoundingClientRect().width`);
    requireProof(ctx,'wide status view uses the exact 905px cap',Math.abs(status-905)<1,{status});
    await s.click(B+' [aria-label=Status]');

    await s.click('[aria-label=Settings]');
    await setInput(s,slider,'1200');
    requireProof(ctx,'slider and field stay synchronized',await s.eval(`${q(field)}.value==='1200' && document.documentElement.style.getPropertyValue('--read')==='1200px'`));
    await setInput(s,field,'2400');
    requireProof(ctx,'out-of-range field leaves slider at its own spoken maximum',await s.eval(
      `${q(field)}.value==='2400' && ${q(slider)}.value==='1200' && ${q(slider)}.getAttribute('aria-valuetext')==='1200 pixels'`));
    await setInput(s,slider,'1200');
    await setInput(s,field,'12a');
    await s.eval(`${q(field)}.blur(); true`);
    requireProof(ctx,'malformed width restores the applied number',await s.eval(`${q(field)}.value==='1200'`));
    await enter(ctx);
    requireProof(ctx,'width is restored after reload',await s.eval("document.documentElement.style.getPropertyValue('--read')==='1200px'"));
  });
}

const devicesScript = `(() => {
  const named = (deviceId,label) => ({kind:'audioinput',deviceId,label});
  const alias=named('default','Default'), one=named('next-desk','Desk microphone'), two=named('next-headset','Headset microphone'), three=named('next-extra','Other microphone');
  const media=navigator.mediaDevices;
  if (!media) throw new Error('fixture needs a secure loopback origin');
  window.__nextMic={devices:[alias,named('communications','Communications'),one,two,three],calls:[],streams:[],alias,one,two,three};
  media.enumerateDevices=async()=>window.__nextMic.devices;
  media.getUserMedia=async opts=>{
    window.__nextMic.calls.push(opts);
    const audio=new AudioContext(); await audio.resume();
    const oscillator=audio.createOscillator(), out=audio.createMediaStreamDestination();
    oscillator.connect(out); oscillator.start();
    let stopped=false;
    const dispose=()=>{if(!stopped){stopped=true;oscillator.stop();void audio.close();}};
    out.stream.getTracks().forEach(track=>{
      const stop=track.stop.bind(track);
      track.stop=()=>{stop();dispose();};
      track.addEventListener('ended',dispose,{once:true});
    });
    window.__nextMic.streams.push(out.stream);
    return out.stream;
  };
  window.__nextMic.change=rows=>{window.__nextMic.devices=rows;media.dispatchEvent(new Event('devicechange'));};
})()`;

async function record(ctx, pane, expected) {
  const s=ctx.session;
  const count=await s.eval('window.__nextMic.calls.length');
  await s.send('Runtime.evaluate',{expression:`${q(pane + ' [data-next-mic]')}.click()`,userGesture:true});
  await wait(ctx,`!!${q(pane+' [data-next-voice-bars]')} && window.__nextMic.calls.length===${count+1}`,'capture starts through the real Next recorder');
  const audio=await s.eval('window.__nextMic.calls.at(-1).audio');
  requireProof(ctx,'capture uses the expected native constraint', JSON.stringify(audio)===JSON.stringify(expected),audio);
  await s.click(pane+' [data-next-voice-cancel]');
  await wait(ctx,"window.__nextMic.streams.at(-1).getTracks().every(t=>t.readyState==='ended')",'cancel stops fixture tracks');
}

async function mic(ctx) {
  const s=ctx.session;
  await withNext(ctx,1440,async () => {
    await openChat(ctx); await s.click('[aria-label=Settings]');
    await wait(ctx,`!${q('.next-mic-setting')}.hidden`,'named microphone row');
    await s.click('.next-mic-choice');
    const labels=await s.eval("[...document.querySelectorAll('.next-mic-options button')].map(b=>b.textContent)");
    requireProof(ctx,'System default is first; real device labels follow, with no aliases',
      JSON.stringify(labels)===JSON.stringify(['System default','Desk microphone','Headset microphone','Other microphone']), labels);
    await s.click('.next-mic-options button:nth-child(3)');
    requireProof(ctx,'selection closes the choices',await s.eval(`${q('.next-mic-options')}.hidden`));
    await s.click('[aria-label=Settings]');
    await record(ctx,B,{deviceId:{exact:'next-headset'}});
    await enter(ctx); await s.click('[aria-label=Settings]');
    await wait(ctx,`${q('.next-mic-choice span')}?.textContent==='Headset microphone'`,'selection survived reload');
    await s.eval('window.__nextMic.change([window.__nextMic.alias,window.__nextMic.one,window.__nextMic.three]); true');
    await wait(ctx,`${q('.next-mic-choice span')}?.textContent==='System default' && !localStorage.getItem('pentacle.next.mic')`,'lost input cleared from storage');
    await s.click('[aria-label=Settings]');
    await record(ctx,A,true);
    await s.eval('window.__nextMic.change([window.__nextMic.alias,window.__nextMic.one]); true');
    await s.click('[aria-label=Settings]');
    await wait(ctx,`${q('.next-mic-setting')}.hidden`,'only one real input hides picker');
  },devicesScript);
}

async function phone(ctx) {
  const s=ctx.session;
  await withNext(ctx,440,async () => {
    await openChat(ctx); await s.click('[aria-label=Settings]');
    await wait(ctx,`!${q('.next-mic-setting')}.hidden`,'verified with multiple phone inputs');
    await setInput(s,field,'2400');
    const rows=await measureWidth(s,2400);
    requireProof(ctx,'phone reading area fits inside its actual margins',rows.length===1 && widthsMatch(rows)
      && rows[0].column<440 && rows[0].guides,rows);
    await s.eval('window.__nextMic.change([window.__nextMic.one]); true');
    await wait(ctx,`${q('.next-mic-setting')}.hidden`,'one phone input hides the picker');
  },devicesScript);
}

async function setupScroll(s, selector, long) {
  return s.eval(`(() => {
    const e=${q(selector)};
    if (!e) throw Error('scroll surface missing');
    e.style.background='var(--surface)';
    let filler=e.querySelector(':scope > [data-next-scroll-fixture]');
    if (!filler) { filler=document.createElement('div');filler.dataset.nextScrollFixture='';e.append(filler); }
    filler.style.cssText='height:'+(${long} ? 120000 : 0)+'px;flex:none;pointer-events:none';
    e.scrollTop=0;
    const r=e.getBoundingClientRect();
    return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,diff:e.offsetWidth-e.clientWidth,
      width:getComputedStyle(e).scrollbarWidth, overflow:e.scrollHeight>e.clientHeight,
      leftColumn:e.querySelector('.thread-inner')?.getBoundingClientRect().left};
  })()`);
}
async function sample(ctx, name, box, gutter, y, inset=0) {
  const file=path.join(ctx.report.dir,name+'.png');
  await ctx.session.screenshot(file);
  const capturedAt = Date.now();
  return {...thumbSample(readPng(fs.readFileSync(file)),box,gutter,y,inset),capturedAt};
}

async function scrollbars(ctx) {
  const s=ctx.session;
  await withNext(ctx,1440,async () => {
    const thread=A+' .thread', list='.popover.next-chat-list-popover:not([hidden])';
    for (const theme of ['dark','light','cosmic']) {
      await s.eval(`document.documentElement.dataset.theme=${JSON.stringify(theme)}; true`);
      await mouse(s,'mouseMoved',2,2);
      const short=await setupScroll(s,thread,false), long=await setupScroll(s,thread,true);
      requireProof(ctx,theme+' body keeps two native gutters and the column does not shift',
        long.width==='auto' && !short.overflow && long.overflow && long.diff===28
        && Math.abs(short.leftColumn-long.leftColumn)<.5,{short,long});
      await frame(s); await sleep(1220);
      const rest=await sample(ctx,'next-scroll-'+theme+'-rest',long,14,23);
      requireProof(ctx,theme+' quiet thumb paints 4 by 38',rest.width===4 && rest.length===38,rest);
      await mouse(s,'mouseMoved',long.right-55,long.top+100);
      await wait(ctx,`${q(thread)}.classList.contains('next-scroll-near') &&
        !${q(thread)}.classList.contains('next-scrolling')`,'pointer-over-area class is active without a scroll');
      await frame(s);
      const near=await sample(ctx,'next-scroll-'+theme+'-near',long,14,23);
      await mouse(s,'mouseMoved',rest.x,rest.y); await frame(s);
      const hot=await sample(ctx,'next-scroll-'+theme+'-hot',long,14,23);
      requireProof(ctx,theme+' pointer contrast rises twice and the hot thumb paints 8 px',
        near.contrast>rest.contrast && hot.contrast>near.contrast && hot.width===8,{rest,near,hot});
      await mouse(s,'mousePressed',rest.x,rest.y); await frame(s);
      const active=await sample(ctx,'next-scroll-'+theme+'-active',long,14,23);
      const color=await s.eval(`(() => {const c=document.createElement('canvas').getContext('2d');c.fillStyle=getComputedStyle(document.body).getPropertyValue('--accent').trim();c.fillRect(0,0,1,1);return [...c.getImageData(0,0,1,1).data].slice(0,3);})()`);
      requireProof(ctx,theme+' held thumb uses the theme accent',active.width===8 && delta(active.color,color)<=2,{active,color});
      await mouse(s,'mouseReleased',rest.x,rest.y);
      await mouse(s,'mouseMoved',2,2);
      await wait(ctx,`!${q(thread)}.classList.contains('next-scroll-near')`,'pointer leaving resets area hover');
      const scrollAt=await s.eval(`new Promise((resolve,reject) => {
        const thread=${q(thread)};
        const timeout=setTimeout(()=>reject(Error('no real scroll event')),1000);
        thread.addEventListener('scroll',()=>{clearTimeout(timeout);resolve(Date.now());},{once:true});
        thread.scrollTop=100;
      })`);
      requireProof(ctx,theme+' scroll event marked the actual area',
        await s.eval(`${q(thread)}.classList.contains('next-scrolling')`));
      const recent=await sample(ctx,'next-scroll-'+theme+'-recent',long,14,23);
      requireProof(ctx,theme+' recent scroll matches pointer-near strength within 300 ms',
        recent.capturedAt-scrollAt<300 && delta(recent.color,near.color)<=2,{recent,near});
      await sleep(1220);
      const calm=await sample(ctx,'next-scroll-'+theme+'-calm',long,14,23);
      requireProof(ctx,theme+' thumb returns to rest after inactivity',delta(calm.color,rest.color)<=2,{calm,rest});

      await s.click('.chats-button');
      const box=await setupScroll(s,list,true);
      requireProof(ctx,theme+' overflowing chat list has visible 10px native scrollbar',box.width==='auto' && box.overflow && box.diff===10,box);
      await sleep(1220);
      const listRest=await sample(ctx,'next-scroll-'+theme+'-list-rest',box,10,23,6);
      await mouse(s,'mouseMoved',listRest.x,listRest.y); await frame(s);
      const listHot=await sample(ctx,'next-scroll-'+theme+'-list-hot',box,10,23,6);
      requireProof(ctx,theme+' list thumb paints 4 px at rest and 6 px hot',listRest.width===4 && listHot.width===6,{listRest,listHot});
      await s.click('.chats-button');
    }
  });
}

module.exports={width,mic,scrollbars,phone};
