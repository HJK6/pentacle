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
