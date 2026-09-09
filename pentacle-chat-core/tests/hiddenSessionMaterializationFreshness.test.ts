// Regression: mirror/visibility freshness (public-mobile-visibility
// public protocol contract). Under degraded/coalesced serving a hidden
// session's chat.event can reach a client before its inventory row (which
// carries `visibility`). The reducer used to materialize a list row from that
// event with visibility UNDEFINED, and the mobile guard (missing => visible)
// then flashed the hidden session into the chat list until the inventory row
// arrived and reclassified it. The fix fails closed at materialization: a
// per-session frame must not introduce a list row for a session absent from
// inventory; the row appears when the inventory row (carrying visibility)
// lands.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyPentacleEvent,
  applyPentacleSessionInventory,
  initialPentacleStreamState,
  type PentacleEvent,
  type PentacleSessionSummary,
  type PentacleStreamState,
} from '../src/index.ts';

const HIDDEN_STREAM = 'host_b:codex:hidden-worker';
const VISIBLE_STREAM = 'host_a:codex:visible-lead';

function emptyState(): PentacleStreamState {
  return { ...initialPentacleStreamState, connected: true, hasHydrated: true, sessions: [] };
}

function assistEvent(streamId: string, text: string): PentacleEvent {
  const [host, provider, name] = streamId.split(':');
  return {
    daemon_seq: 1,
    host,
    provider,
    session_id: streamId,
    session_name: name,
    stream_id: streamId,
    timestamp: '2026-08-11T04:31:00.000Z',
    kind: 'ASSIST',
    text,
  };
}

function inventoryRow(streamId: string, visibility: string): PentacleSessionSummary {
  const [host, provider, name] = streamId.split(':');
  return {
    stream_id: streamId,
    host,
    provider,
    session_name: name,
    last_event_at: '2026-08-11T04:30:05.000Z',
    last_text: '',
    last_kind: '',
    draft: '',
    pending: false,
    working: false,
    online: true,
    visibility,
  };
}

const streamIds = (state: PentacleStreamState) => state.sessions.map((s) => s.stream_id);

test('a chat.event for a session absent from inventory does not materialize a list row', () => {
  const afterEvent = applyPentacleEvent(emptyState(), assistEvent(HIDDEN_STREAM, 'secret worker output'));
  // No row is created — an unresolved-visibility session must never render.
  assert.deepEqual(streamIds(afterEvent), []);
  // The event itself is still recorded, so nothing is lost.
  assert.ok(afterEvent.events.some((e) => e.stream_id === HIDDEN_STREAM));
});

test('the row appears only when the inventory row lands, carrying its visibility', () => {
  const afterEvent = applyPentacleEvent(emptyState(), assistEvent(HIDDEN_STREAM, 'secret worker output'));
  const afterInventory = applyPentacleSessionInventory(afterEvent, [inventoryRow(HIDDEN_STREAM, 'hidden')]);
  const row = afterInventory.sessions.find((s) => s.stream_id === HIDDEN_STREAM);
  assert.ok(row, 'inventory introduces the session');
  // Visibility is now resolved, so a client list-filter can exclude it.
  assert.equal(row?.visibility, 'hidden');
});

test('a chat.event still updates a session already known from inventory', () => {
  const seeded = applyPentacleSessionInventory(emptyState(), [inventoryRow(VISIBLE_STREAM, 'default')]);
  const updated = applyPentacleEvent(seeded, assistEvent(VISIBLE_STREAM, 'fresh preview line'));
  const row = updated.sessions.find((s) => s.stream_id === VISIBLE_STREAM);
  assert.ok(row, 'the known session is retained');
  assert.equal(row?.last_text, 'fresh preview line');
  // Its resolved visibility is preserved across the event update.
  assert.equal(row?.visibility, 'default');
});
