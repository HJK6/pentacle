'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const {clampSplit,normalizeSplit,createGridColResizer}=require('../renderer/grid_col_resizer');

function fixture(initialSplit=.5) {
  const dom=new JSDOM('<div class="grid"><div class="grid-col-resizer"></div></div>');
  const win=dom.window,grid=win.document.querySelector('.grid'),handle=grid.firstElementChild;
  let width=1001,visible=true,frames=new Map(),seq=0,observer;
  const saves=[],events=[],fits=[];
  grid.style.columnGap='1px';
  Object.defineProperty(grid,'clientWidth',{get:()=>width});
  grid.getBoundingClientRect=()=>({left:100,top:0,width,height:600});
  win.requestAnimationFrame=fn=>{frames.set(++seq,fn);return seq;};
  win.cancelAnimationFrame=id=>frames.delete(id);
  win.ResizeObserver=class {constructor(fn){observer=fn;}observe(){}disconnect(){}};
  handle.setPointerCapture=id=>{handle.capture=id;};
  handle.hasPointerCapture=id=>handle.capture===id;
  handle.releasePointerCapture=()=>{handle.capture=null;};
  const control=createGridColResizer({grid,handle,initialSplit,save:f=>saves.push(f),onResize:()=>fits.push(true),isVisible:()=>visible,emit:(name,data)=>events.push({name,data})});
  function flush(){const pending=[...frames.values()];frames.clear();pending.forEach(fn=>fn());}
  function pointer(type,x=600,extra={}) {const event=new win.Event(type,{bubbles:true,cancelable:true});Object.assign(event,{pointerId:1,isPrimary:true,button:0,clientX:x,clientY:200,pointerType:'mouse',...extra});handle.dispatchEvent(event);return event;}
  function key(key){handle.dispatchEvent(new win.KeyboardEvent('keydown',{key,bubbles:true,cancelable:true}));flush();}
  return {dom,win,grid,handle,control,saves,events,fits,flush,pointer,key,resize:w=>{width=w;observer();flush();},hide:()=>{visible=false;control.refresh();flush();}};
}

test('measured single-gap bounds and narrow/hidden defaults',()=>{
  assert.ok(Math.abs(clampSplit(1001,1,.7).left-700)<1e-9);
  assert.ok(Math.abs(clampSplit(1001,1,.7).right-300)<1e-9);
  assert.ok(Math.abs(clampSplit(1001,1,1).right-220)<1e-9);
  assert.equal(clampSplit(1001,1,0).left,220);
  assert.equal(clampSplit(300,1,.8).fraction,.5);
  assert.equal(clampSplit(0,1,.8),null);
  for(const f of [null,undefined,'',true,[],{},NaN,Infinity,0,1,-1,'bad'])assert.equal(normalizeSplit(f),.5);
  assert.equal(normalizeSplit('0.65'),.65);
});
test('loads without writing; narrow clamp does not destroy preferred fraction',()=>{
  const f=fixture(.7);assert.equal(f.saves.length,0);
  f.resize(301);assert.equal(f.grid.style.getPropertyValue('--col-left'),'0.5fr');
  f.resize(1001);assert.equal(f.grid.style.getPropertyValue('--col-left'),'0.7fr');
  assert.equal(f.saves.length,0);f.control.destroy();
});
test('shrinking past the minimum during a drag cancels without saving',()=>{
  const f=fixture(.7);f.pointer('pointerdown',800);f.pointer('pointermove',550);f.flush();f.resize(301);
  f.pointer('pointerup',550);f.flush();assert.equal(f.saves.length,0);assert.equal(f.handle.capture,null);
  f.resize(1001);assert.equal(f.grid.style.getPropertyValue('--col-left'),'0.7fr');f.control.destroy();
});
test('pointer capture/rAF, final coordinates, wrong pointer and button guards',()=>{
  const f=fixture();f.pointer('pointerdown',600,{button:2});assert.equal(f.handle.capture,undefined);
  f.pointer('pointerdown');f.pointer('pointermove',750,{pointerId:2});f.flush();assert.equal(f.grid.style.getPropertyValue('--col-left'),'0.5fr');
  f.pointer('pointermove',750);f.pointer('pointermove',800);assert.equal(f.saves.length,0);f.flush();
  f.pointer('pointerup',850);f.flush();assert.ok(Math.abs(f.saves[0]-.7495)<.001);assert.equal(f.saves.length,1);assert.equal(f.handle.capture,null);f.control.destroy();
});
for(const cancel of ['pointercancel','lostpointercapture','blur','hidden'])test('abort '+cancel+' restores preference and releases drag',()=>{
  const f=fixture(.65);f.pointer('pointerdown',750);f.pointer('pointermove',500);f.flush();
  if(cancel==='blur')f.win.dispatchEvent(new f.win.Event('blur'));else if(cancel==='hidden')f.hide();else f.pointer(cancel,500);
  f.flush();assert.equal(f.saves.length,0);assert.equal(f.grid.classList.contains('resizing-columns'),false);
  if(cancel!=='hidden')assert.equal(f.grid.style.getPropertyValue('--col-left'),'0.65fr');f.control.destroy();
});
test('double mouse/touch taps reset exactly once; two real drags never reset',()=>{
  for(const pointerType of ['mouse','touch']){
    const f=fixture(.65);
    for(let i=0;i<2;i++){f.pointer('pointerdown',750,{pointerType});f.pointer('pointerup',750,{pointerType});}
    f.handle.dispatchEvent(new f.win.MouseEvent('dblclick',{bubbles:true}));f.flush();
    assert.deepEqual(f.saves,[.5]);assert.equal(f.events.filter(e=>e.name==='reset').length,1);f.control.destroy();
  }
  const f=fixture();for(const x of [700,800]){f.pointer('pointerdown',x-40);f.pointer('pointermove',x);f.pointer('pointerup',x);}
  f.handle.dispatchEvent(new f.win.MouseEvent('dblclick',{bubbles:true}));f.flush();assert.equal(f.saves.length,2);assert.notEqual(f.saves.at(-1),.5);f.control.destroy();
});
test('keyboard has measured bounds, reset, matching ARIA and ignores hidden/narrow grids',()=>{
  const f=fixture();f.key('ArrowRight');assert.equal(f.saves[0],.52);f.key('Home');assert.equal(f.saves.at(-1),.22);f.key('End');assert.equal(f.saves.at(-1),.78);
  assert.equal(f.handle.getAttribute('aria-valuenow'),'78');f.key('Enter');assert.equal(f.saves.at(-1),.5);
  f.resize(200);const count=f.saves.length;f.key('ArrowLeft');assert.equal(f.saves.length,count);f.hide();f.key('Enter');assert.equal(f.saves.length,count);f.control.destroy();
});
