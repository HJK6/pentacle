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

module.exports = {
  computeBusyBannerState,
  shouldShowAlwaysOn,
  shouldRenderAlwaysOnUi,
};
