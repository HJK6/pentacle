import test from 'node:test';
import assert from 'node:assert/strict';
import { ChatStoreController, type ChatSendBridge } from '../renderer/src/chat_store_controller';
import { renderTranscriptItemHtml } from '../renderer/src/shared_transcript_view';

const STREAM = 'peer:ordinary-chat';
const COMPOSITE = 'peer:assistant';
const flush = () => new Promise<void>(resolve => setImmediate(resolve));
function store(t: { after: (fn: () => void) => void }) {
  const controller = new ChatStoreController();
  controller.applyFrame({ type: 'snapshot', connected: true, events: [], sessions: [
    { stream_id: STREAM, host: 'peer', session_name: 'ordinary-chat', provider: 'claude' },
    { stream_id: COMPOSITE, host: 'peer', session_name: 'assistant', provider: 'composite', session_kind: 'assistant_composite' },
  ] });
  t.after(() => controller.dispose());
  return controller;
}
function echo(optimisticId: string, text: string, daemon_seq: number) {
  return { host: 'peer', provider: 'claude', stream_id: STREAM, session_name: 'ordinary-chat',
    daemon_seq, timestamp: new Date().toISOString(), kind: 'USER', text, optimistic_id: optimisticId,
    raw: { receipt_state: 'landed', receipt_delivery: 'landed' } };
}

test('new ordinary and composite sends are distinct across controllers with a frozen clock and no randomUUID', t => {
  t.mock.method(Date, 'now', () => 1789840000000);
  // getRandomValues is available on supported HTTP hosts; randomUUID need not be.
  const descriptor = Object.getOwnPropertyDescriptor(globalThis.crypto, 'randomUUID');
  Object.defineProperty(globalThis.crypto, 'randomUUID', { configurable: true, value: undefined });
  t.after(() => {
    if (descriptor) Object.defineProperty(globalThis.crypto, 'randomUUID', descriptor);
    else delete (globalThis.crypto as { randomUUID?: unknown }).randomUUID;
  });
  const ids = [store(t), store(t), store(t)].flatMap(controller => [
    controller.sendTurn(STREAM, 'identical input'),
    controller.sendTurn(STREAM, 'identical input'),
    controller.sendTurn(COMPOSITE, 'identical input'),
  ]);
  assert.equal(new Set(ids).size, ids.length, 'independent clients and reloads cannot reuse input identities');
  assert.ok(ids.every(id => typeof id === 'string' && id.length > 0));
});

for (const sameText of [false, true]) {
  test(`a stale pre-reload echo cannot settle a new ${sameText ? 'identical' : 'different'} message`, t => {
    const before = store(t);
    const oldText = 'the previous submission';
    const oldId = before.sendTurn(STREAM, oldText);
    const reloaded = store(t);
    const newText = sameText ? oldText : 'a different new submission';
    const newId = reloaded.sendTurn(STREAM, newText);
    const oldEcho = echo(oldId, oldText, 101);
    reloaded.applyFrame({ type: 'chat.event', event: oldEcho });
    assert.ok(reloaded.getState().optimisticSends?.[newId], 'old echo leaves the new send pending');
    let rows = reloaded.selectSessionDetail(STREAM).transcriptItems.filter(row => row.isUser);
    assert.equal(rows.filter(row => row.optimisticId === newId && row.text === newText).length, 1);
    assert.notEqual(newId, oldId);
    reloaded.applyFrame({ type: 'chat.event', event: echo(newId, newText, 102) });
    assert.equal(reloaded.getState().optimisticSends?.[newId], undefined, 'own echo settles the new send');
    rows = reloaded.selectSessionDetail(STREAM).transcriptItems.filter(row => row.isUser);
    assert.equal(rows.length, 2, 'each distinct submission remains visible exactly once');
    assert.equal(rows.filter(row => row.optimisticId === newId && row.text === newText).length, 1);
    assert.equal(rows.filter(row => row.optimisticId === oldId && row.text === oldText).length, 1);
  });
}

test('replayed snapshot with an old ID cannot consume a new same-text optimistic row', t => {
  const oldId = store(t).sendTurn(STREAM, 'repeat');
  const reloaded = store(t);
  const newId = reloaded.sendTurn(STREAM, 'repeat');
  reloaded.applyFrame({ type: 'snapshot', sessions: reloaded.getState().sessions, events: [echo(oldId, 'repeat', 103)] });
  assert.ok(reloaded.getState().optimisticSends?.[newId], 'history replay leaves the new submission pending');
  assert.equal(reloaded.selectSessionDetail(STREAM).transcriptItems.filter(row => row.isUser).length, 2,
    JSON.stringify({ events: reloaded.getState().events, rows: reloaded.selectSessionDetail(STREAM).transcriptItems }));
});

test('ordinary retry and reconnect keep the input identity while old RPC receipts stay isolated', async t => {
  const controller = store(t);
  const sends: Parameters<ChatSendBridge>[0][] = [];
  controller.setSendBridge(async args => { sends.push(args); return { ok: false, error: 'unavailable' }; });
  const id = controller.sendTurn(STREAM, 'retry this');
  await flush();
  assert.equal(controller.getTurnPhase(STREAM), 'idle', 'the definitive dispatch failure settles its own turn');
  assert.equal(controller.retryOptimisticSend(id), true);
  assert.equal(controller.getTurnState(STREAM)?.optimisticId, id, 'Retry owns a new pending turn');
  assert.equal(controller.getState().optimisticSends?.[id]?.status, 'dispatched');
  await flush();
  assert.equal(sends.length, 2);
  assert.equal(sends[0].optimisticId, id);
  assert.equal(sends[1].optimisticId, id);
  assert.notEqual(sends[0].requestId, sends[1].requestId);
  const reloaded = store(t);
  const freshId = reloaded.sendTurn(STREAM, 'new message');
  reloaded.applyFrame({ type: 'send.result', request_id: sends[0].requestId, delivery: 'landed' });
  assert.equal(reloaded.getState().optimisticSends?.[freshId]?.status, 'queued');
  const reconnecting = store(t);
  const replay: Parameters<ChatSendBridge>[0][] = [];
  reconnecting.setSendBridge(async args => { replay.push(args); return { ok: true }; });
  const replayId = reconnecting.sendTurn(STREAM, 'replay this after disconnect');
  await flush();
  const requestId = replay[0].requestId;
  reconnecting.applyFrame({ type: '__reconnect', generation: 0, next_generation: 1 });
  await flush();
  assert.equal(replay.length, 2, 'eligible reconnect replay is dispatched');
  assert.equal(replay[1].optimisticId, replayId);
  assert.equal(replay[1].requestId, requestId, 'reconnect preserves the current request identity');
});

test('a failure receipt preserves other pending, confirmed and provider-held work', async t => {
  const controller = store(t);
  const sends: Parameters<ChatSendBridge>[0][] = [];
  const inventory = (working: boolean, generation = 'one') =>
    controller.applyFrame({ type: 'session.inventory', sessions: controller.getState().sessions.map(session =>
      session.stream_id === STREAM ? { ...session, working, session_generation: generation } : session) });
  const fail = (index: number) =>
    controller.applyFrame({ type: 'send.result', request_id: sends[index].requestId, delivery: 'not_landed' });
  controller.setSendBridge(async args => {
    sends.push(args);
    return { ok: true, provider_queued: args.text === 'provider-held' };
  });
  inventory(false);
  const owner = controller.sendTurn(STREAM, 'owns the pending turn');
  controller.sendTurn(STREAM, 'another send');
  await flush();
  fail(1);
  assert.equal(controller.getTurnState(STREAM)?.optimisticId, owner);
  inventory(true);
  fail(0);
  assert.equal(controller.getTurnState(STREAM)?.optimisticId, owner, 'inventory confirmed independent work');
  inventory(false, 'two');
  const held = controller.sendTurn(STREAM, 'provider-held');
  await flush();
  assert.equal(controller.getState().optimisticSends?.[held]?.provider_queued, true);
  fail(2);
  assert.equal(controller.getTurnState(STREAM)?.optimisticId, held, 'provider-held work keeps its turn');
  inventory(false, 'three');
  const replacement = controller.sendTurn(STREAM, 'replacement owns the turn');
  await flush();
  fail(2);
  assert.equal(controller.getTurnState(STREAM)?.optimisticId, replacement);
});

test('inventory replaces the predecessor draft and accepts the successor draft', t => {
  const controller = store(t);
  const ordinary = controller.getState().sessions.find(session => session.stream_id === STREAM)!;
  const predecessor = { ...ordinary, session_generation: 'first' };
  const inventory = (session: typeof predecessor) =>
    controller.applyFrame({ type: 'session.inventory', sessions: [session] });
  const draft = (session: typeof predecessor, label: string) => controller.applyFrame({
    type: 'chat.event', event: { ...session, kind: 'DRAFT', text: 'fixture draft',
      timestamp: new Date().toISOString(), raw: { working: true, working_label: label } },
  });

  inventory(predecessor);
  draft(predecessor, '17s');
  inventory(predecessor);
  assert.equal(controller.getState().drafts[STREAM]?.raw?.working_label, '17s');

  const successor = { ...ordinary, session_generation: 'second', working: false,
    draft: 'successor inventory draft' };
  inventory(successor);
  assert.equal(controller.getState().drafts[STREAM], undefined);
  assert.equal(controller.getState().sessions[0].draft, successor.draft);
  draft(successor, '3s');
  assert.equal(controller.getState().drafts[STREAM]?.raw?.working_label, '3s');
});

test('definitive Retry waits for fresh provider custody before enabling Send now', async t => {
  const controller = store(t);
  const sends: Parameters<ChatSendBridge>[0][] = [];
  controller.applyFrame({ type: 'session.inventory', sessions: controller.getState().sessions.map(session =>
    session.stream_id === STREAM
      ? { ...session, session_generation: 'fixture-generation', capabilities: { queued_send_now: true } }
      : session) });
  controller.setSendBridge(async args => {
    sends.push(args);
    return { ok: true, provider_queued: sends.length === 1 };
  });
  controller.setQueuedSendNowBridge(async () => ({ ok: true }));
  const id = controller.sendTurn(STREAM, 'retry a provider-held send');
  await flush();
  assert.equal(controller.canSendQueuedNow(STREAM, id), true);
  controller.applyFrame({ type: 'send.result', request_id: sends[0].requestId, delivery: 'not_landed' });

  assert.equal(controller.retryOptimisticSend(id), true);
  await flush();
  assert.notEqual(sends[1].requestId, sends[0].requestId);
  assert.equal(sends[1].optimisticId, id);
  assert.equal(controller.sendQueuedNow(STREAM, id), false);
  const row = controller.selectSessionDetail(STREAM).transcriptItems.find(item => item.optimisticId === id);
  assert.equal(row?.sendState, 'sending');

  controller.applyFrame({ type: 'send.result', request_id: sends[1].requestId, delivery: 'landed', provider_queued: true });
  assert.equal(controller.canSendQueuedNow(STREAM, id), true);
});

test('weaker completions preserve the landed row and pending turn for the same request', async t => {
  const controller = store(t);
  const sends: Parameters<ChatSendBridge>[0][] = [];
  controller.setSendBridge(async args => { sends.push(args); return { ok: true }; });
  const id = controller.sendTurn(STREAM, 'landed before a weaker completion');
  await flush();
  const requestId = sends[0].requestId;
  controller.applyFrame({ type: 'send.result', request_id: requestId, delivery: 'landed' });
  const landed = controller.getState();
  assert.equal(landed.optimisticSends?.[id]?.status, 'acked');
  assert.equal(controller.getTurnState(STREAM)?.optimisticId, id);

  for (const frame of [
    { type: 'send.result', delivery: 'not_landed', confirmation_pending: true },
    { type: 'send.result', delivery: 'not_landed', action_committed: true },
    { type: 'send.indeterminate' },
    { type: 'send.result', delivery: 'not_landed' },
  ]) {
    assert.equal(controller.applyFrame({ ...frame, request_id: requestId }), landed);
  }
});

for (const reason of ['server_overloaded', 'Request is already pending', 'unavailable', 'socket closed']) {
 test('indeterminate reconciliation keeps original identity after '+reason, async t => {
  const controller=store(t); const sends: Parameters<ChatSendBridge>[0][]=[];
  controller.setSendBridge(async args=>{sends.push(args);return {ok:false,error:sends.length===1?'socket closed':reason};});
  const id=controller.sendTurn(STREAM,'uncertain original');await flush();
  const original=sends[0]; assert.equal(controller.getState().optimisticSends?.[id].status,'indeterminate');
  const future=Date.now()+61000;t.mock.method(Date,'now',()=>future); // age is irrelevant to the identity branch
  assert.equal(controller.retryOptimisticSend(id),true);
  assert.equal(controller.retryOptimisticSend(id),false,'concurrent click is guarded');await flush();
  assert.deepEqual(sends[1],original,'frozen original payload and IDs');
  assert.equal(controller.getState().optimisticSends?.[id].status,'indeterminate');
  assert.equal(controller.retryOptimisticSend(id),true);await flush();assert.deepEqual(sends[2],original);
  controller.applyFrame({type:'send.result',request_id:original.requestId,delivery:'landed',provider_queued:true});
  assert.equal(controller.getState().optimisticSends?.[id].status,'acked');
  assert.equal(controller.retryOptimisticSend(id),false);
  controller.sendTurn(STREAM,'uncertain original');await flush();
  assert.notEqual(sends.at(-1)?.requestId,original.requestId);assert.notEqual(sends.at(-1)?.optimisticId,id);
 });
}
test('an indeterminate original RPC still pending is joined without a second dispatch',async t=>{
 const controller=store(t);const sends: Parameters<ChatSendBridge>[0][]=[];let finish: (value:{ok:true})=>void=()=>{};
 controller.setSendBridge(args=>{sends.push(args);return new Promise(resolve=>{finish=resolve;});});
 const id=controller.sendTurn(STREAM,'original pending');await flush();
 controller.applyFrame({type:'send.indeterminate',request_id:sends[0].requestId});
 controller.retryOptimisticSend(id);controller.retryOptimisticSend(id);await flush();
 assert.equal(sends.length,1);assert.equal(controller.getState().optimisticSends?.[id].status,'indeterminate');
 controller.applyFrame({type:'send.result',request_id:sends[0].requestId,delivery:'landed'});finish({ok:true});await flush();
 assert.equal(controller.getState().optimisticSends?.[id].status,'acked');
});

test('a late receipt wins over a later rejection of the original uncertain RPC',async t=>{
 const controller=store(t);const sends: Parameters<ChatSendBridge>[0][]=[];let reject:(error:Error)=>void=()=>{};
 controller.setSendBridge(args=>{sends.push(args);return new Promise((_,fail)=>{reject=fail;});});
 const id=controller.sendTurn(STREAM,'late ACK');await flush();
 controller.applyFrame({type:'send.indeterminate',request_id:sends[0].requestId});
 assert.equal(controller.retryOptimisticSend(id),false);
 controller.applyFrame({type:'send.result',request_id:sends[0].requestId,delivery:'landed'});
 reject(new Error('socket closed'));await flush();
 assert.equal(controller.getState().optimisticSends?.[id].status,'acked');
 assert.equal(controller.retryOptimisticSend(id),false);assert.equal(sends.length,1);
});

test('unresolved row renders sending while retaining same-ID reconciliation',async t=>{
 const controller=store(t);let request='';controller.setSendBridge(async args=>{request=args.requestId;return {ok:false,error:'socket closed'};});
 const id=controller.sendTurn(STREAM,'uncertain visible action');await flush();
 assert.equal(controller.getState().optimisticSends?.[id].status,'indeterminate');
 const row=controller.selectSessionDetail(STREAM).transcriptItems.find(row=>row.optimisticId===id)!;
 assert.equal(row.sendState,'sending');const html=renderTranscriptItemHtml(row);
 assert.doesNotMatch(html,/Delivery uncertain|slot-chat-send-retry|Failed to send/);
 controller.applyFrame({type:'send.result',request_id:request,delivery:'landed'});
 const settled=controller.selectSessionDetail(STREAM).transcriptItems.find(row=>row.optimisticId===id)!;
 assert.doesNotMatch(renderTranscriptItemHtml(settled),/slot-chat-send-retry/);
});

for(const target of [STREAM,COMPOSITE])test('uncertain Retry retains attachment, reply and voice provenance: '+target,async t=>{
 const controller=store(t);const sends: Parameters<ChatSendBridge>[0][]=[];
 controller.setSendBridge(async args=>{sends.push(args);return {ok:false,error:'socket closed'};});
 const id=controller.sendTurn(target,'Read /tmp/legitimate-operator-note',[{key:'a'.repeat(64),mime:'text/plain',size:4}],{
  reply_to_message_id:'original-message',reply_to_question_id:'original-question',meta:{voice:{duration_s:0}}});
 await flush();assert.equal(controller.retryOptimisticSend(id),true);await flush();
 assert.deepEqual(sends[1],sends[0]);assert.equal(sends[1].text,'Read /tmp/legitimate-operator-note');
 assert.equal(sends[1].replyToMessageId,'original-message');assert.equal(sends[1].attachments?.length,1);
});

function webLoss(epoch: number) {
  return Object.assign(new Error('pentacle web host connection lost'), { code: 'pentacle_web_host_connection_lost', webEpoch: epoch });
}

test('exact web loss reconciles once per ready web epoch with frozen identity and ordinary intake', async t => {
  const c = store(t); const calls: Parameters<ChatSendBridge>[0][] = [];
  c.setSendBridge(async args => { calls.push(args); throw webLoss(calls.length === 1 ? 1 : 2); });
  const id = c.sendTurn(STREAM, 'urgent payload', [], { urgent: true, reply_to_message_id: 'reply-1' } as never);
  c.webHostDisconnected(1); await flush();
  assert.equal(c.getState().optimisticSends?.[id].status, 'indeterminate');
  c.webHostReady(1); await flush(); assert.equal(calls.length, 1, 'same closed epoch is not readiness');
  c.webHostReady(2); c.webHostReady(2); await flush(); await flush();
  assert.equal(calls.length, 2); assert.deepEqual(calls[1], calls[0]); assert.equal(Object.hasOwn(calls[1], 'urgent'), false);
  assert.equal(c.getState().optimisticSends?.[id].status, 'indeterminate');
  c.webHostReady(2); await flush(); assert.equal(calls.length, 2, 'no retry loop after failed reconciliation');
  c.applyFrame({ type: 'send.result', request_id: calls[0].requestId, delivery: 'landed', provider_queued: true });
  c.webHostDisconnected(2); c.webHostReady(3); await flush(); assert.equal(calls.length, 2, 'queued receipt wins');
});

test('server error text mimics and nonretryable denials do not enroll web recovery', async t => {
  for (const error of [new Error('pentacle web host connection lost'), Object.assign(new Error('denied'), { code: 'pentacle_web_host_connection_lost', webEpoch: 1 }), new Error('voice_answers_invalid')]) {
    const c = store(t); let n = 0;
    c.setSendBridge(async () => { n++; throw error; });
    const id = c.sendTurn(STREAM, 'no auto enrollment'); await flush();
    assert.equal(c.getState().optimisticSends?.[id].status, 'failed');
    c.webHostDisconnected(1); c.webHostReady(2); await flush(); assert.equal(n, 1);
  }
});

test('web recovery joins manual Retry and stale readiness cannot consume its successor', async t => {
  const c = store(t); const calls: Parameters<ChatSendBridge>[0][] = []; let settle!: (v: { ok: boolean }) => void;
  c.setSendBridge(async args => { calls.push(args); if (calls.length === 1) throw webLoss(1); return new Promise(resolve => { settle = resolve; }); });
  const id = c.sendTurn(STREAM, 'join'); c.webHostDisconnected(1); await flush();
  assert.equal(c.retryOptimisticSend(id), true); await flush();
  c.webHostReady(2); c.webHostReady(2); await flush(); assert.equal(calls.length, 2);
  c.webHostDisconnected(2); c.webHostReady(1); await flush(); assert.equal(calls.length, 2);
  c.applyFrame({ type: 'send.result', request_id: calls[0].requestId, delivery: 'landed' }); settle({ ok: true }); await flush();
  c.webHostReady(3); await flush(); assert.equal(calls.length, 2); assert.deepEqual(calls[1], calls[0]);
});

test('old invocation settlement drains eligible recovery instead of losing the ready edge', async t => {
  const c = store(t); const calls: Parameters<ChatSendBridge>[0][] = [];
  c.setSendBridge(async args => { calls.push(args); if (calls.length === 1) throw webLoss(1); return { ok: true }; });
  c.subscribe(() => { if (Object.values(c.getState().optimisticSends ?? {}).some(s => s.status === 'indeterminate')) c.webHostReady(2); });
  c.sendTurn(STREAM, 'settling original'); c.webHostDisconnected(1); await flush(); await flush();
  assert.equal(calls.length, 2, 'finally drains the still-current ready epoch'); assert.deepEqual(calls[0], calls[1]);
});

test('web recovery age bound and dispose keep uncertainty without a stale automatic write', async t => {
  const c = store(t); let n = 0; let now = Date.now(); t.mock.method(Date, 'now', () => now);
  c.setSendBridge(async () => { n++; throw webLoss(1); });
  const id = c.sendTurn(STREAM, 'old'); c.webHostDisconnected(1); await flush();
  now += 30 * 60 * 1000 + 1; c.webHostReady(2); await flush(); assert.equal(n, 1);
  assert.equal(c.getState().optimisticSends?.[id].status, 'indeterminate');
  assert.equal(c.retryOptimisticSend(id), true); await flush(); assert.equal(n, 2, 'manual keeps any-age original request');
  c.dispose(); c.webHostReady(3); await flush(); assert.equal(n, 2);
});

test('stale urgency remains ordinary on a composite and new intents remain separate', async t => {
  const c = store(t); const calls: Parameters<ChatSendBridge>[0][] = [];
  c.setSendBridge(async args => { calls.push(args); return { ok: true }; });
  assert.ok(c.sendTurn(COMPOSITE, 'stale urgent', [], { urgent: true } as never));
  c.sendTurn(STREAM, 'same'); c.sendTurn(COMPOSITE, 'same'); await flush();
  assert.equal(calls.length, 3); assert.equal(new Set(calls.map(c => c.requestId)).size, 3);
  assert.ok(calls.every(c => !Object.hasOwn(c, 'urgent')));
});

test('reconnect fetched history with original identity reconciles one row beyond the short window', async t => {
  const c = store(t); let now = Date.now(); t.mock.method(Date, 'now', () => now);
  c.setSendBridge(async () => { throw webLoss(1); });
  const id = c.sendTurn(STREAM, 'history'); await flush(); now += 61000;
  const request = c.getState().optimisticSends![id].request_id;
  c.applyFrame({ type: 'stream_events', stream_id: STREAM, events: [{ ...echo(id, 'history', 401), request_id: request }] });
  assert.equal(c.getState().optimisticSends?.[id], undefined);
  assert.equal(c.selectSessionDetail(STREAM)?.transcriptItems.filter(r => r.isUser && r.text === 'history').length, 1);
  c.webHostReady(2); await flush();
});

test('daemon-only replay cannot bypass readiness for a web-loss survivor', async t => {
  const c = store(t); let n = 0; c.setSendBridge(async () => { n++; throw webLoss(1); });
  c.sendTurn(STREAM, 'separate generations'); c.webHostDisconnected(1); await flush();
  c.applyFrame({ type: '__reconnect', generation: 0, next_generation: 1 }); await flush(); assert.equal(n, 1);
  c.webHostReady(2); await flush(); assert.equal(n, 2);
});

test('composite snapshot then fetched duplicate uses the same correlated row after optimistic pruning', async t => {
  const c = store(t); c.setSendBridge(async () => ({ ok: true }));
  const id = c.sendTurn(COMPOSITE, 'composite history'); await flush();
  const event = { ...echo(id, 'composite history', 901), stream_id: COMPOSITE, provider: 'composite', message_id: id };
  c.applyFrame({ type: 'snapshot', connected: true, sessions: c.getState().sessions, events: [event] });
  assert.equal(c.getState().optimisticSends?.[id], undefined);
  c.applyFrame({ type: 'stream_events', stream_id: COMPOSITE, events: [event] });
  assert.equal(c.selectSessionDetail(COMPOSITE)?.transcriptItems.filter(r => r.isUser && r.text === event.text).length, 1);
});

test('caller attachment and voice mutation cannot change a web recovery payload', async t => {
  const c = store(t); const calls: Parameters<ChatSendBridge>[0][] = [];
  c.setSendBridge(async args => { calls.push(structuredClone(args)); if (calls.length === 1) throw webLoss(1); return { ok: true }; });
  const attachments = [{ key: 'original-key', mime: 'image/png' }]; const meta = { voice: { duration_s: 4 } };
  c.sendTurn(STREAM, 'freeze', attachments, { meta, reply_to_message_id: 'message-1', reply_to_question_id: 'question-1' });
  c.webHostDisconnected(1); await flush(); attachments[0].key = 'mutated'; meta.voice.duration_s = 8;
  c.webHostReady(2); await flush(); assert.deepEqual(calls[1], calls[0]);
});

test('committed not_landed is failed and can be discarded locally', async t => {
  const controller = store(t);
  let request = '';
  controller.setSendBridge(async args => { request = args.requestId; return { ok: true }; });
  const id = controller.sendTurn(STREAM, 'draft stuck');
  await flush();
  assert.equal((controller as any).discardFailedOptimisticSend(id), false);
  controller.applyFrame({ type: 'send.result', request_id: request, delivery: 'not_landed', action_committed: true, confirmation_pending: true });
  assert.equal(controller.getState().optimisticSends?.[id]?.status, 'failed');
  controller.applyFrame({ type: 'send.indeterminate', request_id: request, action_committed: true });
  assert.equal(controller.getState().optimisticSends?.[id]?.status, 'failed', 'a weaker late frame cannot rearm a known failure');
  assert.equal((controller as any).discardFailedOptimisticSend(id), true);
  assert.equal(controller.getState().optimisticSends?.[id], undefined);
  assert.equal(controller.selectSessionDetail(STREAM).transcriptItems.some(row => row.optimisticId === id), false);
});

test('receipt tracking resolves landed and later failure while preserving real landed proof', async t => {
  const c = store(t); let request = ''; let receipt: any = { found: true, receipts: [] };
  c.setSendBridge(async args => { request = args.requestId; return { ok: true }; });
  (c as any).setSendReceiptBridge(async () => receipt);
  const id = c.sendTurn(STREAM, 'receipt'); await flush();
  receipt.receipts = [{ request_id: request, to_stream_id: STREAM, state: 'landed' }];
  c.applyFrame({ type: 'send.result', request_id: request, delivery: 'committed_pending_proof', action_committed: true }); await flush();
  assert.equal(c.getState().optimisticSends?.[id]?.status, 'acked');
  c.applyFrame({ type: 'send.result', request_id: request, delivery: 'not_landed' });
  assert.equal(c.getState().optimisticSends?.[id]?.status, 'acked');
});

for (const failure of ['error', 'timed_out', 'disconnect', 'unknown_session', 'scope_denied']) {
  test(`receipt ${failure} does not fail an unresolved send on reconnect`, async t => {
    const c = store(t); let request = ''; let queries = 0;
    c.setSendBridge(async args => { request = args.requestId; return { ok: true }; });
    (c as any).setSendReceiptBridge(async () => { queries++; throw new Error(failure); });
    const id = c.sendTurn(STREAM, 'pending receipt'); await flush();
    c.applyFrame({ type: 'send.indeterminate', request_id: request }); await flush();
    const before = queries;
    c.applyFrame({ type: '__reconnect', generation: 0, next_generation: 1 }); await flush(); await flush();
    assert.ok(queries > before, 'reconnect actually queried the receipt');
    assert.notEqual(c.getState().optimisticSends?.[id]?.status, 'failed');
  });
}

test('missing receipt fails only on reconnect and only after dispatch ends', async t => {
  const c = store(t); let request = ''; let finish: (v: any) => void = () => {};
  c.setSendBridge(args => { request = args.requestId; return new Promise(resolve => { finish = resolve; }); });
  (c as any).setSendReceiptBridge(async () => ({ found: false, receipts: [] }));
  const id = c.sendTurn(STREAM, 'in flight'); await flush();
  c.applyFrame({ type: 'send.indeterminate', request_id: request }); await flush();
  c.applyFrame({ type: '__reconnect', generation: 0, next_generation: 1 }); await flush();
  assert.notEqual(c.getState().optimisticSends?.[id]?.status, 'failed');
  finish({ ok: true }); await flush();
  c.applyFrame({ type: '__reconnect', generation: 1, next_generation: 2 }); await flush();
  assert.equal(c.getState().optimisticSends?.[id]?.status, 'failed');
});

test('accepted receipt remains Sending through the bound and follows a later failure', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 1800000000000 });
  const c = store(t); let request = ''; let state = 'accepted'; let queries = 0;
  c.setSendBridge(async args => { request = args.requestId; return { ok: true }; });
  (c as any).setSendReceiptBridge(async ({ streamId, requestId }: any) => {
    queries++; return { found: true, receipts: [{ to_stream_id: streamId, request_id: requestId, state }] };
  });
  const id = c.sendTurn(STREAM, 'bound receipt'); await flush();
  c.applyFrame({ type: 'send.result', request_id: request, delivery: 'committed_pending_proof' }); await flush();
  assert.equal(c.selectSessionDetail(STREAM).transcriptItems.find(row => row.optimisticId === id)?.sendState, 'sending');
  t.mock.timers.tick(19999); await flush();
  assert.equal(c.getState().optimisticSends?.[id]?.status, 'indeterminate');
  t.mock.timers.tick(1); await flush();
  assert.equal(c.getState().optimisticSends?.[id]?.status, 'acked');
  assert.ok(queries >= 2);
  state = 'not_landed';
  t.mock.timers.tick(15000); await flush();
  assert.equal(c.getState().optimisticSends?.[id]?.status, 'failed');
});

test('no receipt without reconnect leaves an answered send unresolved', async t => {
  const c = store(t); let request = ''; let queries = 0;
  c.setSendBridge(async args => { request = args.requestId; return { ok: true }; });
  (c as any).setSendReceiptBridge(async () => { queries++; return { found: false, receipts: [] }; });
  const id = c.sendTurn(STREAM, 'no receipt'); await flush();
  c.applyFrame({ type: 'send.indeterminate', request_id: request }); await flush();
  assert.equal(queries, 1);
  assert.equal(c.getState().optimisticSends?.[id]?.status, 'indeterminate');
});

for (const code of ['unknown_session', 'scope_denied', 'timed_out', 'disconnected']) {
  test(`unsuccessful ${code} receipt reply cannot mean absent`, async t => {
    const c = store(t); let request = ''; let queries = 0;
    c.setSendBridge(async args => { request = args.requestId; return { ok: true }; });
    (c as any).setSendReceiptBridge(async () => { queries++; return { ok: false, code, found: false }; });
    const id = c.sendTurn(STREAM, 'failed receipt query'); await flush();
    c.applyFrame({ type: 'send.indeterminate', request_id: request }); await flush();
    const before = queries;
    c.applyFrame({ type: '__reconnect', generation: 0, next_generation: 1 }); await flush(); await flush();
    assert.ok(queries > before, 'reconnect actually queried the receipt');
    assert.equal(c.getState().optimisticSends?.[id]?.status, 'indeterminate');
  });
}

test('daemon restart survivor is queried without a replay', async t => {
  const c = store(t); let request = ''; let sends = 0; let queries = 0;
  c.setSendBridge(async args => { request = args.requestId; sends++; return { ok: true }; });
  (c as any).setSendReceiptBridge(async () => { queries++; return { found: true, receipts: [{ request_id: request, to_stream_id: STREAM, state: 'not_landed' }] }; });
  const id = c.sendTurn(STREAM, 'restart survivor'); await flush();
  c.applyFrame({ type: '__reconnect', generation: 0, next_generation: 1, daemon_restarted: true }); await flush();
  assert.equal(sends, 1); assert.ok(queries > 0);
  assert.equal(c.getState().optimisticSends?.[id]?.status, 'failed');
});

test('a held original request is supplied to receipt reconciliation', async t => {
  const c = store(t); let request = ''; let received: any;
  c.setSendBridge(async args => { request = args.requestId; return { ok: true }; });
  (c as any).setSendReceiptBridge(async (args: any) => { received = args; return { found: false, receipts: [] }; });
  const originalRequest = { protocol: 'voice-v1', logical_id: 'fixture', target: STREAM, generation: 'gen', sequence: 1 };
  c.sendTurn(STREAM, 'voice note', [], { identity: { requestId: 'voice-request', optimisticId: 'voice-id', originalRequest } });
  await flush(); c.applyFrame({ type: 'send.indeterminate', request_id: request }); await flush();
  assert.deepEqual(received.originalRequest, originalRequest);
});

test('recovery RPC rejection cannot reopen a definitive failure frame', async t => {
  const c=store(t); let calls=0; let rejectFirst: (e: Error)=>void=()=>{}; let finish: (v:any)=>void=()=>{};
  c.setSendBridge(() => new Promise((resolve,reject) => { if (++calls===1) rejectFirst=reject; else finish=resolve; }));
  c.setSendReceiptBridge(async () => ({ok:false}));
  const id=c.sendTurn(STREAM,'failed recovery'); await flush(); const request=c.getState().optimisticSends![id].request_id;
  c.webHostDisconnected(1); rejectFirst(Object.assign(new Error('pentacle web host connection lost'),{code:'pentacle_web_host_connection_lost',webEpoch:1})); await flush();
  c.webHostReady(2); await flush(); assert.equal(calls,2);
  c.applyFrame({type:'send.result',request_id:request,delivery:'not_landed',action_committed:false,reason:'cancelled_before_submission'});
  assert.equal(c.getState().optimisticSends![id].status,'failed');
  finish({ok:false,error:'cancelled_before_submission'}); await flush();
  assert.equal(c.getState().optimisticSends![id].status,'failed');
  c.applyFrame({type:'send.result',request_id:request,delivery:'landed'});
  assert.equal(c.getState().optimisticSends![id].status,'failed','a terminal failure stays failed for that attempt');
});

for (const frame of [{type:'send.result',delivery:'committed_pending_proof',action_committed:true},{type:'send.indeterminate'}]) {
  test(`known commitment through ${frame.type} never replays via web recovery or explicit retry`, async t => {
    const c=store(t); let sends=0, queries=0; let drop:(e:Error)=>void=()=>{};
    c.setSendBridge(() => { sends++; return new Promise((_,reject)=>{drop=reject;}); });
    c.setSendReceiptBridge(async ({streamId,requestId}) => { queries++; return {found:true,receipts:[{to_stream_id:streamId,request_id:requestId,state:'accepted'}]}; });
    const id=c.sendTurn(STREAM,'committed recovery'); await flush(); const request=c.getState().optimisticSends![id].request_id;
    c.webHostDisconnected(1); drop(Object.assign(new Error('pentacle web host connection lost'),{code:'pentacle_web_host_connection_lost',webEpoch:1})); await flush();
    c.applyFrame({...frame,request_id:request}); await flush(); c.webHostReady(2); await flush();
    c.retryOptimisticSend(id); await flush();
    assert.equal(sends,1); assert.ok(queries>=1);
  });
}

test('all tracked assumed sent rows retain an explicit Sent caption', async t => {
  t.mock.timers.enable({apis:['setTimeout','Date'],now:1800000000000});
  const c=store(t); c.setSendBridge(async()=>({ok:true}));
  c.setSendReceiptBridge(async ({streamId,requestId})=>({found:true,receipts:[{to_stream_id:streamId,request_id:requestId,state:'accepted'}]}));
  const ids=[c.sendTurn(STREAM,'first followed'),c.sendTurn(STREAM,'second followed')]; await flush();
  for(const id of ids) c.applyFrame({type:'send.result',request_id:c.getState().optimisticSends![id].request_id,delivery:'accepted'});
  await flush(); t.mock.timers.tick(20000); await flush();
  for(const id of ids) assert.equal(c.selectSessionDetail(STREAM)!.transcriptItems.find(row=>row.optimisticId===id)?.receiptCaption,'sent');
});

test('receipt tracker expires exactly at the 30 minute age boundary', async t => {
  t.mock.timers.enable({apis:['setTimeout','Date'],now:1800000000000});
  const c=store(t); c.setSendBridge(async()=>({ok:true}));
  c.setSendReceiptBridge(async ({streamId,requestId})=>({found:true,receipts:[{to_stream_id:streamId,request_id:requestId,state:'accepted'}]}));
  const id=c.sendTurn(STREAM,'expiry'); await flush(); const request=c.getState().optimisticSends![id].request_id;
  c.applyFrame({type:'send.result',request_id:request,delivery:'accepted'}); await flush();
  for(const delta of [20000,15000,30000,60000,120000,240000,240000,240000,240000,240000,240000,115000]) { t.mock.timers.tick(delta); await flush(); }
  assert.equal((c as any).receiptTracking.size,0);
  c.applyFrame({type:'send.result',request_id:request,delivery:'not_landed'});
  assert.equal(c.getState().optimisticSends![id].status,'acked');
});

test('actual restart handshake routes survivor to receipt rather than resend', async t => {
  const shared = require(process.cwd() + '/main/chat_stream_client');
  const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(),'receipt-restart-'));
  const previous = process.env.PENTACLE_RUNTIME_DIR; process.env.PENTACLE_RUNTIME_DIR = dir;
  t.after(() => { if (previous === undefined) delete process.env.PENTACLE_RUNTIME_DIR; else process.env.PENTACLE_RUNTIME_DIR=previous; fs.rmSync(dir,{recursive:true,force:true}); });
  const c=store(t); let sends=0, queries=0;
  c.setSendBridge(async()=>{sends++;return {ok:true};});
  c.setSendReceiptBridge(async()=>{queries++;return {found:false,receipts:[]};});
  const id=c.sendTurn(STREAM,'actual restart'); await flush();
  const host=new (Object.getPrototypeOf(shared).constructor)(), socket={};
  Object.assign(host,{_ws:socket,_handshakeState:'hello_sent',_socketGeneration:0,_buildSha:'test',_heartbeatMs:60000,_forwardFrame:(frame:any)=>c.applyFrame(frame)});
  host._beginRestartRecovery(); host._completeHandshake(socket);
  t.after(()=>host.destroy()); await flush();
  assert.equal(sends,1); assert.ok(queries>=1); assert.equal(c.getState().optimisticSends![id].status,'failed');
});

test('successful absent receipt is a negative control for failed-query guards', async t => {
  const c=store(t); let request='', queries=0;
  c.setSendBridge(async args=>{request=args.requestId;return {ok:true};});
  c.setSendReceiptBridge(async()=>{queries++;return {ok:true,found:false,receipts:[]};});
  const id=c.sendTurn(STREAM,'successful absent'); await flush();
  c.applyFrame({type:'send.indeterminate',request_id:request}); await flush(); const before=queries;
  c.applyFrame({type:'__reconnect',generation:0,next_generation:1}); await flush(); await flush();
  assert.ok(queries>before); assert.equal(c.getState().optimisticSends![id].status,'failed');
});

for (const route of ['web','daemon']) {
 test(`a committed frame arriving before queued ${route} replay prevents the wire resend`, async t => {
  const c=store(t); let sends=0; let known=false; let drop:(e:Error)=>void=()=>{};
  c.setSendBridge(() => { sends++; return route==='web' && sends===1 ? new Promise((_,reject)=>{drop=reject;}) : Promise.resolve({ok:true}); });
  c.setSendReceiptBridge(async ({streamId,requestId})=>known ? ({found:true,receipts:[{to_stream_id:streamId,request_id:requestId,state:'accepted'}]}) : ({found:false,receipts:[]}));
  const id=c.sendTurn(STREAM,'queued replay'); await flush(); const request=c.getState().optimisticSends![id].request_id;
  if(route==='web') { c.webHostDisconnected(1); drop(Object.assign(new Error('pentacle web host connection lost'),{code:'pentacle_web_host_connection_lost',webEpoch:1})); await flush(); c.webHostReady(2); }
  else c.applyFrame({type:'__reconnect',generation:0,next_generation:1});
  known=true; c.applyFrame({type:'send.result',request_id:request,delivery:'committed_pending_proof',action_committed:true});
  await flush(); await flush(); assert.equal(sends,1,'known committed request is never replayed');
 });
}

test('older assumed Sent remains visible when later receipt proves landing', async t => {
  t.mock.timers.enable({apis:['setTimeout','Date'],now:1800000000000});
  const c=store(t); let landed='';
  c.setSendBridge(async()=>({ok:true}));
  c.setSendReceiptBridge(async ({streamId,requestId})=>({found:true,receipts:[{to_stream_id:streamId,request_id:requestId,state:requestId===landed?'landed':'accepted'}]}));
  const first=c.sendTurn(STREAM,'older proven'), second=c.sendTurn(STREAM,'newer followed'); await flush();
  for(const id of [first,second]) c.applyFrame({type:'send.result',request_id:c.getState().optimisticSends![id].request_id,delivery:'accepted'});
  await flush(); t.mock.timers.tick(20000); await flush();
  assert.equal(c.selectSessionDetail(STREAM)!.transcriptItems.find(row=>row.optimisticId===first)?.receiptCaption,'sent');
  landed=c.getState().optimisticSends![first].request_id;
  t.mock.timers.tick(15000); await flush();
  assert.equal((c as any).receiptTracking.has(landed),false,'proof was received and follow-up stopped');
  assert.equal(c.selectSessionDetail(STREAM)!.transcriptItems.find(row=>row.optimisticId===first)?.receiptCaption,'sent','proof must not erase the visible Sent caption');
});

test('older Sent remains visible after receipt follow-up expires', async t => {
  t.mock.timers.enable({apis:['setTimeout','Date'],now:1800000000000});
  const c=store(t); c.setSendBridge(async()=>({ok:true}));
  c.setSendReceiptBridge(async ({streamId,requestId})=>({found:true,receipts:[{to_stream_id:streamId,request_id:requestId,state:'accepted'}]}));
  const first=c.sendTurn(STREAM,'older expired'), second=c.sendTurn(STREAM,'newer expired'); await flush();
  for(const id of [first,second]) c.applyFrame({type:'send.result',request_id:c.getState().optimisticSends![id].request_id,delivery:'accepted'});
  await flush();
  for(const delta of [20000,15000,30000,60000,120000,240000,240000,240000,240000,240000,240000,115000]) { t.mock.timers.tick(delta); await flush(); }
  assert.equal((c as any).receiptTracking.size,0);
  for(const id of [first,second]) assert.equal(c.selectSessionDetail(STREAM)!.transcriptItems.find(row=>row.optimisticId===id)?.receiptCaption,'sent');
});

test('accepted receipt before web recovery prevents replay and explicit resubmission', async t => {
  const c=store(t); let sends=0, queries=0; let drop:(e:Error)=>void=()=>{};
  c.setSendBridge(()=>{sends++; return sends===1 ? new Promise((_,reject)=>{drop=reject;}) : Promise.resolve({ok:true});});
  c.setSendReceiptBridge(async ({streamId,requestId})=>{queries++;return {found:true,receipts:[{to_stream_id:streamId,request_id:requestId,state:'accepted'}]};});
  const id=c.sendTurn(STREAM,'receipt before ready'); await flush();
  c.webHostDisconnected(1); drop(Object.assign(new Error('pentacle web host connection lost'),{code:'pentacle_web_host_connection_lost',webEpoch:1})); await flush();
  assert.ok(queries>=1,'accepted receipt was queried before readiness');
  c.webHostReady(2); c.retryOptimisticSend(id); await flush();
  assert.equal(sends,1,'receipt proof prevents replay and explicit resend');
});

test('an early proof-bound timer retries at the bound rather than the next backoff', async t => {
  t.mock.timers.enable({apis:['setTimeout']}); let now=1800000000000;
  t.mock.method(Date,'now',()=>now);
  const c=store(t); let request='';
  c.setSendBridge(async args=>{request=args.requestId;return {ok:true};});
  c.setSendReceiptBridge(async ({streamId,requestId})=>({found:true,receipts:[{to_stream_id:streamId,request_id:requestId,state:'accepted'}]}));
  const id=c.sendTurn(STREAM,'early clock tick'); await flush();
  c.applyFrame({type:'send.result',request_id:request,delivery:'accepted'}); await flush();
  now+=19999; t.mock.timers.tick(20000); await flush();
  assert.equal(c.getState().optimisticSends![id].status,'indeterminate');
  now+=1; t.mock.timers.tick(1); await flush();
  assert.equal(c.getState().optimisticSends![id].status,'acked');
});

test('late commitment survives follow-up expiry and cannot be resent', async t => {
  t.mock.timers.enable({apis:['Date','setTimeout'],now:1800000000000});
  const c=store(t); let sends=0,queries=0;
  c.setSendBridge(async()=>{sends++;return {ok:true};});
  c.setSendReceiptBridge(async ({streamId,requestId})=>{queries++;return {found:true,receipts:[{to_stream_id:streamId,request_id:requestId,state:'accepted'}]};});
  const id=c.sendTurn(STREAM,'late commitment'); await flush();
  const request=c.getState().optimisticSends![id].request_id;
  t.mock.timers.tick(1790000); await flush();
  c.applyFrame({type:'send.result',request_id:request,delivery:'committed_pending_proof',action_committed:true}); await flush();
  assert.equal(c.getState().optimisticSends![id].status,'indeterminate');
  t.mock.timers.tick(10000); await flush(); assert.equal((c as any).receiptTracking.size,0);
  assert.equal(c.getState().optimisticSends![id].action_committed,true,'commitment is retained on the send');
  const before=queries; c.retryOptimisticSend(id); await flush();
  assert.equal(sends,1,'commitment remains authoritative after tracking ends');
  t.mock.timers.tick(10000); await flush();
  assert.equal(c.getState().optimisticSends![id].status,'acked','late commitment becomes Sent at its own proof bound');
  assert.equal(queries,before,'no receipt reads after the follow-up deadline');
});

test('own landed echoes preserve both Sent captions while row order and latest USER stay fixed', async t => {
  t.mock.timers.enable({apis:['Date','setTimeout'],now:1800000000000});
  const c=store(t); c.setSendBridge(async()=>({ok:true}));
  c.setSendReceiptBridge(async ({streamId,requestId})=>({found:true,receipts:[{to_stream_id:streamId,request_id:requestId,state:'accepted'}]}));
  const a=c.sendTurn(STREAM,'older followed'); t.mock.timers.tick(1);
  const b=c.sendTurn(STREAM,'newer followed'); await flush();
  for(const id of [a,b]) c.applyFrame({type:'send.result',request_id:c.getState().optimisticSends![id].request_id,delivery:'committed_pending_proof',action_committed:true});
  await flush(); t.mock.timers.tick(20000); await flush();
  const rows=()=>c.selectSessionDetail(STREAM)!.transcriptItems.filter(row=>row.isUser);
  const assertStable=()=>{
    const shown=rows(); assert.deepEqual(shown.map(row=>row.text),['older followed','newer followed']);
    assert.equal(shown.at(-1)?.optimisticId,b,'the latest USER stays the newer send');
    for(const id of [a,b]) assert.equal(shown.find(row=>row.optimisticId===id)?.receiptCaption,'sent',`Sent remains on ${id}`);
  };
  assertStable();
  c.applyFrame({type:'chat.event',event:echo(b,'newer followed',102)}); assertStable();
  c.applyFrame({type:'chat.event',event:echo(a,'older followed',101)}); assertStable();
  t.mock.timers.tick(1);
  c.applyFrame({type:'chat.event',event:{host:'peer',provider:'claude',stream_id:STREAM,session_name:'ordinary-chat',daemon_seq:103,timestamp:new Date().toISOString(),kind:'USER',text:'newer from another client'}});
  assert.equal(rows().at(-1)?.text,'newer from another client');
  assert.equal(rows().find(row=>row.optimisticId===a)?.receiptCaption,undefined,'a genuinely newer USER event moves the old caption');
  t.mock.timers.tick(1); const third=c.sendTurn(STREAM,'genuinely newer');
  assert.equal(rows().at(-1)?.optimisticId,third);
  assert.equal(rows().find(row=>row.optimisticId===a)?.receiptCaption,undefined,'a later USER send resumes the latest-row caption policy');
});

test('commitment first observed after expiry is retained without restarting receipt I/O', async t => {
  t.mock.timers.enable({apis:['Date','setTimeout'],now:1800000000000});
  const c=store(t); let sends=0,queries=0;
  c.setSendBridge(async()=>{sends++;return {ok:true};}); c.setSendReceiptBridge(async()=>{queries++;return {found:false,receipts:[]};});
  const id=c.sendTurn(STREAM,'commitment after expiry'); await flush(); const request=c.getState().optimisticSends![id].request_id;
  t.mock.timers.tick(1800001); await flush();
  c.applyFrame({type:'send.result',request_id:request,delivery:'committed_pending_proof'}); await flush();
  assert.equal(c.getState().optimisticSends![id].action_committed,true);
  c.retryOptimisticSend(id); c.applyFrame({type:'__reconnect',generation:0,next_generation:1}); await flush();
  assert.equal(sends,1); assert.equal(queries,0); assert.equal((c as any).receiptTracking.size,0);
  t.mock.timers.tick(20000); await flush(); assert.equal(c.getState().optimisticSends![id].status,'acked');
});

test('a definitive failure cancels the post-expiry proof timer and its retry uses a fresh uncommitted ID', async t => {
  t.mock.timers.enable({apis:['Date','setTimeout'],now:1800000000000});
  const c=store(t); const sends: string[]=[];
  c.setSendBridge(async args=>{sends.push(args.requestId);return {ok:true};});
  c.setSendReceiptBridge(async ({streamId,requestId})=>({found:true,receipts:[{to_stream_id:streamId,request_id:requestId,state:'accepted'}]}));
  const id=c.sendTurn(STREAM,'late failure'); await flush(); const first=sends[0];
  t.mock.timers.tick(1790000); c.applyFrame({type:'send.result',request_id:first,delivery:'accepted'}); await flush();
  t.mock.timers.tick(10000); await flush();
  c.applyFrame({type:'send.result',request_id:first,delivery:'not_landed'});
  assert.equal(c.getState().optimisticSends![id].status,'failed');
  assert.equal((c as any).proofBoundTimers.size,0);
  assert.equal(c.retryOptimisticSend(id),true); await flush();
  assert.equal(sends.length,2); assert.notEqual(sends[1],first);
  assert.equal(c.getState().optimisticSends![id].action_committed,false);
  t.mock.timers.tick(10000); await flush();
  assert.notEqual(c.getState().optimisticSends![id].status,'acked');
});

test('a synchronous listener cannot replay when commitment is announced', async t => {
  const c=store(t); let sends=0;
  c.setSendBridge(async()=>{sends++;return {ok:true};});
  c.setSendReceiptBridge(async()=>({ok:false}));
  const id=c.sendTurn(STREAM,'listener commitment'); await flush();
  const request=c.getState().optimisticSends![id].request_id;
  const unsubscribe=c.subscribe(()=>{const send=c.getState().optimisticSends?.[id]; if(send?.status==='indeterminate') c.retryOptimisticSend(id);});
  t.after(unsubscribe);
  c.applyFrame({type:'send.indeterminate',request_id:request}); await flush();
  assert.equal(sends,1); assert.equal(c.getState().optimisticSends![id].action_committed,true);
});

test('a duplicate commitment after expiry keeps the original proof deadline', async t => {
  t.mock.timers.enable({apis:['setTimeout']}); let now=1800000000000; t.mock.method(Date,'now',()=>now);
  const c=store(t); let sends=0,queries=0;
  c.setSendBridge(async()=>{sends++;return {ok:true};});
  c.setSendReceiptBridge(async ({streamId,requestId})=>{queries++;return {found:true,receipts:[{to_stream_id:streamId,request_id:requestId,state:'accepted'}]};});
  const id=c.sendTurn(STREAM,'duplicate at expiry'); await flush(); const request=c.getState().optimisticSends![id].request_id;
  now+=1790000; t.mock.timers.tick(1790000); await flush();
  c.applyFrame({type:'send.result',request_id:request,delivery:'accepted'}); await flush();
  now+=11000; c.applyFrame({type:'send.result',request_id:request,delivery:'accepted'}); await flush();
  const before=queries; assert.equal((c as any).receiptTracking.size,0);
  now+=9000; t.mock.timers.tick(9000); await flush();
  assert.equal(c.getState().optimisticSends![id].status,'acked'); assert.equal(sends,1); assert.equal(queries,before);
});

for (const source of ['snapshot','stream_events']) {
  test(`landed ${source} reconciliation preserves an older already-Sent row`, async t => {
    t.mock.timers.enable({apis:['Date','setTimeout'],now:1800000000000});
    const c=store(t); c.setSendBridge(async()=>({ok:true}));
    c.setSendReceiptBridge(async ({streamId,requestId})=>({found:true,receipts:[{to_stream_id:streamId,request_id:requestId,state:'accepted'}]}));
    const a=c.sendTurn(STREAM,'older loaded'); t.mock.timers.tick(1);
    const b=c.sendTurn(STREAM,'newer loaded'); await flush();
    for(const id of [a,b]) c.applyFrame({type:'send.result',request_id:c.getState().optimisticSends![id].request_id,delivery:'accepted'});
    await flush(); t.mock.timers.tick(20000); await flush();
    const events=[echo(b,'newer loaded',102),echo(a,'older loaded',101)];
    if(source==='snapshot') c.applyFrame({type:'snapshot',sessions:c.getState().sessions,events});
    else c.applyFrame({type:'stream_events',stream_id:STREAM,events});
    const rows=c.selectSessionDetail(STREAM)!.transcriptItems.filter(row=>row.isUser);
    assert.deepEqual(rows.map(row=>row.text),['older loaded','newer loaded']);
    assert.equal(rows.find(row=>row.optimisticId===a)?.receiptCaption,'sent');
  });
}

test('plain-note retry after definitive failure starts a fresh uncommitted attempt', async t => {
  const c=store(t); const requests:string[]=[];
  c.setSendBridge(async args=>{requests.push(args.requestId); return {ok:true};});
  c.setSendReceiptBridge(async()=>({ok:false}));
  const id=c.sendTurn(STREAM,'bound voice',[],{meta:{voice:{duration_s:2},voice_answers:{version:1}} as any}); await flush();
  const first=requests[0]; c.applyFrame({type:'send.result',request_id:first,delivery:'accepted'});
  c.applyFrame({type:'send.result',request_id:first,delivery:'not_landed',reason:'voice_answers_invalid'});
  assert.equal(c.getState().optimisticSends![id].status,'failed');
  assert.equal(c.sendVoiceAsPlainNote(id),true); await flush();
  assert.notEqual(requests[1],first);
  assert.equal(c.getState().optimisticSends![id].action_committed,false);
});

test('a receipt lookup started before expiry can establish commitment when its reply arrives late', async t => {
  t.mock.timers.enable({apis:['Date','setTimeout'],now:1800000000000});
  const c=store(t); let sends=0, queries=0, hold=false, finish:(v:any)=>void=()=>{};
  c.setSendBridge(async()=>{sends++;return {ok:false,error:'socket closed'};});
  c.setSendReceiptBridge(async ({streamId,requestId}) => { queries++; return hold
    ? new Promise(resolve=>{finish=resolve;})
    : {found:false,receipts:[]}; });
  const id=c.sendTurn(STREAM,'late receipt reply'); await flush(); const request=c.getState().optimisticSends![id].request_id;
  for(const delta of [20000,15000,30000,60000,120000,240000,240000,240000,240000,240000,240000,105000]) {t.mock.timers.tick(delta);await flush();}
  hold=true; c.webHostReady(2); await flush();
  t.mock.timers.tick(10000); await flush(); assert.equal((c as any).receiptTracking.size,0); const before=queries;
  finish({found:true,receipts:[{to_stream_id:STREAM,request_id:request,state:'accepted'}]}); await flush();
  assert.equal(c.getState().optimisticSends![id].action_committed,true,'valid in-flight receipt is still evidence');
  c.retryOptimisticSend(id); await flush(); assert.equal(sends,1);
  t.mock.timers.tick(20000); await flush(); assert.equal(c.getState().optimisticSends![id].status,'acked');
  assert.equal(queries,before,'late receipt creates no further I/O');
});

for (const outcome of ['not_landed','landed']) {
  test(`a fresh Retry on an aged failed row reads its own ${outcome} receipt`, async t => {
    t.mock.timers.enable({apis:['Date','setTimeout'],now:1800000000000});
    const c=store(t); const sends: string[]=[], lookups:string[]=[];
    c.setSendBridge(async ({requestId})=>{sends.push(requestId);return {ok:true};});
    c.setSendReceiptBridge(async ({streamId,requestId})=>{lookups.push(requestId);return {ok:true,found:true,receipts:[{to_stream_id:streamId,request_id:requestId,state:outcome,delivery:outcome}]};});
    const id=c.sendTurn(STREAM,'old failure'); await flush();
    const created=c.getState().optimisticSends![id].created_at; const first=sends[0];
    c.applyFrame({type:'send.result',request_id:first,delivery:'not_landed'});
    t.mock.timers.tick(1801000); assert.equal(c.retryOptimisticSend(id),true); await flush();
    assert.equal(sends.length,2); const fresh=sends[1]; assert.notEqual(fresh,first);
    assert.equal(c.getState().optimisticSends![id].created_at,created,'transcript order retains the original creation time');
    c.applyFrame({type:'send.result',request_id:fresh,delivery:'committed_pending_proof',action_committed:true}); await flush();
    assert.ok(lookups.includes(fresh),'fresh attempt must receive its own receipt read');
    assert.equal(c.getState().optimisticSends![id].status,outcome==='landed'?'acked':'failed');
    t.mock.timers.tick(20000); await flush();
    assert.equal(c.getState().optimisticSends![id].status,outcome==='landed'?'acked':'failed','the proof bound cannot override a definitive receipt');
    assert.equal(sends.length,2);
  });
}

test('discard preserves the confirmed summary when history was not loaded', async t => {
  const c=store(t); const calls:string[]=[];
  const known={last_text:'Confirmed summary',last_kind:'AGENT',last_event_at:'2020-01-15T07:00:00.000Z'};
  c.applyFrame({type:'snapshot',connected:true,events:[],sessions:c.getState().sessions.map(s=>s.stream_id===STREAM?{...s,...known}:s)});
  c.setSendBridge(async ({requestId})=>{calls.push(requestId);return {ok:true};});
  c.setSendReceiptBridge(async ()=>{calls.push('receipt');return {found:false,receipts:[]};});
  const id=c.sendTurn(STREAM,'failed before history loads'); await flush(); const request=calls[0];
  assert.equal(c.getState().sessions.find(s=>s.stream_id===STREAM)?.last_text,'failed before history loads');
  c.applyFrame({type:'send.result',request_id:request,delivery:'not_landed'});
  assert.equal(c.discardFailedOptimisticSend(id),true); await flush();
  const session=c.getState().sessions.find(s=>s.stream_id===STREAM)!;
  assert.deepEqual({last_text:session.last_text,last_kind:session.last_kind,last_event_at:session.last_event_at},known);
  assert.equal(c.getState().optimisticSends?.[id],undefined); assert.deepEqual(calls,[request]);
  const detail=c.selectSessionDetail(STREAM)!;
  assert.equal(detail.transcriptItems.some(row=>row.optimisticId===id),false);
  assert.ok(detail.transcriptItems.some(row=>row.text===known.last_text),'the next render retains the confirmed summary');
  assert.equal(c.getState().sessions.find(s=>s.stream_id===STREAM)?.last_text,known.last_text,'render selection does not lose the summary');
});

for (const confirmed of [false, true]) for (const firstDiscard of ['A', 'B'] as const) {
  test(`discard failures after reconnect (${confirmed ? 'confirmed summary' : 'empty summary'}, ${firstDiscard} first)`, async t => {
    t.mock.timers.enable({apis:['Date','setTimeout'],now:1800000000000});
    const c=store(t), calls:string[]=[];
    const known={last_text:'Confirmed server summary',last_kind:'AGENT',last_event_at:'2020-01-15T07:00:00.000Z'};
    const snapshotSessions=c.getState().sessions.map(s=>s.stream_id===STREAM && confirmed?{...s,...known}:s);
    c.applyFrame({type:'snapshot',connected:true,events:[],sessions:snapshotSessions});
    c.setSendBridge(async ({requestId})=>{calls.push(requestId);return {ok:true};});
    c.setSendReceiptBridge(async ()=>{calls.push('receipt');return {found:false,receipts:[]};});
    const a=c.sendTurn(STREAM,'failed A'); await flush();
    c.applyFrame({type:'send.result',request_id:calls[0],delivery:'not_landed'});
    c.applyFrame({type:'snapshot',connected:true,events:[],sessions:snapshotSessions});
    c.applyFrame({type:'__reconnect',generation:0,next_generation:1}); await flush();
    if (!confirmed) assert.ok(!c.getState().sessions.find(s=>s.stream_id===STREAM)?.last_text);
    t.mock.timers.tick(1000);
    const b=c.sendTurn(STREAM,'failed B'); await flush();
    c.applyFrame({type:'send.result',request_id:calls[1],delivery:'not_landed'});
    const beforeDiscard=[...calls];
    assert.equal(c.discardFailedOptimisticSend(firstDiscard==='A'?a:b),true);
    assert.equal(c.discardFailedOptimisticSend(firstDiscard==='A'?b:a),true); await flush();
    assert.deepEqual(calls,beforeDiscard,'discard sends no RPC');
    const summary=c.getState().sessions.find(s=>s.stream_id===STREAM)!;
    if (confirmed) assert.deepEqual({last_text:summary.last_text,last_kind:summary.last_kind,last_event_at:summary.last_event_at},known);
    else assert.ok(!summary.last_text,'empty server summary stays empty');
    const rows=c.selectSessionDetail(STREAM)!.transcriptItems;
    assert.equal(rows.some(row=>row.isUser),false,'no discarded send appears in the next render');
    assert.equal(rows.some(row=>row.text==='failed A'||row.text==='failed B'),false);
    assert.equal(c.getState().sessions.find(s=>s.stream_id===STREAM)?.last_text,confirmed?known.last_text:summary.last_text);
  });
}

test('a buffered send frame after dispose cannot rearm receipt work', async t => {
  t.mock.timers.enable({apis:['Date','setTimeout'],now:1800000000000});
  const c=store(t); let request='',queries=0;
  c.setSendBridge(async args=>{request=args.requestId;return {ok:true};});
  c.setSendReceiptBridge(async()=>{queries++;return {found:false,receipts:[]};});
  const id=c.sendTurn(STREAM,'buffered response'); await flush(); const before=c.getState().optimisticSends![id].status;
  c.dispose(); c.applyFrame({type:'send.result',request_id:request,delivery:'committed_pending_proof'});
  assert.equal((c as any).receiptTracking.size,0);
  t.mock.timers.tick(20000); await flush();
  assert.equal(c.getState().optimisticSends![id].status,before); assert.equal(queries,0);
});

test('discard of one same-text failure does not own a newer failed row summary', async t => {
  t.mock.timers.enable({apis:['Date','setTimeout'],now:1800000000000});
  const c=store(t), requests:string[]=[];
  const known={last_text:'Earlier confirmed',last_kind:'AGENT',last_event_at:'2020-01-01T00:00:00.000Z'};
  c.applyFrame({type:'snapshot',connected:true,events:[],sessions:c.getState().sessions.map(s=>s.stream_id===STREAM?{...s,...known}:s)});
  c.setSendBridge(async ({requestId})=>{requests.push(requestId);return {ok:true};});
  const first=c.sendTurn(STREAM,'same text'); await flush(); c.applyFrame({type:'send.result',request_id:requests[0],delivery:'not_landed'});
  t.mock.timers.tick(1000);
  const second=c.sendTurn(STREAM,'same text'); await flush(); c.applyFrame({type:'send.result',request_id:requests[1],delivery:'not_landed'});
  const newer={...c.getState().sessions.find(s=>s.stream_id===STREAM)!};
  c.discardFailedOptimisticSend(first);
  const afterFirst=c.getState().sessions.find(s=>s.stream_id===STREAM)!;
  assert.deepEqual({last_text:afterFirst.last_text,last_kind:afterFirst.last_kind,last_event_at:afterFirst.last_event_at},
    {last_text:newer.last_text,last_kind:newer.last_kind,last_event_at:newer.last_event_at});
  c.discardFailedOptimisticSend(second);
  const final=c.getState().sessions.find(s=>s.stream_id===STREAM)!;
  assert.deepEqual({last_text:final.last_text,last_kind:final.last_kind,last_event_at:final.last_event_at},known);
});

test('discard cannot overwrite a newer authoritative USER with the same text and timestamp', async t => {
  t.mock.timers.enable({apis:['Date','setTimeout'],now:1800000000000});
  const c=store(t); let request='';
  c.setSendBridge(async args=>{request=args.requestId;return {ok:true};});
  const id=c.sendTurn(STREAM,'same words'); await flush();
  c.applyFrame({type:'send.result',request_id:request,delivery:'not_landed'});
  const stamp=new Date().toISOString();
  c.applyFrame({type:'chat.event',event:{...echo('foreign-id','same words',501),timestamp:stamp}});
  const before={...c.getState().sessions.find(s=>s.stream_id===STREAM)!};
  c.discardFailedOptimisticSend(id);
  const after=c.getState().sessions.find(s=>s.stream_id===STREAM)!;
  assert.deepEqual({last_text:after.last_text,last_kind:after.last_kind,last_event_at:after.last_event_at},
    {last_text:before.last_text,last_kind:before.last_kind,last_event_at:before.last_event_at});
});

test('a fresh Retry on an aged row keeps its own reconnect replay window', async t => {
  t.mock.timers.enable({apis:['Date','setTimeout'],now:1800000000000});
  const c=store(t);const requests:string[]=[];
  c.setSendBridge(async ({requestId})=>{requests.push(requestId);return {ok:true};});
  const id=c.sendTurn(STREAM,'old row reconnect');await flush(); const original=requests[0];
  c.applyFrame({type:'send.result',request_id:original,delivery:'not_landed'});
  t.mock.timers.tick(1801000); c.retryOptimisticSend(id); await flush(); const fresh=requests[1];
  c.applyFrame({type:'__reconnect',generation:0,next_generation:1}); await flush();
  assert.notEqual(fresh,original); assert.deepEqual(requests,[original,fresh,fresh]);
});

test('discard restores a newer cached predecessor when the prior summary is older', async t => {
  t.mock.timers.enable({apis:['Date','setTimeout'],now:1800000000000});
  const c=store(t); let request='';
  const old={last_text:'Old summary',last_kind:'AGENT',last_event_at:'2020-01-01T00:00:00.000Z'};
  c.applyFrame({type:'snapshot',connected:true,events:[],sessions:c.getState().sessions.map(s=>s.stream_id===STREAM?{...s,...old}:s)});
  const previous={host:'peer',provider:'claude',stream_id:STREAM,session_name:'ordinary-chat',daemon_seq:17,
    timestamp:'2026-01-01T00:00:00.000Z',kind:'ASSIST_TEXT',text:'Cached confirmed predecessor'};
  c.applyFrame({type:'stream_events',stream_id:STREAM,events:[previous]});
  c.setSendBridge(async args=>{request=args.requestId;return {ok:true};});
  const id=c.sendTurn(STREAM,'local failed'); await flush(); c.applyFrame({type:'send.result',request_id:request,delivery:'not_landed'});
  c.discardFailedOptimisticSend(id);
  const summary=c.getState().sessions.find(s=>s.stream_id===STREAM)!;
  assert.deepEqual({last_text:summary.last_text,last_kind:summary.last_kind,last_event_at:summary.last_event_at},
    {last_text:previous.text,last_kind:previous.kind,last_event_at:previous.timestamp});
});

test('discard rebuilds a missing summary from a cached predecessor at the same timestamp', async t => {
  t.mock.timers.enable({apis:['Date','setTimeout'],now:1800000000000});
  const c=store(t); let request=''; const stamp='2026-01-01T00:00:00.000Z';
  c.applyFrame({type:'snapshot',connected:true,events:[],sessions:c.getState().sessions.map(s=>s.stream_id===STREAM?{...s,last_text:'',last_kind:'',last_event_at:stamp}:s)});
  const previous={host:'peer',provider:'claude',stream_id:STREAM,session_name:'ordinary-chat',daemon_seq:17,timestamp:stamp,kind:'ASSIST_TEXT',text:'Cached predecessor'};
  c.applyFrame({type:'stream_events',stream_id:STREAM,events:[previous]});
  c.setSendBridge(async args=>{request=args.requestId;return {ok:true};});
  const id=c.sendTurn(STREAM,'failed input');await flush();c.applyFrame({type:'send.result',request_id:request,delivery:'not_landed'});
  c.discardFailedOptimisticSend(id);
  const summary=c.getState().sessions.find(s=>s.stream_id===STREAM)!;
  assert.deepEqual({last_text:summary.last_text,last_kind:summary.last_kind,last_event_at:summary.last_event_at},
    {last_text:previous.text,last_kind:previous.kind,last_event_at:stamp});
});

test('a fresh Retry on an aged row retains browser-host recovery for its own attempt', async t => {
  t.mock.timers.enable({apis:['Date','setTimeout'],now:1800000000000});
  const c=store(t); const requests:string[]=[];
  c.setSendBridge(async ({requestId})=>{requests.push(requestId); if(requests.length===2) throw webLoss(1); return {ok:true};});
  c.setSendReceiptBridge(async()=>({ok:false}));
  const id=c.sendTurn(STREAM,'old row web recovery'); await flush(); const first=requests[0];
  c.applyFrame({type:'send.result',request_id:first,delivery:'not_landed'});
  t.mock.timers.tick(1801000); c.webHostDisconnected(1); c.retryOptimisticSend(id); await flush();
  const fresh=requests[1]; c.webHostReady(2); await flush(); await flush();
  assert.notEqual(fresh,first); assert.deepEqual(requests,[first,fresh,fresh]);
});
