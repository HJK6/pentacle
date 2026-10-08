import assert from 'node:assert/strict';
import test from 'node:test';
import { initialPentacleStreamState, selectSessionDetail, type PentacleEvent, type PentacleSendMeta } from '../src/index.ts';

const stream = 'fixture:assistant';
const session = { stream_id: stream, host: 'fixture', provider: 'claude', session_name: 'assistant',
  last_event_at: '2026-10-07T12:00:00.000Z', last_text: '', last_kind: '', draft: '', pending: false, working: false, online: true };
function row(seq: number, meta?: PentacleSendMeta, kind: PentacleEvent['kind'] = 'USER'): PentacleEvent {
  return { daemon_seq: seq, host: 'fixture', provider: 'claude', session_id: stream, session_name: 'assistant',
    stream_id: stream, timestamp: `2026-10-07T12:00:0${seq}.000Z`, kind, text: `voice ${seq}`, meta };
}
function project(events: PentacleEvent[]) {
  return selectSessionDetail({ ...initialPentacleStreamState, connected: true, sessions: [session], events }, stream,
    { visibleCount: 'all', emitRenderTelemetry: false })!.transcriptItems;
}

test('mobile USER vectors: dropped, bound with stale items, and plain voice', () => {
  const items = project([
    row(1, { voice: { duration_s: 7 }, voice_answers_status: { state: 'dropped', reason: 'unknown_question', stale_keys: [] } }),
    row(2, { voice: { duration_s: 7 }, voice_answers_status: { state: 'bound', stale_keys: ['n-1:0'] } }),
    row(3, { voice: { duration_s: 7 } }),
  ]);
  assert.deepEqual(items.find(item => item.text === 'voice 1')!.voiceAnswersStatus, { state: 'dropped', reason: 'unknown_question', staleKeys: [] });
  assert.deepEqual(items.find(item => item.text === 'voice 2')!.voiceAnswersStatus, { state: 'bound', staleKeys: ['n-1:0'] });
  assert.equal(items.find(item => item.text === 'voice 3')!.voiceAnswersStatus, undefined);
});

test('P6 status is USER-only and malformed status fields are safely ignored', () => {
  const metas = [null, {}, { state: 'answered' }, { state: 'dropped', reason: 42, stale_keys: [1, 'n-1:0', null] }];
  const items = project(metas.map((status, index) => row(index + 1, { voice_answers_status: status } as unknown as PentacleSendMeta)));
  for (let i = 1; i < 4; i++) assert.equal(items.find(item => item.text === `voice ${i}`)!.voiceAnswersStatus, undefined);
  assert.deepEqual(items.find(item => item.text === 'voice 4')!.voiceAnswersStatus, { state: 'dropped', staleKeys: ['n-1:0'] });
  assert.equal(project([row(1, { voice_answers_status: { state: 'dropped' } }, 'ASSIST')])[0].voiceAnswersStatus, undefined);
});
