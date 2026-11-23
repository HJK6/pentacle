'use strict';

const {
  createBrowserRecorder,
  createVoiceTake,
  uploadVoiceBlob,
  MAX_DURATION_S,
} = require('../web_voice');

const MAX_TEXTAREA_HEIGHT = 160;
const VOICE_SAMPLE_MS = 70;
const VOICE_VISIBLE_BARS = 110;
const VOICE_BUBBLE_BARS = 22;
const ATTACHMENT_LIMIT = 5;
const ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024;
const ATTACHMENT_MIMES = new Set(['image/jpeg', 'image/png']);

let captureOwner = null;
let voiceRequest = 0;

const clampLevel = value => Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0;
const nowMs = env => env.performance?.now?.() ?? Date.now();

function durationLabel(seconds) {
  const safe = Math.max(0, Number(seconds) || 0);
  return `${Math.floor(safe / 60)}:${String(Math.floor(safe % 60)).padStart(2, '0')}`;
}

function downsampleLevels(levels, count = VOICE_BUBBLE_BARS) {
  if (!levels.length) return Array(count).fill(0.2);
  return Array.from({ length: count }, (_, index) => {
    const first = Math.floor(index * levels.length / count);
    const last = Math.max(first + 1, Math.floor((index + 1) * levels.length / count));
    const bucket = levels.slice(first, last).map(clampLevel);
    return bucket.length ? Math.max(...bucket) : 0.2;
  });
}

function makeButton(document, className, label, marker) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  button.setAttribute('aria-label', label);
  button.title = label;
  if (marker) button.dataset[marker] = '';
  return button;
}

function makeDraftRecord(drafts, key) {
  const previous = drafts.get(key);
  const record = previous && typeof previous === 'object'
    ? previous
    : { text: typeof previous === 'string' ? previous : '', attachments: [], version: 0 };
  if (typeof record.text !== 'string') record.text = '';
  if (!Array.isArray(record.attachments)) record.attachments = [];
  if (!Number.isSafeInteger(record.version)) record.version = 0;
  drafts.set(key, record);
  return record;
}

function randomId(env, prefix) {
  const uuid = env.crypto?.randomUUID?.();
  return uuid ? `${prefix}-${uuid}` : `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

function readFileAsDataBase64(file, env) {
  const Reader = env.FileReader || globalThis.FileReader;
  if (!Reader) return Promise.reject(new Error('Image reading is unavailable in this browser.'));
  return new Promise((resolve, reject) => {
    const reader = new Reader();
    reader.onload = () => {
      const value = String(reader.result || '');
      resolve(value.includes(',') ? value.slice(value.indexOf(',') + 1) : value);
    };
    reader.onerror = () => reject(reader.error || new Error('Failed to read image.'));
    reader.readAsDataURL(file);
  });
}

function readImageDimensions(previewUrl, env) {
  const ImageClass = env.Image || globalThis.Image;
  if (!ImageClass) return Promise.resolve({});
  return new Promise(resolve => {
    const image = new ImageClass();
    image.onload = () => resolve({ width: image.naturalWidth || undefined, height: image.naturalHeight || undefined });
    image.onerror = () => resolve({});
    image.src = previewUrl;
  });
}

function decodedBase64Length(value) {
  const padding = (String(value).match(/=+$/) || [''])[0].length;
  return Math.floor(String(value).length * 3 / 4) - padding;
}

/**
 * Mount the new-web composer into a shell-owned host.
 *
 * The shell owns the shared ChatStore, `cc` bridge, transcript renderer and the
 * `drafts` Map. `streamId` may be a string or a getter. Draft values are keyed
 * by the stream id and retain text plus pending image attachments across an
 * unmount. Call `cancelRecording()` before switching panes or showing terminal
 * view; `dispose()` also cancels before removing the component.
 *
 * DOM contract for the shell and styles:
 * [data-next-composer], [data-next-controls], [data-next-attach],
 * [data-next-file-input], [data-next-attachments], [data-next-message],
 * [data-next-mic], [data-next-send], [data-next-voice-panel],
 * [data-next-voice-bars], [data-next-voice-time], [data-next-voice-send],
 * [data-next-voice-cancel], and pending rows [data-next-voice-pending].
 */
function mountComposer({
  host,
  transcript,
  streamId,
  draftKey,
  drafts = new Map(),
  cc = globalThis.cc,
  chatStore = globalThis.PentacleChatStore,
  env = globalThis,
  recorder,
  recorderOptions,
} = {}) {
  if (!host?.ownerDocument) throw new TypeError('Composer host element is required.');
  if (!chatStore || typeof chatStore.sendTurn !== 'function') throw new TypeError('Shared chat store is required.');
  const document = host.ownerDocument;
  const draftMap = drafts instanceof Map ? drafts : new Map();
  const resolveStreamId = typeof streamId === 'function' ? streamId : () => streamId;
  const initialStreamId = resolveStreamId();
  const key = String(initialStreamId || draftKey || '');
  if (!key) throw new TypeError('A stream id or draft key is required.');
  const draft = makeDraftRecord(draftMap, key);
  const capture = recorder || createBrowserRecorder(env, recorderOptions);
  const maxAttachments = Math.min(ATTACHMENT_LIMIT, Math.max(1, Math.floor(Number(env.PentacleChatCore?.MAX_CHAT_ATTACHMENTS) || ATTACHMENT_LIMIT)));
  const urlApi = env.URL || globalThis.URL;

  const element = document.createElement('div');
  element.className = 'next-composer';
  element.dataset.nextComposer = '';
  const form = document.createElement('form');
  form.className = 'next-composer-form';
  form.noValidate = true;
  const controls = document.createElement('div');
  controls.className = 'next-composer-controls';
  controls.dataset.nextControls = '';
  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = 'image/jpeg,image/png';
  fileInput.multiple = true;
  fileInput.hidden = true;
  fileInput.className = 'next-composer-file-input';
  fileInput.dataset.nextFileInput = '';
  const attachmentTray = document.createElement('div');
  attachmentTray.className = 'next-composer-attachments';
  attachmentTray.dataset.nextAttachments = '';
  const attachButton = makeButton(document, 'next-composer-attach', 'Attach', 'nextAttach');
  attachButton.innerHTML = '<svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>';
  const input = document.createElement('textarea');
  input.className = 'next-composer-input';
  input.rows = 1;
  input.setAttribute('aria-label', 'Message');
  input.dataset.nextMessage = '';
  input.value = draft.text;
  const micButton = makeButton(document, 'next-composer-mic', 'Record voice message', 'nextMic');
  micButton.innerHTML = '<svg aria-hidden="true" width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3m-4 0h8"/></svg>';
  const sendButton = makeButton(document, 'next-composer-send', 'Send message', 'nextSend');
  sendButton.innerHTML = '<svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M6 11l6-6 6 6"/></svg>';
  sendButton.hidden = true;
  controls.append(fileInput, attachmentTray, attachButton, input, micButton, sendButton);

  const voicePanel = document.createElement('div');
  voicePanel.className = 'next-voice-panel';
  voicePanel.dataset.nextVoicePanel = '';
  voicePanel.hidden = true;
  form.append(controls, voicePanel);
  const error = document.createElement('div');
  error.className = 'next-composer-error';
  error.setAttribute('role', 'alert');
  error.hidden = true;
  element.append(form, error);
  host.appendChild(element);

  const listeners = [];
  const on = (target, type, handler) => {
    target.addEventListener(type, handler);
    listeners.push([target, type, handler]);
  };
  let disposed = false;
  let sendPending = false;
  let voicePhase = 'idle';
  let voiceGeneration = 0;
  let startedAt = 0;
  let tick = null;
  let levels = [];
  let recordingBars;
  let recordingTimer;
  let startInterrupted = false;
  let pendingTake = null;
  let pendingBubble = null;
  let pendingVoiceGeneration = 0;
  let attachmentGeneration = 0;
  let fileGeneration = 0;

  function currentStreamId() {
    return String(resolveStreamId() || '');
  }

  function setError(message) {
    if (disposed) return;
    error.textContent = String(message || '');
    error.hidden = !error.textContent;
  }

  function fitInput() {
    input.style.height = 'auto';
    const measured = Math.max(0, Number(input.scrollHeight) || 0);
    const height = Math.min(MAX_TEXTAREA_HEIGHT, measured);
    input.style.height = `${height}px`;
    input.style.overflowY = measured > MAX_TEXTAREA_HEIGHT ? 'auto' : 'hidden';
  }

  function updateControls() {
    const hasText = !!input.value.trim();
    const hasAttachments = draft.attachments.length > 0;
    sendButton.hidden = !(hasText || hasAttachments);
    micButton.hidden = hasText || hasAttachments;
    micButton.disabled = voicePhase === 'starting' || voicePhase === 'stopping'
      || voicePhase === 'sending' || voicePhase === 'cancelling'
      || !env.isSecureContext || typeof env.navigator?.mediaDevices?.getUserMedia !== 'function';
    sendButton.disabled = sendPending;
  }

  function setDraftText(value) {
    draft.text = String(value ?? '');
    draft.version++;
    draftMap.set(key, draft);
    updateControls();
    fitInput();
  }

  function renderAttachments() {
    attachmentTray.replaceChildren();
    for (const item of draft.attachments) {
      const chip = document.createElement('div');
      chip.className = 'next-composer-attachment';
      chip.dataset.attachmentId = item.id;
      const image = document.createElement('img');
      image.className = 'next-composer-attachment-preview';
      image.src = item.previewUrl;
      image.alt = item.name || 'Attached image';
      const metadata = document.createElement('span');
      metadata.className = 'next-composer-attachment-name';
      metadata.textContent = item.name || 'image';
      const remove = makeButton(document, 'next-composer-attachment-remove', 'Remove attachment', 'nextRemoveAttachment');
      remove.dataset.attachmentId = item.id;
      remove.textContent = '×';
      chip.append(image, metadata, remove);
      attachmentTray.appendChild(chip);
    }
  }

  function releaseAttachment(item) {
    if (!item?.sent && item?.previewUrl) urlApi?.revokeObjectURL?.(item.previewUrl);
  }

  function removeAttachment(id) {
    const index = draft.attachments.findIndex(item => item.id === id);
    if (index < 0) return;
    const [removed] = draft.attachments.splice(index, 1);
    releaseAttachment(removed);
    draft.version++;
    draftMap.set(key, draft);
    setError('');
    renderAttachments();
    updateControls();
  }

  async function addFiles(files) {
    const selected = Array.from(files || []);
    if (!selected.length) return;
    const generation = ++attachmentGeneration;
    setError('');
    const accepted = selected.filter(file => file && ATTACHMENT_MIMES.has(String(file.type || '').toLowerCase()));
    if (accepted.length !== selected.length) setError('Only JPEG and PNG image attachments are supported.');
    for (const file of accepted) {
      if (draft.attachments.length >= maxAttachments) {
        setError(`At most ${maxAttachments} images can be attached.`);
        break;
      }
      const bytes = Number(file.size);
      if (!bytes || !Number.isSafeInteger(bytes) || bytes > ATTACHMENT_MAX_BYTES) {
        setError(bytes ? 'Each image must be 25 MiB or smaller.' : 'Empty images cannot be attached.');
        continue;
      }
      const previewUrl = urlApi?.createObjectURL?.(file);
      if (!previewUrl) {
        setError('Image preview is unavailable in this browser.');
        continue;
      }
      try {
        const [dataBase64, dimensions] = await Promise.all([
          readFileAsDataBase64(file, env),
          readImageDimensions(previewUrl, env),
        ]);
        if (disposed || generation !== attachmentGeneration) {
          urlApi.revokeObjectURL(previewUrl);
          continue;
        }
        if (!dimensions.width || !dimensions.height || decodedBase64Length(dataBase64) !== bytes) {
          throw new Error('This JPEG or PNG image could not be decoded.');
        }
        if (draft.attachments.length >= maxAttachments) {
          urlApi.revokeObjectURL(previewUrl);
          setError(`At most ${maxAttachments} images can be attached.`);
          break;
        }
        draft.attachments.push({
          id: randomId(env, 'att'), name: file.name || 'image', mime: file.type,
          bytes, dataBase64, previewUrl, width: dimensions.width, height: dimensions.height,
        });
        draft.version++;
        draftMap.set(key, draft);
      } catch (reason) {
        urlApi.revokeObjectURL(previewUrl);
        setError(reason instanceof Error ? reason.message : 'This image could not be read.');
      }
    }
    if (!disposed && generation === attachmentGeneration) {
      renderAttachments();
      updateControls();
    }
  }

  async function uploadAttachments(items) {
    const uploaded = [];
    for (const item of items) {
      if (typeof cc?.chatUploadBlob !== 'function') throw new Error('Image upload is unavailable.');
      const result = await cc.chatUploadBlob({ dataBase64: item.dataBase64, sizeHintBytes: item.bytes });
      if (!result?.ok || !result.blob_sha) throw new Error(result?.error || 'Image upload failed.');
      const key = String(result.blob_sha).toLowerCase();
      uploaded.push({
        key, mime: item.mime, width: item.width, height: item.height,
        bytes: item.bytes, sha256: key, uri: item.previewUrl, name: item.name,
      });
    }
    return uploaded;
  }

  async function sendText() {
    const stream = currentStreamId();
    const literal = input.value;
    const attachments = draft.attachments.slice();
    if (sendPending || (!literal.trim() && !attachments.length)) return;
    if (!stream) { setError('Select a connected chat before sending.'); return; }
    const session = chatStore.getState?.()?.sessions?.find(item => item.stream_id === stream);
    const text = session?.session_kind === 'assistant_composite' ? literal : literal.trim();
    const textAtSend = literal;
    const versionAtSend = draft.version;
    const attachmentIds = new Set(attachments.map(item => item.id));
    sendPending = true;
    setError('');
    updateControls();
    try {
      const descriptors = await uploadAttachments(attachments);
      const optimisticId = chatStore.sendTurn(stream, text, descriptors);
      if (!optimisticId) throw new Error('Chat send failed.');
      for (const item of attachments) item.sent = true;
      draft.attachments = draft.attachments.filter(item => !attachmentIds.has(item.id));
      if (draft.text === textAtSend && versionAtSend === draft.version) draft.text = '';
      draft.version++;
      draftMap.set(key, draft);
      if (!disposed) {
        input.value = draft.text;
        renderAttachments();
        updateControls();
        fitInput();
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason || 'Chat send failed.'));
    } finally {
      sendPending = false;
      if (!disposed) updateControls();
    }
  }

  function makeRecordingPanel() {
    voicePanel.replaceChildren();
    voicePanel.hidden = false;
    voicePanel.setAttribute('aria-live', 'off');
    const label = document.createElement('span');
    label.className = 'next-voice-sr-only';
    label.textContent = 'Recording';
    const cancel = makeButton(document, 'next-voice-cancel', 'Cancel recording', 'nextVoiceCancel');
    cancel.textContent = '×';
    const dot = document.createElement('span');
    dot.className = 'next-voice-recording-dot';
    dot.setAttribute('aria-hidden', 'true');
    recordingTimer = document.createElement('span');
    recordingTimer.className = 'next-voice-time';
    recordingTimer.setAttribute('role', 'timer');
    recordingTimer.dataset.nextVoiceTime = '';
    recordingTimer.textContent = '0:00';
    recordingBars = document.createElement('div');
    recordingBars.className = 'next-voice-bars';
    recordingBars.dataset.nextVoiceBars = '';
    recordingBars.setAttribute('aria-hidden', 'true');
    const send = makeButton(document, 'next-voice-send', 'Send voice message', 'nextVoiceSend');
    send.innerHTML = '<svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M6 11l6-6 6 6"/></svg>';
    voicePanel.append(label, cancel, dot, recordingTimer, recordingBars, send);
    controls.hidden = true;
    cancel.addEventListener('click', () => { void cancelRecording(); });
    send.addEventListener('click', () => { void finishVoice(); });
  }

  function makeStartingPanel() {
    voicePanel.replaceChildren();
    voicePanel.hidden = false;
    voicePanel.setAttribute('aria-live', 'polite');
    const label = document.createElement('span');
    label.className = 'next-voice-status';
    label.textContent = 'Requesting microphone…';
    const cancel = makeButton(document, 'next-voice-cancel', 'Cancel recording', 'nextVoiceCancel');
    cancel.textContent = '×';
    voicePanel.append(label, cancel);
    controls.hidden = true;
    cancel.addEventListener('click', () => { void cancelRecording(); });
  }

  function drawSample() {
    if (voicePhase !== 'recording') return;
    const reading = capture.poll?.() || {};
    const durationMs = Math.max(0, Number(reading.durationMs ?? (nowMs(env) - startedAt)) || 0);
    const level = clampLevel(reading.level ?? 0);
    levels.push(level);
    const bar = document.createElement('span');
    bar.className = 'next-voice-bar';
    bar.dataset.nextVoiceBar = '';
    bar.dataset.sample = String(levels.length - 1);
    bar.style.height = `${Math.max(1, Math.round(level * 26))}px`;
    recordingBars.appendChild(bar);
    while (recordingBars.children.length > VOICE_VISIBLE_BARS) recordingBars.firstElementChild.remove();
    const visible = Array.from(recordingBars.children);
    visible.forEach((item, index) => { item.style.opacity = String(0.2 + 0.8 * (index + 1) / visible.length); });
    recordingTimer.textContent = durationLabel(durationMs / 1000);
    if (reading.interrupted) void finishVoice('interrupted');
    else if (durationMs / 1000 >= MAX_DURATION_S) void finishVoice('limit');
  }

  function clearRecordingTimer() {
    if (tick != null) env.clearInterval(tick);
    tick = null;
  }

  function clearInterruptions() {
    document.removeEventListener('visibilitychange', onVisibilityChange);
    capture.setInterruptionHandler?.(null);
  }

  function releaseCapture() {
    if (captureOwner === owner) {
      captureOwner = null;
      owner.release?.();
      owner.release = null;
    }
  }

  function clearPendingBubble() {
    pendingBubble?.remove();
    pendingBubble = null;
  }

  function restoreComposer() {
    clearRecordingTimer();
    clearInterruptions();
    voicePhase = 'idle';
    voicePanel.hidden = true;
    voicePanel.replaceChildren();
    controls.hidden = false;
    recordingBars = recordingTimer = null;
    updateControls();
  }

  function renderPendingBubble(state, interrupted = false) {
    clearPendingBubble();
    const mount = transcript || host;
    const bubble = document.createElement('div');
    bubble.className = 'next-voice-pending';
    bubble.dataset.nextVoicePending = '';
    bubble.setAttribute('role', 'status');
    const caption = document.createElement('div');
    caption.className = 'next-voice-pending-caption';
    const status = document.createElement('span');
    status.className = 'next-voice-status';
    status.dataset.nextVoiceStatus = '';
    status.textContent = state.status === 'failed' ? 'Failed to send' : 'Sending';
    caption.appendChild(status);
    const bars = document.createElement('div');
    bars.className = 'next-voice-pending-bars';
    bars.setAttribute('aria-hidden', 'true');
    for (const level of downsampleLevels(state.levels || [])) {
      const bar = document.createElement('span');
      bar.className = 'next-voice-pending-bar';
      bar.dataset.nextVoiceBar = '';
      bar.style.height = `${Math.max(2, Math.round(level * 22))}px`;
      bar.style.opacity = String(0.4 + clampLevel(level) * 0.6);
      bars.appendChild(bar);
    }
    const duration = document.createElement('span');
    duration.className = 'next-voice-pending-duration';
    duration.dataset.nextVoiceDuration = '';
    duration.textContent = durationLabel(state.durationS);
    bubble.append(caption, bars, duration);
    if (interrupted) {
      const note = document.createElement('span');
      note.className = 'next-voice-interrupted';
      note.textContent = `interrupted at ${durationLabel(state.durationS)}`;
      bubble.appendChild(note);
    }
    if (state.status === 'failed') {
      const detail = document.createElement('span');
      detail.className = 'next-voice-error';
      detail.textContent = state.error || 'Voice send failed.';
      const retry = makeButton(document, 'next-voice-retry', 'Retry voice message', 'nextVoiceRetry');
      retry.textContent = 'Retry';
      retry.addEventListener('click', () => { void pendingTake?.run(); });
      const discard = makeButton(document, 'next-voice-discard', 'Discard voice message', 'nextVoiceDiscard');
      discard.textContent = '×';
      discard.addEventListener('click', () => { void cancelRecording(); });
      bubble.append(detail, retry, discard);
    }
    mount.appendChild(bubble);
    pendingBubble = bubble;
  }

  function onVoiceState(state, generation, interrupted) {
    if (generation !== pendingVoiceGeneration || disposed) return;
    if (state.status === 'sent' || state.status === 'discarded') {
      pendingTake = null;
      clearPendingBubble();
      restoreComposer();
      return;
    }
    voicePhase = 'sending';
    micButton.disabled = true;
    renderPendingBubble(state, interrupted);
  }

  function onVisibilityChange() {
    if (document.hidden && voicePhase === 'recording') void finishVoice('interrupted');
  }

  async function startVoice() {
    if (disposed || ['starting', 'recording', 'stopping', 'sending', 'cancelling'].includes(voicePhase)) return;
    if (!env.isSecureContext || typeof env.navigator?.mediaDevices?.getUserMedia !== 'function') {
      setError('Microphone recording is unavailable in this browser.');
      return;
    }
    const target = currentStreamId();
    if (!target) { setError('Select a connected chat before recording.'); return; }
    const request = ++voiceRequest;
    const previous = captureOwner;
    if (previous && previous !== owner) {
      const generation = voiceGeneration;
      try { await previous.stopForHandoff(); } catch (error) {
        if (request === voiceRequest) setError(error.message || 'Unable to stop the other recording.');
        return;
      }
      if (disposed || request !== voiceRequest || generation !== voiceGeneration) return;
    }
    setError('');
    captureOwner = owner;
    owner.released = new Promise(resolve => { owner.release = resolve; });
    voicePhase = 'starting';
    const generation = ++voiceGeneration;
    startInterrupted = false;
    makeStartingPanel();
    micButton.disabled = true;
    const interrupted = () => {
      if (voicePhase === 'starting') startInterrupted = true;
      else if (voicePhase === 'recording') void finishVoice('interrupted');
    };
    capture.setInterruptionHandler?.(interrupted);
    document.addEventListener('visibilitychange', onVisibilityChange);
    try {
      await capture.start();
      if (generation !== voiceGeneration) {
        await capture.discard();
        releaseCapture();
        restoreComposer();
        return;
      }
      voicePhase = 'recording';
      startedAt = nowMs(env);
      levels = [];
      makeRecordingPanel();
      tick = env.setInterval(drawSample, VOICE_SAMPLE_MS);
      if (startInterrupted) void finishVoice('interrupted');
    } catch (reason) {
      if (generation !== voiceGeneration) {
        releaseCapture();
        restoreComposer();
        return;
      }
      releaseCapture();
      restoreComposer();
      setError(reason instanceof Error ? reason.message : 'Voice recording failed.');
    }
  }

  async function finishVoice(reason = 'tap') {
    if (voicePhase !== 'recording') return;
    const generation = voiceGeneration;
    clearRecordingTimer();
    clearInterruptions();
    voicePhase = 'stopping';
    const recordedLevels = levels.slice();
    const durationMs = Math.max(0, nowMs(env) - startedAt);
    voicePanel.hidden = true;
    controls.hidden = false;
    updateControls();
    try {
      const audio = await capture.stop();
      if (generation !== voiceGeneration) {
        releaseCapture();
        restoreComposer();
        return;
      }
      releaseCapture();
      const durationS = Math.max(1, Math.min(MAX_DURATION_S, Number(audio.durationS) || durationMs / 1000));
      const origin = currentStreamId();
      const takeId = env.crypto?.randomUUID?.() || `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const interrupted = reason === 'interrupted';
      const pending = { status: 'transcribing', levels: recordedLevels, durationS, interrupted };
      pendingVoiceGeneration = generation;
      renderPendingBubble(pending, interrupted);
      pendingTake = createVoiceTake({
        ...audio, levels: recordedLevels, durationS, interrupted, streamId: origin, id: takeId,
      }, {
        upload: blob => uploadVoiceBlob(cc, blob),
        transcribe: payload => cc.chatTranscribeBlob(payload),
        send: ({ streamId: target, text, meta, identity }) => chatStore.sendTurn(target, text, [], { meta, identity }),
        telemetry: (event, tags) => env.PentacleChatCore?.logTelemetry?.(event, tags),
        commit: unit => cc.chatOutboundCommit?.(unit),
        outcome: operationId => cc.chatOutboundGet?.(operationId),
        cancel: operationId => cc.chatOutboundCancel?.(operationId),
        origin: target => {
          const current = chatStore.getState?.()?.sessions?.find(item => item.stream_id === target);
          return current ? { generation: current.session_generation, composite: current.session_kind === 'assistant_composite' } : null;
        },
        clientBuild: 'pentacle-web',
        onState: state => onVoiceState(state, generation, interrupted),
      });
      voicePhase = 'sending';
      micButton.disabled = true;
      await pendingTake.run();
      const result = pendingTake?.snapshot?.();
      if (result?.status === 'failed') renderPendingBubble(result, interrupted);
    } catch (error) {
      if (generation !== voiceGeneration) {
        releaseCapture();
        restoreComposer();
        return;
      }
      if (disposed) return;
      releaseCapture();
      restoreComposer();
      setError(error instanceof Error ? error.message : 'Voice recording failed.');
    }
  }

  async function cancelRecording() {
    if (voicePhase === 'cancelling') return;
    const phase = voicePhase;
    voiceGeneration++;
    clearRecordingTimer();
    clearInterruptions();
    voicePhase = 'cancelling';
    const take = pendingTake;
    pendingTake = null;
    take?.discard();
    clearPendingBubble();
    if (phase === 'recording') {
      try { await capture.discard(); }
      finally { releaseCapture(); restoreComposer(); }
    } else if (phase === 'starting' || phase === 'stopping') {
      // The pending start/stop owns recorder cleanup and checks the generation.
      voicePanel.hidden = true;
      voicePanel.replaceChildren();
      controls.hidden = false;
      updateControls();
    } else {
      releaseCapture();
      restoreComposer();
    }
  }

  const owner = {
    async stopForHandoff() {
      const released = owner.released;
      await cancelRecording();
      await released;
    },
  };
  on(input, 'input', () => { setError(''); setDraftText(input.value); });
  on(input, 'paste', event => {
    const clipboard = event.clipboardData;
    const files = Array.from(clipboard?.items || [])
      .filter(item => item.kind === 'file' && item.type.toLowerCase().startsWith('image/'))
      .map(item => item.getAsFile()).filter(Boolean);
    if (!files.length) files.push(...Array.from(clipboard?.files || [])
      .filter(file => file.type.toLowerCase().startsWith('image/')));
    // Leave the textarea's native text paste and undo behavior intact.
    if (files.length) void addFiles(files);
  });
  on(input, 'keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      void sendText();
    }
  });
  on(form, 'submit', event => { event.preventDefault(); void sendText(); });
  on(sendButton, 'click', () => { void sendText(); });
  on(attachButton, 'click', () => fileInput.click());
  on(fileInput, 'change', () => {
    const selected = fileInput.files;
    const generation = ++fileGeneration;
    void addFiles(selected).finally(() => {
      if (generation === fileGeneration) fileInput.value = '';
    });
  });
  on(attachmentTray, 'click', event => {
    const remove = event.target?.closest?.('[data-next-remove-attachment]');
    if (remove && attachmentTray.contains(remove)) removeAttachment(remove.dataset.attachmentId || '');
  });
  on(micButton, 'click', () => { void startVoice(); });

  renderAttachments();
  fitInput();
  updateControls();

  return {
    element,
    focus() { input.focus(); },
    cancelRecording,
    async dispose() {
      if (disposed) return;
      await cancelRecording();
      disposed = true;
      attachmentGeneration++;
      for (const [target, type, handler] of listeners) target.removeEventListener(type, handler);
      listeners.length = 0;
      element.remove();
      releaseCapture();
    },
    snapshot() {
      return { streamId: currentStreamId(), voicePhase, draft: draft.text, attachmentCount: draft.attachments.length };
    },
  };
}

module.exports = {
  mountComposer,
  durationLabel,
  downsampleLevels,
  MAX_TEXTAREA_HEIGHT,
  VOICE_SAMPLE_MS,
  VOICE_VISIBLE_BARS,
  VOICE_BUBBLE_BARS,
};
