'use strict';
const assert = require('node:assert/strict');

const geometry = `(() => {
  const grid = document.querySelector('.grid');
  const rect = el => { const r = el.getBoundingClientRect(); return { x:r.x,y:r.y,width:r.width,height:r.height }; };
  const handle = grid.querySelector('.grid-col-resizer');
  return { grid:rect(grid), cells:[0,1,2,3].map(i=>rect(document.getElementById('cell-'+i))),
    handle:handle ? rect(handle):null, gap:parseFloat(getComputedStyle(grid).columnGap),
    sidebar:rect(document.querySelector('.sidebar')), saved:JSON.parse(localStorage.getItem('pentacle.settings.v1')||'{}').appearance?.gridColSplit };
})()`;

async function mouse(session, type, x, y, extra = {}) {
  return session.send('Input.dispatchMouseEvent', { type,x,y,button:'left',...extra });
}
async function settle(session) {
  await session.eval('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
}
async function drag(session, fraction) {
  const g = await session.eval(geometry);
  assert.ok(g.handle && g.handle.width > 0, 'slot-column divider is rendered');
  const x = g.handle.x + g.handle.width/2, y = g.grid.y + g.grid.height/2;
  const target = g.grid.x + (g.grid.width-g.gap)*fraction + g.gap/2;
  await mouse(session,'mousePressed',x,y,{clickCount:1,buttons:1});
  for (let i=1;i<=5;i++) await mouse(session,'mouseMoved',x+(target-x)*i/5,y,{buttons:1});
  await mouse(session,'mouseReleased',target,y,{clickCount:1,buttons:0});
  await settle(session);
  return session.eval(geometry);
}
async function doubleTap(session, touch=false) {
  for(let i=0;i<2;i++) {
    const g=await session.eval(geometry),x=g.handle.x+g.handle.width/2,y=g.grid.y+g.grid.height/2;
    if(touch) {
      await session.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y,id:1}]});
      await session.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
    } else {
      await mouse(session,'mousePressed',x,y,{clickCount:i+1,buttons:1});
      await mouse(session,'mouseReleased',x,y,{clickCount:i+1,buttons:0});
    }
  }
  await settle(session);
}
async function runGridSplit(ctx) {
  const {session,report}=ctx;
  await session.waitFor("document.readyState==='complete' && !!window.cc && !!document.querySelector('.grid')");
  const before=await session.eval(geometry);
  report.ok('slot-column divider is rendered',!!before.handle && before.handle.width>=10,before);
  const after=await drag(session,.65);
  report.ok('drag changes both column tracks and preserves rows/sidebar',
    after.cells[0].width>before.cells[0].width+20 && Math.abs(after.cells[0].width-after.cells[2].width)<2 &&
    Math.abs(after.cells[1].width-after.cells[3].width)<2 && Math.abs(after.cells[0].height-before.cells[0].height)<2 &&
    after.sidebar.width===before.sidebar.width && Math.abs(after.cells[0].width+after.cells[1].width+after.gap-after.grid.width)<2,after);
  report.ok('committed split saved',Math.abs(after.saved-.65)<.005,after.saved);
  await session.eval('window.__splitOldDocument=true');
  await session.send('Page.reload');
  await session.waitFor("!window.__splitOldDocument && document.readyState==='complete' && !!document.querySelector('.grid-col-resizer')?.getAttribute('aria-valuenow')");
  await settle(session);
  const reload=await session.eval(geometry);
  report.ok('split restored after real renderer reload',Math.abs(reload.cells[0].width-after.cells[0].width)<2,reload);
  await doubleTap(session);
  const reset=await session.eval(geometry);
  report.ok('mouse double-click resets to equal columns and persists',Math.abs(reset.cells[0].width-reset.cells[1].width)<2 && reset.saved===.5,reset);
  await drag(session,.62);
  await doubleTap(session,true);
  const touch=await session.eval(geometry);
  report.ok('touch double-tap resets and persists',Math.abs(touch.cells[0].width-touch.cells[1].width)<2 && touch.saved===.5,touch);
  for (const f of [-1,2]) {
    const bound=await drag(session,f);
    report.ok('column bounds '+f,bound.cells[0].width>=219.5 && bound.cells[1].width>=219.5,bound);
  }
  await session.eval("document.querySelector('.grid-col-resizer').focus()");
  for(const [key,code] of [['Home',36],['End',35],['Enter',13],['ArrowRight',39],['ArrowLeft',37]]) {
    await session.send('Input.dispatchKeyEvent',{type:'keyDown',key,code:key,windowsVirtualKeyCode:code});
    await session.send('Input.dispatchKeyEvent',{type:'keyUp',key,code:key,windowsVirtualKeyCode:code});
    await settle(session);
    const g=await session.eval(geometry);
    const want=key==='Home'?220/(g.grid.width-g.gap):key==='End'?1-220/(g.grid.width-g.gap):key==='ArrowRight'?.52:.5;
    report.ok('keyboard '+key+' saves and updates ARIA',Math.abs(g.saved-want)<.001&&await session.eval(`document.querySelector('.grid-col-resizer').getAttribute('aria-valuenow')===${JSON.stringify(String(Math.round(want*100)))}`),g.saved);
  }
  await drag(session,.65);
  return {before,after};
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
  report.ok('all four real terminal widths follow their resized column',[0,2].every(i=>after[i].cols<before[i].cols)&&[1,3].every(i=>after[i].cols>before[i].cols),{before,after});

  // Maximize via the existing control, then prove hidden terminal preservation.
  const split=await session.eval(geometry);
  await session.click('#header-0 .cell-maximize');
  await session.waitFor("document.querySelector('.grid').classList.contains('maximized')");
  await cdp.sleep(150);
  const maximized=await session.eval(geometry),sizes=await session.eval('window.__splitSizes');
  report.ok('maximize hides separator and spans both tracks',maximized.handle.width===0&&Math.abs(maximized.cells[0].width-maximized.grid.width)<2,maximized);
  report.ok('maximized-away terminals keep their previous dimensions',[1,2,3].every(i=>sizes[i].cols===after[i].cols&&sizes[i].rows===after[i].rows),sizes);
  await session.click('#header-0 .cell-maximize');await settle(session);await cdp.sleep(150);
  const restored=await session.eval(geometry);
  report.ok('restore returns previous column split',Math.abs(restored.cells[0].width-split.cells[0].width)<2&&restored.saved===split.saved,restored);
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

  // Close means detach, using the existing control; the owned tmux session lives
  // until the runner's finally block removes it.
  await session.click('#header-3 .cell-close');
  await session.waitFor("!document.querySelector('#cell-3 .xterm')");
  report.ok('existing detach control works and session survives',tmux(['has-session','-t','='+fixtures[3].sessionName])==='');
}

async function runGridSplitWidths({session,report}) {
  await drag(session,.7);
  const saved=(await session.eval(geometry)).saved;
  for(const density of ['comfortable','compact']) {
    await session.eval(`document.querySelector('.settings-row[data-setting="density"] [data-value="${density}"]').click()`);
    await session.send('Emulation.setDeviceMetricsOverride',{width:720,height:700,deviceScaleFactor:1,mobile:false});await settle(session);
    const g=await session.eval(geometry);
    report.ok('720px '+density+' has feasible 220px columns',g.cells[0].width>=219.5&&g.cells[1].width>=219.5&&g.saved===saved,g);
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
  report.ok('infeasible minimum falls back to equal positive halves without losing preference',Math.abs(narrow.cells[0].width-narrow.cells[1].width)<2&&narrow.cells[0].width>0&&narrow.saved===saved,narrow);
  report.ok('narrow grid introduces no page overflow',await session.eval('document.documentElement.scrollWidth<=innerWidth'));
  await session.send('Emulation.clearDeviceMetricsOverride');await settle(session);
  const restored=await session.eval(geometry);
  report.ok('wide viewport restores preferred fraction',Math.abs(restored.cells[0].width/(restored.grid.width-restored.gap)-saved)<.005,restored);
}
module.exports={geometry,mouse,drag,doubleTap,settle,runGridSplit,runGridSplitTerminals,runGridSplitWidths};
