import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import { interpretPentacleEvent, type PentacleEvent } from '../src/index.ts';

function event(overrides: Partial<PentacleEvent>): PentacleEvent {
  return {
    daemon_seq: 42,
    host: 'host_c',
    provider: 'claude',
    session_id: 'sess-1',
    session_name: 'claude-host_c-1',
    stream_id: 'host_c:claude-host_c-1',
    timestamp: '2025-01-15T13:00:00.000Z',
    kind: 'TOOL_RESULT',
    text: '',
    ...overrides,
  };
}

// The agent-question ROW must carry the notification_id so a client can anchor
// the resolved answer projection to the question's original chronological
// position (join key), exactly as the answer-echo row already does.
test('durable ask retains its anchor when CLI delivery warning precedes the result', () => {
  const text = readFileSync(new URL('./fixtures/promptAskWarningResult.txt', import.meta.url), 'utf8');
  const interpreted = interpretPentacleEvent(event({ text }));
  assert.equal(interpreted.caseId, 'agent-question-ask');
  assert.equal(interpreted.notificationId, 'c070f70c-956f-55e0-8d33-63b233f08f8d');
  assert.match(interpreted.text, /^Asked: Example question /);
});

test('mixed output does not invent an ask from failed, malformed, or ambiguous payloads', () => {
  const result = { type: 'prompt.ask.ok', ok: true,
    question: { question_id: 'q1', notification_id: 'n1', envelope: { title: 'Continue?' } } };
  const invalid = [
    JSON.stringify({ ...result, ok: false }),
    JSON.stringify({ ...result, question: { question_id: 'q1' } }),
    JSON.stringify({ ...result, type: 'prompt.ask.error' }),
    JSON.stringify(result).slice(0, -1),
    JSON.stringify(result) + '\n' + JSON.stringify({ ...result, question: { ...result.question, question_id: 'q2' } }),
  ];
  for (const text of invalid) {
    assert.notEqual(interpretPentacleEvent(event({ text: 'Exit code 1\nwarning\n' + text })).caseId, 'agent-question-ask');
  }
  assert.notEqual(interpretPentacleEvent(event({ kind: 'ASSIST', text: 'warning\n' + JSON.stringify(result) })).caseId, 'agent-question-ask');
});

test('agent-question-ask row carries notification_id from the prompt.ask.ok payload', () => {
  const payload = {
    type: 'prompt.ask.ok',
    notification: { notification_id: 'notif-abc', title: 'Pick a color' },
    question: { question_id: 'q1', envelope: { title: 'Pick a color', question_id: 'q1' } },
  };
  const interpreted = interpretPentacleEvent(event({ text: JSON.stringify(payload) }));
  assert.equal(interpreted.caseId, 'agent-question-ask');
  assert.equal(interpreted.notificationId, 'notif-abc');
});

test('question_id-scoped notification_id is used when payload.notification is absent', () => {
  const payload = {
    type: 'prompt.ask.ok',
    question: { question_id: 'q1', notification_id: 'notif-xyz', envelope: { title: 'Deploy?' } },
  };
  const interpreted = interpretPentacleEvent(event({ text: JSON.stringify(payload) }));
  assert.equal(interpreted.caseId, 'agent-question-ask');
  assert.equal(interpreted.notificationId, 'notif-xyz');
});

test('ask row without any notification_id stays a valid question row (no fabricated id)', () => {
  const payload = {
    type: 'prompt.ask.ok',
    question: { question_id: 'q1', envelope: { title: 'Continue?' } },
  };
  const interpreted = interpretPentacleEvent(event({ text: JSON.stringify(payload) }));
  assert.equal(interpreted.caseId, 'agent-question-ask');
  assert.equal(interpreted.notificationId, undefined);
});
