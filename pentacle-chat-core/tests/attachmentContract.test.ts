// Cross-lane attachment contract tests (spec
// public protocol contract, ## Attachment model).
// Verifies the ChatAttachment shape + MAX_CHAT_ATTACHMENTS bound, and that
// attachments thread end-to-end: optimistic send → optimistic event → rendered
// transcript item, for both the in-flight (sendOptimisticMessage) and held
// (enqueueOptimisticMessage) paths, with FIFO order preserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  MAX_CHAT_ATTACHMENTS,
  activateQueuedSends,
  enqueueOptimisticMessage,
  initialPentacleStreamState,
  selectSessionDetail,
  sendOptimisticMessage,
  type ChatAttachment,
  type PentacleSessionSummary,
  type PentacleStreamState,
} from '../src/index.ts';

const STREAM_ID = 'host_c:claude:photo';
const OPTIMISTIC_ID = 'optimistic_host_c_claude_photo_1';
const REQUEST_ID = 'send-req-photo-1';
const CREATED_AT = Date.parse('2026-06-17T12:00:00.000Z');

function session(overrides: Partial<PentacleSessionSummary> = {}): PentacleSessionSummary {
  return {
    stream_id: STREAM_ID,
    host: 'host_c',
    provider: 'claude',
    session_name: 'photo',
    last_event_at: '2026-06-17T12:00:00.000Z',
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

function attachment(overrides: Partial<ChatAttachment> = {}): ChatAttachment {
  return {
    key: 'attachments/abc123.jpg',
    mime: 'image/jpeg',
    width: 1024,
    height: 768,
    bytes: 204800,
    sha256: 'a'.repeat(64),
    ...overrides,
  };
}

function userTranscriptItem(state: PentacleStreamState) {
  const detail = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' });
  assert.ok(detail, 'expected a session detail');
  const userItems = detail.transcriptItems.filter((item) => item.isUser);
  assert.equal(userItems.length, 1, 'expected exactly one user bubble');
  return userItems[0];
}

test('MAX_CHAT_ATTACHMENTS is the spec D2 bound of 5', () => {
  assert.equal(MAX_CHAT_ATTACHMENTS, 5);
});

test('ChatAttachment requires key + mime; sizing/hash optional', () => {
  // Minimal valid shape (only the two required fields the client always sends).
  const minimal: ChatAttachment = { key: 'attachments/x.png', mime: 'image/png' };
  assert.equal(minimal.key, 'attachments/x.png');
  assert.equal(minimal.mime, 'image/png');
  // Full shape compiles with every optional field populated.
  const full = attachment();
  assert.equal(full.width, 1024);
  assert.equal(full.sha256?.length, 64);
});

test('ChatAttachment wire payload never carries daemon-local path fields', () => {
  const wire = attachment({ key: 'a'.repeat(64), mime: 'image/png' });
  const encoded = JSON.stringify(wire);

  assert.equal(Object.prototype.hasOwnProperty.call(wire, 'localPath'), false);
  assert.equal(encoded.includes('localPath'), false);
});

test('sendOptimisticMessage carries attachments onto the optimistic send + event + bubble', () => {
  const attachments = [attachment()];
  const state = sendOptimisticMessage(buildState(), {
    streamId: STREAM_ID,
    text: 'look at this',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: CREATED_AT,
    windowStartedAt: CREATED_AT,
    attachments,
  });

  // 1. optimistic send state
  const send = state.optimisticSends?.[OPTIMISTIC_ID];
  assert.ok(send, 'expected an optimistic send');
  assert.deepEqual(send.attachments, attachments);

  // 2. optimistic (client-origin) event
  const userEvent = state.events.find((e) => e.optimistic_id === OPTIMISTIC_ID && e.client_origin);
  assert.ok(userEvent, 'expected a client-origin user event');
  assert.deepEqual(userEvent.attachments, attachments);

  // 3. rendered transcript item
  assert.deepEqual(userTranscriptItem(state).attachments, attachments);
});

test('enqueueOptimisticMessage compatibility path carries attachments without a local hold', () => {
  const attachments = [attachment({ key: 'attachments/q.png', mime: 'image/png' })];
  const state = enqueueOptimisticMessage(buildState(), {
    streamId: STREAM_ID,
    text: 'native queue with photo',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: CREATED_AT,
    queuedAt: CREATED_AT,
    attachments,
  });

  const send = state.optimisticSends?.[OPTIMISTIC_ID];
  assert.ok(send, 'expected an optimistic send');
  assert.equal(send.turn_queued, undefined);
  assert.deepEqual(send.attachments, attachments);
  assert.deepEqual(userTranscriptItem(state).attachments, attachments);
});

test('enqueueOptimisticMessage compatibility path does not replace the active turn', () => {
  const activeTurn = {
    phase: 'working' as const,
    optimisticId: 'optimistic_active',
    sentAt: CREATED_AT - 1000,
  };
  let state = buildState({
    workingByStream: { [STREAM_ID]: activeTurn },
  });
  state = enqueueOptimisticMessage(state, {
    streamId: STREAM_ID,
    text: 'native one',
    optimisticId: `${OPTIMISTIC_ID}_a`,
    requestId: `${REQUEST_ID}_a`,
    createdAt: CREATED_AT,
    queuedAt: CREATED_AT,
  });
  state = enqueueOptimisticMessage(state, {
    streamId: STREAM_ID,
    text: 'native two',
    optimisticId: `${OPTIMISTIC_ID}_b`,
    requestId: `${REQUEST_ID}_b`,
    createdAt: CREATED_AT + 1,
    queuedAt: CREATED_AT + 1,
  });

  const activated = activateQueuedSends(state, [`${OPTIMISTIC_ID}_a`, `${OPTIMISTIC_ID}_b`]);
  assert.equal(activated.optimisticSends?.[`${OPTIMISTIC_ID}_a`]?.turn_queued, undefined);
  assert.equal(activated.optimisticSends?.[`${OPTIMISTIC_ID}_b`]?.turn_queued, undefined);
  assert.deepEqual(activated.workingByStream?.[STREAM_ID], activeTurn);
  const detail = selectSessionDetail(activated, STREAM_ID, { visibleCount: 'all' });
  const sendStates = detail?.transcriptItems
    .filter((item) => item.isUser)
    .map((item) => item.sendState);
  assert.deepEqual(sendStates, ['sending', 'sending']);
});

test('attachment FIFO order is preserved through to the rendered bubble', () => {
  const attachments = [
    attachment({ key: 'attachments/1.jpg' }),
    attachment({ key: 'attachments/2.jpg' }),
    attachment({ key: 'attachments/3.jpg' }),
  ];
  const state = sendOptimisticMessage(buildState(), {
    streamId: STREAM_ID,
    text: 'three photos',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: CREATED_AT,
    windowStartedAt: CREATED_AT,
    attachments,
  });
  const rendered = userTranscriptItem(state).attachments;
  assert.deepEqual(
    rendered?.map((a) => a.key),
    ['attachments/1.jpg', 'attachments/2.jpg', 'attachments/3.jpg'],
  );
});

test('a text-only send sets no attachments field (clean optimistic + bubble)', () => {
  const state = sendOptimisticMessage(buildState(), {
    streamId: STREAM_ID,
    text: 'no photos here',
    optimisticId: OPTIMISTIC_ID,
    requestId: REQUEST_ID,
    createdAt: CREATED_AT,
    windowStartedAt: CREATED_AT,
  });
  assert.equal(state.optimisticSends?.[OPTIMISTIC_ID]?.attachments, undefined);
  assert.equal(userTranscriptItem(state).attachments, undefined);
});
