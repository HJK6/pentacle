'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createWakeDelivery } = require('../renderer/wake_delivery');
const tagged = (text,id='a') => `[pentacle-input {"origin":"room_mic","conversation_id":"conversation-${id}"}]\n\n${text}`;
const row = (id='home:current', extra={}) => ({ stream_id:id, host: 'home', role:'assistant', status:'open', pane_status:'pane_alive', ...extra });
function fixture() {
  const f = { config:{features:{mic:true,assistantRole:'assistant'},mic:{wakeTargetHost: 'home'},chatStream:{}},
    status:{mode:'on',wake:{enabled:true,generation:'one',pending_count:1}},
    state:{connected:true,sessions:[row()]},claims:[{id:'a',generation:'one',text:'hello'}], sends:[], apiCalls:[], notes:[] };
  f.api = async (method,path) => {
    f.apiCalls.push(path);
    if(path==='/status') return structuredClone(f.status);
    const claim=f.claims.shift() || null;
    return {claim:claim ? {...claim,conversation_id:'conversation-'+claim.id} : null};
  };
  f.make = () => createWakeDelivery({config:f.config, getState:async()=>structuredClone(f.state),
    api:(...args)=>f.api(...args),sendTurn:(...args)=>f.sends.push(args),onStatus:text=>f.notes.push(text)});
  f.helper=f.make(); return f;
}
test('one send for concurrent/repeated polling, identical text in distinct captures remains valid',async()=>{
  const f=fixture();
  await Promise.all([f.helper.tick(f.status),f.helper.tick(f.status)]);
  await f.helper.tick(f.status);
  assert.deepEqual(f.sends,[['home:current',tagged('hello')]]);
  f.claims.push({id:'b',generation:'one',text:'hello'});
  await f.helper.tick(f.status);
  assert.equal(f.sends.length,2);
});
test('no wake work consumes nothing',async()=>{
  const f=fixture(); f.status.wake.pending_count=0;
  await f.helper.tick(f.status); assert.equal(f.apiCalls.length,0);
});
for(const variant of ['disconnected','none','multiple','closed','wrong-role','wrong-host','unconfigured','snapshot-disabled','mic-disabled']) {
  test(`${variant} defers without claiming`,async()=>{
    const f=fixture();
    if(variant==='disconnected') f.state.connected=false;
    if(variant==='none') f.state.sessions=[];
    if(variant==='multiple') f.state.sessions.push(row('home:second'));
    if(variant==='closed') f.state.sessions[0].closed_at='today';
    if(variant==='wrong-role') f.state.sessions[0].role='Assistant';
    if(variant==='wrong-host') f.state.sessions[0].host='other';
    if(variant==='unconfigured') delete f.config.mic.wakeTargetHost;
    if(variant==='snapshot-disabled') f.config.chatStream.snapshot=false;
    if(variant==='mic-disabled') f.config.features.mic=false;
    await f.helper.tick(f.status); assert.equal(f.apiCalls.length,0); assert.equal(f.sends.length,0);
  });
}
test('handoff while claiming holds once, then sends to fresh unique assistant regardless of focus',async()=>{
  const f=fixture(); const original=f.api;
  f.api=async(...args)=>{const result=await original(...args); if(args[1]==='/wake/claim') f.state.sessions=[row('home:new')]; return result;};
  await f.helper.tick(f.status); assert.equal(f.sends.length,0);
  f.status.wake.pending_count=0;
  await f.helper.tick(f.status);
  assert.deepEqual(f.sends,[['home:new',tagged('hello')]]);
  assert.equal(f.apiCalls.filter(x=>x==='/wake/claim').length,1);
});
test('Off during claim cancels work even if an old On status returns',async()=>{
  const f=fixture(); const original=f.api;
  f.api=async(...args)=>{const result=await original(...args); if(args[1]==='/wake/claim') f.helper.cancel(); return result;};
  await f.helper.tick(f.status); await f.helper.tick(f.status);
  assert.equal(f.sends.length,0);
});
test('Off then On generation change cancels held work',async()=>{
  const f=fixture(); const original=f.api;
  f.api=async(...args)=>{const result=await original(...args); if(args[1]==='/wake/claim') f.state.connected=false; return result;};
  await f.helper.tick(f.status);
  f.state.connected=true; f.status.wake.generation='two'; f.status.wake.pending_count=0;
  await f.helper.tick(f.status); assert.equal(f.sends.length,0);
});
test('last service readback Off prevents send',async()=>{
  const f=fixture(); const original=f.api;
  f.api=async(...args)=>{const result=await original(...args); if(args[1]==='/status') result.mode='off'; return result;};
  await f.helper.tick(f.status); assert.equal(f.sends.length,0);
});
test('uncertain send is never retried with a second sendTurn',async()=>{
  const f=fixture(); let count=0;
  f.helper=createWakeDelivery({config:f.config,getState:async()=>f.state,api:(...args)=>f.api(...args),sendTurn:()=>{count++;throw Error('lost response');},onStatus:()=>{}});
  await f.helper.tick(f.status); await f.helper.tick(f.status);
  assert.equal(count,1);
});
test('fresh On lifecycle enables wake after initial Off observation',async()=>{
  const f=fixture();
  await f.helper.tick({mode:'off',wake:{enabled:true,generation:'off'}});
  await f.helper.tick(f.status);
  assert.equal(f.sends.length,1);
});
test('claimed message waits for reconnection without a second claim',async()=>{
  const f=fixture(); const original=f.api;
  f.api=async(...args)=>{const result=await original(...args);if(args[1]==='/wake/claim') f.state.connected=false;return result;};
  await f.helper.tick(f.status);
  f.state={connected:true,sessions:[row('home:new')]};
  await f.helper.tick(f.status);
  assert.deepEqual(f.sends,[['home:new',tagged('hello')]]);
  assert.equal(f.apiCalls.filter(p=>p==='/wake/claim').length,1);
});
test('lost claim response never reconstructs or replays its text',async()=>{
  const f=fixture(); const original=f.api;
  f.api=async(...args)=>{const result=await original(...args);return args[1]==='/wake/claim' ? null : result;};
  await f.helper.tick(f.status); await f.helper.tick(f.status);
  assert.equal(f.sends.length,0);
  assert.match(f.notes.join(' '),/unconfirmed/);
});

test('answer-ready text agrees with echo-tail readiness flag',()=>{
  const f=fixture();f.status.local_actions={enabled:true,pending:{state:'waiting',ready:false}};
  assert.equal(f.helper.message(f.status),'Getting ready for your answer…');
  f.status.local_actions.pending.ready=true;
  assert.equal(f.helper.message(f.status),'Ready for your answer — say over to finish.');
});
function pinnedFixture(){const f=fixture();delete f.config.features.assistantRole;f.config.mic.wakeTargetStreamId='home:direct';f.state.sessions=[row('home:direct',{role:'lead'}),row('home:unrelated',{role:'lead'})];f.helper=f.make();return f;}
test('explicit wake identity bypasses ambiguous lead roles without selecting other leads',async()=>{const f=pinnedFixture();await f.helper.tick(f.status);assert.deepEqual(f.sends,[['home:direct',tagged('hello')]]);});
test('explicit identity follows unique forward handoff lineage',async()=>{const f=pinnedFixture();f.state.sessions=[row('home:direct',{role:'lead',closed_at:'closed'}),row('home:next',{role:'lead',handoff_from_stream_id:'home:direct',closed_at:'closed'}),row('home:current',{role:'lead',handoff_from_stream_id:'home:next'}),row('home:other',{role:'lead'})];await f.helper.tick(f.status);assert.deepEqual(f.sends,[['home:current',tagged('hello')]]);});
for(const kind of ['missing','closed','ambiguous','wrong-host','cycle'])test(`explicit identity fails closed: ${kind}`,async()=>{
 const f=pinnedFixture();f.status.wake.pending_count=0;
 if(kind==='missing')f.state.sessions=[row('home:other',{role:'lead'})];
 if(kind==='closed')f.state.sessions=[row('home:direct',{role:'lead',closed_at:'closed'})];
 if(kind==='ambiguous')f.state.sessions.push(row('home:other-successor',{role:'lead',handoff_from_stream_id:'home:direct'}));
 if(kind==='wrong-host')f.state.sessions[0].host='other';
 if(kind==='cycle')f.state.sessions=[row('home:direct',{role:'lead',handoff_from_stream_id:'home:next'}),row('home:next',{closed_at:'closed',handoff_from_stream_id:'home:direct'})];
 await f.helper.tick(f.status);assert.equal(f.apiCalls.length,0);assert.equal(f.sends.length,0);assert.match(f.notes.join(' '),/no wake target/i);
});
test('explicit handoff during claim holds capture and sends once to successor',async()=>{
 const f=pinnedFixture();const original=f.api;
 f.api=async(...args)=>{const r=await original(...args);if(args[1]==='/wake/claim')f.state.sessions=[row('home:direct',{closed_at:'closed'}),row('home:new',{role:'lead',handoff_from_stream_id:'home:direct'})];return r;};
 await f.helper.tick(f.status);assert.equal(f.sends.length,0);f.status.wake.pending_count=0;await f.helper.tick(f.status);await f.helper.tick(f.status);assert.deepEqual(f.sends,[['home:new',tagged('hello')]]);assert.equal(f.apiCalls.filter(p=>p==='/wake/claim').length,1);
});

test('already-open wake client follows unrelated durable rebind while old target remains open',async()=>{
 const f=pinnedFixture();
 f.state.sessions=[
   row('home:direct',{role:'lead',session_generation:'gen-a'}),
   row('home:unrelated',{role:'lead',session_generation:'gen-b'}),
 ];
 let binding={ok:true,source:'env',stream_id:'home:direct',generation:'gen-a',revision:0};
 f.helper=createWakeDelivery({config:f.config,getState:async()=>structuredClone(f.state),
   getBinding:async()=>binding,api:(...args)=>f.api(...args),
   sendTurn:(...args)=>f.sends.push(args),onStatus:text=>f.notes.push(text)});
 await f.helper.tick(f.status);
 assert.deepEqual(f.sends,[['home:direct',tagged('hello')]]);
 f.claims.push({id:'second',generation:'one',text:'after rebind'});
 binding={ok:true,source:'durable',stream_id:'home:unrelated',generation:'gen-b',revision:1};
 await f.helper.tick(f.status);
 assert.deepEqual(f.sends,[['home:direct',tagged('hello')],['home:unrelated',tagged('after rebind','second')]]);
});

test('durable binding waits when its matching generation has a dead pane',async()=>{
 const f=pinnedFixture();
 f.state.sessions=[row('home:direct',{session_generation:'gen-a',pane_status:'pane_dead'})];
 f.helper=createWakeDelivery({config:f.config,getState:async()=>structuredClone(f.state),
   getBinding:async()=>({ok:true,source:'durable',stream_id:'home:direct',generation:'gen-a',revision:1}),
   api:(...args)=>f.api(...args),sendTurn:(...args)=>f.sends.push(args),onStatus:text=>f.notes.push(text)});
 await f.helper.tick(f.status);
 assert.equal(f.apiCalls.length,0);
 assert.equal(f.sends.length,0);
});

test('binding lookup failure holds wake instead of using stale local pin',async()=>{
 const f=pinnedFixture();
 f.helper=createWakeDelivery({config:f.config,getState:async()=>structuredClone(f.state),
   getBinding:async()=>({ok:false,error_code:'assistant_binding_lookup_failed'}),
   api:(...args)=>f.api(...args),sendTurn:(...args)=>f.sends.push(args),
   onStatus:text=>f.notes.push(text)});
 await f.helper.tick(f.status);
 assert.deepEqual(f.sends,[]);
 assert.equal(f.apiCalls.includes('/wake/claim'),false);
});

test('explicit unconfigured and older daemon fallback work without a local pin',async()=>{
 for(const result of [{ok:true,source:'unconfigured'}, {ok:false,error_code:'unsupported_in_v2'}]){
   const f=fixture();
   f.helper=createWakeDelivery({config:f.config,getState:async()=>structuredClone(f.state),
     getBinding:async()=>result,api:(...args)=>f.api(...args),
     sendTurn:(...args)=>f.sends.push(args)});
   await f.helper.tick(f.status);
   assert.deepEqual(f.sends,[['home:current',tagged('hello')]]);
 }
});

test('bound generation mismatch holds wake',async()=>{
 const f=pinnedFixture();
 f.state.sessions[0].session_generation='old';
 f.helper=createWakeDelivery({config:f.config,getState:async()=>structuredClone(f.state),
   getBinding:async()=>({ok:true,source:'durable',stream_id:'home:direct',generation:'new'}),
   api:(...args)=>f.api(...args),sendTurn:(...args)=>f.sends.push(args)});
 await f.helper.tick(f.status);
 assert.deepEqual(f.sends,[]);
});
test('an answer claim delivers a header carrying answer_to',async()=>{
  const f=fixture();
  f.claims=[{id:'ans',generation:'one',text:'the answer',answer_to:'line-123'}];
  await f.helper.tick(f.status);
  assert.deepEqual(f.sends,[['home:current','[pentacle-input {"origin":"room_mic","conversation_id":"conversation-ans","answer_to":"line-123"}]\n\nthe answer']]);
});
test('mic status surfaces the answer window and silent mode',async()=>{
  const f=fixture();
  const base={mode:'on',wake:{enabled:true,generation:'one'}};
  assert.equal(f.helper.message({...base,speaker:{answer_window:{waiting:true,ready:true}}}),'Ready for Zephyr’s answer — say over.');
  assert.equal(f.helper.message({...base,speaker:{answer_window:{waiting:true,ready:false}}}),'Getting ready for Zephyr’s answer…');
  assert.equal(f.helper.message({...base,capture_origin:'answer'}),'Recording your answer for Zephyr — say over.');
  assert.equal(f.helper.message({...base,speaker:{silent:true}}),'Silent mode is on — Zephyr answers in chat. Say “Hey Zephyr” to speak.');
});


for (const [supplied, name] of [[undefined, 'Zephyr'], ['   ', 'Zephyr'], [' Aster ', 'Aster']]) {
  test(`voice guidance uses effective mic name ${name}`, () => {
    const f = fixture();
    f.status.assistant_name = supplied;
    f.status.wake.pending_count = 0;
    assert.equal(f.helper.message(f.status), `Say “Hey ${name}” to speak; “over” to send.`);
    f.status.local_actions = { enabled: true, wake_policy: 'separate' };
    assert.equal(f.helper.message(f.status), `Hey ${name} for chat; Hey Pentacle for actions. Say over to finish.`);
    f.status.local_actions.wake_policy = 'shared';
    assert.equal(f.helper.message(f.status), `Hey ${name} for chat or local actions. Say over to finish.`);
    f.status.speaker = { answer_window: { waiting: true, ready: true } };
    assert.equal(f.helper.message(f.status), `Ready for ${name}’s answer — say over.`);
    const { computeMicPanelView } = require('../renderer/mic-state');
    assert.equal(computeMicPanelView({ status: f.status }).answerWindow.text, `Ready for ${name}’s answer — say over.`);
    f.status.speaker = { silent: true };
    assert.equal(f.helper.message(f.status), `Silent mode is on — ${name} answers in chat. Say “Hey ${name}” to speak.`);
  });
}

// Binding lookup cache and last-good grace (idle note path only).
const durable = (stream_id, generation, revision = 1) => ({ ok: true, source: 'durable', stream_id, generation, revision });
function bindingFixture() {
  const f = pinnedFixture();
  f.status.wake.pending_count = 0;
  f.state.sessions = [
    row('home:direct', { role: 'lead', session_generation: 'gen-a' }),
    row('home:unrelated', { role: 'lead', session_generation: 'gen-b' }),
  ];
  f.clock = 1000;
  f.calls = 0;
  f.result = durable('home:direct', 'gen-a');
  f.getBinding = async () => {
    f.calls += 1;
    if (f.result instanceof Error) throw f.result;
    return f.result;
  };
  f.helper = createWakeDelivery({ config: f.config, getState: async () => structuredClone(f.state),
    getBinding: (...args) => f.getBinding(...args), now: () => f.clock,
    api: (...args) => f.api(...args), sendTurn: (...args) => f.sends.push(args), onStatus: text => f.notes.push(text) });
  f.normal = 'Say “Hey Zephyr” to speak; “over” to send.';
  f.noTarget = 'No wake target: connect a unique current assistant.';
  f.shown = () => f.helper.message(f.status);
  return f;
}
const overloaded = { ok: false, error_code: 'server_overloaded' };
const lookupFailed = { ok: false, error_code: 'assistant_binding_lookup_failed' };

test('one overloaded lookup after a success leaves the idle wake note on the normal prompt', async () => {
  const f = bindingFixture();
  await f.helper.tick(f.status);
  assert.equal(f.shown(), f.normal);
  f.clock += 3000;
  f.result = overloaded;
  await f.helper.tick(f.status);
  assert.equal(f.calls, 2);
  assert.equal(f.shown(), f.normal);
});

test('idle lookups reuse a successful binding for under 2 s', async () => {
  const f = bindingFixture();
  await f.helper.tick(f.status);
  f.clock += 1999;
  await f.helper.tick(f.status);
  await f.helper.tick(f.status);
  assert.equal(f.calls, 1);
  f.clock += 2;
  await f.helper.tick(f.status);
  assert.equal(f.calls, 2);
});

test('the grace ends 15 s after the last success and the note then appears', async () => {
  const f = bindingFixture();
  await f.helper.tick(f.status);
  f.result = overloaded;
  f.clock += 14000;
  await f.helper.tick(f.status);
  assert.equal(f.shown(), f.normal);
  f.clock += 2000;
  await f.helper.tick(f.status);
  assert.equal(f.shown(), f.noTarget);
});

for (const [name, failure] of [['server_overloaded', overloaded], ['assistant_binding_lookup_failed', lookupFailed],
  ['thrown error', new Error('timed_out')], ['thrown error with another code', Object.assign(new Error('x'), { error_code: 'other' })]]) {
  test(`transient ${name} within the grace keeps the last good binding`, async () => {
    const f = bindingFixture();
    await f.helper.tick(f.status);
    f.result = failure;
    f.clock += 5000;
    await f.helper.tick(f.status);
    assert.equal(f.calls, 2);
    assert.equal(f.shown(), f.normal);
  });
}

for (const [name, failure] of [['non-transient ok:false code', { ok: false, error_code: 'assistant_binding_unauthorized' }],
  ['ok:false without a code', { ok: false }], ['malformed success', { ok: true, source: 'other' }]]) {
  test(`${name} gets no grace`, async () => {
    const f = bindingFixture();
    await f.helper.tick(f.status);
    f.result = failure;
    f.clock += 5000;
    await f.helper.tick(f.status);
    assert.equal(f.shown(), f.noTarget);
  });
}

test('unsupported_in_v2 gets no grace and falls back to the local pin', async () => {
  const f = bindingFixture();
  f.state.sessions = [row('home:other', { role: 'lead', session_generation: 'gen-a' })];
  f.result = durable('home:other', 'gen-a');
  await f.helper.tick(f.status);
  assert.equal(f.shown(), f.normal);
  f.result = { ok: false, error_code: 'unsupported_in_v2' };
  f.clock += 5000;
  await f.helper.tick(f.status);
  assert.equal(f.shown(), f.noTarget);
  f.result = Object.assign(new Error('old daemon'), { error_code: 'unsupported_in_v2' });
  f.clock += 5000;
  await f.helper.tick(f.status);
  assert.equal(f.shown(), f.noTarget);
});

test('a real loss of the target still shows the note despite the cache', async () => {
  const f = bindingFixture();
  await f.helper.tick(f.status);
  f.state.sessions[0].status = 'closed';
  f.clock += 500;
  await f.helper.tick(f.status);
  assert.equal(f.shown(), f.noTarget);
});

test('invalidateBinding drops the cache and the grace', async () => {
  const f = bindingFixture();
  await f.helper.tick(f.status);
  f.helper.invalidateBinding();
  await f.helper.tick(f.status);
  assert.equal(f.calls, 2);
  f.helper.invalidateBinding();
  f.result = overloaded;
  f.clock += 1000;
  await f.helper.tick(f.status);
  assert.equal(f.shown(), f.noTarget);
});

test('cancel and a new wake generation drop the cache and the grace', async () => {
  const f = bindingFixture();
  await f.helper.tick(f.status);
  f.helper.cancel();
  f.status.wake.generation = 'two';
  f.result = overloaded;
  f.clock += 1000;
  await f.helper.tick(f.status);
  assert.equal(f.calls, 2);
  assert.equal(f.shown(), f.noTarget);
});

test('a new wake generation alone drops the cache', async () => {
  const f = bindingFixture();
  await f.helper.tick(f.status);
  f.status.wake.generation = 'two';
  await f.helper.tick(f.status);
  assert.equal(f.calls, 2);
});

test('the mic leaving on drops the cache', async () => {
  const f = bindingFixture();
  await f.helper.tick(f.status);
  await f.helper.tick({ mode: 'off', wake: { enabled: true, generation: 'one' } });
  f.status.wake.generation = 'two';
  await f.helper.tick(f.status);
  assert.equal(f.calls, 2);
});

for (const drop of ['invalidateBinding', 'cancel']) {
  test(`a lookup in flight across ${drop} does not write the cache`, async () => {
    const f = bindingFixture();
    let release;
    f.getBinding = () => new Promise(resolve => { f.calls += 1; release = () => resolve(durable('home:direct', 'gen-a')); });
    const first = f.helper.tick(f.status);
    await new Promise(resolve => setImmediate(resolve));
    f.helper[drop]();
    if (drop === 'cancel') f.status.wake.generation = 'two';
    release();
    await first;
    f.getBinding = async () => { f.calls += 1; return overloaded; };
    f.clock += 500;
    await f.helper.tick(f.status);
    assert.equal(f.calls, 2);
    assert.equal(f.shown(), f.noTarget);
  });
}

test('a rebind with the old seat still open never sends to the cached binding', async () => {
  const f = bindingFixture();
  await f.helper.tick(f.status);
  f.result = durable('home:unrelated', 'gen-b', 2);
  f.status.wake.pending_count = 1;
  f.clock += 100;
  await f.helper.tick(f.status);
  assert.equal(f.calls, 3);
  assert.deepEqual(f.sends, [['home:unrelated', tagged('hello')]]);
});

test('a rebind followed by a failing fresh lookup holds the message instead of sending to the cached binding', async () => {
  const f = bindingFixture();
  await f.helper.tick(f.status);
  f.result = overloaded;
  f.status.wake.pending_count = 1;
  f.clock += 100;
  await f.helper.tick(f.status);
  assert.equal(f.calls, 2);
  assert.deepEqual(f.sends, []);
  assert.equal(f.apiCalls.includes('/wake/claim'), false);
});

test('a lookup that fails after the claim holds the message and never sends to the cached binding', async () => {
  const f = bindingFixture();
  await f.helper.tick(f.status);
  const original = f.api;
  f.api = async (...args) => { const r = await original(...args); if (args[1] === '/wake/claim') f.result = overloaded; return r; };
  f.status.wake.pending_count = 1;
  f.clock += 100;
  await f.helper.tick(f.status);
  assert.deepEqual(f.sends, []);
  assert.equal(f.shown(), 'Wake message waiting for the current assistant.');
});

test('a held claim reads the binding fresh on both lookups', async () => {
  const f = bindingFixture();
  f.status.wake.pending_count = 1;
  const original = f.api;
  f.api = async (...args) => {
    const r = await original(...args);
    if (args[1] === '/wake/claim') f.result = durable('home:unrelated', 'gen-b', 2);
    return r;
  };
  await f.helper.tick(f.status);
  assert.deepEqual(f.sends, []);
  assert.equal(f.calls, 2);
  f.status.wake.pending_count = 0;
  await f.helper.tick(f.status);
  assert.equal(f.calls, 4);
  assert.deepEqual(f.sends, [['home:unrelated', tagged('hello')]]);
});

test('a fresh delivery lookup refreshes the idle cache', async () => {
  const f = bindingFixture();
  f.status.wake.pending_count = 1;
  await f.helper.tick(f.status);
  assert.equal(f.sends.length, 1);
  const calls = f.calls;
  f.status.wake.pending_count = 0;
  f.clock += 1000;
  await f.helper.tick(f.status);
  assert.equal(f.calls, calls);
});
