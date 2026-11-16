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

test('the composite chat remains an ordinary send and cannot release a native queue', async () => {
  const { controller, sent, interrupts, setReceipt } = makeController({ ...working(COMPOSITE) });
  setReceipt({ ok: true, provider_queued: true });
  assert.equal(controller.getTurnPhase(COMPOSITE), 'idle');
  const id = controller.sendTurn(COMPOSITE, 'hello');
  await settle();
  assert.equal(sent.length, 1);
  assert.equal('urgent' in sent[0]!, false);
  assert.equal(controller.sendQueuedNow(COMPOSITE, id), false);
  assert.equal(interrupts.length, 0);
  controller.dispose();
});
