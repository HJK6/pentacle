const test = require('node:test');
const assert = require('node:assert/strict');

const { decideUseChatClose } = require('../renderer/delete_session_gate');

test('decideUseChatClose returns true when chatStream connected and streamSession has stream_id', () => {
  const state = { chatStream: { connected: true } };
  const streamSession = { stream_id: 'node3:claude-20260519175756-mg0i' };
  assert.equal(decideUseChatClose(state, streamSession), true);
});

test('decideUseChatClose returns false when chatStream not connected (even with stream_id)', () => {
  const state = { chatStream: { connected: false } };
  const streamSession = { stream_id: 'node3:claude-20260519175756-mg0i' };
  assert.equal(decideUseChatClose(state, streamSession), false);
});

test('decideUseChatClose returns false when streamSession is null or has no stream_id', () => {
  const state = { chatStream: { connected: true } };
  assert.equal(decideUseChatClose(state, null), false);
  assert.equal(decideUseChatClose(state, undefined), false);
  assert.equal(decideUseChatClose(state, {}), false);
  assert.equal(decideUseChatClose(state, { stream_id: '' }), false);
  assert.equal(decideUseChatClose(state, { stream_id: null }), false);
});

test('decideUseChatClose returns false when state.chatStream is missing', () => {
  const streamSession = { stream_id: 'node3:claude-20260519175756-mg0i' };
  assert.equal(decideUseChatClose(null, streamSession), false);
  assert.equal(decideUseChatClose(undefined, streamSession), false);
  assert.equal(decideUseChatClose({}, streamSession), false);
  assert.equal(decideUseChatClose({ chatStream: null }, streamSession), false);
  assert.equal(decideUseChatClose({ chatStream: undefined }, streamSession), false);
});

test('decideUseChatClose result does not depend on any UI feature flag', () => {
  // Regression guard for spec_pentacle_deletesession_chatui_gating_2026_05_19.
  // The close-vs-kill decision must depend only on (state, streamSession) — not on
  // CONFIG.features.chatUi or any other UI-surface flag, whether passed as an
  // argument or read from ambient global state. Sharpened per Stage 3 code QA:
  // exercises BOTH flag values via a mock `global.CONFIG` so a regression that
  // re-reads a UI flag from ambient state (e.g.
  // `if (global.CONFIG?.features?.chatUi === false) return false;` inside
  // decideUseChatClose) is caught at runtime, not merely at signature level.
  const state = { chatStream: { connected: true } };
  const streamSession = { stream_id: 'node3:claude-20260519175756-mg0i' };

  const priorConfig = global.CONFIG;
  try {
    global.CONFIG = { features: { chatUi: true } };
    const resultWithChatUiTrue = decideUseChatClose(state, streamSession);
    global.CONFIG = { features: { chatUi: false } };
    const resultWithChatUiFalse = decideUseChatClose(state, streamSession);

    assert.equal(resultWithChatUiTrue, resultWithChatUiFalse, 'UI flag must not affect gate decision');
    assert.equal(resultWithChatUiTrue, true);

    // Symmetric check with state shapes that would yield false: still independent.
    const disconnectedState = { chatStream: { connected: false } };
    global.CONFIG = { features: { chatUi: true } };
    const disconnectedTrue = decideUseChatClose(disconnectedState, streamSession);
    global.CONFIG = { features: { chatUi: false } };
    const disconnectedFalse = decideUseChatClose(disconnectedState, streamSession);
    assert.equal(disconnectedTrue, disconnectedFalse, 'UI flag must not flip gate decision when state would yield false');
    assert.equal(disconnectedTrue, false);
  } finally {
    global.CONFIG = priorConfig;
  }
});
