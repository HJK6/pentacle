#!/usr/bin/env node
'use strict';
// Real browser -> web host -> main client -> loopback daemon -> native provider.
// The transport counterpart creates real admission pressure and loses ACKs;
// it never fabricates refusals or receipts and never patches the subject.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn, spawnSync, execFileSync } = require('node:child_process');
const { once } = require('node:events');
const WebSocket = require('ws');
const cdp = require('./lib/cdp');
const { headlessChromeArgs } = require('./lib/chrome_launch.cjs');
const { getFreePort } = require('./lib/disposable_web_daemon_config');
const { withRuntimeDirectory } = require('./lib/runtime_directory');
const ROOT = path.resolve(__dirname, '../..');
if (!process.argv[2] || !process.argv[3]) throw Error('Usage: send_refusal_gate.cjs FRESH_OUTPUT_DIRECTORY PROVISIONED_PYTHON [--red]');
const out = path.resolve(process.argv[2]);
const python = process.argv[3];
const red = process.argv.includes('--red');
const stream = 'local:send-refusal-fixture';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const quote = s => "'" + s.replaceAll("'", "'\\''") + "'";
const write = (name, value) => fs.writeFileSync(path.join(out, name), JSON.stringify(value, null, 2));
async function wait(read, test, label, timeout=20000) {
  const end = Date.now()+timeout;let value;
  while(Date.now()<end) { value=await read();if(test(value))return value;await sleep(50); }
  throw Error(label+': '+JSON.stringify(value));
}
async function stop(proc) {
  if(!proc || proc.exitCode!==null || proc.signalCode)return;
  proc.kill('SIGTERM');await Promise.race([once(proc,'exit'),sleep(4000)]);
  if(proc.exitCode===null&&!proc.signalCode){proc.kill('SIGKILL');await once(proc,'exit');}
}
const sourceBounds = () => Object.fromEntries(['main/chat_stream_client.js', 'renderer/src/chat_store_controller.ts',
 'renderer/src/shared_transcript_view.ts', 'test/e2e/send_refusal_gate.cjs', 'test/e2e/lib/send_refusal_fixture.py',
 'renderer/dist/web/bundle.js', 'renderer/dist/web/chat_core.bundle.js', 'renderer/dist/web/styles.css']
 .map(file => [file, crypto.createHash('sha256').update(fs.readFileSync(path.join(ROOT,file))).digest('hex')]));
async function run(runtimeDir) {
 fs.mkdirSync(out,{recursive:true,mode:0o700});assert.equal(fs.readdirSync(out).length,0,'fresh evidence directory required');
 let daemon, browser, page, web, proxy, tmuxStarted=false;
 const sockets=new Set(), trace=[], submissions=()=>fs.existsSync(path.join(out,'submissions.jsonl'))?fs.readFileSync(path.join(out,'submissions.jsonl'),'utf8').trim().split('\n').filter(Boolean).map(JSON.parse):[];
 const tmux=execFileSync('which',['tmux'],{encoding:'utf8'}).trim();
 const ns='ptest-send-'+crypto.randomBytes(5).toString('hex');
 const tmuxBin=path.join(out,'tmux');fs.writeFileSync(tmuxBin,'#!/bin/sh\nexec '+quote(tmux)+' -L '+quote(ns)+' -f /dev/null "$@"\n',{mode:0o700});
 const env={PATH:process.env.PATH,LANG:'en_US.UTF-8',SHELL:'/bin/sh',PENTACLE_ADMIT_DURABLE:'1',PENTACLE_INGEST_INTERVAL_S:'.1'};
 const wrapper=path.join(ROOT,'test/e2e/lib/web_gate_daemon.py');
 let port;
 const startDaemon=async()=>{
  const log=fs.openSync(path.join(out,'daemon.log'),'a');
  daemon=spawn(python,[wrapper,out,'--host','127.0.0.1','--port',String(port),'--local-host','local',
   '--db',path.join(out,'sessions.db'),'--notifications-db',path.join(out,'notifications.db'),
   '--assets-db',path.join(out,'assets.db'),'--blob-root',path.join(out,'blobs'),
   '--tmux-bin',tmuxBin,'--projects-root',out,'--disable-hosts','--disable-mirror','--disable-nudges',
   '--disable-outbound-notices','--disable-remote-presence','--disable-reconciler'],{cwd:ROOT,env,stdio:['ignore',log,log]});
  fs.closeSync(log);
  await wait(async()=>{try{const ws=new WebSocket('ws://127.0.0.1:'+port);await once(ws,'open');ws.close();return true;}catch{return false;}},Boolean,'daemon listen');
  trace.push({event:'daemon_start',pid:daemon.pid,at:Date.now()});
 };
 const source=sourceBounds();write('source-bounds.json',{head:execFileSync('git',['rev-parse','HEAD'],{cwd:ROOT,encoding:'utf8'}).trim(),files:source});
 let failureClass='HARNESS_ERROR';
 try {
  fs.writeFileSync(path.join(out,'native.jsonl'),'');
  execFileSync(python,[wrapper,out,'--issue'],{cwd:ROOT,env});
  execFileSync(tmuxBin,['new-session','-d','-s','send-refusal-fixture','-x','180','-y','50',
   [python,path.join(ROOT,'test/e2e/lib/send_refusal_fixture.py'),'provider',out].map(quote).join(' ')],{env});tmuxStarted=true;
  await wait(()=>execFileSync(tmuxBin,['capture-pane','-p','-t','=send-refusal-fixture:'],{encoding:'utf8'}),v=>v.includes('READY'),'provider ready');
  execFileSync(python,[path.join(ROOT,'test/e2e/lib/send_refusal_fixture.py'),'seed',out],{cwd:ROOT,env});
  port=await getFreePort();await startDaemon();
  let lose=false, lostId=null, lostOpt=null, acceptedAt=null, manual=false;
  const seen=new Set(), wire=[];
  const upstreams=new Set();
  proxy=new WebSocket.Server({host:'127.0.0.1',port:0});await once(proxy,'listening');
  proxy.on('connection',down=>{
   const up=new WebSocket('ws://127.0.0.1:'+port);sockets.add(down);sockets.add(up);upstreams.add(up);
   const queued=[];up.on('open',()=>queued.splice(0).forEach(raw=>up.send(raw)));
   const send=raw=>up.readyState===1?up.send(raw):queued.push(raw);
   down.on('message',raw=>{
    const frame=JSON.parse(raw);if(frame.type==='send'){
     wire.push({request_id:frame.request_id,optimistic_id:frame.optimistic_id,hash:crypto.createHash('sha256').update(raw).digest('hex'),at:Date.now()});
     if(lose&&!lostId){lostId=frame.request_id;lostOpt=frame.optimistic_id;}
     if(!seen.has(frame.request_id)){
      seen.add(frame.request_id);
      // Batch a harmless invalid-target send before the subject. Both reach the
      // real reader in one TCP write; durable capacity=1 refuses the subject
      // before either scheduled handler runs. The filler cannot reach a pane.
      assert.equal(up.readyState,1);up._socket.cork();
      up.send(JSON.stringify({type:'send',stream_id:'local:missing-fixture',text:'filler',request_id:'pressure-'+frame.request_id}));
      up.send(raw);up._socket.uncork();return;
     }
    }
    send(raw);
   });
   const strip=frame=>{
    if(!lostId||manual)return false;
    if(frame.type==='send.result'&&frame.request_id===lostId){
     acceptedAt ??= Date.now();trace.push({event:'ack_lost',frame,at:acceptedAt});
     // Drop both the reply and broadcast path at the transport boundary.
     setTimeout(()=>{up.terminate();down.close();},5);return true;
    }
    const isLost=e=>e&&(e.optimistic_id===lostOpt||e.request_id===lostId||e.text==='lost ACK acceptance');
    if(frame.type==='chat.event'&&isLost(frame.event))return true;
    if(Array.isArray(frame.events))frame.events=frame.events.filter(e=>!isLost(e));
    return false;
   };
   up.on('message',raw=>{
    const frame=JSON.parse(raw);
    if(frame.type==='send.error'||frame.type==='send.result'||frame.type==='chat.event')trace.push({event:'daemon_frame',frame,at:Date.now()});
    if(String(frame.request_id||'').startsWith('pressure-'))return;
    if(strip(frame))return;
    if(down.readyState===1)down.send(JSON.stringify(frame));
   });
   up.on('error',()=>{});down.on('error',()=>{});
   up.on('close',()=>{if(down.readyState===1)down.close();upstreams.delete(up);sockets.delete(up);});
   down.on('close',()=>{up.terminate();sockets.delete(down);});
  });
  const config=path.join(out,'client.cjs');fs.writeFileSync(config,"module.exports="+JSON.stringify({
   features:{chatUi:true,inputBar:true,mic:false,dashboards:false},hosts:{local:{kind:'local'}},agents:{},tmux:tmuxBin,
   chatStream:{url:'ws://127.0.0.1:'+proxy.address().port,localHost:'local',hosts:['local'],tokenPath:path.join(out,'operator-auth/token'),autoStart:false}
  }));process.env.PENTACLE_CONFIG=config;
  web=await require(path.join(ROOT,'server')).main(['--profile',config,'--bind','127.0.0.1','--port','0']);
  const debug=await getFreePort();const chrome=process.env.PENTACLE_TEST_BROWSER||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  browser=spawn(chrome,[...headlessChromeArgs(),'--disable-gpu','--window-size=1600,1000','--user-data-dir='+path.join(out,'chrome'),'--remote-debugging-port='+debug,web.url],{stdio:'ignore'});
  page=await cdp.connect(debug,{timeoutMs:20000});
  await page.waitFor('window.PentacleChatStore?.getState().connected === true',{timeoutMs:20000});
  await page.waitFor(`window.PentacleChatStore.getState().sessions.some(s=>s.stream_id===${JSON.stringify(stream)})`,{timeoutMs:20000});
  await page.eval(`window.focusStreamId(${JSON.stringify(stream)}),true`);
  await page.waitFor("document.querySelector('#header-0 [data-mode=chat]')",{timeoutMs:10000});
  await page.click('#header-0 [data-mode=chat]');
  await page.waitFor("document.querySelector('#cell-0 .slot-chat-compose-send')",{timeoutMs:10000});
  const compose=async text=>{await page.type('#cell-0 .slot-chat-compose-input',text);await page.click('#cell-0 .slot-chat-compose-send');};
  const dom=()=>page.eval(`({rows:[...document.querySelectorAll('#cell-0 .slot-chat-row')].map(e=>({text:e.textContent,user:e.classList.contains('is-user')})),optimistic:window.PentacleChatStore.getState().optimisticSends,body:document.querySelector('#cell-0')?.innerText})`);
  const servedAssets=await page.eval(`(async()=>{const result={};for(const file of ['bundle.js','chat_core.bundle.js','styles.css']){
   const response=await fetch(file,{cache:'no-store'});if(!response.ok)throw Error('asset unavailable');
   const digest=await crypto.subtle.digest('SHA-256',await response.arrayBuffer());
   result['renderer/dist/web/'+file]=Array.from(new Uint8Array(digest)).map(x=>x.toString(16).padStart(2,'0')).join('');}return result})()`);
  for(const [file,hash] of Object.entries(servedAssets))assert.equal(hash,source[file],'browser served candidate asset '+file);
  write('served-assets.json',servedAssets);
  failureClass='PRODUCT_FAIL';
  await compose('refusal acceptance');await sleep(1000);
  write('refusal-observation.json',{wire:wire.slice(),trace,submissions:submissions(),dom:await dom()});
  if(red){
   assert.equal(wire.length,1);assert.equal(submissions().length,0);
   assert.ok(trace.some(e=>e.frame?.type==='send.error'&&e.frame?.error_code==='server_overloaded'&&e.frame?.retryable===true));
   await page.screenshot(path.join(out,'red.png'));write('verdict.json',{status:'EXPECTED_RED',cause:'actual daemon pre-admission refusal terminal in browser',wire:wire.length,submissions:0});return;
  }
  await wait(submissions,v=>v.length===1,'one physical submission');
  await wait(dom,v=>v.body?.includes('Sent'),'truthful Sent');
  assert.equal(wire.length,2);assert.equal(wire[0].hash,wire[1].hash);
  assert.equal((await dom()).rows.filter(r=>r.user&&r.text.includes('refusal acceptance')).length,1);
  assert.ok(!(await dom()).body.includes('Synthetic reply: refusal acceptance'),'no false model completion');
  fs.writeFileSync(path.join(out,'release-1'),'');
  await wait(dom,v=>v.body?.includes('Synthetic reply: refusal acceptance'),'later actual native assistant response');
  write('s9.json',{wire:wire.slice(),dom:await dom(),submissions:submissions()});await page.screenshot(path.join(out,'recovered.png'));
  lose=true;await compose('lost ACK acceptance');
  await wait(()=>acceptedAt,Boolean,'accepted receipt before lost ACK');
  await wait(()=>page.eval(`Object.values(window.PentacleChatStore.getState().optimisticSends||{}).some(s=>s.request_id===${JSON.stringify(lostId)}&&s.status==='indeterminate')`),Boolean,'browser uncertain after lost ACK');
  assert.equal(submissions().filter(s=>s.text==='lost ACK acceptance').length,1);
  write('s7-checkpoint.json',{acceptedAt,lostId,lostOpt,submissions:submissions(),dom:await dom()});
  console.log('S7_DELAY_STARTED '+JSON.stringify({acceptedAt,pid:daemon.pid}));
  await sleep(Math.max(0,acceptedAt+61000-Date.now()));
  trace.push({event:'daemon_stop',pid:daemon.pid,at:Date.now()});await stop(daemon);await startDaemon();
  await wait(()=>require(path.join(ROOT,'main/chat_stream_client')).connected,Boolean,'main transport reauthenticated',30000);
  await page.waitFor("document.querySelector('#cell-0 .slot-chat-send-retry') && !document.querySelector('#cell-0')?.innerText.includes('Reconnecting')",{timeoutMs:30000});
  // Only receipt reconciliation from the explicit Retry can settle this row;
  // the transport has withheld the lost message's echo/backfill throughout.
  manual=true;
  await page.eval(`(() => { const row=[...document.querySelectorAll('#cell-0 .slot-chat-row')].find(e=>e.textContent.includes('lost ACK acceptance'));const button=[...row.querySelectorAll('button')].find(e=>e.textContent==='Retry');if(!button)throw Error('no real Retry');button.click();return true;})()`);
  await wait(dom,v=>v.body?.includes('Sent')&&!Object.values(v.optimistic||{}).some(s=>s.request_id===lostId&&s.status==='indeterminate'),'same-ID receipt replay');
  const lostWire=wire.filter(f=>f.optimistic_id===lostOpt);
  assert.equal(new Set(lostWire.map(f=>f.request_id)).size,1);
  assert.equal(new Set(lostWire.map(f=>f.hash)).size,1);
  assert.ok(Date.now()-acceptedAt>60000);
  assert.equal(submissions().filter(s=>s.text==='lost ACK acceptance').length,1,'exactly one across both daemon PIDs');
  assert.equal((await dom()).rows.filter(r=>r.user&&r.text.includes('lost ACK acceptance')).length,1);
  write('s7.json',{elapsedMs:Date.now()-acceptedAt,lostId,lostOpt,wire:lostWire,trace,submissions:submissions(),dom:await dom()});
  fs.writeFileSync(path.join(out,'release-2'),'');await wait(dom,v=>v.body?.includes('Synthetic reply: lost ACK acceptance'),'correlated later response');
  await compose('lost ACK acceptance');await wait(submissions,v=>v.filter(s=>s.text==='lost ACK acceptance').length===2,'intentional same-text new message');
  assert.deepEqual(sourceBounds(),source,'tested source and served assets stay unchanged');
  write('verdict.json',{status:'PASS',scope:'S1/S7/S9 real isolated browser/client/daemon/native provider',trace,wire,submissions:submissions(),dom:await dom()});
 } catch(error) {
  write('failure.json',{classification:failureClass,error:error.stack,trace,submissions:submissions(),dom:page?await page.eval('document.body.innerText').catch(()=>null):null});throw error;
 } finally {
  page?.close();await stop(browser);await web?.close();for(const socket of sockets)socket.terminate();
  if(proxy)await new Promise(resolve=>proxy.close(resolve));await stop(daemon);
  if(tmuxStarted)spawnSync(tmuxBin,['kill-server']);
  const tmuxGone=spawnSync(tmuxBin,['list-sessions']).status!==0;
  const secrets=[path.join(out,'operator-auth/token')];for(const file of secrets)if(fs.existsSync(file))fs.unlinkSync(file);
  write('cleanup.json',{runtimeDirectoryIsolated:process.env.PENTACLE_RUNTIME_DIR===runtimeDir,daemonStopped:!daemon||daemon.exitCode!==null||!!daemon.signalCode,browserStopped:!browser||browser.exitCode!==null||!!browser.signalCode,webClosed:true,proxyClosed:true,tmuxGone,operatorTokenRemoved:secrets.every(file=>!fs.existsSync(file))});
 }
}
let ownedRuntime;
withRuntimeDirectory(directory=>{ownedRuntime=directory;return run(directory);}).finally(()=>{
 const file=path.join(out,'cleanup.json');
 if(fs.existsSync(file)){const receipt=JSON.parse(fs.readFileSync(file));receipt.runtimeDirectoryRemoved=!fs.existsSync(ownedRuntime);write('cleanup.json',receipt);}
}).catch(error=>{console.error(error.stack);process.exitCode=1;});
