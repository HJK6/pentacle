import assert from 'node:assert/strict';
import test from 'node:test';
import { coalesceInterpretedEvents, interpretPentacleEvent } from '../src/services/pentacleEventInterpreter.ts';
import type { PentacleEvent } from '../src/types/pentacle.ts';

test('terminal provider errors stay visible and replay once in the shared chat renderer', () => {
  const event: PentacleEvent = {
    host: 'example', provider: 'codex', session_name: 'v2-example', session_id: 'session-example',
    stream_id: 'example:v2-example', timestamp: '2026-09-30T12:00:03Z', kind: 'ERROR',
    text: 'Codex turn failed: selected model is at capacity (server_overloaded).',
    raw: { source: 'structured', transport: 'codex-rollout', jsonl_record_uuid: 'codex-task-complete:turn-example', jsonl_event_index: 0 },
  };
  const interpreted = interpretPentacleEvent(event);
  assert.equal(interpreted.hidden, false);
  assert.equal(interpreted.label, 'ERROR');
  assert.equal(interpreted.displayRule, 'bubble:assistant');
  assert.equal(interpreted.text, event.text);
  assert.equal(coalesceInterpretedEvents([interpreted, interpretPentacleEvent({ ...event })]).length, 1);
});
