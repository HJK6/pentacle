import assert from 'node:assert/strict';
import test from 'node:test';
import { initialPentacleStreamState, selectSessionDetail, sendOptimisticMessage, markOptimisticFailedByRequestId, type PentacleEvent, type PentacleSendMeta, type PentacleStreamState } from '../src/index.ts';

const stream = 'fixture:assistant';
const session = { stream_id: stream, host: 'fixture', provider: 'claude', session_name: 'assistant', last_event_at: '2026-10-07T12:00:00.000Z',
  last_text: '', last_kind: '', draft: '', pending: false, working: false, online: true };
const event: PentacleEvent = { daemon_seq: 1, host: 'fixture', provider: 'claude', session_id: stream, session_name: 'assistant', stream_id: stream,
  timestamp: '2026-10-07T12:00:01.000Z', kind: 'USER', text: 'Durable transcript' };
function state(meta?: PentacleSendMeta): PentacleStreamState {
  return { ...initialPentacleStreamState, sessions: [session], events: [{ ...event, meta }] };
}
function row(value: PentacleStreamState) {
  return selectSessionDetail(value, stream, { visibleCount: 'all', emitRenderTelemetry: false })!.transcriptItems[0];
}
const binding = (count: number): PentacleSendMeta['voice_answers'] => ({ version: 1, recording_id: 'rec-label', blob_sha: 'sha256:audio', duration_s: 7,
  items: Array.from({ length: count }, (_, n) => ({ key: `n-${n}:0`, question_id: `q-${n}`, notification_id: `n-${n}`,
    producer_stream_id: 'fixture:worker', surface_stream_id: stream, prompt: `Prompt ${n}?`, segment: { start_s: n, end_s: n + 2 } })) });

test('mobile count vectors: two bound items, one bound item, and plain note', () => {
  for (const count of [1, 2, 20]) assert.equal(row(state({ voice_answers: binding(count) })).voiceAnswersItemCount, count);
  assert.equal(row(state({ voice: { duration_s: 7 } })).voiceAnswersItemCount, undefined);
  assert.equal(row(state({ voice_answers: binding(0) })).voiceAnswersItemCount, undefined);
});

test('same row identity never caches an obsolete status, reason, stale set or count', () => {
  let current = state({ voice_answers: binding(2), voice_answers_status: { state: 'bound', stale_keys: [] } });
  let previous = row(current);
  const metas: PentacleSendMeta[] = [
    { voice_answers: binding(2), voice_answers_status: { state: 'bound', stale_keys: ['n-1:0'] } },
    { voice_answers: binding(2), voice_answers_status: { state: 'dropped', reason: 'unknown_question' } },
    { voice_answers: binding(2), voice_answers_status: { state: 'dropped', reason: 'identity_mismatch' } },
    { voice_answers: binding(1), voice_answers_status: { state: 'dropped', reason: 'identity_mismatch' } },
    {},
  ];
  for (const meta of metas) {
    current = { ...current, events: [{ ...event, meta }] };
    const next = row(current);
    assert.notEqual(next, previous);
    assert.equal(next.voiceAnswersStatus?.reason, meta.voice_answers_status?.reason);
    assert.deepEqual(next.voiceAnswersStatus?.staleKeys, meta.voice_answers_status ? meta.voice_answers_status.stale_keys || [] : undefined);
    assert.equal(next.voiceAnswersItemCount, meta.voice_answers?.items.length);
    previous = next;
  }
  assert.equal(row(current), previous, 'unchanged rows retain existing cache reuse');
});


test('only a voice_answers_invalid send projects the explicit plain-note affordance', () => {
  const pending = sendOptimisticMessage({ ...state(), events: [] }, {
    streamId: stream, text: 'Ship it', optimisticId: 'opt-1', requestId: 'req-1', createdAt: Date.parse(event.timestamp),
  });
  assert.equal(row(pending).voiceAnswersInvalid, undefined);
  const failed = markOptimisticFailedByRequestId(pending, 'req-1', 'voice_answers_invalid');
  assert.equal(row(failed).voiceAnswersInvalid, true);
  const other = markOptimisticFailedByRequestId(pending, 'req-1', 'backend_busy');
  assert.equal(row(other).voiceAnswersInvalid, undefined);
  assert.notEqual(row(failed), row(other), 'same failed row invalidates when failure reason changes');
});
