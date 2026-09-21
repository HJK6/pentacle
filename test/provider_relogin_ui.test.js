'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const {createProviderRelogin}=require('../renderer/provider_relogin_ui');
const tick=()=>new Promise(r=>setImmediate(r));
function fixture(t) {
  const dom=new JSDOM('<main></main>',{url:'https://fixture.invalid'});
  const calls=[];let push;
  const cc={reloginHosts:async()=>({ok:true,hosts:[{id:'target-a',label:'Target A',available:true},{id:'target-b',label:'Target B',available:true}]}),
    reloginStart:async r=>{calls.push(['start',r]);return {ok:true,id:r.id};},
    reloginCancel:async id=>{calls.push(['cancel',id]);push({id,state:'cancelled'});return {ok:true};},
    reloginCode:async(id,code)=>{calls.push(['code',id,code]);return {ok:true};},
    openExternal:async url=>calls.push(['open',url]),onReloginState:f=>{push=f;}};
  const panel=createProviderRelogin({document:dom.window.document,cc,parent:dom.window.document.querySelector('main'),makeId:()=> 'fixture_ui_attempt_123'});
  const el=n=>panel.element.querySelector(`[data-relogin="${n}"]`);
  t.after(async()=>{await panel.close();dom.window.close();});
  const select=async(provider='codex')=>{await panel.refresh();el('host').value='target-b';panel.element.querySelector(`[data-provider="${provider}"]`).click();};
  const start=async()=>{el('available').checked=true;el('available').dispatchEvent(new dom.window.Event('change'));el('start').click();await tick();};
  return {dom,cc,calls,panel,el,select,start,push:v=>push(v)};
}
for(const provider of ['codex','claude'])test(`${provider}: provider and execution host warning precede explicit Start`,async t=>{
  const f=fixture(t);await f.select(provider);
  assert.match(f.el('title').textContent,/target-b/);assert.match(f.el('warning').textContent,/clear or replace/);assert.match(f.el('warning').textContent,/Cancelling or failing/);
  assert.equal(f.el('start').disabled,true);assert.equal(f.calls.length,0);
  await f.start();assert.equal(f.calls[0][1].provider,provider);assert.equal(f.calls[0][1].host,'target-b');assert.equal(f.calls[0][1].available,true);
  if(provider==='codex')assert.match(f.el('browser-guide').textContent,/different machine cannot reach/);
  assert.equal(f.calls.some(c=>c[0]==='open'),false);
});
test('URL and code are transient; code has password input and terminal success clears material',async t=>{
  const f=fixture(t);await f.select('claude');await f.start();
  const url='https://claude.ai/oauth/authorize?state=FIXTURE_SECRET&scope=x%20y';
  f.push({id:'fixture_ui_attempt_123',state:'awaiting_browser',url});assert.equal(f.el('url').value,url);assert.equal(f.calls.some(c=>c[0]==='open'),false);
  f.el('open').click();await tick();assert.equal(f.calls.at(-1)[1],url);
  assert.equal(f.el('code').type,'password');f.el('code').value='FIXTURE_CODE';
  f.el('code-form').dispatchEvent(new f.dom.window.Event('submit',{cancelable:true}));await tick();
  assert.equal(f.el('code').value,'');assert.deepEqual(f.calls.at(-1),['code','fixture_ui_attempt_123','FIXTURE_CODE']);
  f.push({id:'fixture_ui_attempt_123',state:'verifying'});assert.equal(f.el('url').value,'');
  f.push({id:'fixture_ui_attempt_123',state:'succeeded'});assert.match(f.el('status').textContent,/verified/);
  assert.equal(f.dom.window.localStorage.length,0);assert.equal(f.dom.window.sessionStorage.length,0);
});
for(const state of ['cancelled','failed','timed_out','cleanup_failed'])test(`${state}: honest terminal outcome with no retained URL`,async t=>{
  const f=fixture(t);await f.select();await f.start();f.push({id:'fixture_ui_attempt_123',state:'awaiting_browser',url:'https://fixture.invalid/SECRET'});
  f.push({id:'fixture_ui_attempt_123',state});assert.equal(f.el('url').value,'');assert.equal(f.el('url-group').hidden,true);
  assert.match(f.el('status').textContent,state==='cleanup_failed'?/blocked/:/may be signed out/);
});
test('close cancels pending work and ignores subsequent private output',async t=>{
  const f=fixture(t);await f.select();await f.start();await f.panel.close();
  assert.equal(f.calls.at(-1)[0],'cancel');f.push({id:'fixture_ui_attempt_123',state:'awaiting_browser',url:'https://fixture.invalid/SECRET'});
  assert.equal(f.el('dialog').hidden,true);assert.equal(f.el('url').value,'');
});
test('disconnect clears code/URL, rejects stale state and never restarts automatically',async t=>{
  const f=fixture(t);await f.select('claude');await f.start();f.push({id:'fixture_ui_attempt_123',state:'awaiting_browser',url:'https://fixture.invalid/SECRET'});f.el('code').value='SECRET';
  f.push({state:'disconnected'});f.push({id:'fixture_ui_attempt_123',state:'succeeded'});
  assert.equal(f.el('code').value,'');assert.equal(f.el('url').value,'');assert.match(f.el('status').textContent,/not confirmed/);assert.equal(f.calls.filter(c=>c[0]==='start').length,1);
});
test('busy host rejection gives no false success or automatic retry',async t=>{
  const f=fixture(t);f.cc.reloginStart=async()=>({ok:false,reason:'already_running'});await f.select();await f.start();assert.match(f.el('status').textContent,/unresolved cleanup/);
});

for(const provider of ['codex','claude'])test(`${provider}: DOM through web shim, shared handlers and controller completes the sign-in journey`,async t=>{
  const {createCcHandlers,createCollector}=require('../main/cc_handlers');
  const {createWsBridge}=require('../server/ws_bridge');
  const {buildCc}=require('../renderer/web_cc');
  const dom=new JSDOM('<main></main>',{url:'https://fixture.invalid'});
  const processes=[];
  const pty={spawn(file,args){let data,exit;const nonce=args.join(' ').match(/__PENTACLE_RELOGIN_([a-f0-9]+):/)[1];
    const proc={file,args,writes:[],onData:f=>data=f,onExit:f=>exit=f,data:d=>data(d),write(d){this.writes.push(d);},kill(){},finish(text){data(text+`\n__PENTACLE_RELOGIN_${nonce}:0\n`);exit({exitCode:0,signal:0});}};processes.push(proc);return proc;}};
  const table=createCollector();const stop=createCcHandlers({CONFIG:{chatStream:{localHost:'target-b'}},chatStreamClient:{},reloginOptions:{pty,log(){}}}).register(table);
  const bridge=createWsBridge({table:table.table});const pending=new Map(),listeners=new Map();let next=0;
  const socket={send(raw){const m=JSON.parse(raw);if(m.event)listeners.get(m.event)?.(...m.args);else{pending.get(m.id)?.(m.result);pending.delete(m.id);}}};
  bridge.addSocket(socket,{reloginAllowed:true});
  const transport={call(method,...args){const id=++next;return new Promise(resolve=>{pending.set(id,resolve);bridge.handleMessage(socket,JSON.stringify({id,method,args}));});},fire(){},on:(event,fn)=>listeners.set(event,fn),onReconnect(){}};
  const cc=buildCc(transport,{clipboard:{},config:{}});
  const panel=createProviderRelogin({document:dom.window.document,cc,parent:dom.window.document.querySelector('main'),makeId:()=> 'integrated_attempt_123'});
  t.after(async()=>{await panel.close();await stop();bridge.closeAll();dom.window.close();});
  const el=n=>panel.element.querySelector(`[data-relogin="${n}"]`);
  await panel.refresh();panel.element.querySelector(`[data-provider="${provider}"]`).click();
  assert.equal(processes.length,0);el('available').checked=true;el('available').dispatchEvent(new dom.window.Event('change'));el('start').click();await tick();
  assert.equal(processes.length,1);assert.equal(processes[0].file,'/bin/bash');
  const url=`https://${provider==='codex'?'auth.openai.com':'claude.ai'}/oauth/authorize?client_id=fixture&state=FIXTURE_STATE&code_challenge=fixture&scope=a%20b`;
  processes[0].data(url+'\n');assert.equal(el('url').value,url);
  if(provider==='claude'){el('code').value='FIXTURE_CODE';el('code-form').dispatchEvent(new dom.window.Event('submit',{cancelable:true}));await tick();assert.deepEqual(processes[0].writes,['FIXTURE_CODE\r']);}
  processes[0].finish(provider==='codex'?'Successfully logged in':'Login successful');assert.equal(processes.length,2);assert.match(el('status').textContent,/Checking/);
  processes[1].finish(provider==='codex'?'Logged in using ChatGPT':'{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty"}');
  assert.match(el('status').textContent,/Sign-in verified/);assert.equal(el('url').value,'');assert.equal(el('code').value,'');
});
