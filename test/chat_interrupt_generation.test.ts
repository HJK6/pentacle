import test from 'node:test';
import assert from 'node:assert/strict';
import { initialPentacleStreamState } from 'pentacle-chat-core';
import { ChatStoreController } from '../renderer/src/chat_store_controller';

const STREAM = 'peer:disposable-seat';

function row(generation: string) {
  return {
    stream_id: STREAM,
    host: 'peer',
    session_name: 'disposable-seat',
    provider: 'codex',
    session_generation: generation,
  };
}

function nextTurn(): Promise<void> {
  return new Promise((resolve) => setImmediate(resolve));
}

test('Stop and retry use the selected row generation after a replacement arrives', async () => {
  const controller = new ChatStoreController({
    ...initialPentacleStreamState,
    sessions: [row('selected-generation')],
    workingByStream: {
      [STREAM]: { phase: 'working', optimisticId: 'turn-1', sentAt: 1 },
    },
  } as never);
  const sent: Array<Record<string, unknown>> = [];
  controller.setCancelBridge(async (args: any) => {
    sent.push({ ...args });
    return { ok: true, interrupted: true, confirm: 'interrupt_unconfirmed' };
  });

  assert.equal(controller.cancelTurn(STREAM), true);
  controller.applyFrame({ type: 'session.inventory', sessions: [row('replacement-generation')] });
  await nextTurn();
  assert.equal(controller.getState().sessions[0]?.session_generation, 'replacement-generation');
  assert.equal(sent.length, 1);
  assert.equal(sent[0]?.expectedSessionGeneration, 'selected-generation');

  controller.applyFrame({ type: 'session.inventory', sessions: [row('later-generation')] });
  assert.equal(controller.cancelTurn(STREAM), true);
  await nextTurn();
  assert.equal(sent.length, 2);
  assert.equal(sent[1]?.expectedSessionGeneration, 'selected-generation');

  // A daemon working update can re-expose the same turn before another retry.
  // That path must still use the value captured by the first Stop press.
  (controller.getState().workingByStream as any)[STREAM] = {
    phase: 'working', optimisticId: 'turn-1', sentAt: 1,
  };
  controller.applyFrame({ type: 'session.inventory', sessions: [row('newer-generation')] });
  assert.equal(controller.cancelTurn(STREAM), true);
  await nextTurn();
  assert.equal(sent.length, 3);
  assert.equal(sent[2]?.expectedSessionGeneration, 'selected-generation');
  controller.dispose();
});
