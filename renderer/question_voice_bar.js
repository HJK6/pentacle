'use strict';
const { bindComposerMic, createBrowserRecorder } = require('./web_voice');
const { createSegments, selectedSet, isVoiceEligible, registerVoiceAnswersBinding,
  releaseVoiceAnswersBinding, buildVoiceAnswersMeta } = require('./voice_answers_binding');

// The deck adds coverage and interaction to the existing capture/upload/ASR unit.
// Its second controller participates in web_voice's module-wide captureOwner.
function createQuestionVoiceBar({ env = globalThis, mount, takeMount, scope = takeMount, getStreamId,
  upload, transcribe, send, sendPlain, telemetry, onFinished = () => {}, onDiscarded = () => {}, onRecordingChange = () => {},
  recorder = createBrowserRecorder(env), bindMic = bindComposerMic }) {
  const doc = mount.ownerDocument;
  const mic = doc.createElement('button'); mic.type = 'button';
  mic.className = 'question-voice-mic'; mic.dataset.questionVoiceMic = '';
  const bar = doc.createElement('div'); bar.className = 'question-voice-bar'; bar.dataset.questionVoiceBar = ''; bar.hidden = true;
  const captureMount = doc.createElement('div'); captureMount.className = 'question-voice-capture';
  const panel = doc.createElement('div'); captureMount.appendChild(panel); bar.appendChild(captureMount);
  const progress = doc.createElement('span'); progress.dataset.questionVoiceProgress = ''; progress.setAttribute('role', 'status');
  const done = doc.createElement('button'); done.type = 'button'; done.dataset.questionVoiceDone = ''; done.textContent = 'Done'; done.setAttribute('aria-label', 'Finish voice answers');
  const controls = doc.createElement('div'); controls.className = 'question-voice-controls'; controls.append(progress, done); bar.appendChild(controls);
  const confirmation = doc.createElement('div'); confirmation.dataset.questionVoiceConfirm = ''; confirmation.hidden = true;
  confirmation.setAttribute('role', 'alertdialog'); confirmation.setAttribute('aria-label', 'Discard this recording?');
  const question = doc.createElement('span'); question.textContent = 'Discard this recording?'; confirmation.appendChild(question);
  const keep = doc.createElement('button'); keep.type = 'button'; keep.textContent = 'Keep'; keep.dataset.questionVoiceKeep = '';
  const discard = doc.createElement('button'); discard.type = 'button'; discard.textContent = 'Discard'; discard.dataset.questionVoiceDiscard = '';
  confirmation.append(keep, discard); bar.appendChild(confirmation); mount.appendChild(bar);
  let pages = []; let currentKey = null; let pageState = null; let segments = null;
  let frozen = []; let recordingId = null; let thresholdTimer = null; let pendingLeave = null; let surfaceStreamId = null;
  let controller; let destroyed = false; let transformBinding = value => value;
  let captureReleased = Promise.resolve(); let releaseCapture = () => {}; let lastRecording = false;
  const now = () => env.performance?.now?.() ?? Date.now();
  const live = () => ['recording', 'starting'].includes(controller?.snapshot().phase);
  const clearThreshold = () => { if (thresholdTimer !== null) env.clearTimeout(thresholdTimer); thresholdTimer = null; };
  function paint() {
    if (destroyed) return;
    const recording = !!segments && live();
    if (recording !== lastRecording) {
      lastRecording = recording;
      Promise.resolve().then(() => { if (!destroyed) onRecordingChange(recording); });
    }
    const covered = segments ? selectedSet(segments, pages, surfaceStreamId) : [];
    mic.hidden = !live() && !pages.some(isVoiceEligible);
    mic.setAttribute('aria-label', recording ? 'Finish voice answers' : 'Record answers by voice');
    controls.hidden = !recording;
    const progressText = `${covered.length} of ${segments?.n ?? 0} answered by voice`;
    if (progress.textContent !== progressText) progress.textContent = progressText;
    bar.hidden = (panel.hidden || panel.parentNode !== captureMount) && confirmation.hidden;
    if (pageState) {
      const page = pages.find(entry => entry.key === currentKey);
      const tap = !isVoiceEligible(page) || (segments && !segments.tracked(currentKey));
      pageState.hidden = !tap && !recording;
      pageState.textContent = tap ? 'ANSWER BY TAP' : covered.some(item => item.key === currentKey) ? 'ANSWER RECORDED' : 'RECORDING YOUR ANSWER…';
      if (tap) pageState.dataset.questionVoiceLegacy = ''; else delete pageState.dataset.questionVoiceLegacy;
    }
    // web_voice owns its pending bubble and metering; only its extra P6 label is ours.
    if (panel.classList.contains('is-pending') && frozen.length && !panel.querySelector('[data-voice-answers-count]')) {
      const count = doc.createElement('span'); count.dataset.voiceAnswersCount = '';
      count.className = 'question-voice-pending-count'; count.textContent = `ANSWERS ${frozen.length} QUESTIONS`;
      panel.querySelector('.voice-pending-bubble')?.appendChild(count);
    }
  }
  function scheduleThreshold() {
    clearThreshold();
    const wait = segments?.msUntilCovered();
    if (wait !== null && wait !== undefined) thresholdTimer = env.setTimeout(() => { thresholdTimer = null; paint(); }, wait);
  }
  function freeze() {
    confirmation.hidden = true; pendingLeave = null;
    if (!segments) return [];
    segments.finish(); frozen = selectedSet(segments, pages, surfaceStreamId); clearThreshold();
    return frozen;
  }
  const capture = {
    setInterruptionHandler: callback => recorder.setInterruptionHandler?.(callback),
    async start() {
      surfaceStreamId = controller?.snapshot().streamId || getStreamId();
      captureReleased = new Promise(resolve => { releaseCapture = resolve; });
      try { await recorder.start(); } catch (error) { releaseCapture(); throw error; }
      if (destroyed) return;
      frozen = []; recordingId = null;
      segments = createSegments(now); segments.start(pages, currentKey); scheduleThreshold();
    },
    poll() { paint(); return recorder.poll?.(); },
    async stop() {
      freeze();
      if (!frozen.length) {
        // Invalidate this stop's generation before the voice unit can upload.
        void controller.cancel();
        try { await recorder.discard(); } finally { releaseCapture(); }
        segments = null;
        return { blob: null, durationS: 0 };
      }
      segments = null;
      try { const audio = await recorder.stop(); onFinished(); return audio; }
      finally { releaseCapture(); }
    },
    async discard() {
      clearThreshold(); segments = null; frozen = [];
      if (recordingId) releaseVoiceAnswersBinding(recordingId);
      recordingId = null;
      try { await recorder.discard(); } finally { releaseCapture(); }
    },
  };
  panel.addEventListener('click', event => {
    if (event.target.closest?.('.voice-recording-discard') && live()) {
      event.preventDefault(); event.stopImmediatePropagation(); requestLeave();
    }
  }, true);
  controller = bindMic({ web: true, env, button: mic, panel, composer: null, takeMount,
    getStreamId, recorder: capture, upload,
    transcribe: payload => {
      recordingId = payload.request_id.slice('transcribe-'.length);
      registerVoiceAnswersBinding(recordingId, frozen);
      activeBlobSha = payload.blob_sha; paint();
      return transcribe(payload);
    },
    send: ({ streamId, text, meta }) => {
      const binding = buildVoiceAnswersMeta(recordingId, { blobSha: activeBlobSha, durationS: meta.voice.duration_s });
      if (!binding) throw new Error('Voice answers binding is unavailable.');
      const id = send({ streamId, text, meta: { ...meta, voice_answers: transformBinding(binding) } });
      if (id) releaseVoiceAnswersBinding(recordingId);
      return id;
    }, telemetry });
  let activeBlobSha;
  const observer = new env.MutationObserver(paint);
  observer.observe(panel, { attributes: true, attributeFilter: ['class', 'hidden'], childList: true, subtree: true });
  function requestLeave(action = onDiscarded) {
    if (controller?.snapshot().phase === 'cancelling') return true;
    if (!live()) return false;
    pendingLeave = action; confirmation.hidden = false; paint(); keep.focus(); return true;
  }
  async function cancel() {
    clearThreshold(); confirmation.hidden = true; pendingLeave = null;
    segments = null; frozen = [];
    if (recordingId) releaseVoiceAnswersBinding(recordingId);
    await controller?.cancel(); await captureReleased; paint();
  }
  keep.addEventListener('click', () => { pendingLeave = null; confirmation.hidden = true; paint(); mic.focus(); });
  discard.addEventListener('click', async () => { const action = pendingLeave; await cancel(); action?.(); });
  done.addEventListener('click', () => { if (controller?.snapshot().phase === 'recording') mic.click(); });
  const escape = event => {
    if (event.key !== 'Escape' || !live()) return;
    if (!scope?.contains(event.target) && !mount.contains(event.target) && !mic.contains(event.target)) return;
    event.preventDefault(); event.stopImmediatePropagation(); requestLeave();
  };
  doc.addEventListener('keydown', escape, true);
  const plainClick = event => {
    const button = event.target.closest?.('[data-question-voice-plain]');
    if (!button || !takeMount?.contains(button) || button.disabled) return;
    event.preventDefault(); event.stopPropagation();
    button.disabled = true;
    if (!sendPlain?.(button.dataset.optimisticId || '')) button.disabled = false;
  };
  takeMount?.addEventListener('click', plainClick);
  return {
    update({ entries, activeKey, header, before = null, stateMount, barMount = mount }) {
      pages = entries; currentKey = activeKey;
      if (segments) { segments.enter(currentKey); scheduleThreshold(); }
      if (header) header.insertBefore(mic, before);
      if (bar.parentNode !== barMount) barMount.appendChild(bar);
      pageState = doc.createElement('span'); pageState.dataset.questionVoicePageState = ''; pageState.className = 'question-voice-page-state';
      stateMount?.prepend(pageState); paint();
    },
    requestLeave, cancel,
    snapshot: () => ({ ...controller?.snapshot(), n: segments?.n ?? 0, selected: segments ? selectedSet(segments, pages, surfaceStreamId) : frozen }),
    async dispose() { destroyed = true; observer.disconnect(); doc.removeEventListener('keydown', escape, true); takeMount?.removeEventListener('click', plainClick); await cancel(); mic.remove(); bar.remove(); },
    ...(env.PentacleHarness ? { setBindingTransformForTest(fn) { transformBinding = fn; } } : {}),
  };
}
module.exports = { createQuestionVoiceBar };
