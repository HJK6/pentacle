import test from 'node:test';
import assert from 'node:assert/strict';
import { initialPentacleStreamState } from 'pentacle-chat-core';
import { ChatStoreController } from '../renderer/src/chat_store_controller';
import { renderTranscriptItemHtml } from '../renderer/src/shared_transcript_view';
import { JSDOM } from 'jsdom';

const STREAM = 'peer:queue-seat';
const OTHER = 'peer:other-seat';
const COMPOSITE = 'peer:assistant';
const settle = () => new Promise<void>(resolve => setImmediate(resolve));
const session = (stream_id: string, generation = 'generation-1') => ({
  stream_id, host: 'peer', session_name: stream_id.split(':')[1], provider: 'claude',
  session_generation: generation, ...(stream_id === COMPOSITE ? { session_kind: 'assistant_composite' } : {}),
});
function setup(working = true) {
  const controller = new ChatStoreController({ ...initialPentacleStreamState, connected: true,
    sessions: [session(STREAM), session(OTHER), session(COMPOSITE)],
    workingByStream: working ? { [STREAM]: { phase: 'working', optimisticId: 'original-turn', sentAt: 1 } } : {},
  } as never);
  const sends: any[] = [], interrupts: any[] = [];
  let queued = true;
  let interrupt: any = { ok: true, confirm: 'interrupt_unconfirmed' };
  controller.setSendBridge(async args => { sends.push(args); return { ok: true, ...(queued ? { provider_queued: true } : {}) }; });
  controller.setCancelBridge(async args => { interrupts.push(args); return interrupt; });
  return { controller, sends, interrupts, setQueued: (value: boolean) => { queued = value; },
    setInterrupt: (value: any) => { interrupt = value; } };
}
const sendNow = (controller: ChatStoreController, stream: string, id: string) =>
  (controller as any).sendQueuedNow(stream, id);
function status(overrides: Record<string, unknown> = {}, options: Record<string, unknown> = {}) {
  const html = renderTranscriptItemHtml({ id: 'row', kind: 'USER', text: 'queued message', isUser: true,
    optimisticId: 'optimistic-row', timestamp: '', timestampLabel: '', label: 'You', tone: 'user',
    ...overrides } as never, undefined, { streamId: STREAM, allowQueuedSendNow: true, ...options } as never);
  return new JSDOM(html).window.document;
}

test('send is immediate and ordinary while idle, working, or on the composite', async () => {
  for (const working of [false, true]) {
    const { controller, sends, interrupts } = setup(working);
    controller.sendTurn(STREAM, 'first');
    controller.sendTurn(STREAM, 'second');
    controller.sendTurn(COMPOSITE, 'assistant');
    await settle();
    assert.deepEqual(sends.map(row => row.text), ['first', 'second', 'assistant']);
    assert.ok(sends.every(row => !('urgent' in row)));
    assert.equal(interrupts.length, 0);
    controller.dispose();
  }
});

test('a local queued lifecycle and working hint render sending until provider queue proof', () => {
  const doc = status({ sendState: 'queued', queuedWhileWorking: true });
  assert.equal(doc.querySelector('.slot-chat-send-status')?.textContent, 'sending…');
  assert.equal(doc.querySelector('.slot-chat-send-now'), null);
});

test('a provider queue explains unavailable acceleration and retains the queued row', () => {
  const doc = status({ sendState: 'sending', receiptCaption: 'queued', providerQueued: true });
  const button = doc.querySelector<HTMLButtonElement>('.slot-chat-send-now')!;
  assert.equal(button.textContent, 'Send now unavailable');
  assert.equal(button.disabled, true);
  assert.match(doc.querySelector('.slot-chat-send-now-reason')?.textContent || '', /interrupt.*draft.*stays queued/i);
  assert.equal(button.getAttribute('data-optimistic-id'), 'optimistic-row');
  assert.equal(status({ receiptCaption: 'sent' }).querySelector('.slot-chat-send-now'), null);
  assert.equal(status({ receiptCaption: 'queued', providerQueued: true }, { allowQueuedSendNow: false }).querySelector('.slot-chat-send-now'), null);
});

for (const provider of ['claude', 'codex']) {
for (const stage of ['queued', 'processing', 'uncertain', 'replaced', 'closed', 'reconnected']) {
  test(`${provider}: repeated/stale queued-row actions are inert after ${stage}`, async () => {
    const { controller, sends, interrupts } = setup();
    controller.applyFrame({ type: 'session.inventory', sessions: [session(STREAM), session(OTHER), session(COMPOSITE)].map(s => ({ ...s, provider })) });
    const first = controller.sendTurn(STREAM, 'first queued', [{ key: 'blob', mime: 'image/png' }]);
    const second = controller.sendTurn(STREAM, 'second queued');
    await settle();
    const request = controller.getState().optimisticSends![first].request_id;
    if (stage === 'processing') controller.applyFrame({ type: 'chat.event', event: { daemon_seq: 101, host: 'peer', provider,
      stream_id: STREAM, session_name: 'queue-seat', timestamp: new Date().toISOString(), kind: 'USER', text: 'first queued',
      optimistic_id: first, request_id: request, raw: { receipt_state: 'landed', receipt_delivery: 'landed' } } });
    if (stage === 'uncertain') controller.applyFrame({ type: 'send.indeterminate', request_id: request, reason: 'disconnected' });
    if (stage === 'replaced' || stage === 'closed') controller.applyFrame({ type: 'session.inventory', sessions: [
      { ...session(STREAM, stage === 'replaced' ? 'replacement' : 'generation-1'), ...(stage === 'closed' ? { status: 'closed' } : {}) }, session(OTHER), session(COMPOSITE)] });
    if (stage === 'reconnected') { controller.webHostDisconnected(1); controller.webHostReady(2); }
    const before = structuredClone(controller.getState().optimisticSends);
    for (const [stream, id] of [[STREAM, first], [STREAM, second], [OTHER, first], [COMPOSITE, second], [STREAM, 'missing']]) {
      assert.equal(controller.canSendQueuedNow(stream, id), false);
      assert.equal(sendNow(controller, stream, id), false);
      assert.equal(sendNow(controller, stream, id), false);
    }
    await settle();
    assert.equal(interrupts.length, 0);
    assert.equal(sends.length, 2);
    assert.deepEqual(controller.getState().optimisticSends, before);
    assert.equal(controller.getInterruptState(STREAM), null);
    controller.dispose();
  });
}}

test('USER echo between stale click and microtask cannot cancel the newly processing input', async () => {
  const { controller, interrupts, sends } = setup();
  const id = controller.sendTurn(STREAM, 'delayed proof'); await settle();
  const send = controller.getState().optimisticSends![id];
  assert.equal(sendNow(controller, STREAM, id), false);
  controller.applyFrame({ type: 'chat.event', event: { daemon_seq: 55, host: 'peer', provider: 'claude', stream_id: STREAM,
    session_name: 'queue-seat', timestamp: new Date().toISOString(), kind: 'USER', text: send.text,
    optimistic_id: id, request_id: send.request_id, raw: { receipt_state: 'landed', receipt_delivery: 'landed' } } });
  await settle(); assert.equal(interrupts.length, 0); assert.equal(sends.length, 1); controller.dispose();
});

test('stale urgency options use ordinary intake for agent and composite, preserving reply/voice/attachments', async () => {
  const { controller, sends, interrupts } = setup();
  for (const stream of [STREAM, COMPOSITE]) assert.ok(controller.sendTurn(stream, 'ordinary', [{ key: 'blob', mime: 'image/png' }],
    { urgent: true, reply_to_message_id: 'reply', meta: { voice: { duration_s: 2 } } } as never));
  await settle(); assert.equal(sends.length, 2); assert.equal(interrupts.length, 0);
  for (const send of sends) { assert.equal('urgent' in send, false); assert.equal(send.replyToMessageId, 'reply');
    assert.deepEqual(send.attachments, [{ key: 'blob', mime: 'image/png' }]); assert.deepEqual(send.meta, { voice: { duration_s: 2 } }); }
  controller.dispose();
});

test('unavailable acceleration does not consume Stop; explicit Stop still interrupts once', async () => {
  const { controller, sends, interrupts } = setup(); const id = controller.sendTurn(STREAM, 'keep queued'); await settle();
  assert.equal(sendNow(controller, STREAM, id), false); assert.equal(controller.cancelTurn(STREAM), true); await settle();
  assert.equal(sendNow(controller, STREAM, id), false); controller.cancelTurn(STREAM); await settle();
  assert.equal(interrupts.length, 1); assert.equal(sends.length, 1);
  assert.equal(controller.getState().optimisticSends![id].provider_queued, true); controller.dispose();
});

for (const confirm of ['interrupt_unconfirmed', 'interrupted', 'coalesced']) {
test(`Stop preserves its turn-owning native queue through delivery uncertainty (${confirm})`, async () => {
  const { controller, interrupts, setInterrupt } = setup(false);
  setInterrupt({ ok: true, confirm });
  const id = controller.sendTurn(STREAM, 'native queue owns the locally pending turn');
  await settle();
  const request = controller.getState().optimisticSends?.[id]?.request_id;
  assert.equal(controller.getTurnState(STREAM)?.optimisticId, id);
  controller.applyFrame({ type: 'send.indeterminate', request_id: request, reason: 'disconnected' });
  assert.equal(controller.getState().optimisticSends?.[id]?.provider_queued, true);
  assert.equal(controller.cancelTurn(STREAM), true);
  await settle();
  const cancelled = controller.getState().optimisticSends?.[id]?.status === 'cancelled';
  controller.getInterruptState(STREAM);
  controller.cancelTurn(STREAM);
  await settle();
  controller.applyFrame({ type: 'send.result', request_id: request, delivery: 'landed', provider_queued: true });
  const repeated = sendNow(controller, STREAM, id);
  await settle();
  assert.deepEqual({ cancelled, interrupts: interrupts.length, repeated },
    { cancelled: false, interrupts: 1, repeated: false });
  controller.dispose();
});
}

test('Stop still cancels an undelivered local row without native queue proof', async () => {
  const { controller, interrupts } = setup(false);
  controller.setSendBridge(() => new Promise(() => {}));
  const id = controller.sendTurn(STREAM, 'not delivered');
  assert.equal(controller.cancelTurn(STREAM), true);
  await settle();
  assert.equal(controller.getState().optimisticSends?.[id]?.status, 'cancelled');
  assert.equal(interrupts.length, 1);
  controller.dispose();
});

test('Stop does not announce a retired generation queue for a nonqueued current turn', async () => {
  const { controller, setQueued } = setup();
  const old = controller.sendTurn(STREAM, 'retired provider queue');
  await settle();
  controller.applyFrame({ type: 'session.inventory', sessions: [session(STREAM, 'generation-2'), session(OTHER), session(COMPOSITE)] });
  setQueued(false);
  controller.sendTurn(STREAM, 'current nonqueued work');
  await settle();
  controller.cancelTurn(STREAM);
  await settle();
  assert.equal(controller.getState().optimisticSends?.[old]?.provider_queued, true);
  assert.equal(controller.getComposerNotice(STREAM), '');
  controller.dispose();
});
