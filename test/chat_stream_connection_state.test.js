const test = require('node:test');
const assert = require('node:assert/strict');

const { applyVersionedConnectionState } = require('../renderer/chat_stream_connection_state');

function fixture() {
  const classes = new Set();
  return {
    chatStream: { connected: false, stateVersion: -1, error: null },
    classes,
    apply(payload) {
      return applyVersionedConnectionState(this.chatStream, payload, (degraded) => {
        if (degraded) classes.add('chat-stream-degraded');
        else classes.delete('chat-stream-degraded');
      });
    },
  };
}

test('newer connected push wins over a stale disconnected state pull', () => {
  const view = fixture();

  assert.equal(view.apply({ connected: true, state_version: 1 }), true);
  assert.equal(view.apply({ connected: false, state_version: 0 }), false);

  assert.equal(view.chatStream.connected, true);
  assert.equal(view.classes.has('chat-stream-degraded'), false);
});

test('stale disconnected pull followed by newer connected push becomes live', () => {
  const view = fixture();

  assert.equal(view.apply({ connected: false, state_version: 0 }), true);
  assert.equal(view.apply({ connected: true, state_version: 1 }), true);

  assert.equal(view.chatStream.connected, true);
  assert.equal(view.classes.has('chat-stream-degraded'), false);
});

test('disconnected refusal reason is retained for the degraded sidebar banner', () => {
  const view = fixture();

  assert.equal(view.apply({
    connected: false,
    error: 'operator_auth_v2_private_path_required',
    state_version: 0,
  }), true);

  assert.equal(view.chatStream.error, 'operator_auth_v2_private_path_required');
  assert.equal(view.classes.has('chat-stream-degraded'), true);
});

test('equal-version generic startup state cannot erase a refusal reason', () => {
  const view = fixture();

  assert.equal(view.apply({
    connected: false,
    error: 'operator_auth_v2_private_path_required',
    state_version: 2,
  }), true);
  assert.equal(view.apply({
    connected: false,
    error: 'Stream disconnected',
    state_version: 2,
  }), true);

  assert.equal(view.chatStream.error, 'operator_auth_v2_private_path_required');
});

// ── web host restart: the input-freeze bug and its fix ───────────────────────
// (spec_pentacle__web_reconnect_input_frozen_2026_09)

test('a fresh host connected:true at a reset (lower) version is rejected as stale — the input-freeze bug', () => {
  const view = fixture();
  // A long web session left the browser degraded at a HIGH state_version: the
  // old host process accumulated connection-state churn before it was stopped.
  assert.equal(view.apply({ connected: false, error: 'Stream disconnected', state_version: 7 }), true);
  assert.equal(view.chatStream.connected, false);
  assert.equal(view.classes.has('chat-stream-degraded'), true, 'input frozen (degraded)');

  // `pentacle-web-start stop && pentacle-web-start` brings up a FRESH host whose
  // state_version namespace restarts, so its connected:true arrives at a low
  // version — and is gated as stale. Without a reconnect re-sync the live app
  // stays frozen (connected:false / degraded) until a manual reload.
  assert.equal(view.apply({ connected: true, state_version: 1 }), false,
    'the fresh host recovery frame is dropped as stale');
  assert.equal(view.chatStream.connected, false, 'still frozen');
  assert.equal(view.classes.has('chat-stream-degraded'), true);
});

test('resetting the version baseline on reconnect lets the fresh host connected:true through', () => {
  const view = fixture();
  view.apply({ connected: false, error: 'Stream disconnected', state_version: 7 });
  assert.equal(view.chatStream.connected, false);

  // The fix: app.js resets the version baseline to its initial -1 on websocket
  // reconnect (before re-pulling the fresh host's snapshot), so the fresh host's
  // connected:true at its reset version is accepted and the input re-enables.
  view.chatStream.stateVersion = -1;
  assert.equal(view.apply({ connected: true, state_version: 1 }), true);
  assert.equal(view.chatStream.connected, true, 'input restored after the reconnect re-sync');
  assert.equal(view.classes.has('chat-stream-degraded'), false);
});
