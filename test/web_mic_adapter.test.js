const test=require('node:test');
const assert=require('node:assert/strict');
const {buildCc}=require('../renderer/web_cc');
const {createWsBridge}=require('../server/ws_bridge');
test('web microphone uses host RPC rather than viewer loopback',async()=>{
 const calls=[];const cc=buildCc({call:async(...a)=>{calls.push(a);return {mode:'on'};},fire(){},on(){}},{config:{features:{mic:true}},clipboard:{}});
 assert.deepEqual(await cc.micRequest('GET','/status'),{mode:'on'});
 assert.deepEqual(calls,[['mic:request','GET','/status',undefined]]);
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
