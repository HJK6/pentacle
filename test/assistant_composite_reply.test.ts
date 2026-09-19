import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { ChatStoreController, type ChatSendBridge } from '../renderer/src/chat_store_controller';
import { renderTranscriptTimelineHtml } from '../renderer/src/shared_transcript_view';

const streamId = 'hostc:assistant';
function store() {
  const controller = new ChatStoreController();
  controller.applyFrame({ type: 'snapshot', connected: true, sessions: [{
    host: 'hostc', session_name: 'assistant', stream_id: streamId,
    session_kind: 'assistant_composite', provider: 'composite', visibility: 'visible',
    capabilities: { pane: false, terminal: false, assistant_composite_v1: true },
  }], events: [{
    host: 'hostc', session_name: 'assistant', session_id: 'assistant', stream_id: streamId,
    kind: 'ASSIST_TEXT', provider: 'composite', daemon_seq: 1,
    timestamp: new Date().toISOString(), message_id: 'message-1', text: 'A question <script>bad()</script>',
  }] });
  return controller;
}

test('explicit reply uses the retained message identity and escapes the rendered body', () => {
  const controller = store();
  const detail = controller.selectSessionDetail(streamId);
  const dom = new JSDOM(renderTranscriptTimelineHtml(detail, undefined, { allowReplies: true }));
  assert.equal(dom.window.document.querySelector('script'), null);
  assert.equal(dom.window.document.querySelector<HTMLButtonElement>('.slot-chat-reply-btn')?.dataset.replyMessageId, 'message-1');
  assert.equal(renderTranscriptTimelineHtml(detail).includes('slot-chat-reply-btn'), false);
  dom.window.close();
});

test('retry retains reply binding and stable input identity while rotating the RPC request', async () => {
  const controller = store();
  const sends: Parameters<ChatSendBridge>[0][] = [];
  controller.setSendBridge(async args => { sends.push(args); return { ok: false, error: 'socket unavailable' }; });
  const id = controller.sendTurn(streamId, 'Yes', [], { reply_to_message_id: 'message-1', reply_to_question_id: 'question-1' });
  await new Promise(setImmediate);
  assert.equal(sends[0].replyToMessageId, 'message-1');
  assert.equal(sends[0].replyToQuestionId, 'question-1');
  assert.equal(controller.retryOptimisticSend(id), true);
  await new Promise(setImmediate);
  assert.equal(sends[1].replyToMessageId, 'message-1');
  assert.equal(sends[1].replyToQuestionId, 'question-1');
  assert.equal(sends[1].optimisticId, sends[0].optimisticId);
  assert.notEqual(sends[1].requestId, sends[0].requestId);
});

test('independent and reloaded composite clients cannot reuse a durable input identity', async (t) => {
  // A timestamp or process-local counter alone cannot separate concurrent tabs.
  t.mock.method(Date, 'now', () => 1789840000000);
  const first = store();
  const second = store();
  const reloaded = store();
  const accepted = new Map<string, string>();
  const conflicts: string[] = [];
  for (const controller of [first, second, reloaded]) {
    controller.setSendBridge(async args => {
      if (accepted.has(args.optimisticId)) conflicts.push(args.optimisticId);
      accepted.set(args.optimisticId, args.text);
      return { ok: true };
    });
  }
  const ids = [first.sendTurn(streamId, 'first approval'), second.sendTurn(streamId, 'independent input'),
    reloaded.sendTurn(streamId, 'heading answer', [], { reply_to_question_id: 'question-1' })];
  await new Promise(setImmediate);
  assert.deepEqual(conflicts, []);
  assert.equal(new Set(ids).size, 3);
  assert.equal(accepted.size, 3);
  for (const controller of [first, second, reloaded]) controller.dispose();
});
