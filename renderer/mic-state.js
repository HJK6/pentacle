'use strict';

function micAssistantName(status) {
  return String(status?.assistant_name || '').trim() || 'Zephyr';
}

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

// Whether the microphone is waiting for the operator's answer to an assistant question.
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
  const name = micAssistantName(status);
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
        ? (answer.ready ? `Ready for ${name}’s answer — say over.` : `Getting ready for ${name}’s answer…`)
        : '',
    },
  };
}

// Web MIC panel Source choice. `room` (the room mic host, default everywhere) or
// `device` (this browser's microphone for chat voice and question voice).
const MIC_SOURCE_STORAGE_KEY = 'pentacle.micSource';
// Chrome lists aliases of real inputs under these ids; `System default` covers them.
const ALIAS_DEVICE_IDS = new Set(['default', 'communications']);

// Per-browser choice: { source, deviceId }. Absent, unreadable or invalid -> room.
const micSourceStorage = {
  read(storage) {
    try {
      const value = JSON.parse(storage.getItem(MIC_SOURCE_STORAGE_KEY));
      if (value?.source === 'device') return { source: 'device', deviceId: typeof value.deviceId === 'string' ? value.deviceId : '' };
    } catch (_) { /* fall through to the default */ }
    return { source: 'room', deviceId: '' };
  },
  // The room Source is the default, so choosing it (or a fallback) clears the stored value.
  write(storage, choice) {
    try {
      if (choice?.source === 'device') storage.setItem(MIC_SOURCE_STORAGE_KEY, JSON.stringify({ source: 'device', deviceId: choice.deviceId || '' }));
      else storage.removeItem(MIC_SOURCE_STORAGE_KEY);
      return true;
    } catch (_) { return false; }
  },
};

// Which Source options render, which is active, and the device picker rows.
// A stored device that is missing from an enumeration with real ids is lost;
// before permission (empty ids) loss cannot be judged, so the choice is kept.
// The room Source is labelled with the web host's configured machine name.
function micRoomSourceLabel({ hostNames, localHost } = {}) {
  const name = hostNames && localHost ? hostNames[localHost] : '';
  return (typeof name === 'string' && name.trim()) || 'Room mic';
}

function computeMicSourceState({ secureContext, audioInputs, stored } = {}) {
  const inputs = (Array.isArray(audioInputs) ? audioInputs : []).filter(device => device && device.kind === 'audioinput');
  const deviceAvailable = secureContext === true && inputs.length > 0;
  const real = inputs.filter(device => !ALIAS_DEVICE_IDS.has(device.deviceId));
  const devices = [{ deviceId: '', label: 'System default' },
    ...real.map((device, index) => ({ deviceId: device.deviceId || '', label: device.label || `Microphone ${index + 1}` }))];
  const choice = stored?.source === 'device' ? { source: 'device', deviceId: stored.deviceId || '' } : { source: 'room', deviceId: '' };
  const idsKnown = real.some(device => !!device.deviceId);
  const deviceLost = choice.source === 'device'
    && (!deviceAvailable || (!!choice.deviceId && idsKnown && !real.some(device => device.deviceId === choice.deviceId)));
  const active = choice.source === 'device' && !deviceLost ? 'device' : 'room';
  const selectedDeviceId = active === 'device' ? choice.deviceId : '';
  const selected = devices.find(device => device.deviceId === selectedDeviceId);
  return {
    options: deviceAvailable ? ['room', 'device'] : ['room'],
    active,
    deviceLost,
    devices,
    selectedDeviceId,
    activeLabel: active === 'device' ? (selected?.label || 'Selected microphone') : '',
  };
}

module.exports = {
  MIC_SOURCE_STORAGE_KEY,
  micSourceStorage,
  computeMicSourceState,
  micRoomSourceLabel,
  micAssistantName,
  computeBusyBannerState,
  shouldShowAlwaysOn,
  shouldRenderAlwaysOnUi,
  computeSilentState,
  computeAnswerWindowState,
  computeMicPanelView,
};
