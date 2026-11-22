'use strict';

const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const implemented = fs.existsSync(path.join(__dirname,'../renderer/next/reading_width.js'));
// The base's real Settings behavior is asserted by the U1/U2 shell cases.
const unit = (name, fn) => test(name,{skip:!implemented && 'pure edge cases; base verified through real shell'},fn);
const input = (deviceId,label) => ({kind:'audioinput',deviceId,label});

unit('U1 whole-pixel width and stored bounds, field changes and slider saturation', () => {
  const { parseReadingWidth, sliderPosition, createReadingWidth } = require('../renderer/next/reading_width');
  assert.deepEqual(['320','720','2400'].map(parseReadingWidth),[320,720,2400]);
  for (const raw of [319,2401,'12a','400.','1e3',' 905','',409.5]) assert.equal(parseReadingWidth(raw),null,String(raw));
  assert.deepEqual([300,480,840,1200,1500].map(sliderPosition),[480,480,840,1200,1200]);
  const {window} = new JSDOM('',{url:'https://fixture.invalid'});
  window.localStorage.setItem('pentacle.next.read','2401');
  const width = createReadingWidth(window.document.documentElement,window.localStorage);
  assert.equal(width.width,720);
  assert.equal(width.set('905'),true);
  assert.equal(width.set('12a'),false);
  assert.equal(width.width,905);
  assert.equal(window.localStorage.getItem('pentacle.next.read'),'905');
  const fallback=createReadingWidth(window.document.documentElement,{getItem(){throw Error();},setItem(){throw Error();}});
  assert.equal(fallback.width,720);
  assert.ok(fallback.set(320));
  assert.equal(fallback.width,320);
  window.close();
});

unit('U2 input reduction ignores aliases and labels gate the picker without losing a stored choice', () => {
  const {reduceInputs,selectionAfterFailure}=require('../renderer/next/microphone');
  const d=input('default','System'), c=input('communications','Communications');
  const a=input('a','Desk'), b=input('b','Headset'), opaque=input('','');
  assert.deepEqual(reduceInputs([d,c,a],null,false).options,[]);
  assert.deepEqual(reduceInputs([d,c,a,b],null,false).options.map(x=>x.label),['System default','Desk','Headset']);
  assert.deepEqual(reduceInputs([opaque,opaque],'b',false).options,[]);
  assert.equal(reduceInputs([opaque],'b',true).selection,'b');
  assert.equal(reduceInputs([opaque],'b',true).nativeId,null);
  assert.equal(reduceInputs([a,input('b','')],'b',false).selection,'b');
  assert.equal(reduceInputs([a],'b',false).selection,null);
  assert.equal(reduceInputs([], 'b', false).selection,'b');
  assert.equal(reduceInputs([], 'b', true).selection,null);
  assert.equal(reduceInputs([b],'b',true).selection,'b');
  assert.equal(reduceInputs([b],'b',true).nativeId,'b');
  assert.equal(selectionAfterFailure('b','b','NotFoundError'),null);
  assert.equal(selectionAfterFailure('b','b','OverconstrainedError'),null);
  assert.equal(selectionAfterFailure('b','b','NotAllowedError'),'b');
  assert.equal(selectionAfterFailure('b','b','NotReadableError'),'b');
  assert.equal(selectionAfterFailure('a','b','NotFoundError'),'a');
});

unit('scroll activity captures nonbubbling scrolls, resets after 900 ms and leaves excluded scrollers alone', () => {
  const {trackScroll}=require('../renderer/next/scroll_activity');
  const {window}=new JSDOM('<div id="a"></div><div id="b"></div><div class="terminal"><div id="term"></div></div><div class="design-tabs"></div>');
  const jobs=new Map(); let n=0;
  trackScroll(window.document,{setTimer(fn,ms){assert.equal(ms,900);jobs.set(++n,fn);return n;},cancelTimer(id){jobs.delete(id);}});
  const els=[...window.document.querySelectorAll('#a,#b,#term,.design-tabs')];
  for (const el of els) el.dispatchEvent(new window.Event('scroll'));
  assert.deepEqual(els.map(e=>e.classList.contains('next-scrolling')),[true,true,false,false]);
  els[0].dispatchEvent(new window.Event('scroll'));
  assert.deepEqual([...jobs.keys()],[2,3]);
  jobs.get(2)(); assert.ok(!els[1].classList.contains('next-scrolling'));
  assert.ok(els[0].classList.contains('next-scrolling'));
  jobs.get(3)(); assert.ok(!els[0].classList.contains('next-scrolling'));
  window.close();
});

test('U3 theme tokens define each approved thumb strength; supported Chrome styles use a 14px native box', () => {
  const css=fs.readFileSync(path.join(__dirname,'../renderer/next/style.css'),'utf8');
  for (const [color,suffixes] of [['255,255,255',['.16','.28','.46']],['16,28,22',['.19','.31','.52']],['120,255,160',['.2','.32','.56']]]) {
    for (const [i,token] of ['rest','near','hot'].entries())
      assert.ok(css.includes(`--scroll-${token}:rgba(${color},${suffixes[i]})`),color+' '+token);
  }
  assert.match(css,/::-webkit-scrollbar\{width:14px;height:14px}/);
  assert.match(css,/@supports not selector\(::-webkit-scrollbar\)\{/);
  assert.match(css,/\.thread\{scrollbar-gutter:stable both-edges/);
});

unit('scroll screenshot sampling decodes actual RGB rows and distinguishes a 4 by 38 thumb', () => {
  const {deflateSync}=require('node:zlib');
  const {readPng,thumbSample}=require('./e2e/lib/next_settings_pixels');
  const w=32,h=62, scan=Buffer.alloc((w*3+1)*h);
  for (let y=0;y<h;y++) for (let x=0;x<w;x++) {
    const i=y*(w*3+1)+1+x*3;
    const color=x>=23&&x<27&&y>=5&&y<43?60:20;
    scan[i]=scan[i+1]=scan[i+2]=color;
  }
  const header=Buffer.alloc(13);
  header.writeUInt32BE(w);header.writeUInt32BE(h,4);
  header[8]=8;header[9]=2;
  const chunk=(name,body)=>{const b=Buffer.alloc(body.length+12);b.writeUInt32BE(body.length);b.write(name,4);body.copy(b,8);return b;};
  const png=Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',header),
    chunk('IDAT',deflateSync(scan)),chunk('IEND',Buffer.alloc(0))]);
  const px=readPng(png);
  assert.deepEqual(thumbSample(px,{right:32,top:0},14,23),
    {x:25,y:23,width:4,length:38,color:[60,60,60],contrast:40});
  assert.throws(()=>thumbSample(px,{right:18,top:0},14,23),/no visible native thumb/);
});
