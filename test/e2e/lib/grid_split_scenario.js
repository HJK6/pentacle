'use strict';
const assert = require('node:assert/strict');

const geometry = `(() => {
  const grid = document.querySelector('.grid');
  const rect = el => { const r = el.getBoundingClientRect(); return { x:r.x,y:r.y,width:r.width,height:r.height }; };
  const handles = [...grid.querySelectorAll('.grid-col-resizer')];
  const handle = handles[0];
  const appearance=JSON.parse(localStorage.getItem('pentacle.settings.v1')||'{}').appearance||{};
  return { grid:rect(grid), cells:[0,1,2,3].map(i=>rect(document.getElementById('cell-'+i))),
    handles:handles.map(rect), handle:handle ? rect(handle):null, gap:parseFloat(getComputedStyle(grid).columnGap),
    sidebar:rect(document.querySelector('.sidebar')), saved:appearance.gridColSplitTop ?? appearance.gridColSplit ?? .5, savedBottom:appearance.gridColSplitBottom ?? appearance.gridColSplit ?? .5, appearance };
})()`;

async function mouse(session, type, x, y, extra = {}) {
  return session.send('Input.dispatchMouseEvent', { type,x,y,button:'left',...extra });
}
async function settle(session) {
  await session.eval('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
}
async function drag(session, fraction, row=0) {
  const g = await session.eval(geometry);
  const handle=g.handles[row];
  assert.ok(handle && handle.width > 0, 'slot-column divider is rendered');
  const x = handle.x + handle.width/2, y = handle.y + handle.height/2;
  const target = g.grid.x + (g.grid.width-g.gap)*fraction + g.gap/2;
  await mouse(session,'mousePressed',x,y,{clickCount:1,buttons:1});
  for (let i=1;i<=5;i++) await mouse(session,'mouseMoved',x+(target-x)*i/5,y,{buttons:1});
  await mouse(session,'mouseReleased',target,y,{clickCount:1,buttons:0});
  await settle(session);
  return session.eval(geometry);
}
async function doubleTap(session, touch=false, row=0) {
  const g=await session.eval(geometry),h=g.handles[row],x=h.x+h.width/2,y=h.y+h.height/2;
  await session.eval(`window.__splitTapEvents=[]; if(!window.__splitTapObserver){window.__splitTapObserver=true;for(const type of ['pointerdown','pointerup'])document.addEventListener(type,e=>window.__splitTapEvents.push({type,at:Date.now(),pointerType:e.pointerType,row:e.target.closest('.grid-row')?.dataset.row,primary:e.isPrimary,x:e.clientX,y:e.clientY}),true);}`);
  for(let i=0;i<2;i++) {
    if(touch) {
      await session.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y,id:1}]});
      await session.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
    } else {
      await mouse(session,'mousePressed',x,y,{clickCount:i+1,buttons:1});
      await mouse(session,'mouseReleased',x,y,{clickCount:i+1,buttons:0});
    }
  }
  await settle(session);
  const events=await session.eval('window.__splitTapEvents');
  const valid=events.length===4&&events.every(e=>e.row===(row===0?'top':'bottom')&&e.primary!==false&&e.pointerType===(touch?'touch':'mouse'))&&events.map(e=>e.type).join(',')==='pointerdown,pointerup,pointerdown,pointerup'&&events[1].at-events[0].at<=300&&events[3].at-events[2].at<=300&&events[3].at-events[1].at<=350;
  if(!valid)throw Object.assign(Error('double-tap injection did not meet gesture timing contract: '+JSON.stringify(events)),{classification:'HARNESS_ERROR'});
}

async function reload(session) {
  await session.eval('window.__splitOldDocument=true');
  await session.send('Page.reload');
  await session.waitFor("!window.__splitOldDocument && document.readyState==='complete' && document.querySelectorAll('.grid-col-resizer[aria-valuenow]').length===2");
  await settle(session);
}
async function runGridSplit({session,report}) {
  await session.waitFor("document.readyState==='complete' && document.querySelectorAll('.grid-col-resizer[aria-valuenow]').length===2");
  const before=await session.eval(geometry);
  report.ok('two row-scoped accessible dividers are rendered',before.handles.length===2&&before.handles.every((h,i)=>h.width>=10&&Math.abs(h.height-before.cells[i*2].height)<2),before);
  const top=await drag(session,.3);
  report.ok('top30/70 leaves bottom50/50 and rows/sidebar unchanged',Math.abs(top.cells[0].width/(top.grid.width-top.gap)-.3)<.005&&Math.abs(top.cells[2].width-before.cells[2].width)<2&&top.cells.every((c,i)=>c.y===before.cells[i].y&&c.height===before.cells[i].height)&&top.sidebar.width===before.sidebar.width,top);
  const both=await drag(session,.65,1);
  report.ok('bottom65/35 leaves top30/70 unchanged',Math.abs(both.cells[2].width/(both.grid.width-both.gap)-.65)<.005&&Math.abs(both.cells[0].width-top.cells[0].width)<2,both);
  await reload(session);
  const restored=await session.eval(geometry);
  report.ok('both row preferences survive true reload',Math.abs(restored.saved-.3)<.005&&Math.abs(restored.savedBottom-.65)<.005&&restored.cells.every((c,i)=>Math.abs(c.width-both.cells[i].width)<2),restored);
  await doubleTap(session);
  const topReset=await session.eval(geometry);
  report.ok('top mouse reset preserves bottom',topReset.saved===.5&&topReset.savedBottom===restored.savedBottom&&Math.abs(topReset.cells[2].width-restored.cells[2].width)<2,topReset);
  await drag(session,.3);
  await doubleTap(session,true,1);
  const bottomReset=await session.eval(geometry);
  report.ok('bottom touch reset preserves top',bottomReset.savedBottom===.5&&bottomReset.saved===bottomReset.appearance.gridColSplitTop&&Math.abs(bottomReset.cells[0].width-top.cells[0].width)<2,bottomReset);

  // Migrate a real old settings record; a true reload must not rewrite it.
  await session.eval(`localStorage.setItem('pentacle.settings.v1',JSON.stringify({appearance:{gridColSplit:.61,theme:'dark',density:'comfortable',keep:'unchanged'},features:{inputBar:true}}))`);
  await reload(session);
  const migrated=await session.eval(geometry);
  report.ok('legacy preference initializes both without eager persistence',migrated.saved===.61&&migrated.savedBottom===.61&&migrated.appearance.gridColSplitTop===undefined&&migrated.appearance.gridColSplitBottom===undefined&&migrated.cells.filter((_,i)=>i%2===0).every(c=>Math.abs(c.width/(migrated.grid.width-migrated.gap)-.61)<.005),migrated);
  await drag(session,.3);
  await reload(session);
  const partial=await session.eval(geometry);
  report.ok('one saved row leaves sibling legacy fallback and unrelated preferences intact',Math.abs(partial.saved-.3)<.005&&partial.savedBottom===.61&&partial.appearance.keep==='unchanged'&&partial.appearance.gridColSplit===.61,partial);
  for(const row of [0,1]) {
    const savedKey=row===0?'saved':'savedBottom', otherKey=row===0?'savedBottom':'saved';
    const sibling=(await session.eval(geometry))[otherKey];
    for(const f of [-1,2]) {
      const bound=await drag(session,f,row);
      report.ok('row '+row+' bounds '+f,bound.cells[row*2].width>=219.5&&bound.cells[row*2+1].width>=219.5&&bound[otherKey]===sibling,bound);
    }
    await session.eval(`document.querySelectorAll('.grid-col-resizer')[${row}].focus()`);
    for(const [key,code] of [['Home',36],['End',35],['Enter',13],['ArrowRight',39],['ArrowLeft',37]]) {
      await session.send('Input.dispatchKeyEvent',{type:'keyDown',key,code:key,windowsVirtualKeyCode:code});
      await session.send('Input.dispatchKeyEvent',{type:'keyUp',key,code:key,windowsVirtualKeyCode:code});
      await settle(session);
      const g=await session.eval(geometry),want=key==='Home'?220/(g.grid.width-g.gap):key==='End'?1-220/(g.grid.width-g.gap):key==='ArrowRight'?.52:.5;
      report.ok('row '+row+' keyboard '+key+' preserves sibling and updates ARIA',Math.abs(g[savedKey]-want)<.001&&g[otherKey]===sibling&&await session.eval(`document.querySelectorAll('.grid-col-resizer')[${row}].getAttribute('aria-valuenow')===${JSON.stringify(String(Math.round(want*100)))}`),g);
    }
    await drag(session,.62,row); await doubleTap(session,true,row);
    const touchReset=await session.eval(geometry);
    report.ok('row '+row+' touch reset stays local',touchReset[savedKey]===.5&&touchReset[otherKey]===sibling,{geometry:touchReset,timing:await session.eval('window.__splitTapEvents')});
    // Cancel a real touch drag; no preference or sibling may change.
    const start=await session.eval(geometry),h=start.handles[row],x=h.x+h.width/2,y=h.y+h.height/2;
    await session.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y,id:1}]});
    await session.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:x+80,y,id:1}]});
    await settle(session);
    const moving=await session.eval(geometry);
    report.ok('row '+row+' cancellation probe moved its own track before abort',Math.abs(moving.cells[row*2].width-start.cells[row*2].width)>20&&moving.cells[(1-row)*2].width===start.cells[(1-row)*2].width,moving);
    await session.send('Input.dispatchTouchEvent',{type:'touchCancel',touchPoints:[]});
    await settle(session);
    const cancelled=await session.eval(geometry);
    report.ok('row '+row+' cancelled drag restores both widths and persisted preferences',cancelled.saved===start.saved&&cancelled.savedBottom===start.savedBottom&&cancelled.cells.every((c,i)=>Math.abs(c.width-start.cells[i].width)<2)&&await session.eval("!document.querySelector('.resizing-columns')"),cancelled);
  }
  await drag(session,.65); await drag(session,.65,1);
  return {before,after:await session.eval(geometry)};
}

async function runGridSplitTerminals({session,report,fixtures,tmux,cdp}) {
  await session.eval(`(() => {
    window.__splitSizes={};window.__splitResizes=[];
    const create=window.cc.createPty,resize=window.cc.resizePty;
    window.cc.createPty=async(...args)=>{const pane=await create(...args);window.__splitSizes[args[0]]={cols:args[3],rows:args[4],pane};return pane;};
    window.cc.resizePty=(slot,cols,rows)=>{window.__splitSizes[slot]={...window.__splitSizes[slot],cols,rows};window.__splitResizes.push({slot,cols,rows});return resize(slot,cols,rows);};
  })()`);
  for(let i=0;i<fixtures.length;i++) {
    const selector=`.session-item[data-name="${fixtures[i].sessionName}"]`;
    await session.waitFor(`!!document.querySelector(${JSON.stringify(selector)})`);
    assert.ok(await session.click(selector),'fixture sidebar row clicked');
    await session.waitFor(`!!window.__splitSizes[${i}]?.pane && !!document.querySelector('#cell-${i} .xterm')`);
  }
  async function dimensions() {
    await session.waitFor('Object.keys(window.__splitSizes).length===4');
    const values=await session.eval('window.__splitSizes');
    for(const [slot,value] of Object.entries(values)) {
      const deadline=Date.now()+5000;let actual;
      do {
        actual=tmux(['display-message','-p','-t',value.pane,'#{pane_width} #{pane_height}']).split(' ').map(Number);
        // tmux reserves one row for its enabled status line.
        if(actual[0]===value.cols && actual[1]===value.rows-1)break;
        await cdp.sleep(50);
      } while(Date.now()<deadline);
      report.ok('native PTY dimensions agree with visible terminal '+slot,actual[0]===value.cols && actual[1]===value.rows-1,{actual,requested:value});
    }
    return values;
  }
  await cdp.sleep(150);
  const before=await dimensions();
  await drag(session,.4);
  await cdp.sleep(150);
  const after=await dimensions();
  report.ok('top real terminal widths resize while bottom stays fixed',after[0].cols<before[0].cols&&after[1].cols>before[1].cols&&[2,3].every(i=>after[i].cols===before[i].cols),{before,after});
  await drag(session,.35,1); await cdp.sleep(150);
  const bottom=await dimensions();
  report.ok('bottom real terminal widths resize while top stays fixed',bottom[2].cols<after[2].cols&&bottom[3].cols>after[3].cols&&[0,1].every(i=>bottom[i].cols===after[i].cols),{after,bottom});

  // Maximize via the existing control, then prove hidden terminal preservation.
  const split=await session.eval(geometry);
  await session.click('#header-0 .cell-maximize');
  await session.waitFor("document.querySelector('.grid').classList.contains('maximized')");
  await cdp.sleep(150);
  const maximized=await session.eval(geometry),sizes=await session.eval('window.__splitSizes');
  report.ok('maximize hides separator and spans both tracks',maximized.handles.every(h=>h.width===0)&&Math.abs(maximized.cells[0].width-maximized.grid.width)<2,maximized);
  report.ok('maximized-away terminals keep their previous dimensions',[1,2,3].every(i=>sizes[i].cols===bottom[i].cols&&sizes[i].rows===bottom[i].rows),sizes);
  await session.click('#header-0 .cell-maximize');await settle(session);await cdp.sleep(150);
  const restored=await session.eval(geometry);
  report.ok('restore returns previous column split',Math.abs(restored.cells[0].width-split.cells[0].width)<2&&restored.saved===split.saved,restored);
  await dimensions();
  await session.click('#header-2 .cell-maximize'); await settle(session);
  const lowerMax=await session.eval(geometry);
  report.ok('bottom-row maximize spans the full outer grid',Math.abs(lowerMax.cells[2].width-lowerMax.grid.width)<2&&Math.abs(lowerMax.cells[2].height-lowerMax.grid.height)<2&&lowerMax.handles.every(h=>h.width===0),lowerMax);
  await session.click('#header-2 .cell-maximize'); await settle(session); await cdp.sleep(150);
  await dimensions();

  // A chat surface hides its xterm without hiding its cell.
  await session.click('#header-1 [data-mode="chat"]');
  await cdp.sleep(100);
  const hiddenBefore=await session.eval('window.__splitSizes[1]');
  await drag(session,.6);await cdp.sleep(150);
  const hiddenAfter=await session.eval('window.__splitSizes[1]');
  report.ok('hidden chat terminal receives no resize',hiddenBefore.cols===hiddenAfter.cols&&hiddenBefore.rows===hiddenAfter.rows,{hiddenBefore,hiddenAfter});
  await session.click('#header-1 [data-mode="terminal"]');await cdp.sleep(150);await dimensions();
  const gridBeforeDashboard=await session.eval(geometry);
  await session.eval("document.querySelector('[data-flag=\"dashboards\"] .settings-switch')?.click()");
  await session.click('.view-btn[data-view="dashboards"]');await settle(session);
  report.ok('dashboard hides grid and divider',(await session.eval(geometry)).grid.width===0);
  await session.click('.view-btn[data-view="chats"]');await settle(session);await cdp.sleep(150);
  report.ok('dashboard return restores split',Math.abs((await session.eval(geometry)).cells[0].width-gridBeforeDashboard.cells[0].width)<2);
  await dimensions();

  report.ok('all emitted terminal sizes remain positive',await session.eval('window.__splitResizes.every(r=>r.cols>0&&r.rows>0)'));

  // Close means detach, using the existing control; the owned tmux session lives
  // until the runner's finally block removes it.
  await session.click('#header-3 .cell-close');
  await session.waitFor("!document.querySelector('#cell-3 .xterm')");
  report.ok('existing detach control works and session survives',tmux(['has-session','-t','='+fixtures[3].sessionName])==='');
}

async function runGridSplitWidths({session,report}) {
  await drag(session,.7); await drag(session,.3,1);
  const initial=await session.eval(geometry),saved=initial.saved,savedBottom=initial.savedBottom;
  for(const density of ['comfortable','compact']) {
    await session.eval(`document.querySelector('.settings-row[data-setting="density"] [data-value="${density}"]').click()`);
    await session.send('Emulation.setDeviceMetricsOverride',{width:720,height:700,deviceScaleFactor:1,mobile:false});await settle(session);
    const g=await session.eval(geometry);
    report.ok('720px '+density+' has feasible 220px columns',g.cells.every(c=>c.width>=219.5)&&g.saved===saved&&g.savedBottom===savedBottom,g);
    const controls=await session.eval(`(() => {
      const cell=document.getElementById('cell-1'),button=cell.querySelector('.cell-close');
      button.focus();
      const b=button.getBoundingClientRect(),c=cell.getBoundingClientRect();
      return {cellScroll:cell.scrollLeft,buttonLeft:b.left,buttonRight:b.right,cellLeft:c.left,cellRight:c.right,hit:document.elementFromPoint(b.x+b.width/2,b.y+b.height/2)===button};
    })()`);
    report.ok('narrow header controls are reachable without scrolling terminal content',controls.cellScroll===0&&controls.buttonLeft>=controls.cellLeft&&controls.buttonRight<=controls.cellRight&&controls.hit,controls);
  }
  await session.send('Emulation.setDeviceMetricsOverride',{width:500,height:700,deviceScaleFactor:1,mobile:false});await settle(session);
  const narrow=await session.eval(geometry);
  report.ok('infeasible minimum falls back to equal positive halves without losing preference',narrow.cells.every(c=>c.width>0&&Math.abs(c.width-narrow.cells[0].width)<2)&&narrow.saved===saved&&narrow.savedBottom===savedBottom,narrow);
  report.ok('narrow grid introduces no page overflow',await session.eval('document.documentElement.scrollWidth<=innerWidth'));
  await session.send('Emulation.clearDeviceMetricsOverride');await settle(session);
  const restored=await session.eval(geometry);
  report.ok('wide viewport restores preferred fraction',Math.abs(restored.cells[0].width/(restored.grid.width-restored.gap)-saved)<.005&&Math.abs(restored.cells[2].width/(restored.grid.width-restored.gap)-savedBottom)<.005,restored);
}
module.exports={geometry,mouse,drag,doubleTap,settle,runGridSplit,runGridSplitTerminals,runGridSplitWidths};
