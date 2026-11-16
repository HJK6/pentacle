import assert from 'node:assert/strict';
import test from 'node:test';

import {
  initialPentacleStreamState,
  selectOpenQuestions,
  selectUnifiedFeed,
  setHostConfigProvider,
  type PentacleEvent,
  type PentacleSessionSummary,
  type PentacleStreamState,
} from '../src';

function session(overrides: Partial<PentacleSessionSummary>): PentacleSessionSummary {
  const streamId = overrides.stream_id || `${overrides.host || 'host_b'}:${overrides.session_name || 'session'}`;
  return {
    stream_id: streamId,
    host: overrides.host || 'host_b',
    provider: overrides.provider || 'codex',
    session_name: overrides.session_name || streamId.split(':').pop() || 'session',
    title: overrides.title || '',
    last_event_at: overrides.last_event_at || '2026-07-03T12:00:00.000Z',
    last_text: overrides.last_text || '',
    last_kind: overrides.last_kind || '',
    draft: '',
    pending: false,
    working: false,
    online: true,
    question: overrides.question,
  };
}

function event(overrides: Partial<PentacleEvent>): PentacleEvent {
  return {
    daemon_seq: overrides.daemon_seq ?? 1,
    host: overrides.host || 'host_b',
    provider: overrides.provider || 'codex',
    session_id: overrides.session_id || '',
    session_name: overrides.session_name || 'session',
    stream_id: overrides.stream_id || 'host_b:session',
    timestamp: overrides.timestamp || '2026-07-03T12:00:00.000Z',
    kind: overrides.kind || 'ASSIST',
    text: overrides.text || '',
    raw: overrides.raw,
    client_origin: overrides.client_origin,
  };
}

function state(overrides: Partial<PentacleStreamState>): PentacleStreamState {
  return {
    ...initialPentacleStreamState,
    ...overrides,
  };
}

test.beforeEach(() => {
  setHostConfigProvider({
    getHostOrder: () => ['host_b', 'host_c', 'host_a'],
    getHostTheme: (host) => ({
      label: host === 'host_b' ? 'hosta' : host === 'host_c' ? 'host_c' : 'host_a',
      color: host === 'host_b' ? '#3dff66' : host === 'host_c' ? '#29d4ff' : '#ff2e3e',
    }),
  });
});

test('selectUnifiedFeed returns assistant messages across streams in flat chronological order', () => {
  const sessions = [
    session({ stream_id: 'host_b:one', host: 'host_b', session_name: 'one', title: 'host_b lane' }),
    session({ stream_id: 'host_c:two', host: 'host_c', session_name: 'two', title: 'host_c lane' }),
    session({ stream_id: 'host_a:three', host: 'host_a', session_name: 'three', title: 'host_a lane' }),
  ];
  const events = [
    event({ stream_id: 'host_c:two', host: 'host_c', session_name: 'two', daemon_seq: 20, timestamp: '2026-07-03T12:02:00.000Z', text: 'second assist' }),
    event({ stream_id: 'host_b:one', host: 'host_b', session_name: 'one', daemon_seq: 10, timestamp: '2026-07-03T12:01:00.000Z', text: 'first assist' }),
    event({ stream_id: 'host_a:three', host: 'host_a', session_name: 'three', daemon_seq: 30, timestamp: '2026-07-03T12:03:00.000Z', text: 'third assist' }),
  ];

  const feed = selectUnifiedFeed(state({ sessions, events }));

  assert.deepEqual(feed.map((item) => item.text), ['first assist', 'second assist', 'third assist']);
  assert.deepEqual(feed.map((item) => item.chatTitle), ['host_b lane', 'host_c lane', 'host_a lane']);
  assert.deepEqual(feed.map((item) => item.accent), ['#3dff66', '#29d4ff', '#ff2e3e']);
  assert.ok(feed.every((item) => item.timestampLabel));
});

test('selectUnifiedFeed filters user, tool, thinking, system, hidden, and sub-agent rows', () => {
  const sessions = [session({ stream_id: 'host_b:one', host: 'host_b', session_name: 'one', title: 'host_b lane' })];
  const events = [
    event({ stream_id: 'host_b:one', kind: 'USER', daemon_seq: 1, text: 'operator' }),
    event({ stream_id: 'host_b:one', kind: 'TOOL_USE', daemon_seq: 2, text: 'tool call' }),
    event({ stream_id: 'host_b:one', kind: 'THINKING', daemon_seq: 3, text: 'thinking' }),
    event({ stream_id: 'host_b:one', kind: 'SYSTEM', daemon_seq: 4, text: 'system' }),
    event({ stream_id: 'host_b:one', kind: 'ASSIST', daemon_seq: 5, text: '✻ Thinking… (1s)' }),
    event({ stream_id: 'host_b:one', kind: 'ASSIST', daemon_seq: 6, text: 'visible assistant' }),
    event({ stream_id: 'host_b:one', kind: 'ASSIST_TEXT', daemon_seq: 7, text: 'assistant text' }),
  ];

  const feed = selectUnifiedFeed(state({ sessions, events }));

  assert.deepEqual(feed.map((item) => item.text), ['visible assistant', 'assistant text']);
});

test('selectOpenQuestions returns pending questions in chronological order', () => {
  const sessions = [
    session({
      stream_id: 'host_c:later',
      host: 'host_c',
      session_name: 'later',
      title: 'Later',
      last_event_at: '2026-07-03T12:03:00.000Z',
      question: { prompt: 'Later?', options: [{ index: 0, label: 'Yes' }] },
    }),
    session({ stream_id: 'host_b:quiet', host: 'host_b', session_name: 'quiet', last_event_at: '2026-07-03T12:02:00.000Z' }),
    session({
      stream_id: 'host_a:earlier',
      host: 'host_a',
      session_name: 'earlier',
      title: 'Earlier',
      last_event_at: '2026-07-03T12:01:00.000Z',
      question: { prompt: 'Earlier?', options: [{ index: 0, label: 'No' }] },
    }),
  ];

  const questions = selectOpenQuestions(state({ sessions }));

  assert.deepEqual(questions.map((item) => item.streamId), ['host_a:earlier', 'host_c:later']);
  assert.deepEqual(questions.map((item) => item.chatTitle), ['Earlier', 'Later']);
  assert.deepEqual(questions.map((item) => item.accent), ['#ff2e3e', '#29d4ff']);
});
