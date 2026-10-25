import test from 'node:test';
import assert from 'node:assert/strict';
import fixture from '../pentacle-chat-core/tests/fixtures/image_echo_bridge_order.json';
import { ChatStoreController } from '../renderer/src/chat_store_controller';
import { selectPentacleDerivedEventIndex, type PentacleEvent } from 'pentacle-chat-core';

const stream = fixture.event.stream_id;
const flush = () => new Promise<void>(resolve => setImmediate(resolve));

function setup() {
  const store = new ChatStoreController();
  store.applyFrame({ type: 'snapshot', events: [], sessions: [{
    stream_id: stream, host: 'fixture', provider: 'claude', session_name: 'image-echo',
    last_text: '', last_kind: '', last_event_at: fixture.event.timestamp,
    online: true, pending: false, working: false,
  }] });
  let resolveBridge: (result: { ok: boolean }) => void = () => {};
  let calls = 0;
  store.setSendBridge(() => { calls++; return new Promise(resolve => { resolveBridge = resolve; }); });
  return { store, resolve: () => resolveBridge({ ok: true }), calls: () => calls };
}

function assertReconciled(store: ChatStoreController, optimisticId: string) {
  const rows = store.selectSessionDetail(stream)!.transcriptItems.filter(row => row.isUser);
  assert.equal(rows.length, 1, 'one image/caption row, not a Sending twin plus native wrapper');
  assert.equal(rows[0].text, fixture.caption, 'original NFD caption and LF preserved');
  assert.equal(rows[0].attachments?.[0].key, fixture.attachment.key);
  assert.equal(rows[0].sendState, undefined);
  assert.equal(rows[0].optimisticId, optimisticId);
  assert.equal(store.getState().optimisticSends?.[optimisticId], undefined);
  const event = selectPentacleDerivedEventIndex(store.getState()).byStream.get(stream)
    ?.find(item => item.optimistic_id === optimisticId);
  assert.equal(event?.correlatedDaemonSeq, fixture.event.daemon_seq);
  assert.equal(event?.raw?.user_delivery_state, 'sent', 'native evidence preserved');
  assert.equal(event?.raw?.receipt_delivery, undefined, 'no fabricated transport receipt');
}

for (const mode of ['live-before-bridge', 'snapshot-before-bridge', 'bridge-before-live'] as const) {
  test(`captured image echo order: ${mode}`, async t => {
    t.mock.method(Date, 'now', () => Date.parse(fixture.created_at));
    const x = setup();
    t.after(() => x.store.dispose());
    const id = x.store.sendTurn(stream, fixture.caption, [fixture.attachment]);
    await flush();
    const requestId = x.store.getState().optimisticSends![id].request_id;
    assert.equal(x.store.getState().optimisticSends![id].status, 'queued');
    assert.equal(x.calls(), 1);
    if (mode === 'bridge-before-live') { x.resolve(); await flush(); }
    if (mode === 'snapshot-before-bridge') {
      const session = x.store.getState().sessions[0];
      x.store.applyFrame({ type: 'snapshot', sessions: [session], events: [fixture.event] });
    } else {
      x.store.applyFrame({ type: 'chat.event', event: fixture.event });
    }
    assertReconciled(x.store, id);
    // The actual installed send result was committed_pending_proof. Its later
    // arrival and bridge completion must not resurrect a reconciled send.
    x.store.applyFrame({ type: 'send.result', request_id: requestId,
      delivery: 'committed_pending_proof', action_committed: true, confirmation_pending: true });
    x.resolve();
    await flush();
    assertReconciled(x.store, id);
    assert.equal(x.calls(), 1, 'no resend needed to settle the row');
    x.store.applyFrame({ type: 'chat.event', event: fixture.event as unknown as PentacleEvent });
    assertReconciled(x.store, id);
  });
}
