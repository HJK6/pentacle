import assert from 'node:assert/strict';
import test from 'node:test';
import { applyPentacleEvent, initialPentacleStreamState, selectSessionDetail, type PentacleEvent, type PentacleSendMeta } from '../src/index.ts';

// Port of mobile voiceAnswersWire's durable echo vector. The host fake-socket
// suite separately proves the existing composite send leg and retry identity.
test('wire metadata survives USER ingestion and projects the daemon verdict', () => {
  const meta: PentacleSendMeta = { voice: { duration_s: 7 }, voice_answers: {
    version: 1, recording_id: 'rec-9', blob_sha: 'sha256:audio', duration_s: 7,
    items: [{ key: 'n-1:0', question_id: 'q-1', notification_id: 'n-1', producer_stream_id: 'fixture:worker',
      surface_stream_id: 'fixture:assistant', prompt: 'Ship it?', segment: { start_s: 0, end_s: 3.2 } }],
  }, voice_answers_status: { state: 'bound', stale_keys: [] } };
  const event: PentacleEvent = { daemon_seq: 1, host: 'fixture', provider: 'claude', session_id: 'fixture:assistant', session_name: 'assistant',
    stream_id: 'fixture:assistant', timestamp: '2026-10-07T12:00:00.000Z', kind: 'USER', text: 'Ship it', meta };
  const state = applyPentacleEvent(initialPentacleStreamState, JSON.parse(JSON.stringify(event)));
  assert.deepEqual(state.events[0].meta, meta);
  const row = selectSessionDetail(state, event.stream_id, { visibleCount: 'all', emitRenderTelemetry: false })!.transcriptItems[0];
  assert.deepEqual(row.voiceAnswersStatus, { state: 'bound', staleKeys: [] });
  assert.equal(row.voiceAnswersItemCount, 1);
});
