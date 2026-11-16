import test from 'node:test';
import assert from 'node:assert/strict';
import fixture from './fixtures/limits_provider_failure.json';
import { ChatStoreController } from '../renderer/src/chat_store_controller';

test('shared renderer cache accepts retained provider failure on startup and live recovery', () => {
  const store = new ChatStoreController();
  try {
    store.applyFrame({ type: 'snapshot', sessions: [], events: [], ...fixture.failed });
    assert.deepEqual(store.getLimits().map(row => row.pct), [17, 10, 26]);
    assert.equal(store.getLimitsHealth()?.claude.error?.message, fixture.failed.limits_health.claude.error.message);
    store.applyFrame({ type: 'limits.update', ...fixture.healthy });
    assert.equal(store.getLimitsHealth()?.claude.outcome, 'ok');
    store.applyFrame({ type: 'limits.update', ...fixture.failed });
    assert.equal(store.getLimitsHealth()?.claude.outcome, 'provider_error');
    store.applyFrame({ type: 'limits.update', ...fixture.healthy, limits_health: { broken: true } });
    assert.equal(store.getLimitsHealth()?.claude.outcome, 'provider_error');
  } finally { store.dispose(); }
});

test('shared renderer cache accepts same-second precision but rejects later upstream stamps', () => {
  const store = new ChatStoreController();
  const pair = structuredClone(fixture.healthy);
  pair.limits[2].upstream_reported_at = '2026-09-04T04:50:00.089123Z';
  pair.limits[2].probed_at = '2026-09-04T04:50:00Z';
  try {
    store.applyFrame({ type: 'snapshot', sessions: [], events: [], ...pair });
    assert.deepEqual(store.getLimits().map(row => row.pct), [17, 10, 26]);
    pair.limits[2].upstream_reported_at = '2026-09-04T04:50:01Z';
    pair.limits[2].pct = 90;
    store.applyFrame({ type: 'limits.update', ...pair });
    assert.equal(store.getLimits()[2].pct, 26);
  } finally { store.dispose(); }
});
