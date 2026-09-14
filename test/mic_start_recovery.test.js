const test = require('node:test');
const assert = require('node:assert/strict');
const {buildCc} = require('../renderer/web_cc');
const {createCcHandlers,createCollector}=require('../main/cc_handlers');
const {createMicStarter}=require('../server/mic_starter');
const config={features:{mic:true},mic:{autoSpawn:false,startCommand:{file:'/trusted/helper',args:['literal $arg']}}};
test('web Start calls the existing RPC without accepting caller commands',async()=>{
 const calls=[];const transport={call:async(...a)=>{calls.push(a);return {ok:true};},fire(){},on(){}};
 const cc=buildCc(transport,{config,clipboard:{}});
 assert.deepEqual(await cc.startMicServer('/untrusted'),{ok:true});
 assert.deepEqual(calls,[['mic:start-server']]);
});
test('server-owned launcher is omitted from browser configuration',()=>{
 const handlers=createCcHandlers({CONFIG:config,chatStreamClient:{}});
 assert.equal(handlers.publicConfig().mic.startCommand,undefined);
 assert.equal(config.mic.startCommand.file,'/trusted/helper');
});
test('concurrent starts execute once, use argv without shell, and wait for helper readiness',async()=>{
 let finish,calls=0; const starter=createMicStarter(config,{execFile:(file,args,opts,cb)=>{calls++;assert.equal(file,'/trusted/helper');assert.deepEqual(args,['literal $arg']);assert.equal(opts.shell,false);assert.equal(opts.windowsHide,true);assert.equal(opts.timeout,75000);finish=cb;}});
 const a=starter(),b=starter();assert.equal(calls,1);
 finish(null,'{"ok":true,"ready":true}','');
 assert.deepEqual(await a,{ok:true});assert.deepEqual(await b,{ok:true});
 const c=starter();assert.equal(calls,2);finish(null,'{"ok":true,"ready":true}','');assert.equal((await c).ok,true);
});
for(const [label,c] of [['disabled',{features:{mic:false},mic:config.mic}],['remote',{...config,mic:{...config.mic,useStreamHost:true}}],['absent',{features:{mic:true}}],['relative',{...config,mic:{startCommand:{file:'relative',args:[]}}}]])test(`no start for ${label}`,async()=>{
 let calls=0; const result=await createMicStarter(c,{execFile:()=>calls++})();assert.equal(result.ok,false);assert.equal(calls,0);assert.ok(result.error);
});
for(const [label,error,stdout] of [['timeout',Object.assign(Error('raw secret'),{killed:true}), ''],['exit',Error('raw secret'), ''],['no proof',null,''],['false proof',null,'{"ok":false,"ready":false}']])test(`helper ${label} is not success`,async()=>{
 const r=await createMicStarter(config,{execFile:(_f,_a,_o,cb)=>cb(error,stdout,'secret stderr')})();assert.equal(r.ok,false);assert.doesNotMatch(r.error,/secret/);
});
test('shared handler uses injected web starter and ignores RPC arguments',async()=>{
 let calls=0;const collector=createCollector();
 const stop=createCcHandlers({CONFIG:config,chatStreamClient:new Proxy({},{get:()=>async()=>({})}),harness:true,startMicServer:()=>{calls++;return {ok:true};}}).register(collector);
 assert.deepEqual(await collector.table['mic:start-server'].handler({},'/untrusted'),{ok:true});assert.equal(calls,1);stop?.();
});
const {micStartSameOrigin}=require('../server/mic_starter');
const {createWsBridge}=require('../server/ws_bridge');
test('start admission requires exact upgrade origin, missing/cross/null origin refused',()=>{
 for(const origin of [undefined,'null','https://elsewhere.test','http://localhost:7796','http://u@localhost:7795'])
  assert.equal(micStartSameOrigin({headers:{origin,host:'localhost:7795'}}),false);
 assert.equal(micStartSameOrigin({headers:{origin:'http://localhost:7795',host:'localhost:7795'}}),true);
});
test('bridge rejects start before invoking handler unless upgrade admitted it',async()=>{
 let invoked=0;const frames=[];const socket={send:s=>frames.push(JSON.parse(s))};
 const bridge=createWsBridge({table:{'mic:start-server':{mode:'invoke',handler:()=>{invoked++;return {ok:true};}}}});
 bridge.addSocket(socket);await bridge.handleMessage(socket,JSON.stringify({id:1,method:'mic:start-server'}));assert.equal(invoked,0);assert.equal(frames[0].error.code,'mic_origin_refused');
 bridge.removeSocket(socket);bridge.addSocket(socket,{micStartAllowed:true});await bridge.handleMessage(socket,JSON.stringify({id:2,method:'mic:start-server'}));assert.equal(invoked,1);assert.equal(frames[1].result.ok,true);
 bridge.closeAll();
});
