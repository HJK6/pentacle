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

test('a provider-queued row exposes a stream-bound Send now and Sent removes it', () => {
  const doc = status({ sendState: 'sending', receiptCaption: 'queued', providerQueued: true });
  const button = doc.querySelector('.slot-chat-send-now');
  assert.equal(button?.textContent, 'Send now');
  assert.equal(button?.getAttribute('data-stream-id'), STREAM);
  assert.equal(button?.getAttribute('data-optimistic-id'), 'optimistic-row');
  assert.equal(status({ receiptCaption: 'sent' }).querySelector('.slot-chat-send-now'), null);
  assert.equal(status({ receiptCaption: 'queued', providerQueued: true }, { allowQueuedSendNow: false }).querySelector('.slot-chat-send-now'), null);
});

test('Send now interrupts exactly once without resending text, attachments, or changing IDs', async () => {
  const { controller, sends, interrupts } = setup();
  const attachments = [{ key: 'blob-key', mime: 'image/png' }];
  const id = controller.sendTurn(STREAM, 'one message', attachments as never);
  await settle();
  const before = structuredClone(controller.getState().optimisticSends?.[id]);
  assert.equal(sendNow(controller, STREAM, id), true);
  assert.equal(sendNow(controller, STREAM, id), false);
  controller.cancelTurn(STREAM);
  await settle();
  assert.equal(sends.length, 1);
  assert.deepEqual(interrupts, [{ streamId: STREAM, expectedSessionGeneration: 'generation-1' }]);
  assert.deepEqual(controller.getState().optimisticSends?.[id], before);
  assert.equal(controller.getTurnPhase(STREAM), 'working');
  assert.match(controller.getComposerNotice(STREAM), /[Ww]ait|requested/);
  controller.dispose();
});

test('two provider-queued rows retain identity and share one interrupt request', async () => {
  const { controller, sends, interrupts } = setup();
  const first = controller.sendTurn(STREAM, 'first queued');
  const second = controller.sendTurn(STREAM, 'second queued');
  await settle();
  const ids = Object.keys(controller.getState().optimisticSends ?? {});
  assert.equal(sendNow(controller, STREAM, second), true);
  assert.equal(sendNow(controller, STREAM, first), false);
  await settle();
  assert.deepEqual(sends.map(row => row.text), ['first queued', 'second queued']);
  assert.deepEqual(Object.keys(controller.getState().optimisticSends ?? {}), ids);
  assert.equal(interrupts.length, 1);
  controller.dispose();
});

test('stale, cross-stream, nonqueued and composite rows cannot interrupt', async () => {
  const { controller, interrupts, setQueued } = setup();
  const queued = controller.sendTurn(STREAM, 'queued');
  const composite = controller.sendTurn(COMPOSITE, 'composite');
  setQueued(false);
  await settle();
  assert.equal(sendNow(controller, STREAM, 'missing'), false);
  assert.equal(sendNow(controller, OTHER, queued), false);
  assert.equal(sendNow(controller, COMPOSITE, composite), false);
  assert.equal(sendNow(controller, STREAM, queued), false);
  assert.equal(interrupts.length, 0);
  controller.dispose();
});

test('a queue action is fenced to the generation that received the original send', async () => {
  const { controller, interrupts } = setup();
  const id = controller.sendTurn(STREAM, 'old generation');
  await settle();
  controller.applyFrame({ type: 'session.inventory', sessions: [session(STREAM, 'generation-2'), session(OTHER), session(COMPOSITE)] });
  assert.equal(sendNow(controller, STREAM, id), false);
  await settle();
  assert.equal(interrupts.length, 0);
  controller.dispose();
});

test('a positive queue remains actionable when the local working hint is stale idle', async () => {
  const { controller, interrupts } = setup(false);
  const id = controller.sendTurn(STREAM, 'queued with stale hint');
  await settle();
  (controller as any).setState({ ...controller.getState(), workingByStream: {} });
  assert.equal(sendNow(controller, STREAM, id), true);
  await settle();
  assert.equal(interrupts.length, 1);
  assert.equal(controller.getState().optimisticSends?.[id]?.provider_queued, true);
  controller.dispose();
});

test('an explicit interrupt failure leaves the message queued and allows one user retry', async () => {
  const { controller, sends, interrupts, setInterrupt } = setup();
  setInterrupt({ ok: false, error: 'bridge refused' });
  const id = controller.sendTurn(STREAM, 'stay queued');
  await settle();
  assert.equal(sendNow(controller, STREAM, id), true);
  await settle();
  assert.equal(controller.getState().optimisticSends?.[id]?.provider_queued, true);
  assert.match(controller.getComposerNotice(STREAM), /[Ff]ail|[Cc]ould not|did not take/);
  setInterrupt({ ok: true, confirm: 'interrupt_unconfirmed' });
  assert.equal(sendNow(controller, STREAM, id), true);
  await settle();
  assert.equal(interrupts.length, 2);
  assert.equal(sends.length, 1);
  controller.dispose();
});

test('a matching processing echo makes a formerly queued row ineligible', async () => {
  const { controller, interrupts } = setup();
  const id = controller.sendTurn(STREAM, 'processed message');
  await settle();
  const request = controller.getState().optimisticSends?.[id]?.request_id;
  controller.applyFrame({ type: 'chat.event', event: { daemon_seq: 20, host: 'peer', provider: 'claude',
    stream_id: STREAM, session_name: 'queue-seat', timestamp: new Date().toISOString(), kind: 'USER',
    text: 'processed message', optimistic_id: id, request_id: request,
    raw: { receipt_state: 'landed', receipt_delivery: 'landed' } } });
  assert.equal(sendNow(controller, STREAM, id), false);
  assert.equal(interrupts.length, 0);
  controller.dispose();
});

test('Stop followed by Send now cannot emit another Escape after an unconfirmed acknowledgement', async () => {
  const { controller, interrupts } = setup();
  const id = controller.sendTurn(STREAM, 'queued behind stopped turn');
  await settle();
  controller.cancelTurn(STREAM);
  await settle();
  assert.equal(sendNow(controller, STREAM, id), false);
  await settle();
  assert.equal(interrupts.length, 1);
  controller.dispose();
});

test('reconnect never replays a queued-row interrupt', async () => {
  const { controller, interrupts } = setup();
  const id = controller.sendTurn(STREAM, 'queue across reconnect');
  await settle();
  assert.equal(sendNow(controller, STREAM, id), true);
  await settle();
  controller.applyFrame({ type: '__reconnect', next_generation: 1, previous_generation: 0 });
  await settle();
  assert.equal(interrupts.length, 1);
  controller.dispose();
});

test('a stale queued button cannot interrupt after the send becomes indeterminate', async () => {
  const { controller, interrupts } = setup();
  const id = controller.sendTurn(STREAM, 'queue before a lost connection');
  await settle();
  const requestId = controller.getState().optimisticSends?.[id]?.request_id;
  controller.applyFrame({ type: 'send.indeterminate', request_id: requestId, reason: 'disconnected' });
  assert.equal(controller.getState().optimisticSends?.[id]?.status, 'indeterminate');
  assert.equal(sendNow(controller, STREAM, id), false);
  await settle();
  assert.equal(interrupts.length, 0);
  controller.dispose();
});
