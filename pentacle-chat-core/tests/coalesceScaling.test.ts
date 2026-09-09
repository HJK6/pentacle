import assert from 'node:assert/strict';
import test from 'node:test';
import {
  coalesceInterpretedEvents,
  interpretPentacleEvent,
  type PentacleEvent,
} from '../src/index.ts';

test('retained smart-list preview coalescing checks only the bounded progressive tail', () => {
  let textReads = 0;
  const rows = Array.from({ length: 1200 }, (_, index) => {
    const event: PentacleEvent = {
      stream_id: 'host_c:codex:retained',
      host: 'host_c',
      provider: 'codex',
      session_id: 'retained',
      session_name: 'retained',
      daemon_seq: index + 1,
      timestamp: new Date(index).toISOString(),
      kind: 'ASSIST',
      text: `retained row ${index + 1}`,
    };
    const interpreted = interpretPentacleEvent(event, 'Codex');
    return Object.defineProperty({ ...interpreted }, 'text', {
      enumerable: true,
      get() {
        textReads += 1;
        return event.text;
      },
    });
  });

  const result = coalesceInterpretedEvents(rows);

  assert.equal(result.length, 1200);
  assert.equal(textReads, 5990);
});

test('long equal text remains distinct without a shared replay identity', () => {
  const text = 'This independently delivered message is intentionally longer than forty characters.';
  const rows = [1, 2].map((daemonSeq) => interpretPentacleEvent({
    stream_id: 'host_c:codex:dedupe',
    host: 'host_c',
    provider: 'codex',
    session_id: 'dedupe',
    session_name: 'dedupe',
    daemon_seq: daemonSeq,
    timestamp: new Date(daemonSeq).toISOString(),
    kind: 'ASSIST',
    text,
  }, 'Codex'));

  assert.equal(coalesceInterpretedEvents(rows).length, 2);
});
