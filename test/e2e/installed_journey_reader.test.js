'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { installedJourney } = require('./lib/installed_journey_scenario');

async function readHistory(mode) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'installed-reader-'));
  const text = 'sassy sessions  preserve\tspaces\nand letter s';
  const fixtures = [0, 1, 2].map(i => ({ stream_id: 'fixture:reader-' + i, host: 'fixture', session_name: 'reader-' + i }));
  const before = [{ daemon_seq:1, kind:'USER', text, attachments:[] },
    { daemon_seq:2, kind:'ASSIST_TEXT', text:'fixture reply', attachments:[] }];
  let selected;
  const expressions = [];
  const session = {
    async send(method, args) {
      if (method === 'Page.navigate') selected = JSON.parse(new URL(args.url).searchParams.get('pentacle-chat-popout')).stream_id;
      return { identifier:'owned-hook' };
    },
    async waitFor() {},
    async eval(expression) {
      if (expression.includes('injected:window.__PENTACLE_CONFIG__')) return { injected:'accepted', served:'accepted' };
      if (expression.startsWith('window.__dashboardReloadMarker')) return true;
      if (expression.includes('window.cc.requestStreamEvents(')) return { ok:true, received:2, exhausted:true };
      if (expression.includes('getState().events')) { assert.ok(selected); return before; }
      if (expression.includes('document.querySelectorAll')) {
        expressions.push(expression);
        const nodes = mode === 'missing' ? [] : Array(mode === 'duplicate' ? 2 : 1).fill({ textContent:text });
        return vm.runInNewContext(expression, { document:{ querySelectorAll:() => nodes } });
      }
      if (expression.includes('optimisticSends')) return [];
      throw new Error('unexpected scenario evaluation: ' + expression);
    },
  };
  const descriptor = { phase:'history', url:'https://fixture.invalid', fixtures,
    expected_build_id:'accepted', before:{ 0:before, 1:before }, inputs:{ 0:[text], 1:[text] } };
  try {
    const run = installedJourney({ session, cdp:{ sleep:async () => {} }, report:{ dir, ok() {} } }, descriptor);
    if (mode === 'once') {
      await run;
      assert.equal(expressions.length, 2);
      for (const expression of expressions) assert.ok(expression.includes('replace(/\\s+/g'), 'serialized browser reader retains whitespace escape');
    } else {
      await assert.rejects(run, error => error.message.includes('rendered logical input appears once')
        && error.actual === (mode === 'duplicate' ? 2 : 0));
    }
  } finally {
    fs.rmSync(dir, { recursive:true, force:true });
  }
}

test('actual serialized history reader preserves letter s and normalizes whitespace', () => readHistory('once'));
test('actual serialized history reader refuses duplicate logical input', () => readHistory('duplicate'));
test('actual serialized history reader refuses missing logical input', () => readHistory('missing'));

// Execute the actual serialized report reader in the repository's existing DOM fixture.
const { JSDOM } = require('jsdom');
const { CdpSession } = require('./lib/cdp');
async function readReport(mode) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'installed-report-reader-'));
  const summary = 'Owned report summary', reportId = 'owned-report-id', assetId = 'owned-asset';
  const owner = 'fixture:report', stream = mode === 'wrong-stream' ? 'fixture:foreign' : owner;
  const slot = mode === 'wrong-slot' ? 1 : 0;
  const dom = new JSDOM('<div id="cell-0"><div id="header-0"></div><input class="slot-chat-compose-input"><div class="slot-asset-view-body"></div></div><div id="cell-1"><div id="header-1"></div></div>', { runScripts:'outside-only', url:'https://fixture.invalid/' });
  const w = dom.window, document = w.document, context = dom.getInternalVMContext();
  w.HTMLElement.prototype.getBoundingClientRect = () => ({ width:100, height:20 });
  w.HTMLElement.prototype.scrollIntoView = () => {};
  class Socket {
    constructor() { this.listeners = {}; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    send(bytes) {
      const f = JSON.parse(bytes);
      const reply = mode === 'good' && f.id === 7
        ? { id:f.id, ok:true, result:{ asset:{ asset_id:assetId, session_key:{ stream_id:owner }, body:'owned report payload' } } }
        : { id:f.id, ok:false, error:{ code:'fixture_asset_error', message:'owned body failed' } };
      if (mode !== 'rpc-pending') for (const fn of this.listeners.message || []) fn({ data:JSON.stringify(reply) });
    }
  }
  w.WebSocket = Socket;
  w.cc = { getBuild:async () => ({ buildId:'accepted' }) };
  w.__PENTACLE_CONFIG__ = { buildId:'accepted' };
  w.PentacleChatStore = { getState:() => ({ events:[], sessions:[] }) };
  const addTab = () => {
    const tab = document.createElement('button'); tab.className = 'slot-asset-tab';
    Object.assign(tab.dataset, { assetId, streamId:stream, assetKey:'session:owned-asset' });
    tab.textContent = summary + (['header-decoy','wrong-stream','wrong-slot','duplicate'].includes(mode) ? ' ' + reportId : ''); // Whole-cell text is deliberately an unsafe oracle.
    tab.addEventListener('click', () => {
      tab.classList.add('active');
      if (mode === 'good' || mode === 'wrong-stream' || mode === 'wrong-slot' || mode === 'duplicate') {
        document.querySelector('.slot-asset-view-body').innerHTML = '<div class="slot-asset-report"><h1>' + summary + '</h1><h2>' + reportId + '</h2></div>';
      }
      const socket = new w.WebSocket('wss://fixture.invalid/cc');
      socket.send(JSON.stringify({ id:7, method:'chat-stream:asset-get', args:[{ stream_id:owner, asset_id:assetId }] }));
      socket.send(JSON.stringify({ id:8, method:'auth:hello', args:[{ token:'NEVER_RETAIN_THIS' }] }));
      socket.send(JSON.stringify({ id:9, method:'chat-stream:asset-get', args:[{ stream_id:'fixture:foreign', asset_id:'foreign' }] }));
    });
    document.querySelector('#header-' + slot).appendChild(tab);
  };
  addTab(); if (mode === 'duplicate') addTab();
  let hook, clickCount = 0, captureFailed = false;
  const session = {
    consoleLines:[],
    async send(method,args) {
      if (method === 'Page.addScriptToEvaluateOnNewDocument') { hook=args.source; return { identifier:'owned-hook' }; }
      if (method === 'Page.navigate') vm.runInContext(hook, context);
      return {};
    },
    async eval(expression) {
      if (mode === 'capture-error' && !captureFailed && expression.includes('reportEvidenceSnapshot:failure')) { captureFailed=true; throw new Error('fixture diagnostic capture unavailable'); }
      return vm.runInContext(expression, context);
    },
    async waitFor(expression) { if (!await this.eval(expression)) throw new Error('fixture waitFor timeout: ' + expression); },
    async click(selector) { clickCount++; if (mode === 'lost-click') document.querySelector(selector)?.remove(); return CdpSession.prototype.click.call(this,selector); },
    async screenshot(file) { fs.writeFileSync(file,'owned fixture screenshot'); },
  };
  let error;
  try {
    try { await installedJourney({ session, report:{ dir, ok() {} } }, { phase:'report', url:'https://fixture.invalid', expected_build_id:'accepted', fixtures:[{ stream_id:owner,host:'fixture',session_name:'report' }], asset_id:assetId, summary, report_id:reportId }); } catch(e) { error=e; }
    return { error, clickCount, observation:JSON.parse(fs.readFileSync(path.join(dir,'installed-observation.json'),'utf8')) };
  } finally { dom.window.close(); fs.rmSync(dir,{ recursive:true,force:true }); }
}

test('actual report reader checks lost click dispatch and retains false receipt', async () => {
  const r=await readReport('lost-click'); assert.ok(r.error,'lost click cannot pass on header text');
  assert.equal(r.observation.report.click_result,false);
  assert.match(r.observation.primary_error,/click/i);
});
for (const mode of ['wrong-slot','wrong-stream','duplicate']) test('actual report reader refuses '+mode+' target before click',async()=>{
  const r=await readReport(mode); assert.ok(r.error); assert.equal(r.clickCount,0); assert.ok(r.observation.report.snapshots.length);
});
test('actual report reader refuses header decoy without a rendered body',async()=>{
  const r=await readReport('header-decoy'); assert.ok(r.error,'header text cannot qualify report rendering');
});
for (const mode of ['rpc-error','rpc-pending']) test('actual report reader retains failure DOM and scoped '+mode+' evidence',async()=>{
  const r=await readReport(mode); assert.ok(r.error); const e=r.observation.report;
  assert.ok(e.last_sample && e.last_sample.bodyDOM!==undefined); assert.ok(e.snapshots.some(s=>s.label==='failure'));
  assert.ok(JSON.stringify(e).includes('chat-stream:asset-get'));
  assert.equal(JSON.stringify(e).includes('NEVER_RETAIN_THIS'),false); assert.equal(JSON.stringify(e).includes('fixture:foreign'),false);
  assert.ok(e.rpc.some(x=>mode==='rpc-error'?x.state==='error':x.state==='pending'));
});
test('actual report reader preserves primary failure when diagnostic capture fails',async()=>{
  const r=await readReport('capture-error');assert.ok(r.error);assert.match(r.observation.primary_error,/waitFor timeout/);assert.ok(r.observation.report.capture_errors.length);
});
test('actual report reader accepts the selected visible report body',async()=>{
  const r=await readReport('good');assert.equal(r.error,undefined);assert.equal(r.observation.report.click_result,true);assert.equal(r.observation.report.last_sample.checks.pass,true);
  assert.ok(r.observation.report.rpc.some(x=>x.state==='success' && x.response.body==='owned report payload'));
});
