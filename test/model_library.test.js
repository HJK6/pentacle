'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {spawnSync}=require('node:child_process');
const {box,drop,glb,temp,png,fakeCapture}=require('./fixtures/model_library.cjs');
const cli=path.join(__dirname,'../scripts/model-library.cjs');
function command(command, ...args) {
  const p=spawnSync(process.execPath,[cli,command,...args],{encoding:'utf8'});
  return {status:p.status, value:p.stdout ? JSON.parse(p.stdout) : null, stderr:p.stderr};
}
function lib(root) { return require('../lib/model_library.cjs').createLibrary(root); }
function setup(t,options) {
  const root=temp(t), dir=path.join(root,'drop'), store=path.join(root,'store');
  drop(dir,options);
  return {dir, store,args:['--drop',dir,'--root',store]};
}
const option=(name,n)=>['--'+name,String(n)];
const parts=s=>s.views[0].parts;
const view=s=>s.views[0];
const fixtureDate=new Date('2026-07-01T01:02:03Z');

test('U1 rejects independent schema and semantic violations with field and fix', async t=>{
  const checks=[
    ['project/alias',s=>s.project.alias='extra'],
    ['facts/0/extra',s=>s.facts[0].extra='extra'],
    ['changes/0/extra',s=>s.changes[0].extra='extra'],
    ['views/0/extra',s=>view(s).extra='extra'],
    ['schema',s=>s.schema='v0'],['project/id',s=>s.project.id='Bench'],
    ['project/title',s=>s.project.title=' '],['id',s=>s.id='abc_'],
    ['title',s=>s.title='a'.repeat(61)],['summary',s=>s.summary='a\nb'],
    ['summary',s=>s.summary='x'.repeat(281)],['units',s=>s.units='inch'],['up',s=>s.up='x'],
    ['facts',s=>s.facts=Array(7).fill(s.facts[0])],
    ['facts/0/label',s=>s.facts[0].label='x'.repeat(17)],
    ['facts/0/value',s=>s.facts[0].value='x'.repeat(81)],
    ['facts/0/url',s=>s.facts[0].url='ftp://example.test'],
    ['facts/0/url',s=>s.facts[0].url='https:///'],
    ['changes',s=>s.changes=Array(9).fill(s.changes[0])],
    ['changes/0/label',s=>delete s.changes[0].label],
    ['changes/0/to',s=>s.changes[0].to='x\ny'],
    ['unverified',s=>s.unverified=Array(7).fill('note')],
    ['unverified/0',s=>s.unverified[0]='x'.repeat(161)],
    ['views',s=>s.views=[]],['views',s=>s.views=Array(21).fill(view(s))],
    ['views/0/id',s=>view(s).id='Front'],['views/1/id',s=>s.views.push(view(s))],
    ['views/0/title',s=>view(s).title='x'.repeat(41)],
    ['views/0/file',s=>view(s).file='../Sample_A.glb'],['views/0/file',s=>view(s).file='Absent.glb'],
    ['views/0/camera/extra',s=>view(s).camera.extra=1],
    ['views/0/parts/0/id',s=>parts(s)[0].id='A'],['views/0/parts/0/role',s=>parts(s)[0].role='unknown'],
    ['views/0/parts/0/material',s=>parts(s)[0].material=' '],
    ['views/0/parts/0/name',s=>parts(s)[0].name='a'.repeat(41)],
    ['views/0/parts/0/about',s=>parts(s)[0].about=' '],
    ['views/0/parts/0/about',s=>parts(s)[0].about='X. Y. Z.'],
    ['views/0/parts/0/about',s=>parts(s)[0].about='x'.repeat(201)],
    ['views/0/parts/1/about',s=>parts(s).push({...parts(s)[0],id:'copy'})],
    ['views/0/parts/1/id',s=>parts(s).push({...parts(s)[0],about:'Another purpose.'})],
    ['views/0/parts/0/nodes/0',s=>parts(s)[0].nodes=['Missing']],
    ['views/0/parts/0/nodes/1',s=>parts(s)[0].nodes.push('Spacer A')],
    ['views/0/parts',s=>parts(s)[0].nodes=[]],
    ['views/0/parts/0/size',s=>parts(s)[0].size=[1,2,3]],['status',s=>s.status='verified'],
  ];
  // One CLI probe on the exact base establishes whether the public command is
  // available. At tip independent cases use the same implementation as CLI.
  const probe=setup(t,{edit:s=>s.schema='v0'});
  const r=command('check',...probe.args);
  assert.equal(r.status,1);
  assert.ok(r.value?.diagnostics.some(x=>x.field.endsWith('/schema')),r.stderr);
  const {checkDrop}=require('../lib/model_library.cjs');
  for (const [field,edit] of checks) {
    drop(probe.dir,{edit});
    const invalid=checkDrop(probe.dir);
    assert.equal(invalid.ok,false,field);
    assert.ok(invalid.diagnostics.some(x=>x.field.endsWith('/'+field) && x.fix),JSON.stringify(invalid.diagnostics));
  }
  drop(probe.dir,{edit:s=>{s.status='ready';s.updated='2025-02-30';parts(s)[0].nodes=['Unmapped'];}});
  assert.ok(checkDrop(probe.dir).diagnostics.length>=4);
  for (const mesh of [Buffer.from('broken'),glb([{}],g=>{g.images=[{uri:'https://example.test/texture.png'}];}),
    glb([{}, {node:{name:'Spacer A'}}]),glb([{}],g=>g.scenes[0].nodes=[]),
    glb([{}],g=>g.accessors[0].byteOffset=100000),glb([{}],g=>g.nodes[0].children=[0])]) {
    drop(probe.dir,{mesh});
    assert.ok(checkDrop(probe.dir).diagnostics.some(x=>x.field.endsWith('/file')));
  }
  assert.equal(fs.existsSync(probe.store),false);
});

function measure(t, options) {
  const {args,dir}=setup(t,options);
  const result=command('check',...args);
  assert.equal(result.status,0,result.stderr || JSON.stringify(result.value));
  return require('../lib/model_library.cjs').checkDrop(dir);
}
test('U2 local dimensions ignore placed transforms and match only equivalent baked copies',t=>{
  const angle=Math.PI/12;
  const placed=measure(t,{mesh:glb([{node:{rotation:[0,0,Math.sin(angle),Math.cos(angle)]}}])}).checked[0].view;
  assert.deepEqual(placed.parts[0].dimensions,[30,20,10]);
  assert.deepEqual(placed.extents,[18.7,22.3,30]);
  const baked=box().map(([x,y,z])=>[-y,x,z]);
  const edit=s=>parts(s)[0].nodes.push('Spacer B');
  const matching=measure(t,{mesh:glb([{}, {points:baked}]),edit}).checked[0].view;
  assert.deepEqual(matching.parts[0].dimensions,[30,20,10]);
  assert.deepEqual(matching.extents,[20,20,30]);
  const split=measure(t,{mesh:glb([{}],g=>{
    g.nodes.push({translation:[50,0,0],children:[0]});
    g.scenes[0].nodes=[1];
    g.meshes[0].primitives.push(g.meshes[0].primitives[0]);
  })}).checked[0].view;
  assert.equal(split.triangles,24);
  assert.deepEqual(split.parts[0].dimensions,[30,20,10]);
  assert.deepEqual(split.extents,[10,20,30]);
  const bad=setup(t,{mesh:glb([{}, {points:box(11)}]),edit});
  assert.match(JSON.stringify(command('check',...bad.args).value),/matching copy/);
});
test('B3 copy spread is bounded independently of copy order',t=>{
  for (const [order,ok] of [
    [[10,10.09,9.91],false], [[9.91,10,10.09],false], [[10.09,9.91,10],false],
    [[10,10.098,10.001],true], [[10.098,10.001,10],true],
  ]) {
    const {dir}=setup(t,{mesh:glb(order.map(x=>({points:box(x)}))),
      edit:s=>parts(s)[0].nodes.push('Spacer B','Spacer C')});
    const result=require('../lib/model_library.cjs').checkDrop(dir);
    assert.equal(result.ok,ok,'copy-order spread '+order.join(','));
    if(!ok) assert.ok(result.diagnostics.some(x=>x.fix.includes('matching copy')));
  }
});

test('U3 three nodes are one part with three copies',t=>{
  const {checked}=measure(t,{mesh:glb([{}, {}, {}]),edit:s=>parts(s)[0].nodes.push('Spacer B','Spacer C')});
  assert.equal(checked[0].view.parts.length,1);
  assert.equal(checked[0].view.parts[0].copy_count,3);
});

async function publishFixture(t) {
  const {dir,store,args}=setup(t);
  const probe=command('check',...args);
  assert.equal(probe.status,0,probe.stderr);
  const library=lib(store);
  const publish=(now=fixtureDate,options={})=>library.publish(dir,{capture:fakeCapture,now,...options});
  const first=await publish();
  assert.equal(first.ok,true);
  const pointer=path.join(store,'models','bench','spacer','current.json');
  return {dir,store,library,publish,first,pointer,args};
}
test('U4 repeat is a byte-identical no-op and a refusal preserves the prior selection', async t=>{
  const {store,dir,publish,first,pointer,library}=await publishFixture(t);
  const before=fs.readFileSync(pointer);
  const objects=()=>fs.readdirSync(path.join(store,'objects')).map(f=>fs.readFileSync(path.join(store,'objects',f)));
  const bytes=objects();
  const again=await publish(new Date('2026-07-02'));
  assert.deepEqual(again,{...first,unchanged:true});
  assert.deepEqual(fs.readFileSync(pointer),before);
  assert.deepEqual(objects(),bytes);
  await assert.rejects(publish(new Date(),{capture:async()=>{throw new Error('No WebGL');}}),/No WebGL/);
  assert.deepEqual(objects(),bytes);
  assert.deepEqual(fs.readFileSync(pointer),before);
  drop(dir,{edit:s=>s.status='draft'});
  assert.equal((await publish()).ok,false);
  assert.deepEqual(fs.readFileSync(pointer),before);
  assert.equal(library.verify().revisions,1);
});
test('U5 a sheet-only revision reuses the stored mesh object; partial copies fail closed',async t=>{
  const {store,dir,library,publish,first,pointer}=await publishFixture(t);
  const objectDir=path.join(store,'objects');
  const list=()=>fs.readdirSync(objectDir).filter(f=>f.endsWith('.glb'));
  const before=list();
  const original=fs.statSync(path.join(objectDir,before[0]));
  const oldPointer=fs.readFileSync(pointer);
  assert.equal(library.catalog().projects[0].models[0].revision,first.revision);
  drop(dir,{edit:s=>s.title='Revised spacer'});
  const second=await publish(new Date('2026-07-02'));
  assert.notEqual(first.revision,second.revision);
  assert.deepEqual(before,list());
  assert.equal(fs.statSync(path.join(objectDir,before[0])).mtimeMs,original.mtimeMs);
  const copied=fs.readFileSync(pointer);
  fs.writeFileSync(pointer,oldPointer);
  const warm=lib(store);
  assert.equal(warm.catalog().projects[0].models[0].revision,first.revision);
  const revisionPath=path.join(path.dirname(pointer),'revisions',second.revision,'manifest.json');
  const contents=fs.readFileSync(revisionPath);
  fs.unlinkSync(revisionPath);
  fs.writeFileSync(pointer,copied);
  assert.deepEqual(lib(store).catalog().projects,[]);
  assert.equal(warm.catalog().projects[0].models[0].revision,first.revision);
  assert.throws(()=>lib(store).verify());
  fs.writeFileSync(revisionPath,contents);
  assert.equal(warm.catalog().projects[0].models[0].revision,second.revision);
});
test('U6 removal, retained rollback and local preview',async t=>{
  const {args,dir,store,library,publish,first}=await publishFixture(t);
  const key=['--root',store,'--project','bench','--model','spacer'];
  drop(dir,{edit:s=>{s.title='Revised spacer';s.views.push({...view(s),title:'Rear',id:'rear'});}});
  const pictures=[png(),png(160)];
  const second=await publish(new Date('2026-07-02'),{capture:async()=>pictures});
  assert.notEqual(first.revision,second.revision);
  for(const [opts,title,bytes] of [[[],'Front',pictures[0]],[['--view','rear'],'Rear',pictures[1]]]) {
    await t.test('B4 opens the stored '+title+' PNG',()=>{
      const preview=command('link-preview',...key,...opts).value;
      assert.equal(preview.view_title,title);
      assert.equal(path.dirname(preview.picture_path),path.join(store,'objects'));
      assert.deepEqual(fs.readFileSync(preview.picture_path),bytes);
    });
  }
  assert.equal(command('link-preview',...key,'--view','invalid').status,1);
  assert.equal(command('remove',...key).status,0);
  assert.deepEqual(library.catalog().projects,[]);
  assert.equal(command('verify',...args.slice(2)).value.revisions,2);
  assert.equal(command('rollback',...key,'--revision',first.revision).value.revision,first.revision);
  assert.deepEqual(library.preview('bench','spacer'),{project_title:'Bench fixtures',model_title:'Test spacer',view_title:'Front',
    revision:first.revision,picture_path:path.join(store,'objects',library.media('bench','spacer',first.revision,'picture','front').digest+'.png')});
  drop(dir,{edit:s=>s.project.title='Renamed fixtures'});
  await assert.rejects(publish(),/rename-project/);
  assert.equal((await publish(new Date('2026-07-04'),{rename:true})).ok,true);
  assert.equal(library.catalog().projects[0].title,'Renamed fixtures');
});
test('U8 inclusive byte and triangle limits and independent refusals report both numbers',t=>{
  const {args}=setup(t);
  const expected={view:'front',bytes:glb().length,triangles:12};
  assert.deepEqual(command('check',...args).value?.measurements,[expected]);
  for (const [flag,n] of [['bytes',expected.bytes],['triangles',expected.triangles]]) {
    const bad=command('check',...args,...option('max-'+flag,n-1));
    assert.equal(bad.status,1);
    assert.deepEqual(bad.value.measurements,[expected]);
    assert.match(JSON.stringify(bad.value.diagnostics),new RegExp('Reduce '+flag));
    assert.equal(command('check',...args,...option('max-'+flag,n)).status,0);
  }
  assert.deepEqual(require('../lib/model_library.cjs').DEFAULT_LIMITS,{bytes:33554432,triangles:500000});
});
test('U9 complete sheet, calendar, color, camera, explode and up conventions',t=>{
  const first=measure(t);
  assert.equal(first.sheet.up,'z');
  const {dir,args}=setup(t,{edit:s=>{s.up='y';delete view(s).camera;}});
  assert.equal(command('check',...args).status,0);
  assert.deepEqual(require('../lib/model_library.cjs').checkDrop(dir).checked[0].view.camera,{turn:-35,tilt:25});
  for (const edit of [
    s=>s.updated='2025-02-29',s=>s.updated='9999-12-31',s=>parts(s)[0].color='red',
    s=>view(s).camera.tilt=90,s=>view(s).camera.turn='0',s=>parts(s)[0].explode=[0,1],
  ]) {
    drop(dir,{edit});
    assert.equal(command('check',...args).value.ok,false);
  }
});
