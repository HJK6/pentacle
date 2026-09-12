const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.env.VOICE_RENDERER_SOURCE || 'renderer/app.js', 'utf8');
function harness() {
  const sent = []; let tick; let target = 'original';
  const backend = { mode: 'on', on_listener_state: 'LISTENING', on_last_copied: '' };
  const context = { console, setTimeout() {},
    setInterval(fn) { tick = fn; return 1; }, clearInterval() {},
    state: { terminals: { 0: {} } },
    CONFIG: { mic: { alwaysOnEnabled: true } },
    document: { querySelector: () => ({ classList: { add() {}, remove() {}, toggle() {} } }), getElementById: () => ({}) },
    showToast() {}, alwaysOnVisible: () => true, useRemoteClipboardMode: () => false,
    chatControlTargetForSlot: () => ({ hostId: 'host', sessionName: target, streamSession: { stream_id: target } }),
    micApi: async (method, path) => {
      if (path === '/status') return {...backend};
      if (path === '/copy/start') { backend.on_last_copied = ''; backend.on_listener_state = 'CAPTURING'; return {ok: true}; }
      if (path === '/copy/stop') { backend.on_listener_state = 'LISTENING'; return {ok:true, copied:backend.on_last_copied}; }
    },
    sendProgrammaticInput: async (slot,text) => {sent.push([target,text]); return true;},
    window: {PentacleChatStore: {sendTurn: async (id,text) => {sent.push([id,text]); return true;}}, cc:{}},
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('const voiceState ='), source.indexOf('// Wire up voice buttons')), context);
  return {sent, backend, start: () => context.toggleVoiceRecord(0), tick: () => tick(), switch: () => { target='other'; }};
}
test('identical consecutive recordings each send once', async () => {
 const h=harness();
 for(let i=0;i<2;i++){ await h.start(); h.backend.on_last_copied='Please check again.';h.backend.on_listener_state='LISTENING';await h.tick(); }
 assert.deepEqual(h.sent,[['original','Please check again.'],['original','Please check again.']]);
});
test('over completion and overlapping poll ticks deliver only once',async()=>{
 const h=harness();await h.start();h.backend.on_last_copied='A message';h.backend.on_listener_state='LISTENING';await Promise.all([h.tick(),h.tick()]);assert.equal(h.sent.length,1);
});
test('changing the visible chat does not redirect a recording',async()=>{
 const h=harness();await h.start();h.switch();h.backend.on_last_copied='Original destination';h.backend.on_listener_state='LISTENING';await h.tick();assert.deepEqual(h.sent,[['original','Original destination']]);
});
test('empty manual stop never replays the previous recording',async()=>{
 const h=harness();await h.start();h.backend.on_last_copied='Previous';h.backend.on_listener_state='LISTENING';await h.tick();await h.start();await h.start();assert.deepEqual(h.sent,[['original','Previous']]);
});
test('manual stop racing a completion poll sends once',async()=>{
 const h=harness();await h.start();h.backend.on_last_copied='Final words';await Promise.all([h.tick(),h.start()]);assert.deepEqual(h.sent,[['original','Final words']]);
});

test('manual click just after over and before next poll sends the completed message',async()=>{
 const h=harness();await h.start();h.backend.on_last_copied='Already finished';h.backend.on_listener_state='LISTENING';await h.start();await h.tick();assert.deepEqual(h.sent,[['original','Already finished']]);
});
