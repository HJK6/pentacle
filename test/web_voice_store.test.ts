import test from 'node:test';
import assert from 'node:assert/strict';
import { ChatStoreController } from '../renderer/src/chat_store_controller';

test('voice text sends use one optimistic row and preserve metadata on explicit Retry', async () => {
  const store = new ChatStoreController();
  store.applyFrame({ type: 'snapshot', sessions: [{ stream_id: 'local:voice', host: 'local', session_name: 'voice', provider: 'claude' }], events: [] });
  const calls: any[] = [];
  store.setSendBridge(async p => { calls.push(p); return { ok: false, error: 'backend_busy' }; });
  const meta = { voice: { duration_s: 2 } };
  const id = store.sendTurn('local:voice', 'Voice text', [], { meta });
  await new Promise(r => setImmediate(r));
  assert.deepEqual(calls[0].meta, meta);
  assert.equal(store.getState().optimisticSends?.[id].status, 'failed');
  assert.equal(store.retryOptimisticSend(id), true);
  await new Promise(r => setImmediate(r));
  assert.equal(calls.length, 2); assert.deepEqual(calls[1].meta, meta);
  assert.equal(calls[1].optimisticId, id); assert.notEqual(calls[1].requestId, calls[0].requestId);
  assert.equal(store.selectSessionDetail('local:voice')!.transcriptItems.filter(item => item.text === 'Voice text').length, 1);
});
