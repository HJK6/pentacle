'use strict';
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const os=require('node:os');
const {execFileSync}=require('node:child_process');
const {drop,box,glb}=require('../../fixtures/model_library.cjs');
const {stageFixture}=require('./next_design_fixture.cjs');
const {operator,general,tailnetArgs,proxy,writeFixture,request,withHost}=require('./model_library_fixture.cjs');

async function withFixture(chromeBin, fn) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'pentacle-model-gate-'));
  const previousChrome = process.env.PENTACLE_CHROME;
  try {
    // The publish CLI and in-process picture checks use the same Design recorder executable.
    if (chromeBin) process.env.PENTACLE_CHROME = chromeBin;
    return await fn(root,writeFixture(root));
  } finally {
    if (previousChrome === undefined) delete process.env.PENTACLE_CHROME;
    else process.env.PENTACLE_CHROME = previousChrome;
    fs.rmSync(root,{recursive:true,force:true});
  }
}
function cli(fixture, cmd, ...args) {
  const file=path.join(__dirname,'../../../scripts/model-library.cjs');
  return JSON.parse(execFileSync(process.execPath,[file,cmd,'--root',fixture.library,...args],{
    encoding:'utf8',timeout:120000,windowsHide:true,
  }));
}

async function modelLibraryPublish({report,chromeBin}) {
  await withFixture(chromeBin,async(root,fixture)=>{
    const stage=await stageFixture(root,{additional:false});
    fixture.design=stage.library;
    const args=['--drop',fixture.source];
    assert.equal(cli(fixture,'check',...args).measurements.length,2);
    assert.ok(!fs.existsSync(fixture.library));
    const published=cli(fixture,'publish',...args);
    assert.equal(cli(fixture,'verify').revisions,1);
    const addresses=new Map();
    const privateText=/Test spacer|Bench fixtures|Opposite|bench|spacer|model-media/;
    const missing='/model-media/bench/spacer/'+published.revision+'/picture/absent';
    function denial(res,status) {
      assert.equal(res.status,status);
      assert.equal(res.headers['cache-control'],'private, no-store');
      for (const k of ['etag','last-modified','accept-ranges','content-range','location']) assert.equal(res.headers[k],undefined);
      assert.doesNotMatch(res.body.toString(),privateText);
      assert.ok(!res.body.toString().includes(published.revision));
    }
    const methods=(etag)=>[['GET',{}],['HEAD',{}],['GET',{'if-none-match':etag}],['GET',{range:'bytes=0-7'}]];
    await withHost(fixture,tailnetArgs(operator,true),async ({port})=>{
      const admitted = proxy();
      const requestAs=(url,method='GET',headers={})=>request(port,url,{method,headers:{...admitted,...headers}});
      const design=await requestAs('/api/designs');
      assert.equal(design.status,200);
      assert.match(design.body.toString(),/Delivery/);
      const response=await requestAs('/api/models');
      assert.equal(response.status,200);
      const catalog=JSON.parse(response.body);
      assert.equal(catalog.schema,'model-catalog.v1');
      const model=catalog.projects[0].models[0];
      assert.equal(model.revision,published.revision);
      const prefix='/model-media/bench/spacer/'+published.revision;
      const first=await requestAs(model.views[0].picture_url);
      const want=new Map([['/api/models',response.body],[prefix+'/picture',first.body]]);
      const manifest=JSON.parse(fs.readFileSync(path.join(fixture.library,'models','bench','spacer','revisions',model.revision,'manifest.json')));
      for (const [i,v] of model.views.entries()) {
        const mesh=path.join(fixture.source,'Sample_A.glb');
        const picture=path.join(fixture.library,'objects',manifest.views[i].picture+'.png');
        want.set(v.mesh_url,fs.readFileSync(mesh));
        want.set(v.picture_url,fs.readFileSync(picture));
      }
      for (const [target,bytes] of want) {
        const initial=await requestAs(target);
        assert.deepEqual(initial.body,bytes);
        assert.equal(initial.headers['cache-control'],'private, no-store');
        assert.ok(initial.headers.etag);
        addresses.set(target,initial.headers.etag);
        for (const [i,[method,headers]] of methods(initial.headers.etag).entries()) {
          const ret=await requestAs(target,method,headers);
          assert.equal(ret.status,[200,200,304,target==='/api/models'?200:206][i]);
          if (i===1||i===2) assert.equal(ret.body.length,0);
          if (ret.status===206) assert.deepEqual(ret.body,bytes.subarray(0,8));
          for (const address of [target,missing]) {
            denial(await request(port,address,{method,headers:{...headers,...proxy(general)}}),401);
            denial(await request(port,address,{method,headers}),401);
          }
        }
      }
    });
    await withHost(fixture,tailnetArgs(general),async ({port})=>{
      const design=await request(port,'/api/designs',{headers:proxy(general)});
      assert.equal(design.status,200);
      assert.match(design.body.toString(),/Delivery/);
      for(const [address,etag] of addresses) for(const [method,headers] of methods(etag)) {
        for(const target of [address,missing]) denial(await request(port,target,{method,headers:{...headers,...proxy(general)}}),404);
      }
    });
    await assert.rejects(withHost(fixture,[...tailnetArgs(operator,true),'--allow-login',general],()=>assert.fail('Host started')),
      /exactly one matching/);
    report.ok('CLI checked, published and verified both views; sequential operator and general hosts enforce all method variants; Design survives',true);
  });
}

function snapshot(root) {
  return fs.readdirSync(root,{recursive:true,withFileTypes:true}).filter(d=>d.isFile()).map(d=>{
    const dir=d.parentPath ?? d.path;
    const filename=path.join(dir,d.name);
    return [path.relative(root,filename),fs.readFileSync(filename).toString('hex')];
  }).sort(([a],[b])=>a.localeCompare(b));
}

async function modelLibraryPicture({report,chromeBin}) {
  await withFixture(chromeBin,async(root,fixture)=>{
    const {openPicturePage,capturePictures,themeWells}=require('../../../lib/model_picture.cjs');
    const {createLibrary}=require('../../../lib/model_library.cjs');
    const pub=cli(fixture,'publish','--drop',fixture.source);
    const library=createLibrary(fixture.library);
    const model=library.catalog().projects[0].models[0];
    assert.equal(pub.revision,model.revision);
    const {browser,page}=await openPicturePage();
    try {
      for (const view of model.views) {
        const result=await page.evaluate(async({png,glb,view,up,wells})=>{
          const viewer=window.PentacleModelViewer;
          const bytes=Uint8Array.from(atob(glb),x=>x.charCodeAt(0));
          const image=new Image(); image.src='data:image/png;base64,'+png; await image.decode();
          const canvas=document.createElement('canvas');
          canvas.width=448;canvas.height=280;
          const context=canvas.getContext('2d');
          context.drawImage(image,0,0);
          const stored=context.getImageData(0,0,448,280).data;
          const bare=document.createElement('canvas');bare.width=448;bare.height=280;
          const pic=viewer.openModelViewer(bare,{bytes,up,view,picture:true});
          let evidence, missingStroke=false;
          try {
            context.clearRect(0,0,448,280);context.drawImage(bare,0,0);
            const unadorned=context.getImageData(0,0,448,280).data;
            evidence=viewer.assessPicture(unadorned,stored,448,280,wells);
            try { viewer.assessPicture(unadorned,unadorned,448,280,wells); }
            catch { missingStroke=true; }
          } finally { pic.dispose(); }
          const interactive=document.createElement('canvas');interactive.width=448;interactive.height=280;
          document.body.append(interactive);
          const model=viewer.openModelViewer(interactive,{bytes,up,view});
          let boxes;
          try {
            model.zoom(4); model.slide(150,70); model.orbit(18,-6);model.explode(1);model.select(view.parts[0].id);
            model.fit();boxes=model.projectedPartBoxes();
          } finally { model.dispose();interactive.remove(); }
          return {width:image.naturalWidth,height:image.naturalHeight,corner:stored[3],centre:stored[(140*448+224)*4+3],
            evidence,missingStroke,boxes};
        },{png:library.media('bench','spacer',pub.revision,'picture',view.id).bytes.toString('base64'),
          glb:library.media('bench','spacer',pub.revision,'mesh',view.id).bytes.toString('base64'),up:model.sheet.up,view,wells:themeWells()});
        assert.deepEqual([result.width,result.height,result.corner],[448,280,0]);
        assert.ok(result.centre>0 && result.evidence.expected>0 && result.missingStroke);
        assert.ok(result.evidence.contrast.every(x=>x>=0.9));
        assert.equal(result.boxes.length,view.parts.length);
        assert.ok(result.boxes.every(b=>b.left>=0 && b.top>=0 && b.right<=448 && b.bottom<=280 && b.right>b.left && b.bottom>b.top));
        report.ok('Stored PNG '+view.id+' has validated two-pixel exterior, contrast on three wells, and fitted part boxes',true,result);
      }
    } finally { await browser.close(); }
    const preimage=snapshot(fixture.library);
    await assert.rejects(library.publish(fixture.source,{capture:(items,sheet)=>capturePictures(items,sheet,{
      launch:(opts,start)=>start({...opts,args:[...opts.args,'--disable-webgl']}),
    })}),/needs WebGL 2/);
    assert.deepEqual(snapshot(fixture.library),preimage);
    const blank=path.join(root,'blank');
    drop(blank,{mesh:glb([{points:box().map(([x,,z])=>[x,0,z])}]),edit:s=>{
      s.up='y';s.views[0].camera={turn:0,tilt:0};
    }});
    await assert.rejects(library.publish(blank),/blank|visible exterior/);
    assert.deepEqual(snapshot(fixture.library),preimage);
    report.ok('Test-only no-WebGL and a valid edge-on geometry fixture refuse without changing immutable files or pointers',true);
  });
}
module.exports={modelLibraryPublish,modelLibraryPicture};
