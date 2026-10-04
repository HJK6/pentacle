'use strict';

// Pure presentation helpers for an optional microphone provider. The provider
// supplies an opaque caller id; this module never invents or persists identity.
function computeBusyBannerState({ status, localHostId } = {}) {
  const source = status && typeof status === 'object' ? status : {};
  const lastError = source.last_error || null;
  const mode = source.mode || null;
  const caller = Object.prototype.hasOwnProperty.call(source, 'caller') ? source.caller : null;
  const pausedMode = source.paused_mode || null;
  const pausedCaller = Object.prototype.hasOwnProperty.call(source, 'paused_caller')
    ? source.paused_caller : null;
  const local = localHostId || 'unknown';
  const blockingMode = mode === 'clipboard' || mode === 'meeting';
  return {
    visible: !!(status && blockingMode && caller !== local),
    mode,
    caller,
    pausedMode,
    pausedCaller,
    lastError,
  };
}

function shouldShowAlwaysOn({ alwaysOnEnabled } = {}) {
  return alwaysOnEnabled === true;
}

function shouldRenderAlwaysOnUi({ status, alwaysOnEnabled } = {}) {
  return !!(status && status.mode === 'on' && alwaysOnEnabled === true);
}

// Silent mode is reported by the speaker service and shown wherever the mic status is.
// Silent mode is independent of the mic mode, so its state is available in any mode.
function computeSilentState({ status } = {}) {
  const speaker = (status && status.speaker) || {};
  const on = speaker.silent === true;
  const source = typeof speaker.silent_source === 'string' ? speaker.silent_source : null;
  const changedAt = typeof speaker.silent_changed_at === 'number' ? speaker.silent_changed_at : null;
  return {
    on,
    source,
    changedAt,
    label: on ? 'Silent mode on' : 'Silent mode off',
  };
}

// Whether the microphone is waiting for the operator's answer to a Bart question.
function computeAnswerWindowState({ status } = {}) {
  const speaker = (status && status.speaker) || {};
  const window = (speaker.answer_window && typeof speaker.answer_window === 'object') ? speaker.answer_window : {};
  const waiting = window.waiting === true;
  return {
    waiting,
    ready: waiting && window.ready === true,
    conversationId: waiting && typeof window.conversation_id === 'string' ? window.conversation_id : null,
    lineId: waiting && typeof window.line_id === 'string' ? window.line_id : null,
    expiresIn: waiting && typeof window.expires_in === 'number' ? window.expires_in : null,
  };
}

// The view model the mic panel renders for silent mode and the answer window:
// the silent toggle button (label/active and the state a click should request)
// and the waiting-for-answer indicator (shown while a window is open, cleared
// when the status fixture clears it).
function computeMicPanelView({ status } = {}) {
  const silent = computeSilentState({ status });
  const answer = computeAnswerWindowState({ status });
  return {
    silent: {
      on: silent.on,
      source: silent.source,
      label: silent.on ? 'Silent: On' : 'Silent',
      nextOn: !silent.on,
    },
    answerWindow: {
      waiting: answer.waiting,
      ready: answer.ready,
      text: answer.waiting
        ? (answer.ready ? 'Ready for Bart’s answer — say over.' : 'Getting ready for Bart’s answer…')
        : '',
    },
  };
}

module.exports = {
  computeBusyBannerState,
  shouldShowAlwaysOn,
  shouldRenderAlwaysOnUi,
  computeSilentState,
  computeAnswerWindowState,
  computeMicPanelView,
};
