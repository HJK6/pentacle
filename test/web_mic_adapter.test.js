const test=require('node:test');
const assert=require('node:assert/strict');
const {buildCc}=require('../renderer/web_cc');
const {createWsBridge}=require('../server/ws_bridge');
test('web microphone uses host RPC rather than viewer loopback',async()=>{
 const calls=[];const cc=buildCc({call:async(...a)=>{calls.push(a);return {mode:'on'};},fire(){},on(){}},{config:{features:{mic:true}},clipboard:{}});
 assert.deepEqual(await cc.micRequest('GET','/status'),{mode:'on'});
 assert.deepEqual(calls,[['mic:request','GET','/status']]);
});
test('mic operations require same-origin websocket admission',async()=>{
 let calls=0;const frames=[];const socket={send:s=>frames.push(JSON.parse(s))};
 const bridge=createWsBridge({table:{'mic:request':{mode:'invoke',handler:()=>{calls++;return {ok:true};}}}});
 bridge.addSocket(socket);await bridge.handleMessage(socket,JSON.stringify({id:1,method:'mic:request',args:['POST','/mode/off']}));
 assert.equal(calls,0);assert.equal(frames[0].error.code,'mic_origin_refused');
 bridge.removeSocket(socket);bridge.addSocket(socket,{micStartAllowed:true});await bridge.handleMessage(socket,JSON.stringify({id:2,method:'mic:request',args:['GET','/status']}));assert.equal(calls,1);bridge.closeAll();
});
const {createMicRequest}=require('../main/mic_request');
const http=require('node:http');
test('host adapter reaches configured service and forwards existing response/error shape',async()=>{
 const seen=[];
 const server=http.createServer((req,res)=>{let body='';req.on('data',c=>body+=c);req.on('end',()=>{seen.push([req.method,req.url,body]);res.setHeader('Content-Type','application/json');if(req.url==='/copy/start'){res.statusCode=409;res.end('{"error":"wake active"}');}else res.end('{"mode":"off","ok":true}');});});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 try {
  const request=createMicRequest({features:{mic:true},micServerUrl:`http://127.0.0.1:${server.address().port}`});
  assert.deepEqual(await request('GET','/status'),{mode:'off',ok:true});
  assert.deepEqual(await request('POST','/mode/off',{caller:'fixture'}),{mode:'off',ok:true});
  assert.deepEqual(await request('POST','/copy/start'),{error:'wake active',ok:false,status:409});
  assert.deepEqual(seen,[['GET','/status',''],['POST','/mode/off','{"caller":"fixture"}'],['POST','/copy/start','']]);
 } finally {await new Promise(r=>server.close(r));}
});
test('unsupported operations, arbitrary destinations and oversize data never reach backend',async()=>{
 let calls=0;const request=createMicRequest({features:{mic:true}},async()=>{calls++;throw Error('never');});
 for(const [method,path,body] of [['DELETE','/status'],['POST','http://elsewhere.test/'],['GET','/../status'],['GET','/status?x=1'],['POST','/mode/off',[]],['POST','/mode/off',{x:'a'.repeat(9000)}]])assert.equal((await request(method,path,body)).ok,false);
 assert.equal(calls,0);
 assert.equal((await createMicRequest({features:{mic:false}},()=>calls++)('GET','/status')).status,503);assert.equal(calls,0);
});
test('offline backend preserves renderer recovery path without leaking diagnostics',async()=>{
 const request=createMicRequest({features:{mic:true}},()=>{throw Error('private details');});assert.equal(await request('GET','/status'),null);
});

const {createTransport}=require('../renderer/web_cc');
const {WebSocketServer}=require('ws');
test('every browser mic call shape survives real JSON websocket transport',async()=>{
 const seen=[];const backend=http.createServer((req,res)=>{let body='';req.on('data',c=>body+=c);req.on('end',()=>{seen.push([req.method,req.url,body]);res.setHeader('Content-Type','application/json');res.end('{"ok":true,"mode":"off"}');});});
 await new Promise(r=>backend.listen(0,'127.0.0.1',r));
 const request=createMicRequest({features:{mic:true},micServerUrl:`http://127.0.0.1:${backend.address().port}`});
 const bridge=createWsBridge({table:{'mic:request':{mode:'invoke',handler:(_e,...args)=>request(...args)},'mic:start-server':{mode:'invoke',handler:()=>true}}});
 const wire=http.createServer();const ws=new WebSocketServer({server:wire});
 ws.on('connection',socket=>{bridge.addSocket(socket,{micStartAllowed:true});socket.on('message',raw=>bridge.handleMessage(socket,raw));socket.on('close',()=>bridge.removeSocket(socket));});
 await new Promise(r=>wire.listen(0,'127.0.0.1',r));
 const transport=createTransport({url:`ws://127.0.0.1:${wire.address().port}`});
 const cc=buildCc(transport,{config:{features:{mic:true}},clipboard:{}});
 try {
  assert.equal(await cc.startMicServer(),true);
  for(const [method,path,body] of [['GET','/status'],['GET','/transcript/since/0'],['GET','/clipboard/since/0'],['GET','/calibration'],['GET','/logs'],['GET','/transcripts'],['GET','/transcript'],['GET','/wake/last-claim'],['POST','/mode/on'],['POST','/mode/clipboard',{}],['POST','/mode/meeting',{}],['POST','/mode/off',{}],['POST','/copy/start'],['POST','/copy/stop'],['POST','/wake/claim',{actions_version:2}],['POST','/actions/outcome',{id:'fixture',outcome:'delivered'}],['POST','/calibrate/start',{group:'wake'}],['POST','/calibrate/stop',{}],['POST','/audio/keep',{seconds:1}]] ) {
   assert.equal((await cc.micRequest(method,path,body)).ok,true,`${method} ${path}`);
  }
  const count=seen.length;
  assert.equal((await cc.micRequest('POST','/mode/off',null)).status,400);
  assert.equal((await cc.micRequest('GET','/status',{})).status,400);
  assert.equal(seen.length,count,'invalid bodies must not reach configured backend');
  assert.ok(seen.some(([m,p,b])=>m==='GET'&&p==='/status'&&b===''));
  assert.ok(seen.some(([m,p,b])=>m==='POST'&&p==='/copy/start'&&b===''));
 }finally {transport.close();bridge.closeAll();await new Promise(r=>ws.close(r));await new Promise(r=>wire.close(r));await new Promise(r=>backend.close(r));}
});
