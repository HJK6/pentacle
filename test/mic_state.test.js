const test = require('node:test');
const assert = require('node:assert/strict');

const {
  computeBusyBannerState,
  shouldRenderAlwaysOnUi,
  shouldShowAlwaysOn,
  computeSilentState,
  computeAnswerWindowState,
} = require('../renderer/mic-state');

test('test_busy_banner_state_other_caller', () => {
  assert.deepEqual(computeBusyBannerState({
    status: { mode: 'clipboard', caller: 'hosta' },
    localHostId: 'hostb',
  }), {
    visible: true,
    mode: 'clipboard',
    caller: 'hosta',
    pausedMode: null,
    pausedCaller: null,
    lastError: null,
  });
});

test('test_busy_banner_state_self_caller', () => {
  assert.deepEqual(computeBusyBannerState({
    status: { mode: 'clipboard', caller: 'hostb' },
    localHostId: 'hostb',
  }), {
    visible: false,
    mode: 'clipboard',
    caller: 'hostb',
    pausedMode: null,
    pausedCaller: null,
    lastError: null,
  });
});

test('test_busy_banner_hidden_when_other_caller_is_always_on', () => {
  // The sample mic service auto-starts always-on at boot. Because the service
  // auto-pauses always-on for any incoming clipboard/meeting, this is not
  // a blocking state for the local user, so the banner stays hidden.
  assert.deepEqual(computeBusyBannerState({
    status: { mode: 'on', caller: 'hosta' },
    localHostId: 'hostb',
  }), {
    visible: false,
    mode: 'on',
    caller: 'hosta',
    pausedMode: null,
    pausedCaller: null,
    lastError: null,
  });
});

test('test_busy_banner_hidden_when_mode_off', () => {
  assert.deepEqual(computeBusyBannerState({
    status: { mode: 'off', caller: null },
    localHostId: 'hostb',
  }), {
    visible: false,
    mode: 'off',
    caller: null,
    pausedMode: null,
    pausedCaller: null,
    lastError: null,
  });
});

test('test_busy_banner_state_paused_mode_secondary_line', () => {
  // hostb is in clipboard mode on the shared server; hostc's renderer
  // sees mode=clipboard caller=hostb paused_mode=on paused_caller=hosta.
  // Banner shows because mode=clipboard is blocking; pausedMode propagates so
  // the renderer can append "always-on paused — hosta" line.
  const result = computeBusyBannerState({
    status: {
      mode: 'clipboard',
      caller: 'hostb',
      paused_mode: 'on',
      paused_caller: 'hosta',
    },
    localHostId: 'hostc',
  });
  assert.equal(result.visible, true);
  assert.equal(result.pausedMode, 'on');
  assert.equal(result.pausedCaller, 'hosta');
});

test('test_busy_banner_state_resume_failed', () => {
  assert.deepEqual(computeBusyBannerState({
    status: { mode: 'off', last_error: 'always_on_resume_failed: device gone' },
    localHostId: 'hostb',
  }), {
    visible: false,
    mode: 'off',
    caller: null,
    pausedMode: null,
    pausedCaller: null,
    lastError: 'always_on_resume_failed: device gone',
  });
});

test('test_should_show_always_on_true', () => {
  assert.equal(shouldShowAlwaysOn({ alwaysOnEnabled: true }), true);
});

test('test_should_show_always_on_false', () => {
  assert.equal(shouldShowAlwaysOn({ alwaysOnEnabled: false }), false);
});

test('test_should_render_always_on_ui_true', () => {
  assert.equal(shouldRenderAlwaysOnUi({
    status: { mode: 'on' },
    alwaysOnEnabled: true,
  }), true);
});

test('test_should_render_always_on_ui_false_when_disabled', () => {
  assert.equal(shouldRenderAlwaysOnUi({
    status: { mode: 'on' },
    alwaysOnEnabled: false,
  }), false);
});

test('test_should_render_always_on_ui_false_when_mode_off', () => {
  assert.equal(shouldRenderAlwaysOnUi({
    status: { mode: 'off' },
    alwaysOnEnabled: true,
  }), false);
});

test('test_silent_state_on_with_source', () => {
  assert.deepEqual(computeSilentState({ status: { speaker: { silent: true, silent_source: 'voice', silent_changed_at: 123.5 } } }),
    { on: true, source: 'voice', changedAt: 123.5, label: 'Silent mode on' });
});

test('test_silent_state_off_default', () => {
  assert.deepEqual(computeSilentState({ status: { speaker: {} } }),
    { on: false, source: null, changedAt: null, label: 'Silent mode off' });
});

test('test_answer_window_waiting_and_cleared', () => {
  assert.deepEqual(computeAnswerWindowState({ status: { speaker: { answer_window: { waiting: true, ready: true, conversation_id: 'C', line_id: 'L', expires_in: 18 } } } }),
    { waiting: true, ready: true, conversationId: 'C', lineId: 'L', expiresIn: 18 });
  assert.deepEqual(computeAnswerWindowState({ status: { speaker: { answer_window: { waiting: false } } } }),
    { waiting: false, ready: false, conversationId: null, lineId: null, expiresIn: null });
});
