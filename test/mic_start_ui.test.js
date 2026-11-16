const test=require('node:test'), assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {JSDOM}=require('jsdom');
const source=fs.readFileSync(require.resolve('../renderer/app.js'),'utf8');
const update=source.slice(source.indexOf('function updateMicUI(data) {'),source.indexOf('async function fetchMicStatus()'));
const handler=source.slice(source.indexOf("document.getElementById('mic-btn-toggle').addEventListener"),source.indexOf("document.getElementById('mic-btn-copy').addEventListener"));
function setup(start){
 const dom=new JSDOM(['mic-status-dot','mic-info','mic-btn-toggle','mic-btn-copy','mic-btn-meeting','mic-transcript-preview'].map(id=>`<button id="${id}"></button>`).join(''));
 let click;dom.window.document.getElementById('mic-btn-toggle').addEventListener=(_e,f)=>{click=f;};
 const ctx={document:dom.window.document,window:{cc:{startMicServer:start}},CONFIG:{mic:{alwaysOnEnabled:true}},micState:{mode:'offline'},voiceState:{},alwaysOnVisible:()=>true,shouldRenderAlwaysOnUi:()=>true,stopRemoteClipboardPoller(){},setTimeout(){},fetchMicStatus:async()=>ctx.updateMicUI(null)};
 vm.createContext(ctx);vm.runInContext(update+handler,ctx);return {ctx,click,doc:dom.window.document};
}
test('pending Start stays disabled through offline status polling and ignores repeat click',async()=>{
 let finish,calls=0;const {ctx,click,doc}=setup(()=>{calls++;return new Promise(r=>finish=r);});
 const pending=click();ctx.updateMicUI(null);await click();assert.equal(calls,1);assert.equal(doc.getElementById('mic-btn-toggle').disabled,true);assert.match(doc.getElementById('mic-info').textContent,/Starting/);
 finish({ok:false,error:'Check microphone connection'});await pending;assert.equal(doc.getElementById('mic-btn-toggle').disabled,false);assert.match(doc.getElementById('mic-info').textContent,/Check microphone connection/);
 ctx.updateMicUI(null);assert.match(doc.getElementById('mic-info').textContent,/Check microphone connection/);
});
test('RPC rejection restores usable Start and displays error',async()=>{
 const {click,doc}=setup(async()=>{throw Error('disconnect');});await click();assert.equal(doc.getElementById('mic-btn-toggle').disabled,false);assert.match(doc.getElementById('mic-info').textContent,/Lost connection/);
});
