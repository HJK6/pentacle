import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyPentacleEvent,
  applySnapshotWithOptimisticReconciliation,
  initialPentacleStreamState,
  markOptimisticAckedByRequestId,
  markOptimisticDispatchedByRequestId,
  selectSessionDetail,
  sendOptimisticMessage,
  type ChatAttachment,
  type PentacleEvent,
  type PentacleSessionSummary,
  type PentacleStreamState,
} from '../src/index.ts';

// After an image send the daemon rewrites the send into an agent-facing wrapper
// ("Look at the image file at <attachment-root>/<sha>.<ext>, then respond to the
// user's message: <caption>") before injecting it into the agent; the transcript
// re-emits that wrapper as a server USER echo. Because the wrapper prefix defeats
// the equality-based dedup, the feed shows two USER rows for one send. These
// tests pin: the wrapper echo collapses into the operator's optimistic bubble
// (image + caption), and only the caption renders.

const STREAM_ID = 'host_c:codex:one';
const OPTIMISTIC_ID = 'optimistic_host_c_codex_one_1';
const REQUEST_ID = 'send-req-1';
const CREATED_AT = Date.parse('2026-09-11T12:00:00.000Z');
const CAPTION = 'Fix Cedar Splunk Polling';
const SHA = 'a3f5c9d2e1b4a6f7c8d9e0a1b2c3d4e5f60718293a4b5c6d7e8f9012345678ab';
const ATTACHMENT_PATH = `/home/agent/.cache/pentacle-stream/attachments/${SHA}.jpg`;

function session(overrides: Partial<PentacleSessionSummary> = {}): PentacleSessionSummary {
  return {
    stream_id: STREAM_ID,
    host: 'host_c',
    provider: 'codex',
    session_name: 'one',
    last_event_at: '2026-09-11T12:00:00.000Z',
    last_text: '',
    last_kind: '',
    draft: '',
    pending: false,
    working: false,
    online: true,
    ...overrides,
  };
}

function buildState(overrides: Partial<PentacleStreamState> = {}): PentacleStreamState {
  return {
    ...initialPentacleStreamState,
    connected: true,
    sessions: [session()],
    ...overrides,
  };
}

function imageAttachment(overrides: Partial<ChatAttachment> = {}): ChatAttachment {
  return { key: SHA, mime: 'image/jpeg', width: 120, height: 240, ...overrides };
}

function ackedImageSend(caption = CAPTION): PentacleStreamState {
  const queued = sendOptimisticMessage(buildState(), {
    streamId: STREAM_ID,
    text: caption,
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: CREATED_AT,
    windowStartedAt: CREATED_AT,
    attachments: [imageAttachment()],
  });
  const dispatched = markOptimisticDispatchedByRequestId(queued, REQUEST_ID, CREATED_AT + 10);
  return markOptimisticAckedByRequestId(dispatched, REQUEST_ID, CREATED_AT + 20);
}

function wrapperEcho(overrides: Partial<PentacleEvent> = {}): PentacleEvent {
  return {
    daemon_seq: 42,
    host: 'host_c',
    provider: 'codex',
    session_id: STREAM_ID,
    session_name: 'one',
    stream_id: STREAM_ID,
    timestamp: '2026-09-11T12:00:01.000Z',
    kind: 'USER',
    text: `Look at the image file at ${ATTACHMENT_PATH}, then respond to the user's message: ${CAPTION}`,
    ...overrides,
  };
}

test('live wrapper echo collapses into the optimistic image bubble, showing only the caption', () => {
  const next = applyPentacleEvent(ackedImageSend(), wrapperEcho());
  const detail = selectSessionDetail(next, STREAM_ID, { visibleCount: 'all' });
  assert.equal(detail?.transcriptItems.length, 1);
  const row = detail?.transcriptItems[0];
  assert.equal(row?.optimisticId, OPTIMISTIC_ID);
  assert.equal(row?.text, CAPTION);
  assert.equal(row?.attachments?.length, 1);
  assert.equal(row?.attachments?.[0].key, SHA);
});

test('snapshot replay of the wrapper echo also yields one caption bubble', () => {
  const state = ackedImageSend();
  const replayed = applySnapshotWithOptimisticReconciliation(state, {
    events: [wrapperEcho()],
    sessions: [session({ last_event_at: '2026-09-11T12:00:01.000Z' })],
  });
  const detail = selectSessionDetail(replayed, STREAM_ID, { visibleCount: 'all' });
  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0]?.text, CAPTION);
  assert.equal(detail?.transcriptItems[0]?.optimisticId, OPTIMISTIC_ID);
});

test('caption-less image send collapses the no-caption wrapper form', () => {
  const state = ackedImageSend('');
  const echo = wrapperEcho({ text: `Image at ${ATTACHMENT_PATH}` });
  const detail = selectSessionDetail(applyPentacleEvent(state, echo), STREAM_ID, { visibleCount: 'all' });
  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0]?.optimisticId, OPTIMISTIC_ID);
});

test('a plain (no-attachment) send still collapses only on exact text equality', () => {
  const queued = sendOptimisticMessage(buildState(), {
    streamId: STREAM_ID,
    text: 'hello there',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: CREATED_AT,
    windowStartedAt: CREATED_AT,
  });
  const acked = markOptimisticAckedByRequestId(
    markOptimisticDispatchedByRequestId(queued, REQUEST_ID, CREATED_AT + 10),
    REQUEST_ID,
    CREATED_AT + 20,
  );
  const echo = wrapperEcho({ text: 'hello there' });
  const detail = selectSessionDetail(applyPentacleEvent(acked, echo), STREAM_ID, { visibleCount: 'all' });
  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0]?.text, 'hello there');
});

test('a wrapper-shaped session summary does not append a second bubble on snapshot', () => {
  const state = ackedImageSend();
  const echo = wrapperEcho();
  const replayed = applySnapshotWithOptimisticReconciliation(state, {
    events: [echo],
    sessions: [session({ last_text: echo.text, last_kind: 'USER', last_event_at: '2026-09-11T12:00:01.000Z' })],
  });
  const detail = selectSessionDetail(replayed, STREAM_ID, { visibleCount: 'all' });
  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0]?.text, CAPTION);
});

test('wrapper session summary stays collapsed after live reconcile/prune then snapshot replay', () => {
  const afterLive = applyPentacleEvent(ackedImageSend(), wrapperEcho());
  const replay = applySnapshotWithOptimisticReconciliation(afterLive, {
    events: [],
    sessions: [session({ last_text: wrapperEcho().text, last_kind: 'USER', last_event_at: '2026-09-11T12:00:01.000Z' })],
  });
  const detail = selectSessionDetail(replay, STREAM_ID, { visibleCount: 'all' });
  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0]?.text, CAPTION);
});

test('a duplicate live wrapper echo replay stays one caption bubble', () => {
  // After the first live wrapper echo reconciles and prunes the send, a second
  // live delivery of the same wrapper USER event re-enters the seq-correlated
  // merge path. Without carrying the prior caption there, the replay re-rendered
  // the daemon wrapper text plus a caption fallback (two rows). Regression for
  // the duplicate/reconnect preserve path (spec-QA final-candidate finding).
  const afterLive = applyPentacleEvent(ackedImageSend(), wrapperEcho());
  const dup = applyPentacleEvent(afterLive, wrapperEcho());
  const detail = selectSessionDetail(dup, STREAM_ID, { visibleCount: 'all' });
  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0]?.text, CAPTION);
  assert.equal(detail?.transcriptItems[0]?.optimisticId, OPTIMISTIC_ID);
});

test('an unrelated server USER row is not swallowed by an image send awaiting its echo', () => {
  const state = ackedImageSend();
  const unrelated = wrapperEcho({ daemon_seq: 43, text: 'a totally different agent message' });
  const detail = selectSessionDetail(applyPentacleEvent(state, unrelated), STREAM_ID, { visibleCount: 'all' });
  // The optimistic image bubble plus the unrelated row: two rows, wrapper not matched.
  assert.equal(detail?.transcriptItems.length, 2);
});
