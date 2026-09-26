#!/usr/bin/env node
'use strict';

// Disposable, actual /cc transport journey: one open 120-row chat, then a
// different stream fills the host's 500-event ring before the browser socket
// drops and reconnects. The selected stream must backfill from its own history.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { startDaemon, targetId } = require('./scenarios/assistant_direct');
const { getFreePort } = require('./lib/disposable_web_daemon_config');
const cdp = require('./lib/cdp');

const out = path.resolve(process.argv[2] || fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-history-transport-')));
const productRoot = path.resolve(process.env.PENTACLE_GATE_PRODUCT_ROOT || path.join(__dirname, '../..'));
const legacy = process.env.PENTACLE_GATE_EXPECT_LEGACY === '1';
const browserBinary = process.env.PENTACLE_TEST_BROWSER || process.env.PENTACLE_CHROME || 'google-chrome';
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

function p95(values) {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.ceil(sorted.length * 0.95) - 1];
}

async function measureAppends(page, daemon, label, count = 20) {
  const samples = [];
  let baselineRows = null;
  const longTasks = await page.eval(`(() => {
    window.__appendLongTasks = [];
    window.__appendTaskObserver?.disconnect();
    window.__appendTaskObserver = new PerformanceObserver(list => {
      for (const entry of list.getEntries()) window.__appendLongTasks.push(entry.duration);
    });
    window.__appendTaskObserver.observe({entryTypes:['longtask']});
    return true;
  })()`);
  assert.equal(longTasks, true);
  for (let i = 0; i < count; i++) {
    const marker = `${label} ${i}`;
    const before = await page.eval(`(() => {
      const scroll=document.querySelector('#cell-0 .slot-chat-scroll');
      scroll.scrollTop=Math.round((scroll.scrollHeight-scroll.clientHeight)/2);
      const list=document.querySelector('#cell-0 .slot-chat-list');
      const marker=${JSON.stringify(marker)};
      window.__appendSample={start:performance.now(),done:null};
      const observer=new MutationObserver(() => {
        if (list.textContent.includes(marker)) {
          window.__appendSample.done=performance.now(); observer.disconnect();
        }
      });
      observer.observe(list,{childList:true,subtree:true,characterData:true});
      return {top:scroll.scrollTop,rows:document.querySelectorAll('#cell-0 .slot-chat-row').length};
    })()`);
    assert.ok(before.top > 0, 'fixture must be scrollable');
    if (baselineRows === null) baselineRows = before.rows;
    const sentAt = Date.now();
    daemon.appendTarget(marker);
    await page.waitFor('window.__appendSample?.done !== null', {timeoutMs:10000});
    const after = await page.eval(`(() => ({
      top:document.querySelector('#cell-0 .slot-chat-scroll').scrollTop,
      elapsedMs:window.__appendSample.done-window.__appendSample.start,
      doneEpochMs:performance.timeOrigin+window.__appendSample.done,
      rows:document.querySelectorAll('#cell-0 .slot-chat-row').length
    }))()`);
    samples.push({marker, before, after, hostToDomMs:after.doneEpochMs-sentAt});
    assert.ok(Math.abs(after.top-before.top) <= 3, 'mid-history scroll anchor preserved');
    if (!legacy) assert.ok(after.rows >= baselineRows && after.rows <= baselineRows + count,
      `visible history stays bounded while unread grows: ${JSON.stringify({before,after})}`);
  }
  const tasks = await page.eval('window.__appendLongTasks.slice()');
  const stats = { samples, p95BrowserMs:p95(samples.map(s=>s.after.elapsedMs)),
    p95HostToDomMs:p95(samples.map(s=>s.hostToDomMs)), longTasksMs:tasks };
  fs.writeFileSync(path.join(out, `${label.replace(/[^a-z0-9]+/gi, '-')}.json`), JSON.stringify(stats, null, 2));
  if (!legacy) {
    assert.ok(stats.p95BrowserMs <= 100, `${label} browser append p95 <=100ms`);
    assert.ok(stats.p95HostToDomMs <= 100, `${label} host-to-DOM append p95 <=100ms`);
    assert.ok(tasks.filter(ms=>ms>200).length < 2, `${label} has no repeated >200ms long task`);
  }
  return stats;
}

async function run() {
  fs.mkdirSync(out, { recursive: true, mode: 0o700 });
  const daemon = await startDaemon({ artifactsDir: out });
  daemon.seedHistoryPressure();
  let web, browser, page;
  try {
    process.env.PENTACLE_CONFIG = daemon.configFile;
    web = await require(path.join(productRoot, 'server')).main(['--profile', daemon.configFile, '--bind', '127.0.0.1', '--port', '0']);
    let browserConnections = 0;
    web.wss.on('connection', () => { browserConnections++; });
    const debugPort = await getFreePort();
    browser = spawn(browserBinary, ['--headless=new', '--no-first-run', '--no-default-browser-check', '--no-sandbox',
      '--disable-gpu', '--window-size=1600,900', `--user-data-dir=${path.join(out, 'browser-profile')}`,
      `--remote-debugging-port=${debugPort}`, web.url], { stdio: 'ignore' });
    page = await cdp.connect(debugPort, { timeoutMs: 15000 });
    await page.waitFor(`document.querySelector('.session-item[data-stream-id=${JSON.stringify(targetId)}]')`, { timeoutMs: 15000 });
    await page.click(`.session-item[data-stream-id="${targetId}"]`);
    await page.click('#cell-0 .cell-view-toggle[data-mode="chat"]');
    await page.waitFor(`document.querySelectorAll('#cell-0 .slot-chat-row').length >= ${legacy ? 16 : 120}`, { timeoutMs: 15000 });
    const before = await page.eval(`(() => ({
      detail:window.PentacleChatStore.selectSessionDetail(${JSON.stringify(targetId)},{visibleCount:500,includeDraft:false}).transcriptItems.length,
      rows:document.querySelectorAll('#cell-0 .slot-chat-row').length,
      first:document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Target history 0')
    }))()`);
    assert.ok(before.detail >= 205);
    assert.equal(before.rows, legacy ? 16 : 120);
    const noise = await page.eval(`window.cc.requestStreamEvents({streamId:${JSON.stringify(daemon.noiseId)}})`);
    assert.equal(noise.ok, true);
    assert.equal(noise.count, 430);
    const pressure = await page.eval(`window.cc.getChatStreamState().then(s=>({events:s.events.length,sessions:s.sessions.length}))`);
    assert.equal(pressure.sessions, 5);
    const fetchesBefore = daemon.requests.filter(r => r.type === 'request_stream_events' && r.stream_id === targetId).length;
    assert.ok(fetchesBefore >= 1);
    daemon.setTargetHistoryReplyDelay(1800);
    for (const socket of web.wss.clients) socket.terminate();
    for (let i = 0; i < 100 && browserConnections < 2; i++) await wait(100);
    assert.ok(browserConnections >= 2, 'actual browser /cc websocket reconnected');
    for (let i = 0; i < 100 && daemon.requests.filter(r => r.type === 'request_stream_events' && r.stream_id === targetId).length <= fetchesBefore; i++) await wait(100);
    const fetchesAfter = daemon.requests.filter(r => r.type === 'request_stream_events' && r.stream_id === targetId).length;
    assert.ok(fetchesAfter > fetchesBefore, 'reconnect refetched bound stream history');
    await wait(100);
    const duringBackfill = await page.eval(`(() => ({
      detail:window.PentacleChatStore.selectSessionDetail(${JSON.stringify(targetId)},{visibleCount:500,includeDraft:false}).transcriptItems.length,
      rows:document.querySelectorAll('#cell-0 .slot-chat-row').length,
      empty:!!document.querySelector('#cell-0 .slot-chat-empty')
    }))()`);
    fs.writeFileSync(path.join(out, 'during-backfill.json'), JSON.stringify({ before, pressure,
      browserConnections, fetchesBefore, fetchesAfter, duringBackfill, productRoot }, null, 2));
    if (legacy) assert.ok(duringBackfill.detail < before.detail, 'legacy transport RED must lose older rows during delayed backfill');
    else assert.equal(duringBackfill.detail, before.detail, 'candidate keeps older rows while transport backfill is pending');
    await page.waitFor(`window.PentacleChatStore.selectSessionDetail(${JSON.stringify(targetId)},{visibleCount:500,includeDraft:false}).transcriptItems.length >= 205`, { timeoutMs: 10000 });
    const after = await page.eval(`(() => ({
      detail:window.PentacleChatStore.selectSessionDetail(${JSON.stringify(targetId)},{visibleCount:500,includeDraft:false}).transcriptItems.length,
      rows:document.querySelectorAll('#cell-0 .slot-chat-row').length,
      first:document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Target history 0'),
      last:document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes('Target history 204')
    }))()`);
    fs.writeFileSync(path.join(out, 'reconnect-observation.json'), JSON.stringify({ before, pressure,
      browserConnections, fetchesBefore, fetchesAfter, duringBackfill, after, productRoot }, null, 2));
    assert.equal(after.detail, before.detail);
    assert.equal(after.rows, legacy ? 16 : 120);
    assert.equal(after.last, true);
    // The backfill is complete here; these samples isolate live append from
    // fetch latency and retain the reader's mid-history scroll anchor.
    const append = await measureAppends(page, daemon, 'one-pane live append');
    // Exercise the same live append and scroll anchor with four occupied chat
    // panes, where the renderer has substantially more DOM work per frame.
    const otherIds = [daemon.noiseId, ...daemon.extraSessions.map(session => session.stream_id)];
    for (let index = 0; index < otherIds.length; index++) {
      await page.click(`.session-item[data-stream-id="${otherIds[index]}"]`);
      await page.click(`#cell-${index + 1} .cell-view-toggle[data-mode="chat"]`);
      await page.waitFor(`document.querySelectorAll('#cell-${index + 1} .slot-chat-row').length >= ${legacy ? 16 : 120}`, { timeoutMs: 10000 });
    }
    const fourBefore = await page.eval(`(() => ({
      rows:[0,1,2,3].map(i=>document.querySelectorAll('#cell-'+i+' .slot-chat-row').length),
      widths:[0,1,2,3].map(i=>document.querySelector('#cell-'+i)?.getBoundingClientRect().width)
    }))()`);
    assert.ok(fourBefore.rows.every(rows => rows >= (legacy ? 16 : 120)));
    assert.ok(fourBefore.widths.every(width => width > 200));
    const fourAppend = await measureAppends(page, daemon, 'four-pane live append');
    const result = { status: legacy ? 'BASELINE_RED' : 'PASS', before, pressure, browserConnections, fetchesBefore, fetchesAfter,
      duringBackfill, after,
      append, fourBefore, fourAppend,
      source: require('node:child_process').execFileSync('git', ['-C', productRoot, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim() };
    fs.writeFileSync(path.join(out, 'verdict.json'), JSON.stringify(result, null, 2));
    console.log(`${result.status} history /cc transport, append, scroll: ${out}`);
  } finally {
    page?.close();
    browser?.kill('SIGTERM');
    if (web) await web.close();
    await daemon.stop();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
