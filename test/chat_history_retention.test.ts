import test from 'node:test';
import assert from 'node:assert/strict';
import { ChatStoreController } from '../renderer/src/chat_store_controller';

const target = 'fixture:chat';
const noise = 'fixture:pressure';
function row(streamId: string, daemon_seq: number, kind: string, text: string) {
  return { stream_id: streamId, host: 'fixture', session_name: streamId.split(':')[1],
    provider: 'claude', daemon_seq, kind, text, timestamp: '2026-09-28T10:00:00Z' };
}
function fixture() {
  const store = new ChatStoreController();
  store.applyFrame({ type: 'snapshot', sessions: [target, noise].map(stream_id => ({
    stream_id, host: 'fixture', session_name: stream_id.split(':')[1], status: 'open', working: false, provider: 'claude', session_kind: 'ordinary',
  })), events: [] });
  return store;
}
function pressure(store: ChatStoreController) {
  // Cross-stream traffic exceeds the real weighted cache budget. Both buckets
  // start with equal access ranks; the ordinary chat sorts before pressure.
  for (let i = 0; i < 30; i++) store.applyFrame({ type: 'chat.event', event: row(noise, 100 + i, 'TOOL_RESULT', 'noise'.repeat(50000)) });
}
function history(store: ChatStoreController) {
  store.applyFrame({ type: 'stream_events', stream_id: target, events: [
    row(target, 1, 'USER', 'Fixture user history'), row(target, 2, 'ASSIST_TEXT', 'Fixture assistant history'),
    row(target, 3, 'TOOL_RESULT', 'tool'.repeat(300000)),
    { ...row(target, 4, 'SYSTEM', 'Worked for 1s'), raw: { subtype: 'turn-summary' } },
  ] });
}

test('an attached chat retains its ordinary history under unrelated-stream cache pressure', () => {
  const store = fixture();
  store.setFocusedChatStreams([target]);
  history(store);
  pressure(store);
  const texts = store.selectSessionDetail(target, { includeDraft: false })!.transcriptItems.map(item => item.text);
  assert.ok(texts.includes('Fixture user history'));
  assert.ok(texts.includes('Fixture assistant history'));
});

test('leaving a chat releases its focus pin so inactive history remains evictable', () => {
  const store = fixture();
  store.setFocusedChatStreams([target]); history(store);
  store.setFocusedChatStreams([]); pressure(store);
  assert.equal(!!store.getState().eventBucketsByStream?.[target], false, 'inactive target is evicted');
});

test('focus changes preserve every attached pane and repeated synchronization is a no-op', () => {
  const store = fixture();
  store.setFocusedChatStreams([target, noise]);
  const before = store.getState();
  store.setFocusedChatStreams([noise, target, target]);
  assert.equal(store.getState(), before);
  store.setFocusedChatStreams([noise]);
  assert.equal(store.getState().eventBucketsByStream?.[target]?.pins.focused, undefined);
  assert.equal(store.getState().eventBucketsByStream?.[noise]?.pins.focused, true);
});

function bulk(streamId: string, count: number) {
  return Array.from({ length: count }, (_value, index) => row(streamId, 1000 + index, index % 6 === 0 ? 'USER' : 'TOOL_RESULT', `event ${index}`));
}

test('an attached chat that paged older history keeps more than the per-stream cap', () => {
  const store = fixture();
  store.setFocusedChatStreams([target]);
  store.setHistoryExpanded(target);
  store.applyFrame({ type: 'stream_events', stream_id: target, events: bulk(target, 900) });
  store.applyFrame({ type: 'stream_events', stream_id: target, events: bulk(target, 900).map(event => ({ ...event, daemon_seq: event.daemon_seq - 900 })) });
  assert.equal(store.getState().events.filter(event => event.stream_id === target).length, 1800);
  assert.equal(store.getState().eventBucketsByStream?.[target]?.events.length, 1800);
  store.applyFrame({ type: 'chat.event', event: row(target, 5000, 'ASSIST_TEXT', 'live after paging') });
  assert.equal(store.getState().eventBucketsByStream?.[target]?.events.length, 1801);
});

test('without paging the per-stream cap still evicts the oldest events', () => {
  const store = fixture();
  store.setFocusedChatStreams([target]);
  store.applyFrame({ type: 'stream_events', stream_id: target, events: bulk(target, 900) });
  store.applyFrame({ type: 'stream_events', stream_id: target, events: bulk(target, 900).map(event => ({ ...event, daemon_seq: event.daemon_seq + 900 })) });
  assert.equal(store.getState().events.filter(event => event.stream_id === target).length, 1200);
});

test('detaching a paged chat releases its expansion', () => {
  const store = fixture();
  store.setFocusedChatStreams([target, noise]);
  store.setHistoryExpanded(target);
  assert.deepEqual(store.getState().historyExpandedStreamIds, [target]);
  store.setFocusedChatStreams([noise]);
  assert.deepEqual(store.getState().historyExpandedStreamIds, []);
});
