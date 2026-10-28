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
  assert.equal(controller.retryOptimisticSend(id), true);
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

test('uncertain row offers a truthful rendered Retry for same-ID reconciliation',async t=>{
 const controller=store(t);let request='';controller.setSendBridge(async args=>{request=args.requestId;return {ok:false,error:'socket closed'};});
 const id=controller.sendTurn(STREAM,'uncertain visible action');await flush();
 assert.equal(controller.getState().optimisticSends?.[id].status,'indeterminate');
 const row=controller.selectSessionDetail(STREAM).transcriptItems.find(row=>row.optimisticId===id)!;
 assert.equal(row.sendState,'indeterminate');const html=renderTranscriptItemHtml(row);
 assert.match(html,/Delivery uncertain/);assert.match(html,/slot-chat-send-retry/);assert.doesNotMatch(html,/Failed to send/);
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

test('exact web loss reconciles once per ready web epoch with frozen identity and urgent intent', async t => {
  const c = store(t); const calls: Parameters<ChatSendBridge>[0][] = [];
  c.setSendBridge(async args => { calls.push(args); throw webLoss(calls.length === 1 ? 1 : 2); });
  const id = c.sendTurn(STREAM, 'urgent payload', [], { urgent: true, reply_to_message_id: 'reply-1' });
  c.webHostDisconnected(1); await flush();
  assert.equal(c.getState().optimisticSends?.[id].status, 'indeterminate');
  c.webHostReady(1); await flush(); assert.equal(calls.length, 1, 'same closed epoch is not readiness');
  c.webHostReady(2); c.webHostReady(2); await flush(); await flush();
  assert.equal(calls.length, 2); assert.deepEqual(calls[1], calls[0]); assert.equal(calls[1].urgent, true);
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

test('urgent is rejected for a composite and ordinary new intents remain separate', async t => {
  const c = store(t); const calls: Parameters<ChatSendBridge>[0][] = [];
  c.setSendBridge(async args => { calls.push(args); return { ok: true }; });
  assert.equal(c.sendTurn(COMPOSITE, 'stale urgent', [], { urgent: true }), '');
  c.sendTurn(STREAM, 'same'); c.sendTurn(COMPOSITE, 'same'); await flush();
  assert.equal(calls.length, 2); assert.notEqual(calls[0].requestId, calls[1].requestId);
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
  c.sendTurn(STREAM, 'freeze', attachments, { meta, urgent: true, reply_to_message_id: 'message-1', reply_to_question_id: 'question-1' });
  c.webHostDisconnected(1); await flush(); attachments[0].key = 'mutated'; meta.voice.duration_s = 8;
  c.webHostReady(2); await flush(); assert.deepEqual(calls[1], calls[0]);
});
