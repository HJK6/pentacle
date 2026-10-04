import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { interpretPentacleEvent } from 'pentacle-chat-core';
import { ChatStoreController } from '../renderer/src/chat_store_controller';
import { renderStreamTranscript } from '../renderer/src/shared_transcript_view';

const stream='fixture:assistant';
function render(mime='application/pdf', text='', filename='sample.pdf', key='a'.repeat(64), size=22) {
  const event:any={stream_id:stream,host:'fixture',session_name:'assistant',provider:'composite',
    daemon_seq:2,kind:'ASSIST_TEXT',text,timestamp:'2026-01-01T00:00:00Z',message_id:'publication:fixture',
    attachments:[{key,mime,size,filename}],raw:{assistant_composite:true}};
  const controller=new ChatStoreController();
  controller.applyFrame({type:'snapshot',events:[],sessions:[{stream_id:stream,host:'fixture',session_name:'assistant',provider:'composite',online:true}],drafts:{}} as any);
  controller.applyFrame({type:'chat.event',event} as any);
  const detail=controller.selectSessionDetail(stream,{includeTools:true,includeSystem:true,visibleCount:120,includeDraft:false});
  const dom=new JSDOM('<!doctype html><body><div id="root"></div></body>');
  const root=dom.window.document.getElementById('root')!;
  renderStreamTranscript(stream,root as any,{store:{selectSessionDetail:()=>detail} as any});
  return {root,event,dom};
}

test('attachment-only composite reply survives interpreter and actual store/view',()=>{
  const {root,event}=render();
  assert.equal(interpretPentacleEvent(event).displayRule,'bubble:assistant');
  assert.equal(root.querySelectorAll('.slot-chat-file-download').length,1);
});

test('nonimage has inert named download placeholder, never caller URI preview',()=>{
  const {root}=render('application/pdf','file ready','sample.pdf');
  const a=root.querySelector<HTMLAnchorElement>('.slot-chat-file-download')!;
  assert.ok(a);assert.equal(a.getAttribute('download'),'sample.pdf');
  assert.equal(a.getAttribute('href'),null);
  assert.match(root.textContent||'',/application\/pdf/);
  assert.equal(root.querySelectorAll('iframe,object,embed').length,0);
});

import { createHash, webcrypto } from 'node:crypto';
import { createFileDownloads } from '../renderer/file_downloads';
import { fetchBlobReply } from '../main/blob_fetch_reply';
const bytes=Buffer.from('%PDF synthetic download');
const digest=createHash('sha256').update(bytes).digest('hex');
function downloadSetup(fetchBlob:any) {
  const {root,dom}=render('application/pdf','','sample.pdf',digest,bytes.length);
  const created:any[]=[];const revoked:string[]=[];
  const hydrator=createFileDownloads({fetchBlob,cryptoApi:webcrypto,
    urlApi:{createObjectURL:(blob:any)=>{created.push(blob);return `blob:fixture-${created.length}`;},revokeObjectURL:(url:string)=>revoked.push(url)}});
  return {root,dom,hydrator,created,revoked,anchor:root.querySelector('.slot-chat-file-download')! as HTMLAnchorElement};
}

test('authenticated bridge bytes are hash-checked before named download is enabled',async()=>{
  const calls:string[]=[];
  const s=downloadSetup(async(key:string)=>{calls.push(key);return {ok:true,content_b64:bytes.toString('base64')};});
  await s.hydrator.hydrate(s.root);
  assert.deepEqual(calls,[digest]);assert.equal(s.anchor.dataset.fileState,'ready');
  assert.equal(s.anchor.getAttribute('href'),'blob:fixture-1');
  assert.equal(s.anchor.download,'sample.pdf');
  assert.equal(s.created[0].type,'application/octet-stream');
  assert.deepEqual(Buffer.from(await s.created[0].arrayBuffer()),bytes);
  await s.hydrator.hydrate(s.root);assert.equal(calls.length,1);
  s.hydrator.dispose();assert.deepEqual(s.revoked,['blob:fixture-1']);
});

for(const failure of ['digest','size','oversize'])test(`bad ${failure} never yields a download`,async()=>{
  const content=failure==='digest'?Buffer.from('X'.repeat(bytes.length)).toString('base64'):
    failure==='size'?'AA==':'A'.repeat(Math.ceil(25*1024*1024/3)*4+4);
  const s=downloadSetup(async()=>({ok:true,content_b64:content}));
  await s.hydrator.hydrate(s.root);
  assert.equal(s.anchor.getAttribute('href'),null);assert.equal(s.created.length,0);
  assert.equal(s.anchor.dataset.fileState,'failed');s.hydrator.dispose();
});

test('daemon blob_unknown survives actual bridge reply adapter and shows unavailable',async()=>{
  const s=downloadSetup((key:string)=>fetchBlobReply({fetchBlob:async()=>{throw {error_code:'blob_unknown'};}},key));
  await s.hydrator.hydrate(s.root);
  assert.equal(s.anchor.dataset.fileState,'unavailable');
  assert.match(s.root.textContent||'',/File expired or unavailable/);
  assert.equal(s.anchor.getAttribute('href'),null);s.hydrator.dispose();
});

test('temporary fetch failure offers retry, does not claim expired',async()=>{
  let calls=0;
  const s=downloadSetup(async()=>++calls===1?{ok:false,error_code:'fetch_failed'}:{ok:true,content_b64:bytes.toString('base64')});
  await s.hydrator.hydrate(s.root);
  assert.equal(s.anchor.dataset.fileState,'failed');assert.doesNotMatch(s.root.textContent||'',/expired/);
  s.anchor.dispatchEvent(new s.dom.window.MouseEvent('click',{bubbles:true,cancelable:true}));
  for(let i=0;i<30&&s.anchor.dataset.fileState!=='ready';i++)await new Promise(r=>setTimeout(r,5));
  assert.equal(s.anchor.dataset.fileState,'ready');assert.equal(calls,2);s.hydrator.dispose();
});

test('removed nodes release URLs and late fetch cannot resurrect a stale link',async()=>{
  const s=downloadSetup(async()=>({ok:true,content_b64:bytes.toString('base64')}));
  await s.hydrator.hydrate(s.root);s.root.remove();
  await new Promise(r=>setImmediate(r));assert.deepEqual(s.revoked,['blob:fixture-1']);
  let resolve:any;
  const late=downloadSetup(()=>new Promise(r=>{resolve=r;}));
  const pending=late.hydrator.hydrate(late.root);late.root.remove();
  resolve({ok:true,content_b64:bytes.toString('base64')});await pending;
  assert.equal(late.created.length,0);s.hydrator.dispose();late.hydrator.dispose();
});

test('filename is escaped and unsupported active content never gets a link',()=>{
  const {root}=render('application/pdf','ready','evil" onload="x<svg>.pdf');
  assert.equal(root.querySelectorAll('svg,script,iframe,embed,object').length,0);
  assert.equal(root.querySelectorAll('[onload]').length,0);
  assert.equal(root.querySelector('a')?.getAttribute('download'),'evil" onload="x<svg>.pdf');
  const unsupported=render('text/html','ready','active.html');
  assert.equal(unsupported.root.querySelectorAll('a,iframe,object,embed').length,0);
});

test('image viewer markup is retained',()=>{
  const {root}=render('image/png','image','fixture.png');
  assert.equal(root.querySelectorAll('.slot-chat-media-button img').length,1);
  assert.equal(root.querySelectorAll('.slot-chat-file-download').length,0);
});

for (const [mime,name] of [
  ['application/zip','fixture.zip'],['model/3mf','fixture.3mf'],
  ['model/stl','fixture.stl'],['model/step','fixture.step'],['application/x-openscad','fixture.scad'],
]) test(`supported ${mime} is a named download without a preview`,()=>{
  const {root}=render(mime,'',name);
  assert.equal(root.querySelector<HTMLAnchorElement>('a.slot-chat-file-download')?.download,name);
  assert.equal(root.querySelectorAll('img,iframe,object,embed').length,0);
});

test('blob-forbidden stays a refusal and never enables a link',async()=>{
  const s=downloadSetup((key:string)=>fetchBlobReply({fetchBlob:async()=>{throw {error_code:'blob_forbidden',message:'DO_NOT_PRINT'};}},key));
  await s.hydrator.hydrate(s.root);
  assert.equal(s.anchor.dataset.fileState,'failed');assert.equal(s.anchor.getAttribute('href'),null);
  assert.equal(s.created.length,0);assert.doesNotMatch(s.root.textContent||'',/DO_NOT_PRINT/);
  s.hydrator.dispose();
});
