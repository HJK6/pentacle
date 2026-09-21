'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const WebSocket=require('ws');
const {createWsBridge}=require('../server/ws_bridge');
const {micStartSameOrigin}=require('../server/mic_starter');
const {createTransport,buildCc}=require('../renderer/web_cc');
const {createCcHandlers,createCollector}=require('../main/cc_handlers');
const {main}=require('../server');
const channels=['hosts','start','code','cancel'].map(n=>'provider-relogin:'+n);
test('all auth channels require admitted origin before any handler/input/output',async()=>{
  for(const origin of ['https://evil.invalid','null',undefined]) {
    let calls=0;const replies=[];
    const table=Object.fromEntries(channels.map(ch=>[ch,{mode:'invoke',handler:()=>{calls++;return 'SECRET_FIXTURE';}}]));
    const bridge=createWsBridge({table});const socket={send:r=>replies.push(JSON.parse(r))};
    bridge.addSocket(socket,{reloginAllowed:micStartSameOrigin({headers:{host:'127.0.0.1:7795',origin},socket:{}})});
    for(const method of channels)await bridge.handleMessage(socket,JSON.stringify({id:method,method,args:[]}));
    assert.equal(calls,0);assert.ok(replies.every(r=>r.error.code==='relogin_origin_refused'));assert.equal(JSON.stringify(replies).includes('SECRET_FIXTURE'),false);bridge.closeAll();
  }
});
test('same-origin auth responses stay with their sender and all destruction callbacks run',async()=>{
  const table={'provider-relogin:start':{mode:'invoke',handler:event=>{event.sender.send('provider-relogin:state',{url:'SECRET_FIXTURE'});return {ok:true};}}};
  const bridge=createWsBridge({table});const a=[],b=[];const sa={send:r=>a.push(JSON.parse(r))},sb={send:r=>b.push(JSON.parse(r))};
  const sender=bridge.addSocket(sa,{reloginAllowed:true});bridge.addSocket(sb,{reloginAllowed:true});
  let terminalCleanup=0,authCleanup=0;
  sender.once('destroyed',()=>terminalCleanup++);sender.once('destroyed',()=>authCleanup++);
  await bridge.handleMessage(sa,JSON.stringify({id:1,method:'provider-relogin:start'}));
  assert.equal(a.length,2);assert.equal(b.length,0);bridge.removeSocket(sa);bridge.removeSocket(sa);
  assert.equal(terminalCleanup,1);assert.equal(authCleanup,1);bridge.closeAll();
});
test('shared desktop/web table has all sign-in methods without calling daemon auth',async()=>{
  const c=createCollector();const client=new Proxy({},{get:()=>()=>{throw new Error('auth reached daemon');}});
  const stop=createCcHandlers({CONFIG:{chatStream:{localHost:'fixture'}},chatStreamClient:client}).register(c);
  try {for(const ch of channels)assert.equal(typeof c.table[ch]?.handler,'function');assert.equal(c.table[channels[0]].handler().hosts[0].id,'fixture');} finally {await stop();}
});
test('web auth calls never queue before open or after loss; disconnected event wipes consumers',async t=>{
  const instances=[];
  class Socket {
    static OPEN=1;constructor(){this.readyState=0;this.handlers={};this.sent=[];instances.push(this);}
    addEventListener(n,f){this.handlers[n]=f;}send(raw){this.sent.push(JSON.parse(raw));}
    open(){this.readyState=1;this.handlers.open();}close(){this.readyState=3;this.handlers.close();}
  }
  const prev=global.WebSocket;global.WebSocket=Socket;
  const transport=createTransport({url:'ws://fixture/cc',logger:{warn(){}}});
  t.after(()=>{transport.close();global.WebSocket=prev;});
  const cc=buildCc(transport,{clipboard:{},config:{}});const states=[];cc.onReloginState(v=>states.push(v));
  await assert.rejects(cc.reloginStart({id:'fixture_attempt_123'}),/unavailable/);await assert.rejects(cc.reloginCode('fixture_attempt_123','SECRET_FIXTURE'),/unavailable/);
  instances[0].open();assert.deepEqual(instances[0].sent,[]);
  const pending=cc.reloginCode('fixture_attempt_123','SECRET_FIXTURE');
  // close() suppresses reconnect scheduling and still signals loss.
  transport.close();await assert.rejects(pending,/connection lost/);assert.equal(states.at(-1).state,'disconnected');
  await assert.rejects(cc.reloginCancel('fixture_attempt_123'),/unavailable/);assert.equal(instances[0].sent.length,1);
});
function connect(url,headers) {
  return new Promise((resolve,reject)=>{const ws=new WebSocket(url,{headers});ws.once('open',()=>resolve(ws));ws.once('error',reject);});
}
function request(ws,method) {
  return new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('fixture response timeout')),2000);ws.once('message',raw=>{clearTimeout(timer);resolve(JSON.parse(raw));});ws.send(JSON.stringify({id:1,method,args:[]}));});
}
for(const protectedMode of [false,true])test(`actual web server origin admission; token protection=${protectedMode}`,async t=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'relogin-origin-'));
  const profile=path.join(dir,'profile.js');fs.writeFileSync(profile,"module.exports={chatStream:{localHost:'fixture',hosts:['local']},features:{mic:false}};\n");
  const args=['--profile',profile,'--port','0'];
  if(protectedMode){const token=path.join(dir,'web.token');fs.writeFileSync(token,'FIXTURE_WEB_TOKEN\n',{mode:0o600});args.push('--token-file',token);}
  const server=await main(args);const sockets=[];
  t.after(async()=>{for(const ws of sockets)ws.terminate();await server.close();fs.rmSync(dir,{recursive:true,force:true});});
  const base=`http://127.0.0.1:${server.port}`;let cookie;
  if(protectedMode){const response=await fetch(base+'/login',{method:'POST',body:'token=FIXTURE_WEB_TOKEN',headers:{'content-type':'application/x-www-form-urlencoded'},redirect:'manual'});assert.equal(response.status,302);cookie=response.headers.get('set-cookie').split(';')[0];}
  let calls=0;
  for(const ch of channels)server.handlers[ch].handler=()=>{calls++;return {fixture:true};}; // never invoke a provider
  for(const origin of ['https://evil.invalid','null',undefined,base]) {
    const headers={...(cookie?{Cookie:cookie}:{}),...(origin?{Origin:origin}:{})};
    const ws=await connect(`ws://127.0.0.1:${server.port}/cc`,headers);sockets.push(ws);
    const before=calls;
    for(const ch of channels){const result=await request(ws,ch);assert.equal(result.ok,origin===base);if(origin!==base)assert.equal(result.error.code,'relogin_origin_refused');}
    assert.equal(calls-before,origin===base?channels.length:0);ws.close();
  }
});
