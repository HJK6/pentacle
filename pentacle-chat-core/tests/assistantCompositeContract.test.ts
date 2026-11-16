import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyPentacleEvent,
  applyPentacleSnapshotMessage,
  initialPentacleStreamState,
  peekEventsForStream,
  recordOptimisticSendAcceptanceByRequestId,
  selectChatList,
  selectSessionDetail,
  sendOptimisticMessage,
  type PentacleEvent,
  type PentacleSessionSummary,
  type PentacleStreamState,
} from '../src/index.ts';

const STREAM_ID = 'fixture-host:assistant';
const CAPABILITIES = {
  pane: false,
  terminal: false,
  assistant_composite_v1: true,
  reply_metadata_v1: true,
};

function compositeSession(overrides: Record<string, unknown> = {}): PentacleSessionSummary {
  return {
    stream_id: STREAM_ID,
    host: 'fixture-host',
    provider: 'composite',
    session_name: 'assistant',
    session_kind: 'assistant_composite',
    session_generation: 'assistant-composite-v1',
    role: 'assistant_composite',
    visibility: 'visible',
    capabilities: CAPABILITIES,
    last_event_at: '2026-09-19T12:00:00.000Z',
    last_text: '',
    last_kind: '',
    draft: '',
    pending: false,
    working: false,
    online: true,
    ...overrides,
  } as PentacleSessionSummary;
}

function event(seq: number, overrides: Record<string, unknown> = {}): PentacleEvent {
  return {
    daemon_seq: seq,
    host: 'fixture-host',
    provider: 'composite',
    session_id: STREAM_ID,
    session_name: 'assistant',
    stream_id: STREAM_ID,
    timestamp: new Date(Date.UTC(2026, 8, 19, 12, 0, 0, seq % 60)).toISOString(),
    kind: seq % 2 === 0 ? 'ASSIST_TEXT' : 'USER',
    text: `composite message ${seq}`,
    ...overrides,
  } as PentacleEvent;
}

test('composite session metadata and reply metadata survive core selectors', () => {
  const session = compositeSession();
  let state = applyPentacleSnapshotMessage(initialPentacleStreamState, {
    sessions: [session],
    events: [event(1, {
      message_id: 'message-1',
      reply_to_message_id: 'message-0',
      reply_to_question_id: 'question-1',
      lane_id: 'lane-1',
      publish_kind: 'prose',
    })],
  });

  const chat = selectChatList(state)[0];
  assert.ok(chat);
  assert.equal((chat as any).sessionKind, 'assistant_composite');
  assert.deepEqual((chat as any).capabilities, CAPABILITIES);
  assert.equal((chat as any).sessionGeneration, 'assistant-composite-v1');
  assert.equal((chat as any).visibility, 'visible');

  const detail = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' });
  assert.ok(detail);
  assert.equal((detail as any).sessionKind, 'assistant_composite');
  assert.deepEqual((detail as any).capabilities, CAPABILITIES);
  assert.equal((detail as any).sessionGeneration, 'assistant-composite-v1');
  assert.equal(detail.transcriptItems[0]?.messageId, 'message-1');
  assert.equal(detail.transcriptItems[0]?.replyToMessageId, 'message-0');
  assert.equal(detail.transcriptItems[0]?.replyToQuestionId, 'question-1');
  assert.equal(detail.transcriptItems[0]?.laneId, 'lane-1');
  assert.equal(detail.transcriptItems[0]?.publishKind, 'prose');

  state = applyPentacleEvent(state, event(2, { text: 'second response' }));
  assert.equal((selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' }) as any).sessionKind, 'assistant_composite');
});

test('composite history is not capped at the ordinary per-stream client retention limit', () => {
  const events = Array.from({ length: 2001 }, (_, index) => event(index + 1));
  const state = applyPentacleSnapshotMessage(initialPentacleStreamState, {
    sessions: [compositeSession()],
    events,
  }, 2001);

  assert.equal(peekEventsForStream(state, STREAM_ID).length, 2001);
  assert.equal(state.events.filter((item) => item.stream_id === STREAM_ID).length, 2001);
});

test('optimistic composite input carries reply binding and accepted-send metadata without changing identity', () => {
  const state = sendOptimisticMessage(initialPentacleStreamState, {
    streamId: STREAM_ID,
    text: 'follow up',
    optimisticId: 'optimistic-composite-1',
    requestId: 'send-composite-1',
    createdAt: Date.parse('2026-09-19T12:00:00.000Z'),
    replyToMessageId: 'message-1',
    replyToQuestionId: 'question-1',
  } as any);
  const send = state.optimisticSends?.['optimistic-composite-1'];
  assert.ok(send);
  assert.equal(send.reply_to_message_id, 'message-1');
  assert.equal(send.reply_to_question_id, 'question-1');
  assert.equal(state.events[0]?.reply_to_message_id, 'message-1');
  assert.equal(state.events[0]?.reply_to_question_id, 'question-1');

  const accepted = recordOptimisticSendAcceptanceByRequestId(state, 'send-composite-1', {
    message_id: 'message-2',
    routing_state: 'queued',
    accepted_sequence: 44,
    queue_sequence: 44,
    action_committed: true,
  });
  const acceptedSend = accepted.optimisticSends?.['optimistic-composite-1'];
  assert.equal(acceptedSend?.optimistic_id, 'optimistic-composite-1');
  assert.equal(acceptedSend?.message_id, 'message-2');
  assert.equal(acceptedSend?.routing_state, 'queued');
  assert.equal(acceptedSend?.accepted_sequence, 44);
  assert.equal(acceptedSend?.queue_sequence, 44);
  assert.equal(acceptedSend?.action_committed, true);
});
