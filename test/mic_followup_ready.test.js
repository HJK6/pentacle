const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {JSDOM} = require('jsdom');
const source = fs.readFileSync(require.resolve('../renderer/app.js'),'utf8');
const update = source.slice(source.indexOf('function updateMicUI(data) {'), source.indexOf('async function fetchMicStatus()'));
function render(status) {
  const dom = new JSDOM(['mic-status-dot','mic-info','mic-btn-toggle','mic-btn-copy','mic-btn-meeting','mic-transcript-preview'].map(id=>`<div id="${id}"></div>`).join(''));
  const ctx = {document:dom.window.document, window:{}, CONFIG:{mic:{alwaysOnEnabled:true}},
    micState:{}, voiceState:{}, wakeDelivery:null,
    alwaysOnVisible:()=>true, shouldRenderAlwaysOnUi:()=>true, resolveLocalMicCaller:()=> 'local',
    computeBusyBannerState:()=>({visible:false}), stopRemoteClipboardPoller:()=>{}, esc:String};
  vm.createContext(ctx);vm.runInContext(update,ctx);ctx.updateMicUI(status);
  return dom.window.document.getElementById('mic-status-dot').className;
}
const base={mode:'on',on_listener_state:'LISTENING',local_actions:{enabled:true,pending:{state:'waiting',ready:true}}};
test('green answer-ready indicator appears before first utterance',()=>{
  assert.match(render(base),/active-capturing/);
});
for(const label of ['tail','expired','off','in-flight']) test(`no answer-ready green during ${label}`,()=>{
  const status=structuredClone(base);
  if(label==='tail')status.local_actions.pending.ready=false;
  if(label==='expired')status.local_actions.pending=null;
  if(label==='off')status.mode='off';
  if(label==='in-flight')status.local_actions.pending.state='in_flight';
  assert.doesNotMatch(render(status),/active-capturing/);
});
