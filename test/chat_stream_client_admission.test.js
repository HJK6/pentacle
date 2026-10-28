'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {setTimeout:delay}=require('node:timers/promises');
const root=process.env.PENTACLE_TEST_REPO||process.cwd();
const shared=require(root+'/main/chat_stream_client');
function fixture(t){
 const client=new (Object.getPrototypeOf(shared).constructor)(),sent=[];
 client.connected=true;client._ws={readyState:1,send(raw){sent.push(JSON.parse(raw));},terminate(){}};
 t.after(()=>client.destroy());
 const command=(type,id,options={})=>{const p=client.sendCommand({type},type,{requestId:id,...options});p.catch(()=>{});return p;};
 const reply=(id,extra={})=>client._handleCommandResponse({type:'fixture.ok',request_id:id,...extra});
 return {client,sent,command,reply};
}
test('background startup burst preserves an ordinary slot for upload',async t=>{
 const {sent,command}=fixture(t);
 for(let i=0;i<12;i++)command('asset.list','bg'+i);
 command('upload_blob_init','upload');
 await delay(0);
 assert.ok(sent.filter(x=>x.type==='asset.list').length<=4,'background dispatch is bounded');
 assert.ok(sent.length<=6,'ordinary dispatch remains below daemon connection cap');
 assert.ok(sent.some(x=>x.request_id==='upload'),'user upload dispatches despite queued reads');
});
test('interactive work overtakes queued reads; send and parked/control stay independent',async t=>{
 const {sent,command,reply}=fixture(t);
 for(let i=0;i<12;i++)command('asset.list','bg'+i);
 command('rename','first');command('rename','second');command('upload_blob_init','upload');
 command('send','durable');command('transcribe_blob','voice');command('ping','control');
 await delay(0);
 for(const id of ['durable','voice','control'])assert.ok(sent.some(x=>x.request_id===id));
 assert.ok(!sent.some(x=>x.request_id==='upload'),'ordinary window is full');
 reply('first');await delay(0);
 assert.equal(sent.filter(x=>!['send','transcribe_blob','ping'].includes(x.type)).at(-1).request_id,'upload');
});
test('only an explicit retryable read refusal retries once with the same identity',async t=>{
 const {client,sent,command,reply}=fixture(t);const p=command('asset.list','retry');
 client._handleCommandResponse({type:'asset.list.error',request_id:'retry',error_code:'server_overloaded',retryable:true});
 assert.equal(sent.length,1);await delay(280);assert.equal(sent.length,2);assert.equal(sent[1].request_id,'retry');
 reply('retry',{assets:[]});assert.deepEqual((await p).assets,[]);
 const twice=command('asset.list','twice');
 client._handleCommandResponse({type:'asset.list.error',request_id:'twice',error_code:'server_overloaded',retryable:true});
 await delay(280);client._handleCommandResponse({type:'asset.list.error',request_id:'twice',error_code:'server_overloaded',retryable:true});
 await assert.rejects(twice,e=>e.error_code==='server_overloaded');await delay(280);
 assert.equal(sent.filter(x=>x.request_id==='twice').length,2);
});
test('nonretryable busy, mutation refusal and send outcome never replay',async t=>{
 const {client,sent,command}=fixture(t);
 for(const [type,id,extra] of [['asset.list','no',{retryable:false}],['rename','mutation',{retryable:true}],['send','send',{retryable:false}]]){
  const p=command(type,id);client._handleCommandResponse({type:type+'.error',request_id:id,error_code:'server_overloaded',...extra});
  await assert.rejects(p,e=>e.error_code==='server_overloaded');
 }
 await delay(280);assert.equal(sent.length,3);
});
test('active timeout retains credit until late terminal; queued timeout never dispatches',async t=>{
 const {client,sent,command,reply}=fixture(t);
 const first=command('asset.list','expired',{timeoutMs:20});
 for(let i=1;i<4;i++)command('asset.list','held'+i);
 const queued=command('asset.list','queued',{timeoutMs:20});
 await delay(30); // The production deadlines are unref'd; keep the fixture alive.
 await assert.rejects(first,e=>e.error==='timed_out');await assert.rejects(queued,e=>e.error==='timed_out');
 command('asset.list','next');await delay(0);assert.ok(!sent.some(x=>x.request_id==='next'));
 client._handleCommandResponse({type:'request_stream_events.chunk',request_id:'expired',events:[{text:'late'}]});
 assert.ok(!client._streamEventChunks.has('expired'),'expired chunks are discarded');
 reply('expired');await delay(0);assert.ok(sent.some(x=>x.request_id==='next'));
 assert.ok(!sent.some(x=>x.request_id==='queued'));
});
test('queue/backoff is abandoned on disconnect, never replayed onto a new socket',async t=>{
 const {client,sent,command}=fixture(t);const all=[];
 for(let i=0;i<8;i++)all.push(command('asset.list','q'+i));
 client._handleCommandResponse({type:'asset.list.error',request_id:'q0',error_code:'server_overloaded',retryable:true});
 client.connected=false;client._rejectPending('Stream disconnected');
 await Promise.all(all.map(p=>assert.rejects(p)));
 const before=sent.length;client.connected=true;client._ws={readyState:1,send(){throw Error('old job leaked');},terminate(){}};
 await delay(300);assert.equal(sent.length,before);assert.equal(client._pending.size,0);
});

test('never-answered calls expire locally after60s grace without dropping the shared socket',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});
 const {client,sent,command,reply}=fixture(t);let terminated=0;client._ws.terminate=()=>terminated++;
 const calls=[];
 for(let i=0;i<6;i++)calls.push(command(i<4?'asset.list':'rename','lost'+i,{timeoutMs:20}));
 t.mock.timers.tick(20);await Promise.all(calls.map(p=>assert.rejects(p,e=>e.error==='timed_out')));
 assert.equal(client._pending.size,6);t.mock.timers.tick(59999);
 const later=command('upload_blob_init','later',{timeoutMs:100});assert.ok(!sent.some(x=>x.request_id==='later'));
 t.mock.timers.tick(1);await Promise.resolve();assert.ok(sent.some(x=>x.request_id==='later'));
 reply('lost0');assert.ok(client._pending.has('later'),'late old reply cannot settle newer work');
 reply('later');await later;assert.equal(client._pending.size,0);assert.equal(terminated,0);
});

test('background waiting overflow reserves capacity for an interactive upload',async t=>{
 const {sent,command,reply}=fixture(t),reads=[];
 for(let i=0;i<100;i++)reads.push(command('asset.list','flood'+i).then(()=>null,e=>e));
 assert.equal(sent.length,4);command('rename','r1');command('rename','r2');command('upload_blob_init','priority');
 assert.ok(!sent.some(x=>x.request_id==='priority'));reply('r1');await delay(0);
 assert.equal(sent.at(-1).request_id,'priority');
 // Only active4 + waiting48 background calls may remain pending.
 const settled=reads.slice(52);assert.ok((await Promise.all(settled)).every(e=>/busy/i.test(e.error)));
});

test('wire type routes independent pools, while an ordinary verb with send prefix remains bounded',async t=>{
 const {client,sent,command}=fixture(t);
 for(let i=0;i<6;i++)command('rename','held'+i);
 const bypass=['send','notification.resolve','notification.resolve_by_dedup','prompt.answer','voice_answer.answer',
  'await_report','await_spawn','transcribe_blob','ping','spawn_cancel','daemon.stats'];
 for(const type of bypass)command(type,type);
 const ordinary=client.sendCommand({type:'spawn'},'send',{requestId:'ordinary-spawn'});ordinary.catch(()=>{});
 assert.equal(sent.filter(x=>bypass.includes(x.type)).length,bypass.length);
 assert.ok(!sent.some(x=>x.request_id==='ordinary-spawn'));
});

test('nonfinal upload refusal cancels a queued final chunk; no retry or truncated success',async t=>{
 const {client,sent,command,reply}=fixture(t);
 const upload=client.uploadBlob({data:Buffer.alloc(1024*1024+1,0x5a),requestId:'chunks'});upload.catch(()=>{});
 client._handleCommandResponse({type:'upload_blob.init.ok',request_id:'chunks'});
 for(let i=0;i<6;i++)command('rename','held'+i);
 await delay(0);
 assert.equal(sent.filter(x=>x.type==='upload_blob_chunk'&&!x.final).length,1);
 assert.ok(!sent.some(x=>x.type==='upload_blob_chunk'&&x.final),'final is queued');
 client._handleCommandResponse({type:'upload_blob_chunk.error',request_id:'chunks',error_code:'server_overloaded',retryable:true});
 await assert.rejects(upload,e=>e.error_code==='server_overloaded');
 for(let i=0;i<6;i++)reply('held'+i);await delay(280);
 assert.ok(!sent.some(x=>x.type==='upload_blob_chunk'&&x.final));assert.equal(client._uploadRefusals.size,0);
});

test('the full original image digest is required before an upload reports success',async t=>{
 const {client}=fixture(t);const p=client.uploadBlob({data:Buffer.from('original'),requestId:'digest'});p.catch(()=>{});
 client._handleCommandResponse({type:'upload_blob.init.ok',request_id:'digest'});await delay(0);
 client._handleCommandResponse({type:'upload_blob.ok',request_id:'digest',blob_sha:'0'.repeat(64)});
 await assert.rejects(p,/could not be verified/);assert.equal(client._uploadRefusals.size,0);
});

test('an explicit retry cannot extend the caller deadline',async t=>{
 const {client,sent,command}=fixture(t);const p=command('asset.list','deadline',{timeoutMs:20});
 client._handleCommandResponse({type:'asset.list.error',request_id:'deadline',error_code:'server_overloaded',retryable:true});
 await delay(280);await assert.rejects(p,e=>e.error==='timed_out');assert.equal(sent.length,1);
});

test('encoded request and background queue byte bounds reserve room for user work',async t=>{
 const {client,sent,reply}=fixture(t);const reads=[];
 const call=(id,type,padding)=>{const p=client.sendCommand({type,padding},type,{requestId:id});p.catch(()=>{});return p;};
 const oversized=call('oversized','rename','x'.repeat(6*1024*1024));
 await assert.rejects(oversized,e=>/exceeds/.test(e.error));assert.equal(sent.length,0);
 for(let i=0;i<20;i++)reads.push(call('bytes'+i,'asset.list','x'.repeat(1024*1024)).then(()=>null,e=>e));
 assert.equal(sent.length,4);const upload=call('large-user','upload_blob_init','y'.repeat(5*1024*1024));
 for(let i=0;i<4;i++)reply('bytes'+i);await delay(0);
 assert.equal(sent[4].request_id,'large-user','interactive bytes take precedence when the window frees');
 reply('large-user');await upload;
 assert.ok((await Promise.all(reads.slice(11))).every(e=>/busy/.test(e.error)));
});

test('the refusal latch exists before the first nonfinal upload frame',async t=>{
 const {client,sent}=fixture(t),send=client._ws.send;
 client._ws.send=function(raw){send(raw);const frame=JSON.parse(raw);if(frame.type==='upload_blob_chunk'&&!frame.final)
  client._handleCommandResponse({type:'upload_blob_chunk.error',request_id:frame.request_id,error_code:'server_overloaded',retryable:true});};
 const p=client.uploadBlob({data:Buffer.alloc(1024*1024+1),requestId:'early-error'});p.catch(()=>{});
 client._handleCommandResponse({type:'upload_blob.init.ok',request_id:'early-error'});
 await assert.rejects(p,e=>e.error_code==='server_overloaded');
 assert.ok(!sent.some(x=>x.type==='upload_blob_chunk'&&x.final));assert.equal(client._uploadRefusals.size,0);
});

const refusal = id => ({ type: 'send.error', request_id: id, error_code: 'server_overloaded', retryable: true });
for (const shape of [
 {type:'send',stream_id:'peer:ordinary',text:'original',optimistic_id:'logical',urgent:true,attachments:[{blob_sha:'a'.repeat(64),name:'note.txt'}],reply_to_message_id:'reply',meta:{voice:{source:'fixture'}}},
 {type:'send',stream_id:'peer:assistant',text:'composite',msg_id:'logical-composite'},
]) test('pre-admission send retry preserves bytes and bypass priority: '+shape.stream_id,async t=>{
 t.mock.timers.enable({apis:['setTimeout']});
 const {client,sent,command}=fixture(t);
 for(let i=0;i<12;i++)command('asset.list','saturated'+i);
 const raw=[];const original=client._ws.send;client._ws.send=data=>{raw.push(data);original(data);};
 const p=client.sendCommand(shape,'send',{requestId:'refused'});p.catch(()=>{});
 client._handleCommandResponse(refusal('refused'));
 t.mock.timers.tick(249);assert.equal(raw.length,1);
 t.mock.timers.tick(1);assert.equal(raw.length,2);assert.equal(raw[1],raw[0]);
 assert.equal(client._ordinaryRequests.active.size <= 6,true);
 client._handleCommandResponse({type:'send.result',request_id:'refused',delivery:'landed',provider_queued:true});
 assert.equal((await p).provider_queued,true);
 t.mock.timers.tick(1000);assert.equal(raw.length,2);
});
for(const extra of [{retryable:false},{retryable:undefined},{error_code:'busy'},
 {error_code:'provider_429'},{action_committed:true},{confirmation_pending:true},{provider_queued:true},
 {do_not_resubmit:true},{accepted:true},{delivery:'accepted'},{delivery:'landed'},{state:'accepted'}])
 test('send does not retry ineligible refusal '+JSON.stringify(extra),async t=>{
  t.mock.timers.enable({apis:['setTimeout']});const {client,sent,command}=fixture(t);
  const p=command('send','ineligible');client._handleCommandResponse({...refusal('ineligible'),...extra});
  await assert.rejects(p);t.mock.timers.tick(1000);assert.equal(sent.length,1);
 });
test('duplicate refusals during backoff cannot trigger a third write or leak a timer',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});const {client,sent,command}=fixture(t);const p=command('send','twice');
 client._handleCommandResponse(refusal('twice'));t.mock.timers.tick(250);
 client._handleCommandResponse(refusal('twice'));await assert.rejects(p);client._handleCommandResponse(refusal('twice'));
 t.mock.timers.tick(1000);assert.equal(sent.length,2);assert.equal(client._pending.size,0);
});
for(const cancel of ['result','disconnect','replacement','destroy','generation'])
 test('send backoff is owned by pending/socket/generation: '+cancel,async t=>{
  t.mock.timers.enable({apis:['setTimeout']});const {client,sent,command}=fixture(t);const p=command('send','cancel');p.catch(()=>{});
  client._handleCommandResponse(refusal('cancel'));
  if(cancel==='result')client._handleCommandResponse({type:'send.result',request_id:'cancel',delivery:'landed'});
  if(cancel==='disconnect'){client.connected=false;client._rejectPending('Stream disconnected');}
  if(cancel==='destroy')client.destroy();
  if(cancel==='replacement')client._ws={readyState:1,send(){throw Error('stale write');},terminate(){}};
  if(cancel==='generation')client._socketGeneration++;
  t.mock.timers.tick(1000);assert.equal(sent.length,1);
  if(cancel==='result')await p;else await assert.rejects(p);
 });
for(const type of ['send.result','send.indeterminate'])test(type+' never triggers automatic send retry',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});const {client,sent,command}=fixture(t);const p=command('send','receipt');
 client._handleCommandResponse({...refusal('receipt'),type,delivery:'not_landed'});await p;
 t.mock.timers.tick(1000);assert.equal(sent.length,1);
});

test('wrong correlation, non-send error and timeout cannot grant a send retry',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});const {client,sent,command}=fixture(t);
 const p=command('send','timeout',{timeoutMs:20});
 client._handleCommandResponse(refusal('someone-else'));t.mock.timers.tick(20);await assert.rejects(p,e=>e.error==='timed_out');
 const q=command('send','wrong-type');client._handleCommandResponse({...refusal('wrong-type'),type:'rename.error'});await assert.rejects(q);
 t.mock.timers.tick(1000);assert.equal(sent.length,2);
});
