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
    const c = store(t); let request = '';
    c.setSendBridge(async args => { request = args.requestId; return { ok: true }; });
    (c as any).setSendReceiptBridge(async () => { throw new Error(failure); });
    const id = c.sendTurn(STREAM, 'pending receipt'); await flush();
    c.applyFrame({ type: 'send.indeterminate', request_id: request }); await flush();
    c.applyFrame({ type: '__reconnect', generation: 0, next_generation: 1 }); await flush();
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
    const c = store(t); let request = '';
    c.setSendBridge(async args => { request = args.requestId; return { ok: true }; });
    (c as any).setSendReceiptBridge(async () => ({ ok: false, code, found: false }));
    const id = c.sendTurn(STREAM, 'failed receipt query'); await flush();
    c.applyFrame({ type: 'send.indeterminate', request_id: request }); await flush();
    c.applyFrame({ type: '__reconnect', generation: 0, next_generation: 1 }); await flush();
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
