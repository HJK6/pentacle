import assert from 'node:assert/strict';
import test from 'node:test';

import type {
  PentacleSessionSummary,
  SessionStatusCard,
  SessionStatusCardSpec,
} from '../src/types/pentacle.js';

const okSpec: SessionStatusCardSpec = {
  id: 'spec_repo__ok',
  label: 'Ready spec',
  ok: true,
  updated: '2026-07-11T00:00:00Z',
  status: 'in_progress',
};

const issueSpec: SessionStatusCardSpec = {
  id: 'obl-1',
  label: 'Problem spec',
  ok: false,
  updated: null,
  note: 'Needs attention',
};

function summary(status_card?: SessionStatusCard | null): PentacleSessionSummary {
  return {
    stream_id: 'host_b:lead',
    host: 'host_b',
    provider: 'codex',
    session_name: 'lead',
    last_event_at: '',
    last_text: '',
    last_kind: '',
    draft: '',
    pending: false,
    working: false,
    online: true,
    status_card,
  };
}

test('status-card specs remain optional and accept empty, ok, and issue entries', () => {
  assert.equal(summary(undefined).status_card, undefined);
  assert.deepEqual(summary({ updated_at: 'now', specs: [] }).status_card?.specs, []);
  assert.deepEqual(summary({ updated_at: 'now', specs: [okSpec, issueSpec] }).status_card?.specs, [okSpec, issueSpec]);
  assert.equal('note' in okSpec, false);
  assert.equal(issueSpec.updated, null);
  assert.equal(okSpec.status, 'in_progress');
});

test('session summaries accept optional model and effort metadata', () => {
  const enriched = { ...summary(), model: 'gpt-5.6-codex', effort: 'high' };
  assert.equal(enriched.model, 'gpt-5.6-codex');
  assert.equal(enriched.effort, 'high');
  assert.equal(summary().model, undefined);
});
