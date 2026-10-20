import test from 'node:test';
import assert from 'node:assert/strict';
import { initialPentacleStreamState } from 'pentacle-chat-core';
import { ChatStoreController } from '../renderer/src/chat_store_controller';

const STREAM = 'peer:disposable-seat';
const COMPOSITE = 'peer:composite-seat';

function row(streamId: string, kind?: string) {
  return {
    stream_id: streamId,
    host: 'peer',
    session_name: streamId.split(':')[1],
    provider: 'claude',
    session_generation: 'generation-1',
    ...(kind ? { session_kind: kind } : {}),
  };
}

function working(streamId = STREAM) {
  return { [streamId]: { phase: 'working', optimisticId: 'turn-1', sentAt: 1 } };
}

function makeController(workingByStream: Record<string, unknown> = working()) {
  const controller = new ChatStoreController({
    ...initialPentacleStreamState,
    connected: true,
    sessions: [row(STREAM), row(COMPOSITE, 'assistant_composite')],
    workingByStream,
  } as never);
  const sent: Array<Record<string, any>> = [];
  const interrupts: Array<Record<string, any>> = [];
  let receipt: Record<string, unknown> = { ok: true };
  controller.setSendBridge(async (args: any) => {
    sent.push({ ...args });
    return receipt as never;
  });
  controller.setCancelBridge(async (args: any) => {
    interrupts.push({ ...args });
    return { ok: true, interrupted: true, confirm: 'interrupted' };
  });
  return { controller, sent, interrupts, setReceipt: (next: Record<string, unknown>) => { receipt = next; } };
}

const settle = () => new Promise<void>((resolve) => setImmediate(resolve));

test('Steer now sends urgent on the first dispatch; Queue and idle sends never do', async () => {
  const { controller, sent } = makeController();
  controller.sendTurn(STREAM, 'queue me');
  controller.sendTurn(STREAM, 'steer me', [], { urgent: true });
  await settle();
  assert.equal(sent.length, 2);
  assert.equal('urgent' in sent[0]!, false);
  assert.equal(sent[1]!.urgent, true);

  const idle = makeController({});
  idle.controller.sendTurn(STREAM, 'idle steer', [], { urgent: true });
  await settle();
  assert.equal('urgent' in idle.sent[0]!, false);
  assert.equal(idle.controller.getComposerNotice(STREAM), '');
  controller.dispose();
  idle.controller.dispose();
});

test('one Escape per working turn is shared by Steer now and Stop', async () => {
  const { controller, sent, interrupts } = makeController();
  controller.sendTurn(STREAM, 'first steer', [], { urgent: true });
  controller.sendTurn(STREAM, 'second steer', [], { urgent: true });
  await settle();
  assert.equal(sent[0]!.urgent, true);
  assert.equal('urgent' in sent[1]!, false);
  assert.equal(controller.getComposerNotice(STREAM), 'Steer did not take; queued instead');

  // Stop after the Escape was spent by Steer now is a no-op.
  assert.equal(controller.cancelTurn(STREAM), true);
  await settle();
  assert.equal(interrupts.length, 0);
  assert.equal(controller.getTurnPhase(STREAM), 'working');
  controller.dispose();
});

test('Stop spends the Escape; a later Steer in the same turn goes out without urgent', async () => {
  const { controller, sent, interrupts } = makeController();
  assert.equal(controller.cancelTurn(STREAM), true);
  await settle();
  assert.equal(interrupts.length, 1);
  controller.cancelTurn(STREAM);
  await settle();
  assert.equal(interrupts.length, 1, 'a second Stop is a no-op');

  // The budget resets once the turn phase leaves working and a new turn starts.
  (controller as any).setState({ ...controller.getState(), workingByStream: working() });
  controller.sendTurn(STREAM, 'steer after new turn', [], { urgent: true });
  await settle();
  assert.equal(sent.at(-1)!.urgent, true);
  controller.dispose();
});

test('a receipt that is still provider_queued shows the steer-did-not-take notice', async () => {
  const { controller, setReceipt } = makeController();
  setReceipt({ ok: true, provider_queued: true });
  const optimisticId = controller.sendTurn(STREAM, 'steer', [], { urgent: true });
  await settle();
  assert.equal(controller.getComposerNotice(STREAM), 'Steer did not take; queued instead');
  assert.equal(controller.getState().optimisticSends?.[optimisticId]?.provider_queued, true);

  const taken = makeController();
  const takenId = taken.controller.sendTurn(STREAM, 'steer', [], { urgent: true });
  await settle();
  assert.equal(taken.controller.getComposerNotice(STREAM), '');
  assert.notEqual(taken.controller.getState().optimisticSends?.[takenId]?.provider_queued, true);
  controller.dispose();
  taken.controller.dispose();
});

test('an urgent row is not rendered as queued while in flight and does not store urgent', async () => {
  const { controller } = makeController();
  const optimisticId = controller.sendTurn(STREAM, 'steer', [], { urgent: true });
  const send = controller.getState().optimisticSends?.[optimisticId] as Record<string, unknown>;
  assert.equal(send.queued_at, undefined);
  assert.equal(send.turn_queued === true, false);
  assert.equal('urgent' in send, false);
  controller.dispose();
});

test('retry and reconnect replay send without urgent', async () => {
  const { controller, sent, setReceipt } = makeController();
  setReceipt({ ok: false, error: 'rejected' });
  const optimisticId = controller.sendTurn(STREAM, 'steer', [], { urgent: true });
  await settle();
  assert.equal(sent[0]!.urgent, true);
  assert.equal(controller.getState().optimisticSends?.[optimisticId]?.status, 'failed');

  setReceipt({ ok: true });
  assert.equal(controller.retryOptimisticSend(optimisticId), true);
  await settle();
  assert.equal(sent.length, 2);
  assert.equal('urgent' in sent[1]!, false);

  // Replay re-dispatches through the same private path with no flag source.
  (controller as any).replayReconnectSurvivors(0);
  await settle();
  for (const call of sent.slice(1)) assert.equal('urgent' in call, false);
  controller.dispose();
});

test('Stop never claims to clear the queue and says so while queued rows remain', async () => {
  const { controller, setReceipt } = makeController();
  setReceipt({ ok: true, provider_queued: true });
  const queuedId = controller.sendTurn(STREAM, 'queued text');
  await settle();
  assert.equal(controller.getState().optimisticSends?.[queuedId]?.provider_queued, true);
  controller.cancelTurn(STREAM);
  await settle();
  assert.equal(controller.getComposerNotice(STREAM), 'Stopped. Queued messages run next');
  assert.equal(controller.getState().optimisticSends?.[queuedId]?.provider_queued, true);

  // The next send clears the notice.
  controller.sendTurn(STREAM, 'next');
  assert.equal(controller.getComposerNotice(STREAM), '');
  controller.dispose();
});

test('the composite chat never takes urgent, an Escape budget or a notice', async () => {
  const { controller, sent } = makeController({ ...working(COMPOSITE) });
  assert.equal(controller.getTurnPhase(COMPOSITE), 'idle');
  controller.sendTurn(COMPOSITE, 'hello', [], { urgent: true });
  await settle();
  assert.equal(sent.length, 1);
  assert.equal('urgent' in sent[0]!, false);
  assert.equal(controller.getComposerNotice(COMPOSITE), '');
  controller.dispose();
});
