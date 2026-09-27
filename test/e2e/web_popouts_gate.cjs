#!/usr/bin/env node
'use strict';

// Disposable real-browser popout journey. Only the local scripted daemon and
// temporary browser profile are touched; no shared chat receives test input.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const http = require('node:http');
const { startDaemon, sourceId, targetId } = require('./scenarios/assistant_direct');
const { getFreePort } = require('./lib/disposable_web_daemon_config');
const cdp = require('./lib/cdp');

const out = path.resolve(process.argv[2] || fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-popouts-')));
const browserBinary = process.env.PENTACLE_TEST_BROWSER || process.env.PENTACLE_CHROME || 'google-chrome';
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function waitForTargetClosed(port, id) {
  for (let i = 0; i < 60; i++) {
    if (!(await cdp.listTargets(port)).some(t => t.id === id)) return;
    await wait(100);
  }
  throw new Error(`closed browser target ${id} remained listed`);
}

async function closeTarget(port, id) {
  await new Promise((resolve, reject) => {
    http.get(`http://127.0.0.1:${port}/json/close/${encodeURIComponent(id)}`, response => {
      response.resume(); response.on('end', resolve);
    }).on('error', reject);
  });
  await waitForTargetClosed(port, id);
}

async function realClick(page, selector) {
  await page.send('Page.bringToFront');
  const box = await page.eval(`(() => {
    const el=document.querySelector(${JSON.stringify(selector)});
    if (!el) return null;
    el.scrollIntoView({block:'center'});
    const r=el.getBoundingClientRect();
    const x=r.left+r.width/2,y=r.top+r.height/2;
    return {x,y,hit:el.contains(document.elementFromPoint(x,y)),
      hitElement:document.elementFromPoint(x,y)?.outerHTML.slice(0,300),rect:{x:r.x,y:r.y,w:r.width,h:r.height},
      toggles:[...document.querySelectorAll('#header-0 .cell-view-toggle')].map(e=>({mode:e.dataset.mode,className:e.className})),
      layerDisplay:document.querySelector('#cell-0 .slot-asset-layer') && getComputedStyle(document.querySelector('#cell-0 .slot-asset-layer')).display,
      slotClass:document.querySelector('#cell-0')?.className};
  })()`);
  assert.ok(box, `${selector} exists`);
  assert.equal(box.hit,true,`${selector} receives pointer: ${JSON.stringify(box)}`);
  await page.send('Input.dispatchMouseEvent', {type:'mousePressed',x:box.x,y:box.y,button:'left',clickCount:1});
  await page.send('Input.dispatchMouseEvent', {type:'mouseReleased',x:box.x,y:box.y,button:'left',clickCount:1});
}

async function run() {
  fs.mkdirSync(out, {recursive:true,mode:0o700});
  const daemon = await startDaemon({artifactsDir:out,includeReport:true});
  daemon.seedHistoryPressure({targetRows:205,noiseRows:430});
  let web,browser,main,chat,asset;
  try {
    process.env.PENTACLE_CONFIG = daemon.configFile;
    web = await require('../../server').main(['--profile',daemon.configFile,'--bind','127.0.0.1','--port','0']);
    const debugPort = await getFreePort();
    browser = spawn(browserBinary,['--headless=new','--no-first-run','--no-default-browser-check',
      '--no-sandbox','--disable-gpu','--window-size=1600,900',
      `--user-data-dir=${path.join(out,'browser-profile')}`,`--remote-debugging-port=${debugPort}`,web.url],{stdio:'ignore'});
    main = await cdp.connect(debugPort,{timeoutMs:15000});
    await main.waitFor(`document.querySelector('.session-item[data-stream-id=${JSON.stringify(sourceId)}]')`,{timeoutMs:15000});
    const initialPressure = require('../../main/chat_stream_client').snapshot({includeEvents:true}).events.length;
    assert.equal(initialPressure,500,'web host received bounded competing-stream daemon snapshot');
    // The scripted daemon has no tmux transport. Keep the browser slot mounted
    // while exercising chat/asset behavior; the real host PTY has its own gate.
    await main.eval(`(() => { window.cc.createPty=async()=>'%disposable-popout-pane'; window.cc.resizePty=()=>{}; })()`);
    await main.click(`.session-item[data-stream-id="${sourceId}"]`);
    await main.click('#cell-0 .cell-view-toggle[data-mode="chat"]');
    await main.waitFor(`document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Target history 204')`,{timeoutMs:15000});
    // Populate the browser's bounded reducer through ordinary UI navigation.
    // The fixture alone stores these rows server-side; without the following
    // fetches there is no competing stream traffic in the actual browser.
    for (const [slot,stream,last] of [
      [1,'mock-host:history-pressure','Pressure history 429'],
      [2,'mock-host:history-extra-1','Extra history-extra-1 history 139'],
      [3,'mock-host:history-extra-2','Extra history-extra-2 history 139'],
    ]) {
      await main.click(`.session-item[data-stream-id="${stream}"]`);
      await main.click(`#cell-${slot} .cell-view-toggle[data-mode="chat"]`);
      await main.waitFor(`document.querySelector('#cell-${slot} .slot-chat-list')?.textContent.includes(${JSON.stringify(last)})`,{timeoutMs:15000});
    }
    const pressure = await main.eval(`(() => ({
      storeKeys:Object.keys(window.PentacleChatStore?.state||{}),
      storeEvents:window.PentacleChatStore?.state?.events?.length,
      targetItems:window.PentacleChatStore?.selectSessionDetail?.('mock-host:live',{visibleCount:500,includeDraft:false})?.transcriptItems?.length,
      noiseItems:window.PentacleChatStore?.selectSessionDetail?.('mock-host:history-pressure',{visibleCount:500,includeDraft:false})?.transcriptItems?.length,
    }))()`);
    fs.writeFileSync(path.join(out,'pressure-observation.json'),JSON.stringify({pressure,
      requests:daemon.requests.map(r=>({type:r.type,stream_id:r.stream_id,streamId:r.streamId}))},null,2));
    assert.ok(pressure.storeEvents>500 && pressure.noiseItems>=430 && pressure.targetItems>=205,
      'real browser fetched target and competing streams after the bounded snapshot');
    assert.ok(daemon.requests.filter(r=>r.type==='request_stream_events').length>=4,
      'target and all competing stream histories fetched over transport');
    await main.type('#cell-0 .slot-chat-compose-input','Unsaved popout draft');
    await realClick(main,'#cell-0 .cell-chat-popout-action');
    for(let i=0;i<60;i++){
      const targets=await cdp.listTargets(debugPort);
      if(targets.some(t=>t.type==='page'&&t.url.includes('pentacle-chat-popout')))break;
      await wait(100);
    }
    chat = await cdp.connect(debugPort,{match:/pentacle-chat-popout/,excludeTargetId:main.target.id,timeoutMs:10000});
    await chat.waitFor(`document.body.classList.contains('chat-popout')`,{timeoutMs:15000});
    await chat.waitFor(`document.querySelector('#cell-0 .slot-chat-compose-input')?.value==='Unsaved popout draft'`,{timeoutMs:15000});
    try {
      await chat.waitFor(`document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Target history 204')`,{timeoutMs:5000});
    } catch (error) {
      const debug=await chat.eval(`(() => ({url:location.href,body:document.body.textContent.slice(-1000),
        list:document.querySelector('#cell-0 .slot-chat-list')?.textContent,
        context:window.PentacleWebPopoutBridge?.chatContext,
        detail:window.PentacleChatStore?.selectSessionDetail?.('mock-host:live',{visibleCount:120,includeDraft:false})?.transcriptItems?.length}))()`);
      debug.snapshot=await chat.eval(`window.cc.getChatStreamState().then(s=>({connected:s.connected,sessions:s.sessions?.map(x=>x.stream_id),events:s.events?.length}))`);
      debug.manualFetch=await chat.eval(`window.cc.requestStreamEvents({streamId:'mock-host:live'})`);
      debug.detailAfterFetch=await chat.eval(`window.PentacleChatStore?.selectSessionDetail?.('mock-host:live',{visibleCount:120,includeDraft:false})?.transcriptItems?.length`);
      await wait(250);
      debug.view=await chat.eval(`(() => ({list:document.querySelector('#cell-0 .slot-chat-list')?.textContent,
        paintedStreamId:document.querySelector('#cell-0 .slot-chat-list')?.dataset.streamId,
        label:document.querySelector('#cell-0 .cell-label')?.textContent,
        chatDisplay:document.querySelector('#cell-0 .slot-chat-scroll')?.getBoundingClientRect().height,
        mode:document.querySelector('#cell-0 .cell-view-toggle[data-mode=chat]')?.className}))()`);
      debug.requests=daemon.requests.map(r=>({type:r.type,stream_id:r.stream_id,request_id:r.request_id}));
      debug.console=chat.consoleLines.slice(-30);
      fs.writeFileSync(path.join(out,'chat-history-debug.json'),JSON.stringify(debug,null,2));
      throw error;
    }
    const opened=await chat.eval(`(() => ({url:location.href,draft:document.querySelector('#cell-0 .slot-chat-compose-input')?.value,
      transcript:document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Target history 204'),
      targetItems:window.PentacleChatStore?.selectSessionDetail?.('mock-host:live',{visibleCount:500,includeDraft:false})?.transcriptItems?.length,
      foreignPaint:/Pressure history|Extra history/.test(document.querySelector('#cell-0 .slot-chat-list')?.textContent||''),
      context:window.PentacleWebPopoutBridge?.chatContext}))()`);
    assert.equal(opened.context.stream_id,targetId);
    assert.equal(opened.transcript,true);
    assert.ok(opened.targetItems>=205,'cold child refetched the exact target history after crowd-out');
    assert.equal(opened.foreignPaint,false,'competing streams never paint in target popout');
    assert.equal(opened.url.includes('Unsaved popout draft'),false);
    await realClick(chat,'#cell-0 .cell-chat-popout-action');
    await waitForTargetClosed(debugPort,chat.target.id);
    await main.waitFor(`document.querySelector('#cell-0 .slot-chat-compose-input')?.value==='Unsaved popout draft'`,{timeoutMs:10000});
    const docked=await main.eval(`(() => ({draft:document.querySelector('#cell-0 .slot-chat-compose-input')?.value,
      transcript:document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Target history 204'),
      view:document.querySelector('#cell-0 .slot-chat-scroll')?.offsetParent!==null}))()`);
    assert.equal(docked.transcript,true);
    assert.equal(docked.view,true);
    assert.equal(daemon.requests.some(r=>r.type==='send'),false,'draft transfer made no send');
    await realClick(main,'#cell-0 .cell-chat-popout-action');
    chat = await cdp.connect(debugPort,{match:/pentacle-chat-popout/,excludeTargetId:main.target.id,timeoutMs:10000});
    await chat.waitFor(`document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Target history 204')`,{timeoutMs:10000});
    const closedChatTarget = chat.target.id;
    await closeTarget(debugPort,closedChatTarget);
    chat.close(); chat = null;
    await main.waitFor(`document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Target history 204')`,{timeoutMs:5000});
    assert.equal(daemon.requests.some(r=>r.type==='close'||r.type==='delete'),false,'ordinary chat close leaves stream live');
    await realClick(main,'#cell-0 .cell-chat-popout-action');
    chat = await cdp.connect(debugPort,{match:/pentacle-chat-popout/,excludeTargetId:main.target.id,timeoutMs:10000});
    await chat.waitFor(`document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Target history 204')`,{timeoutMs:10000});
    await realClick(chat,'#cell-0 .cell-chat-popout-action');
    await waitForTargetClosed(debugPort,chat.target.id);
    await main.waitFor(`document.querySelector('#cell-0 .slot-chat-compose-input')?.value==='Unsaved popout draft'`,{timeoutMs:10000});
    await main.waitFor(`document.querySelector('#header-0 .slot-asset-tab[data-asset-id="disposable-report"]')`,{timeoutMs:10000});
    await main.click('#header-0 .slot-asset-tab[data-asset-id="disposable-report"]');
    fs.writeFileSync(path.join(out,'asset-before-open.json'),JSON.stringify(await main.eval(`(() => ({
      tab:document.querySelector('#header-0 .slot-asset-tab[data-asset-id="disposable-report"]')?.outerHTML,
      layer:document.querySelector('#cell-0 .slot-asset-layer')?.outerHTML.slice(0,500),
      button:document.querySelector('#cell-0 .slot-asset-view-popout')?.getBoundingClientRect().toJSON(),
      viewMode:document.querySelector('#cell-0 .cell-view-toggle.active')?.dataset.mode,
      slotText:document.querySelector('#cell-0')?.textContent.slice(0,300)}))()`),null,2));
    await main.waitFor(`document.querySelector('#cell-0 .slot-asset-layer')?.textContent.includes('Disposable report body for popout proof')`,{timeoutMs:10000});
    await realClick(main,'#cell-0 .slot-asset-view-popout');
    await wait(300);
    const preAssetTargets = await cdp.listTargets(debugPort);
    fs.writeFileSync(path.join(out,'pre-asset-targets.json'),JSON.stringify({targets:preAssetTargets.map(t=>({type:t.type,url:t.url})),
      main:await main.eval(`(() => ({toast:document.querySelector('.toast')?.textContent,
        bridgeSize:window.PentacleWebPopoutBridge?.size(),button:document.querySelector('#cell-0 .slot-asset-view-popout')?.outerHTML,
        active:document.querySelector('#header-0 .slot-asset-tab.active')?.dataset.assetId}))()`)},null,2));
    asset = await cdp.connect(debugPort,{match:/asset-popout.html/,excludeTargetId:main.target.id,timeoutMs:10000});
    await asset.waitFor(`document.querySelector('#asset-popout-content')?.textContent.includes('Disposable report body for popout proof')`,{timeoutMs:10000});
    const assetOpened = await asset.eval(`(() => ({path:location.pathname,body:document.querySelector('#asset-popout-content')?.textContent,
      title:document.querySelector('#asset-popout-title')?.textContent,context:window.PentacleWebPopoutBridge?.assetContext,
      buttons:[...document.querySelectorAll('#asset-popout-content button')].map(x=>({text:x.textContent,aria:x.getAttribute('aria-label'),className:x.className}))}))()`);
    await asset.screenshot(path.join(out,'asset-opened.png'));
    assert.equal(assetOpened.path,'/asset-popout.html');
    assert.equal(assetOpened.context.stream_id,targetId);
    assert.equal(assetOpened.context.asset_id,'disposable-report');
    await asset.click('.slot-asset-report-comment-pin');
    await asset.waitFor(`document.querySelector('.slot-asset-report-comment-input')`,{timeoutMs:5000});
    await asset.type('.slot-asset-report-comment-input','Disposable popup comment');
    await asset.click('.slot-asset-report-comment-submit');
    await asset.waitFor(`document.querySelector('#asset-popout-content')?.textContent.includes('Disposable popup comment')`,{timeoutMs:5000});
    const selectionBefore = await asset.eval(`(() => { const body=document.querySelector('.slot-asset-report-block-body');
      const text=body.querySelector('*')?.firstChild || body.firstChild; const range=document.createRange();
      range.selectNodeContents(body); const selection=window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      window.__popoutSelectedBody=body; return {text:selection.toString(),sameBody:!!body}; })()`);
    assert.match(selectionBefore.text,/Disposable report body/);
    await asset.eval(`window.cc.assetCommentAdd({stream_id:'${targetId}',asset_id:'disposable-report',host:'mock-host',session_name:'live',
      section_id:'report-section',block_id:'report-block',body:'External disposable comment'})`);
    await asset.waitFor(`document.querySelector('#asset-popout-content')?.textContent.includes('External disposable comment')`,{timeoutMs:5000});
    const selectionAfter = await asset.eval(`(() => ({text:window.getSelection()?.toString(),
      sameBody:window.__popoutSelectedBody===document.querySelector('.slot-asset-report-block-body')}))()`);
    assert.equal(selectionAfter.text,selectionBefore.text,'report selection survives comment update');
    assert.equal(selectionAfter.sameBody,true,'report body node preserved');
    const review = await asset.eval(`window.cc.assetReviewSet({stream_id:'${targetId}',asset_id:'disposable-report',
      host:'mock-host',session_name:'live',review_status:'approved'})`);
    assert.equal(review.asset?.review_status,'approved','review RPC works in authenticated child');
    await asset.waitFor(`document.querySelector('#asset-popout-meta')?.textContent.includes('approved')`,{timeoutMs:5000});
    await realClick(asset,'#asset-popout-dock');
    await waitForTargetClosed(debugPort,asset.target.id);
    await main.waitFor(`document.querySelector('#cell-0 .slot-asset-layer')?.textContent.includes('Disposable report body for popout proof')`,{timeoutMs:10000});
    const assetDocked = await main.eval(`(() => ({body:document.querySelector('#cell-0 .slot-asset-layer')?.textContent,
      active:document.querySelector('#header-0 .slot-asset-tab.active')?.dataset.assetId}))()`);
    assert.equal(assetDocked.active,'disposable-report');
    assert.equal(daemon.requests.some(r=>r.type==='asset.get'),true,'asset fetched over authenticated RPC');
    await realClick(main,'#cell-0 .slot-asset-view-popout');
    asset = await cdp.connect(debugPort,{match:/asset-popout.html/,excludeTargetId:main.target.id,timeoutMs:10000});
    await asset.waitFor(`document.querySelector('#asset-popout-content')?.textContent.includes('Disposable report body for popout proof')`,{timeoutMs:10000});
    const closedAssetTarget = asset.target.id;
    await closeTarget(debugPort,closedAssetTarget);
    asset.close(); asset = null;
    await main.waitFor(`document.querySelector('#cell-0 .slot-asset-layer')?.textContent.includes('Disposable report body for popout proof')`,{timeoutMs:5000});
    assert.equal(daemon.report.review_status,'approved','ordinary asset close leaves asset state intact');
    await realClick(main,'#cell-0 .slot-asset-view-popout');
    asset = await cdp.connect(debugPort,{match:/asset-popout.html/,excludeTargetId:main.target.id,timeoutMs:10000});
    await asset.waitFor(`document.querySelector('#asset-popout-content')?.textContent.includes('Disposable report body for popout proof')`,{timeoutMs:10000});
    await asset.send('Page.reload',{ignoreCache:true});
    await asset.waitFor(`document.querySelector('#asset-popout-content')?.textContent.includes('Disposable report body for popout proof')`,{timeoutMs:10000});
    await closeTarget(debugPort,main.target.id);
    main.close(); main = null;
    await realClick(asset,'#asset-popout-dock');
    await asset.waitFor(`document.querySelector('#pentacle-popout-return')?.textContent.includes('Open Pentacle main window')`,{timeoutMs:5000});
    const openerLost = await asset.eval(`(() => ({returnHref:document.querySelector('#pentacle-popout-return')?.getAttribute('href'),
      bodyStillVisible:document.querySelector('#asset-popout-content')?.textContent.includes('Disposable report body for popout proof')}))()`);
    assert.equal(openerLost.returnHref,'/');
    assert.equal(openerLost.bodyStillVisible,true);
    const result={status:'PASS',source:require('node:child_process').execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),
      opened:{...opened,url:new URL(opened.url).pathname},docked,assetOpened,assetDocked,
      initialPressure,pressure,commentCount:daemon.comments.length,selectionBefore,selectionAfter,reviewStatus:daemon.report.review_status,
      ordinaryClose:{chatStreamStillOpen:true,assetStillReadable:true},refresh:true,openerLost,sendCount:0};
    fs.writeFileSync(path.join(out,'verdict.json'),JSON.stringify(result,null,2));
    console.log(`PASS browser chat popout: ${out}`);
  } finally {
    asset?.close();chat?.close();main?.close();browser?.kill('SIGTERM');
    if(web)await web.close();
    await daemon.stop();
  }
}
run().catch(error=>{console.error(error);process.exitCode=1});
