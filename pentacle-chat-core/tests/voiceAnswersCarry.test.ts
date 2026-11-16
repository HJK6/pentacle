import assert from 'node:assert/strict';
import test from 'node:test';
import { applyPentacleEvent, applySnapshotWithOptimisticReconciliation, initialPentacleStreamState, selectSessionDetail, type PentacleEvent } from '../src/index.ts';

// Mobile carry/status vector at the platform-neutral boundary. No new reducer
// path or answer mutation is needed; the durable USER owns its metadata.
test('voice binding and partial/stale verdict survive summary-only reconnect', () => {
  const event: PentacleEvent = { daemon_seq: 1, host: 'fixture', provider: 'claude', session_id: 'fixture:assistant', session_name: 'assistant',
    stream_id: 'fixture:assistant', timestamp: '2026-10-07T12:00:00.000Z', kind: 'USER', text: 'Ship the first one',
    meta: { voice: { duration_s: 8 }, voice_answers_status: { state: 'bound', stale_keys: ['n-2:0'] }, voice_answers: {
      version: 1, recording_id: 'rec-1', blob_sha: 'sha256:audio', duration_s: 8,
      items: [1, 2].map(n => ({ key: `n-${n}:0`, question_id: `q-${n}`, notification_id: `n-${n}`, producer_stream_id: 'fixture:worker',
        surface_stream_id: 'fixture:assistant', prompt: `Prompt ${n}?`, segment: { start_s: n, end_s: n + 2 } })),
    } } };
  const before = applyPentacleEvent({ ...initialPentacleStreamState, sessions: [{
    stream_id: event.stream_id, host: event.host, session_name: event.session_name, provider: event.provider,
    last_event_at: event.timestamp, last_text: '', last_kind: '', draft: '', pending: false, working: false, online: true,
  }] }, event);
  const after = applySnapshotWithOptimisticReconciliation(before, { sessions: before.sessions, events: [] });
  assert.deepEqual(after.events[0].meta, event.meta);
  const row = selectSessionDetail(after, event.stream_id, { visibleCount: 'all', emitRenderTelemetry: false })!.transcriptItems[0];
  assert.deepEqual(row.voiceAnswersStatus, { state: 'bound', staleKeys: ['n-2:0'] });
  assert.equal(row.voiceAnswersItemCount, 2, 'stale items remain in the binding');
  assert.deepEqual(after.notifications, before.notifications, 'binding metadata never closes a question');
});
