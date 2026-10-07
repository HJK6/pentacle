'use strict';

const MAX_DURATION_S = 300;
const SAMPLE_INTERVAL_MS = 90;
const DISPLAY_BARS = 46;
const BUBBLE_BARS = 30;
const BUG_REF = 'spec_pentacle__web_chat_mic_button_2026_10';
const durationLabel = seconds => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
const clampLevel = level => Number.isFinite(level) ? Math.max(0, Math.min(1, level)) : 0;
const nowMs = env => env.performance?.now?.() ?? Date.now();
// Mobile's peak bins retain the shape of the full recording, not just the
// rolling live strip. Very short takes repeat samples; silence stays visible.
function downsampleLevels(levels) {
  if (!levels.length) return Array(BUBBLE_BARS).fill(0.2);
  return Array.from({ length: BUBBLE_BARS }, (_, i) => {
    const first = Math.floor(i * levels.length / BUBBLE_BARS);
    const last = Math.max(first + 1, Math.floor((i + 1) * levels.length / BUBBLE_BARS));
    return Math.max(...levels.slice(first, last).map(clampLevel));
  });
}
function failureMessage(error) {
  const code = error?.error_code || error?.error || error?.code || error?.message;
  if (error?.name === 'NotAllowedError') return 'Microphone permission denied. Allow microphone access and Retry.';
  if (code === 'backend_unavailable') return 'Transcription is unavailable right now.';
  if (code === 'upload_failed' || code === 'upload') return 'Audio upload failed.';
  if (code === 'too_long') return 'That recording is too long to transcribe.';
  return String(code || 'Voice recording failed.');
}

// Own the audio until transcription succeeds. Once a text frame can leave,
// ordinary optimistic-send reconciliation and Retry own delivery instead.
function createVoiceTake({ streamId, blob, durationS, id, levels = [], interrupted = false }, io) {
  let state = { status: 'ready', durationS, streamId, id, levels, interrupted };
  let running = false; let discarded = false; let blobSha; let transcript;
  const emit = patch => { state = { ...state, ...patch }; io.onState?.(state); };
  const beacon = outcome => io.telemetry?.('chat_voice_send_outcome', { subsystem: 'web_voice', bug_ref: BUG_REF, stream_id: streamId, recording_id: id, duration_s: durationS, outcome });
  return {
    snapshot: () => state,
    discard() { discarded = true; blob = null; emit({ status: 'discarded' }); beacon('discarded'); },
    async run() {
      if (running || discarded || state.status === 'sent') return;
      running = true; emit({ status: 'transcribing', error: '' }); beacon('transcribing');
      try {
        if (!blobSha) {
          let uploaded;
          try { uploaded = await io.upload(blob); }
          catch (_) { throw { code: 'upload_failed' }; }
          if (!uploaded?.ok || !uploaded.blob_sha) throw { code: 'upload_failed' };
          blobSha = uploaded.blob_sha;
        }
        if (discarded) return;
        if (!transcript) {
          const result = await io.transcribe({ request_id: `transcribe-${id}`, blob_sha: blobSha, mime: blob.type });
          if (!result || result.ok === false) throw result || new Error('Transcription failed.');
          transcript = String(result.text || '').trim();
        }
        if (discarded) return;
        if (!transcript) throw new Error('Nothing was recognized.');
        const optimisticId = io.send({ streamId, text: transcript, meta: { voice: { duration_s: durationS } } });
        if (!optimisticId) throw new Error('Originating chat is unavailable.');
        blob = null;
        emit({ status: 'sent', optimisticId }); beacon('dispatched');
      } catch (error) {
        if (!discarded) { emit({ status: 'failed', error: failureMessage(error) }); beacon('failed'); }
      } finally { running = false; }
    },
  };
}

function encodeWav(chunks, sampleRate) {
  const inputLength = chunks.reduce((n, chunk) => n + chunk.length, 0);
  const outputRate = Math.min(sampleRate, 16000);
  const length = Math.floor(inputLength * outputRate / sampleRate);
  const samples = new Float32Array(inputLength);
  let inputOffset = 0;
  for (const chunk of chunks) { samples.set(chunk, inputOffset); inputOffset += chunk.length; }
  const buffer = new ArrayBuffer(44 + length * 2); const view = new DataView(buffer);
  const ascii = (offset, text) => Array.from(text).forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  ascii(0, 'RIFF'); view.setUint32(4, 36 + length * 2, true); ascii(8, 'WAVE'); ascii(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, outputRate, true); view.setUint32(28, outputRate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); ascii(36, 'data'); view.setUint32(40, length * 2, true);
  let offset = 44;
  for (let i = 0; i < length; i++) {
    // Average the input interval to limit aliasing when reducing device PCM.
    const first = Math.floor(i * sampleRate / outputRate);
    const last = Math.min(inputLength, Math.floor((i + 1) * sampleRate / outputRate));
    let sum = 0;
    for (let j = first; j < last; j++) sum += samples[j];
    const sample = sum / Math.max(1, last - first);
    const clamped = Math.max(-1, Math.min(1, sample));
    view.setInt16(offset, clamped < 0 ? clamped * 32768 : clamped * 32767, true); offset += 2;
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

function createBrowserRecorder(env = globalThis) {
  let stream; let context; let source; let analyser; let samples; let processor; let recorder; let chunks; let started;
  let interrupted; let onInterrupted; let captureDone; let trackListeners = [];
  async function cleanup() {
    for (const [track, listener] of trackListeners) track.removeEventListener?.('ended', listener);
    trackListeners = [];
    if (processor) { processor.onaudioprocess = null; processor.disconnect(); }
    analyser?.disconnect();
    source?.disconnect(); stream?.getTracks().forEach(track => track.stop());
    if (context) await context.close().catch(() => {});
    stream = context = source = analyser = samples = processor = recorder = null;
  }
  return {
    setInterruptionHandler(handler) { onInterrupted = handler; },
    async start() {
      try {
        stream = await env.navigator.mediaDevices.getUserMedia({ audio: true }); chunks = []; interrupted = false;
        for (const track of stream.getTracks()) {
          const listener = () => { interrupted = true; onInterrupted?.(); };
          track.addEventListener?.('ended', listener); trackListeners.push([track, listener]);
        }
        const AudioContext = env.AudioContext || env.webkitAudioContext;
        if (!AudioContext) throw new Error('Audio capture and metering are unavailable in this browser.');
        context = new AudioContext(); await context.resume();
        source = context.createMediaStreamSource(stream);
        analyser = context.createAnalyser(); analyser.fftSize = 2048;
        samples = new Float32Array(analyser.fftSize);
        source.connect(analyser); // Meter the same stream; never play the microphone through speakers.
        if (env.MediaRecorder?.isTypeSupported?.('audio/mp4')) {
          recorder = new env.MediaRecorder(stream, { mimeType: 'audio/mp4' });
          recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
          // A device ending may finish MediaRecorder before stop() is called.
          // Observe it from the start so that case neither hangs nor loses data.
          captureDone = new Promise((resolve, reject) => {
            recorder.onstop = resolve;
            recorder.onerror = event => reject(event.error || new Error('Audio capture failed.'));
          });
          void captureDone.catch(() => {}); // stop() reports a capture error.
          recorder.start();
        } else {
          // PCM capture is the fallback because webm/ogg are not daemon codecs.
          processor = context.createScriptProcessor(4096, 1, 1);
          processor.onaudioprocess = event => chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
          source.connect(processor); processor.connect(context.destination);
        }
        started = nowMs(env);
      } catch (error) { await cleanup(); throw error; }
    },
    poll() {
      if (!analyser) return { level: 0, durationMs: 0 };
      analyser.getFloatTimeDomainData(samples);
      let power = 0; for (const value of samples) power += value * value;
      const rms = Math.sqrt(power / samples.length);
      // Mobile's native dBFS meter maps -60..0 dB to 0..1. Browser PCM RMS
      // supplies the equivalent level without a transcription/audio side path.
      const level = rms > 0 ? clampLevel((20 * Math.log10(rms) + 60) / 60) : 0;
      return { level, durationMs: Math.max(0, nowMs(env) - started), interrupted: interrupted || stream.getTracks().some(track => track.readyState === 'ended') };
    },
    async stop() {
      try {
        const durationS = Math.max(1, Math.min(MAX_DURATION_S, (nowMs(env) - started) / 1000));
        let blob;
        if (recorder) {
          if (recorder.state !== 'inactive') recorder.stop();
          await captureDone;
          blob = new Blob(chunks, { type: 'audio/mp4' });
        } else { blob = encodeWav(chunks, context.sampleRate); }
        return { blob, durationS };
      } finally { await cleanup(); chunks = null; }
    },
    async discard() {
      try {
        if (recorder) {
          if (recorder.state !== 'inactive') recorder.stop();
          await captureDone.catch(() => {});
        }
      } finally { await cleanup(); chunks = null; }
    },
  };
}

let captureOwner = null;
function bindComposerMic({ web, env = globalThis, button, panel, composer = button.closest('.slot-chat-compose'), takeMount, roomToggle, getStreamId, recorder = createBrowserRecorder(env), upload, transcribe, send, telemetry }) {
  if (!web) { button.addEventListener('click', roomToggle); return; }
  button.setAttribute('aria-label', 'Record voice message'); button.title = 'Record voice message';
  button.innerHTML = '<svg class="voice-mic-glyph" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>';
  if (!env.isSecureContext || typeof env.navigator?.mediaDevices?.getUserMedia !== 'function') {
    button.disabled = true; button.title = 'Microphone recording is unavailable. Use a secure browser with microphone support.'; return;
  }
  const panelParent = panel.parentNode; const panelNext = panel.nextSibling;
  const owner = {}; let phase = 'idle'; let started; let tick; let origin; let take; let generation = 0;
  let levels = []; let timer; let bars; let startInterrupted = false;
  const document = panel.ownerDocument;
  const interrupted = () => {
    if (phase === 'starting') startInterrupted = true;
    else if (phase === 'recording') void stop('interrupted');
  };
  const visibilityChanged = () => { if (document.hidden) interrupted(); };
  const clearInterruptions = () => { document.removeEventListener('visibilitychange', visibilityChanged); recorder.setInterruptionHandler?.(null); };
  panel.setAttribute('aria-live', 'polite'); panel.className = 'slot-chat-voice-take'; panel.hidden = true;
  const clearTick = () => { if (tick != null) env.clearInterval(tick); tick = null; };
  function recordingFace(active) {
    composer?.classList.toggle('is-voice-recording', active);
    button.classList.toggle('is-recording', active);
    const label = active ? 'Stop and send' : 'Record voice message';
    button.setAttribute('aria-label', label); button.title = label;
    button.querySelectorAll('.voice-record-ring').forEach(ring => ring.remove());
    if (active) for (let i = 0; i < 2; i++) {
      const ring = panel.ownerDocument.createElement('span'); ring.className = 'voice-record-ring';
      ring.setAttribute('aria-hidden', 'true'); button.prepend(ring);
    }
    if (!active) {
      panel.classList.remove('is-recording', 'voice-recording-strip');
      panel.setAttribute('aria-live', 'polite');
      if (composer && panel.parentNode === composer) panelParent.insertBefore(panel, panelNext);
    }
  }
  const reset = () => { clearTick(); clearInterruptions(); phase = 'idle'; take = null; button.disabled = false; recordingFace(false); panel.hidden = true; panel.classList.remove('is-pending', 'is-failed'); panel.replaceChildren(); panelParent.insertBefore(panel, panelNext); levels = []; timer = bars = null; if (captureOwner === owner) captureOwner = null; };
  function paint(text, retry, cancel) {
    panel.hidden = false; panel.classList.remove('is-pending', 'is-failed'); panel.replaceChildren();
    const label = panel.ownerDocument.createElement('span'); label.className = 'slot-chat-voice-status'; label.textContent = text; panel.appendChild(label);
    for (const [title, action] of [['Retry', retry], ['Cancel', cancel]]) if (action) {
      const control = panel.ownerDocument.createElement('button'); control.type = 'button'; control.textContent = title;
      control.addEventListener('click', action); panel.appendChild(control);
    }
  }
  function paintRecording() {
    recordingFace(true); levels = [];
    panel.hidden = false; panel.classList.add('is-recording', 'voice-recording-strip');
    // Do not announce every metering sample. The accessible label and tabular
    // timer stay available without a continuously changing live region.
    panel.setAttribute('aria-live', 'off');
    panel.innerHTML = '<span class="voice-sr-only">Recording</span><button type="button" class="voice-recording-discard" aria-label="Discard recording"><svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg><span class="voice-sr-only">Cancel recording</span></button><span class="voice-recording-dot" aria-hidden="true"></span><span class="voice-recording-timer" role="timer">0:00</span><div class="voice-recording-bars" aria-hidden="true"></div>';
    panel.querySelector('.voice-recording-discard').addEventListener('click', cancel);
    timer = panel.querySelector('.voice-recording-timer'); bars = panel.querySelector('.voice-recording-bars');
    if (composer) composer.insertBefore(panel, button);
  }
  function paintPending(state) {
    panel.hidden = false; panel.classList.add('is-pending'); panel.classList.toggle('is-failed', state.status === 'failed');
    panel.replaceChildren();
    if (takeMount) takeMount.appendChild(panel);
    const bubble = document.createElement('div'); bubble.className = 'voice-pending-bubble';
    bubble.innerHTML = '<svg class="voice-pending-play" width="12" height="12" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4l13 8-13 8z" fill="currentColor"/></svg><div class="voice-pending-bars" aria-hidden="true"></div><span class="voice-pending-duration"></span>';
    for (const level of downsampleLevels(state.levels)) {
      const bar = document.createElement('div'); bar.className = 'voice-pending-bar voice-bubble-bar';
      bar.style.height = `${Math.max(2, Math.round(level * 22))}px`; bar.style.opacity = String(0.4 + level * 0.6);
      bubble.querySelector('.voice-pending-bars').appendChild(bar);
    }
    bubble.querySelector('.voice-pending-duration').textContent = durationLabel(state.durationS);
    panel.appendChild(bubble);
    if (state.interrupted) {
      const label = document.createElement('span'); label.className = 'voice-pending-interrupted';
      label.textContent = `interrupted at ${durationLabel(state.durationS)}`; panel.appendChild(label);
    }
    const caption = document.createElement('div'); caption.className = 'voice-pending-caption';
    if (state.status === 'failed') {
      const error = document.createElement('span'); error.className = 'voice-pending-error'; error.setAttribute('role', 'alert'); error.textContent = state.error; caption.appendChild(error);
      const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'voice-pending-retry'; retry.textContent = 'Retry';
      retry.addEventListener('click', () => take?.run()); caption.appendChild(retry);
    } else {
      caption.innerHTML = '<span class="voice-pending-spinner" aria-hidden="true"></span><span class="voice-transcribing-label">TRANSCRIBING</span>';
    }
    const discard = document.createElement('button'); discard.type = 'button'; discard.className = 'voice-pending-discard'; discard.setAttribute('aria-label', 'Discard voice message');
    discard.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>';
    discard.addEventListener('click', cancel); caption.appendChild(discard); panel.prepend(caption);
  }
  function sample() {
    if (phase !== 'recording') return;
    const reading = recorder.poll?.();
    const seconds = (reading?.durationMs ?? nowMs(env) - started) / 1000;
    const level = clampLevel(reading?.level ?? 0);
    const bar = panel.ownerDocument.createElement('div'); bar.className = 'voice-recording-bar';
    bar.dataset.sample = String(levels.length); levels.push(level);
    bar.style.height = `${Math.max(1, Math.round(level * 26))}px`; bar.style.opacity = String(0.45 + level * 0.55);
    bars.appendChild(bar);
    while (bars.children.length > DISPLAY_BARS) bars.firstElementChild.remove();
    timer.textContent = durationLabel(seconds);
    if (reading?.interrupted) interrupted();
    else if (seconds >= MAX_DURATION_S) void stop();
  }
  async function cancel() {
    if (phase === 'cancelling') return;
    generation++; clearTick(); take?.discard(); take = null;
    const recording = phase === 'recording'; const capturePending = phase === 'starting' || phase === 'stopping'; phase = 'cancelling'; button.disabled = true;
    panel.hidden = true;
    // Keep capture ownership until a pending start/stop releases its resources.
    // Its generation guard resets the UI after the operation settles.
    if (capturePending) return;
    try { if (recording) await recorder.discard(); }
    finally { reset(); }
  }
  async function start() {
    if (phase === 'starting' || phase === 'cancelling') return;
    if (captureOwner && captureOwner !== owner) { paint('Another chat is recording. Finish or cancel that take first.'); return; }
    origin = getStreamId();
    if (!origin) { paint('Select a connected chat before recording.'); return; }
    captureOwner = owner; phase = 'starting'; const gen = ++generation; button.disabled = true;
    startInterrupted = false;
    // Permission and AudioContext resume may yield while the tab goes away.
    // Remember that interruption even if it becomes visible before start ends.
    recorder.setInterruptionHandler?.(interrupted); document.addEventListener('visibilitychange', visibilityChanged);
    paint('Requesting microphone…', null, cancel);
    try {
      await recorder.start();
      if (gen !== generation) { await recorder.discard(); reset(); return; }
      phase = 'recording'; started = nowMs(env); button.disabled = false;
      paintRecording(); tick = env.setInterval(sample, SAMPLE_INTERVAL_MS);
      telemetry?.('chat_voice_record_started', { subsystem: 'web_voice', bug_ref: BUG_REF, stream_id: origin });
      if (startInterrupted) interrupted();
    } catch (error) {
      if (gen !== generation) { reset(); return; }
      clearInterruptions();
      phase = 'failed'; captureOwner = null; button.disabled = false; paint(failureMessage(error), start, cancel);
    }
  }
  async function stop(reason = 'tap') {
    if (phase !== 'recording') return;
    const gen = generation;
    clearTick(); clearInterruptions(); phase = 'stopping'; button.disabled = true; recordingFace(false);
    const recordedLevels = levels.slice();
    const pending = { status: 'transcribing', levels: recordedLevels, durationS: Math.max(1, Math.min(MAX_DURATION_S, (nowMs(env) - started) / 1000)), interrupted: reason === 'interrupted' };
    paintPending(pending);
    try {
      const audio = await recorder.stop();
      if (gen !== generation) { reset(); return; }
      if (captureOwner === owner) captureOwner = null;
      take = createVoiceTake({ ...audio, levels: recordedLevels, interrupted: pending.interrupted, streamId: origin, id: env.crypto?.randomUUID?.() || `web-${Date.now()}-${Math.random().toString(16).slice(2)}` }, {
        upload, transcribe, send, telemetry,
        onState: state => {
          if (gen !== generation) return;
          phase = state.status;
          if (state.status === 'sent' || state.status === 'discarded') { reset(); return; }
          button.disabled = true;
          paintPending(state);
        },
      });
      await take.run();
    } catch (error) { if (gen !== generation) { reset(); return; } if (captureOwner === owner) captureOwner = null; phase = 'failed'; button.disabled = false; paint(failureMessage(error), start, cancel); }
  }
  button.addEventListener('click', () => { if (phase === 'recording') void stop(); else if (phase === 'idle' || phase === 'failed') void start(); });
  return { cancel, snapshot: () => ({ phase, streamId: origin }) };
}

async function uploadVoiceBlob(cc, blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = ''; for (let offset = 0; offset < bytes.length; offset += 0x8000) binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  return cc.chatUploadBlob({ dataBase64: btoa(binary), sizeHintBytes: bytes.length });
}
module.exports = { createVoiceTake, createBrowserRecorder, bindComposerMic, uploadVoiceBlob, encodeWav, MAX_DURATION_S };
