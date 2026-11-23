'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {createHash}=require('node:crypto');
const {spawnSync}=require('node:child_process');
const {WebSocket}=require('ws');
const {temp,fakeCapture,glb,png}=require('./fixtures/model_library.cjs');
const {operator,general,origin,proxy,tailnetArgs,writeFixture,request,withHost}=require('./e2e/lib/model_library_fixture.cjs');
const {main,parseArgs,createTailscaleAuth,WEB_DIST}=require('../server/index.js');

function checkDenial(res,status,method) {
  assert.equal(res.status,status);
  assert.equal(res.headers['cache-control'],'private, no-store');
  for(const h of ['location','etag','content-range','accept-ranges','last-modified','content-disposition']) assert.equal(res.headers[h],undefined,h);
  assert.doesNotMatch(res.body.toString(),/spacer|opposite|bench|model-media/);
  if(method==='HEAD') assert.equal(res.body.length,0);
}
const cases=[['GET','GET',200,{}],['HEAD','HEAD',200,{}],
  ['conditional','GET',304,{'if-none-match':'VALIDATOR'}],['Range','GET',206,{range:'bytes=0-7'}]];

test('U7 unadmitted token-mode media GET cannot redirect to login',async t=>{
  const root=temp(t), fixture=writeFixture(root), token=path.join(root,'token');
  fs.writeFileSync(token,'synthetic-fixture-secret\n',{mode:0o600});
  await withHost(fixture,['--token-file',token],async host=>{
    const res=await request(host.port,'/model-media/bench/spacer/absent/picture');
    checkDenial(res,401,'GET');
  });
});

for (const [label,args] of [
  ['zero',['--operator-login',operator]],
  ['mismatched',['--allow-login',general,'--operator-login',operator]],
  ['case mismatch',['--allow-login',operator.toUpperCase(),'--operator-login',operator]],
  ['duplicate comma',['--allow-login',operator+','+operator,'--operator-login',operator]],
  ['duplicate flags',['--allow-login',operator,'--allow-login',operator,'--operator-login',operator]],
  ['multiple',['--allow-login',operator+','+general,'--operator-login',operator]],
  ['empty list entry',['--allow-login',operator+',','--operator-login',operator]],
  ['repeated',['--allow-login',operator,'--operator-login',operator,'--operator-login',operator]],
  ['malformed',['--allow-login',operator,'--operator-login='+operator]],
  ['malformed space',['--allow-login',operator,'--operator-login','bad binding']],
]) test('U7 startup refuses '+label+' without echoing values',async()=>{
  await assert.rejects(main(['--port','0','--auth','tailscale','--origin',origin,...args]),error=>
    /^(--operator-login requires tailscale auth and exactly one matching|invalid or repeated --operator-login|use --operator-login followed)/.test(error.message) && !error.message.includes(operator) && !error.message.includes(general));
});
for (const [label,flags] of [
  ['missing allow-list operand',['--allow-login','--operator-login',operator]],
  ['unsupported allow-list equals form',['--allow-login='+operator]],
  ['operator missing operand',['--operator-login','--allow-login',operator]],
  ['unknown raw argument',[operator]],
  ['invalid origin',['--allow-login',operator,'--operator-login',operator,'--origin',operator]],
]) test('B1 startup redacts synthetic operands: '+label,async()=>{
  const args=['--port','0','--auth','tailscale','--origin',origin,...flags];
  await assert.rejects(main(args),error=>!error.message.includes(operator),'thrown startup error contained a synthetic operand');
  const child=spawnSync(process.execPath,[path.join(__dirname,'../server/index.js'),...args],{encoding:'utf8',timeout:5000});
  assert.equal(child.error,undefined);
  assert.equal(child.status,1);
  assert.equal(child.stdout.includes(operator),false,'stdout contained a synthetic operand');
  assert.equal(child.stderr.includes(operator),false,'stderr contained a synthetic operand');
});

for (const binding of [false,true]) test('B2 static, descendant and aliased roots are refused; disjoint store works (binding '+binding+')',async t=>{
  const f=writeFixture(temp(t));
  fs.mkdirSync(WEB_DIST,{recursive:true});
  const staging=fs.mkdtempSync(path.join(WEB_DIST,'model-root-test-'));
  t.after(()=>fs.rmSync(staging,{force:true,recursive:true}));
  const alias=path.join(path.dirname(f.profile),'static-alias');
  fs.symlinkSync(staging,alias,'junction');
  const args=binding?tailnetArgs(operator,true):[];
  for (const library of [WEB_DIST,staging,path.join(staging,'not-yet-created'),alias,path.join(alias,'not-yet-created')]) {
    await assert.rejects(withHost({...f,library},args,()=>{}),/model library root must be outside the public web directory/);
  }
  await require('../lib/model_library.cjs').createLibrary(f.library).publish(f.source,{capture:fakeCapture});
  await withHost(f,args,async ({port})=>{
    const result=await request(port,'/api/models',{headers:binding?proxy():{}});
    assert.equal(result.status,binding?200:404);
    if(binding) assert.equal(JSON.parse(result.body).projects[0].models.length,1);
  });
});

test('U7 startup refuses token binding without echoing it',async()=>{
  await assert.rejects(main(['--port','0','--operator-login',operator]),error=>
    /requires tailscale/.test(error.message) && !error.message.includes(operator));
});
test('U7 exact sole operator startup succeeds',()=>{
  assert.equal(parseArgs(tailnetArgs(operator,true)).operatorLogin,operator);
});

test('U7 operator route contracts and uniform denials across methods and targets',async t=>{
  const root=temp(t), fixture=writeFixture(root);
  // Baseline supplies ordinary admission; an admitted general login must
  // still see an explicitly private response rather than a static-file miss.
  await withHost(fixture,tailnetArgs(general),async ({port})=>{
    checkDenial(await request(port,'/api/models',{headers:proxy(general)}),404,'GET');
  });
  const library=require('../lib/model_library.cjs').createLibrary(fixture.library);
  const pub=await library.publish(fixture.source,{capture:fakeCapture});
  const prefix='/model-media/bench/spacer/'+pub.revision+'/';
  const targets=['/api/models',prefix+'mesh/front',prefix+'picture/front',prefix+'picture/opposite',prefix+'picture'];
  const missing=[prefix+'mesh/absent',prefix+'picture/absent',prefix.replace('spacer','absent')+'picture'];
  const etags=new Map();
  await withHost(fixture,tailnetArgs(operator,true),async host=>{
    const initial=await request(host.port,targets[0],{headers:proxy()});
    const body=JSON.parse(initial.body.toString());
    assert.equal(body.schema,'model-catalog.v1');
    const model=body.projects[0].models[0];
    assert.equal(model.sheet.title,'Test spacer');
    assert.equal(model.revision,pub.revision);
    assert.equal(model.published_at,pub.published_at);
    assert.deepEqual(model.views.map(x=>[x.mesh_url,x.picture_url]),[[targets[1],targets[2]],[prefix+'mesh/opposite',targets[3]]]);
    assert.deepEqual(model.views[0].extents,[10,20,30]);
    assert.equal(model.views[0].bytes,glb().length);
    assert.equal(model.views[0].triangles,12);
    assert.deepEqual(model.views[0].parts.map(x=>[x.dimensions,x.copy_count]),[[[30,20,10],1]]);
    for(const target of targets) {
      const get=await request(host.port,target,{headers:proxy()});
      etags.set(target,get.headers.etag);
      if(target!==targets[0]) assert.deepEqual(get.body,target.includes('/mesh/')?glb():png());
      for (const [label,method,status,headers] of cases) {
        const conditional=label==='conditional'?{'if-none-match':get.headers.etag}:headers;
        const good=await request(host.port,target,{method,headers:{...proxy(),...conditional}});
        assert.equal(good.status,target===targets[0] && label==='Range'?200:status);
        assert.equal(good.headers['cache-control'],'private, no-store');
        assert.equal(good.headers.etag,get.headers.etag);
        if (status===304 || method==='HEAD') assert.equal(good.body.length,0);
        if (good.status===206) assert.deepEqual(good.body,get.body.subarray(0,8));
        for (const denied of [target,...missing]) {
          checkDenial(await request(host.port,denied,{method,headers:{...conditional,'x-role':'operator'}}),401,method);
          checkDenial(await request(host.port,denied,{method,headers:{...conditional,...proxy(general)}}),401,method);
        }
      }
      for(const invalid of [{...proxy(),'x-forwarded-proto':'http'},{...proxy(),'x-forwarded-for':'127.0.0.1'},
        {...proxy(),'tailscale-user-login':[operator,operator]}]) {
        checkDenial(await request(host.port,target,{headers:invalid}),401,'GET');
      }
    }
    const cfg=(await request(host.port,'/api/config',{headers:proxy()})).body.toString();
    assert.ok(!cfg.includes(operator) && !cfg.includes('operatorLogin'));
    const serverSocket=new Promise(resolve=>host.wss.once('connection',resolve));
    const ws=new WebSocket('ws://127.0.0.1:'+host.port+'/cc',{headers:proxy()});
    try {
      const connected=new Promise((resolve,reject)=>{ws.once('open',resolve);ws.once('error',reject);});
      const [socket]=await Promise.all([serverSocket,connected]);
      const verified = require('../server/index.js').viewerForSocket(socket);
      assert.equal(verified.login,operator);
      assert.equal(host.canReadForge(verified),true);
    } finally { ws.terminate(); }
  });
  const token=path.join(root,'token');
  fs.writeFileSync(token,'synthetic-fixture-secret\n',{mode:0o600});
  for (const [flags,admitted,anon] of [
    [tailnetArgs(general),async()=>proxy(general),false], [tailnetArgs(operator),async()=>proxy(),false],
    [[],async()=>({}),false],
    [['--token-file',token],async port=>{
      const body='token=synthetic-fixture-secret';
      const login=await request(port,'/login',{method:'POST',body,headers:{
        'content-type':'application/x-www-form-urlencoded','content-length':Buffer.byteLength(body)}});
      return {cookie:login.headers['set-cookie'][0].split(';')[0]};
    },true],
  ]) await withHost(fixture,flags,async ({port})=>{
    const head=await admitted(port);
    for(const [target,etag] of etags) for(const [label,method,,headers] of cases) {
      const conditional=label==='conditional'?{'if-none-match':etag}:headers;
      for(const targetOrMiss of [target,...missing]) {
        checkDenial(await request(port,targetOrMiss,{method,headers:{...head,...conditional,'x-role':'operator'}}),404,method);
        if (anon) checkDenial(await request(port,targetOrMiss,{method,headers:conditional}),401,method);
        if (!flags.length) checkDenial(await request(port,targetOrMiss,{method,headers:{
          ...conditional,origin:'https://unadmitted.example.test',
        }}),401,method);
      }
    }
  });
});

test('U7 verified viewers alone drive ordered future projections',()=>{
  const {createForgeEntitlement}=require('../server/forge_entitlement');
  const entitlement=createForgeEntitlement(operator);
  const auth=createTailscaleAuth({allowLogins:[operator,general],origin});
  const viewer=login=>entitlement.viewerFor({socket:{remoteAddress:'127.0.0.1'},rawHeaders:Object.entries(proxy(login)).flat()},auth);
  const items=['Design','Forge','Other'];
  const project=v=>items.filter(item=>item!=='Forge'||entitlement.canReadForge(v));
  assert.deepEqual(project(viewer(operator)),items);
  assert.deepEqual(project(viewer(general)),['Design','Other']);
  assert.deepEqual(project({login:operator}),['Design','Other']);
  assert.deepEqual(project(entitlement.viewerFor({},null)),['Design','Other']);
});
