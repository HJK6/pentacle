'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const vm=require('node:vm');
const {buildSync}=require('esbuild');

test('picture contrast counts 2 px exterior, not a similarly coloured fill',()=>{
  const root=path.join(__dirname,'..');
  const built=buildSync({entryPoints:[path.join(root,'renderer/next/model_viewer.js')],bundle:true,
    platform:'browser',format:'iife',globalName:'PentacleModelViewer',write:false,logLevel:'silent'});
  const realm=vm.createContext({AbortController});
  vm.runInContext(built.outputFiles[0].text,realm);
  const {assessPicture}=realm.PentacleModelViewer;
  const wells=[[12,14,13],[238,240,237],[9,16,13]], size=13;
  const input=new Uint8ClampedArray(size*size*4);
  const withStroke=input.slice();
  function set(target,x,y,alpha=255) { target.set([130,130,130,alpha],(y*size+x)*4); }
  for (let x=0;x<size;x++) for (let y=0;y<size;y++) {
    const dx=Math.max(0,4-x,x-8),dy=Math.max(0,4-y,y-8);
    if(dx===0 && dy===0) set(input,x,y);
    if(dx*dx+dy*dy<=4) set(withStroke,x,y);
  }
  const proof=assessPicture(input,withStroke,size,size,wells);
  assert.equal(proof.expected,44);
  assert.equal(proof.matched,proof.expected);
  assert.deepEqual(Array.from(proof.contrast),[1,1,1]);
  assert.throws(()=>assessPicture(input,input,size,size,wells),/silhouette/);
  const thin=withStroke.slice();
  thin.fill(0,(4*size+2)*4,(4*size+3)*4);
  assert.throws(()=>assessPicture(input,thin,size,size,wells),/silhouette/);
  const translucent=withStroke.slice();
  for(let x=0;x<size;x++) for(let y=0;y<size;y++) {
    if(x<4 || x>8 || y<4 || y>8) translucent[(y*size+x)*4+3]/=10;
  }
  assert.throws(()=>assessPicture(input,translucent,size,size,wells),/contrast/);
  assert.throws(()=>assessPicture(new Uint8Array(input.length),withStroke,size,size,wells),/blank/);
});
