'use strict';

// Serialized into the page so unit tests exercise the same style-to-observation
// conversion as the real gate, including percentage and elliptical units.
function readStopControl(stop, env = stop.ownerDocument.defaultView) {
  const style = env.getComputedStyle(stop); const rect = stop.getBoundingClientRect();
  return { label: stop.getAttribute('aria-label'), background: style.backgroundColor,
    width: rect.width, height: rect.height,
    corners: [style.borderTopLeftRadius, style.borderTopRightRadius, style.borderBottomRightRadius, style.borderBottomLeftRadius] };
}

// Resolve all four computed corner longhands in their actual units, then apply
// CSS's overlap scaling. Large equal radii still describe a true circle; small
// percentages, asymmetric corners, and elliptical pairs do not.
function hasCircularStopShape({ width, height, corners }) {
  if (!Number.isFinite(width) || !Number.isFinite(height) || Math.abs(width - 40) >= 1 || Math.abs(height - 40) >= 1 || Math.abs(width - height) > 0.1) return false;
  if (!Array.isArray(corners) || corners.length !== 4) return false;
  const resolve = (value, extent) => {
    const match = /^(\d+(?:\.\d+)?)(px|%)$/.exec(value);
    if (!match) return NaN;
    return Number(match[1]) * (match[2] === '%' ? extent / 100 : 1);
  };
  const radii = corners.map(corner => {
    const parts = String(corner).trim().split(/\s+/);
    return parts.length >= 1 && parts.length <= 2
      ? [resolve(parts[0], width), resolve(parts[1] || parts[0], height)] : [NaN, NaN];
  });
  if (radii.some(pair => pair.some(value => !Number.isFinite(value)))) return false;
  const scale = Math.min(1,
    width / (radii[0][0] + radii[1][0]), width / (radii[3][0] + radii[2][0]),
    height / (radii[0][1] + radii[3][1]), height / (radii[1][1] + radii[2][1]));
  return radii.every(([x, y]) => Math.abs(x * scale - width / 2) < 0.1 && Math.abs(y * scale - height / 2) < 0.1);
}

// These predicates also support isolated negative-control tests of the oracle.
// They consume observations of the shipped browser UI, never synthetic product UI.
function recordingChecks(recording) {
  const stop = recording.stop || {};
  return [
    ['recording strip renders a live metering bar within one second',
      Number.isFinite(recording.firstBarMs) && recording.firstBarMs <= 1000 && recording.maxBars >= 1 && recording.maxBars <= 46,
      { firstBarMs: recording.firstBarMs, maxBars: recording.maxBars }],
    ['recording timer advances in m:ss format',
      recording.timers.includes('0:00') && recording.timers.some(value => /^\d+:[0-5]\d$/.test(value) && value !== '0:00'),
      { timers: recording.timers }],
    ['stop-and-send control is the mobile green 40px circle',
      stop.label === 'Stop and send' && stop.background === 'rgb(61, 255, 102)' && hasCircularStopShape(stop),
      stop],
    ['no partial or interim transcript characters are painted before stop',
      recording.textSamples > 0 && recording.unexpectedText.length === 0 && recording.transcribed === 0 && recording.sent === 0,
      { textSamples: recording.textSamples, unexpectedText: recording.unexpectedText, transcribed: recording.transcribed, sent: recording.sent }],
    ['mounted recording composer capsule has computed green tint and border',
      recording.composer?.background === 'rgba(61, 255, 102, 0.055)' && recording.composer?.border === 'rgba(61, 255, 102, 0.4)',
      recording.composer],
  ];
}

function deliveryChecks(pending, result) {
  return [
    ...(pending ? [['stopping paints a pending TRANSCRIBING voice row before completion',
      pending.visible && pending.inTranscript && pending.text.includes('TRANSCRIBING') && pending.bars === 30 && pending.sent === 0,
      pending]] : []),
    ...(result ? [['completed voice take paints exactly one ordinary text row with positive voice duration',
      result.rows === 1 && result.domRows === 1 && result.sentCount === 1 && result.meta?.voice?.duration_s > 0 && result.rowVoice?.duration_s > 0 && result.captionVisible && /^\d+:[0-5]\d$/.test(result.captionText),
      { rows: result.rows, domRows: result.domRows, sentCount: result.sentCount, meta: result.meta, rowVoice: result.rowVoice, captionVisible: result.captionVisible, captionText: result.captionText }]] : []),
  ];
}

function reportChecks(report, checks) {
  const failures = [];
  for (const [name, passed, detail] of checks) {
    try { report.ok(name, passed, detail); } catch (error) { failures.push(error); }
  }
  // Preserve every failed predicate in the receipt without turning a failed
  // product contract into a pass or preventing independent assertions running.
  if (failures.length) throw new AggregateError(failures, failures.map(error => error.message).join('; '));
}


// Self-contained so the same text observer runs in Chromium and its focused
// MutationObserver negative-control tests. No recorder/provider is mocked here.
function observeRecording(panel, mic, gate, env = panel.ownerDocument.defaultView) {
  gate.recording = { startedAt: null, firstBarMs: null, maxBars: 0, timers: [], textSamples: 0, unexpectedText: [] };
  let observing = true;
  const recordText = text => {
    if (!text) return;
    gate.recording.textSamples++;
    // Every allowed character belongs to recording UI chrome or its timer.
    // An arbitrary partial transcript (including one absent by next poll)
    // fails instead of checking only for the final synthetic phrase.
    const remaining = text.replace(/Requesting microphone|Cancel recording|Discard recording|Recording|Cancel|Discard/g, '').replace(/\b\d+:[0-5]\d\b/g, '').replace(/[\s·.×✕…]/g, '');
    if (remaining && !gate.recording.unexpectedText.includes(text)) gate.recording.unexpectedText.push(text);
  };
  const sample = () => {
    if (!observing) return;
    recordText(panel.textContent);
    if (!mic.classList.contains('is-recording')) return;
    const now = env.performance.now();
    if (gate.recording.startedAt === null) gate.recording.startedAt = now;
    const timer = panel.querySelector('.voice-recording-timer')?.textContent || panel.textContent.match(/\d+:[0-5]\d/)?.[0];
    if (timer && !gate.recording.timers.includes(timer)) gate.recording.timers.push(timer);
    const bars = [...panel.querySelectorAll('.voice-recording-bar[data-sample]')].filter(bar => {
      const rect = bar.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && env.getComputedStyle(bar).visibility !== 'hidden';
    });
    gate.recording.maxBars = Math.max(gate.recording.maxBars, bars.length);
    if (bars.length && gate.recording.firstBarMs === null) gate.recording.firstBarMs = now - gate.recording.startedAt;
  };
  const consumeRecords = records => {
    if (!observing) return;
    // Examine added/removed/changed text too: a transient interim painted
    // and replaced in the same event-loop turn must not evade the oracle.
    for (const record of records) {
      if (record.type === 'characterData') { recordText(record.oldValue); recordText(record.target.textContent); }
      for (const node of [...record.addedNodes, ...record.removedNodes]) recordText(node.textContent);
    }
    sample();
  };
  const observer = new env.MutationObserver(consumeRecords);
  observer.observe(panel, { subtree: true, childList: true, characterData: true, characterDataOldValue: true });
  const interval = env.setInterval(sample, 25);
  gate.stopObserving = () => {
    if (!observing) return;
    // A stop can run before the observer's microtask. Drain its queued records
    // while observation is active so disconnect cannot erase transient text.
    consumeRecords(observer.takeRecords());
    observing = false; observer.disconnect(); env.clearInterval(interval);
  };
  sample();
}

// Only media and the ASR/provider counterparts are fake. This exercises the
// shipped composer, recorder, real blob upload and ordinary optimistic store.
async function webVoice({ session, report, fixture, cdp }) {
  if (!fixture) { report.note('voice fixture is hermetic only'); return; }
  await session.waitFor("document.readyState === 'complete' && typeof window.focusStreamId === 'function'");
  await session.eval(`(() => {
    const originalMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    const originalTranscribe = window.cc.chatTranscribeBlob;
    const originalSend = window.cc.chatSendCorrelated;
    const originalPty = window.cc.createPty;
    window.cc.createPty = async () => '%web-voice-fixture';
    window.__voiceGate = { sent: [], transcribed: [], media: [], roomClicks: 0, holdTranscription: true };
    const room = document.getElementById('mic-btn-toggle');
    const listener = () => window.__voiceGate.roomClicks++;
    room.addEventListener('click', listener);
    navigator.mediaDevices.getUserMedia = async () => {
      const context = new AudioContext(); await context.resume();
      const oscillator = context.createOscillator(); const destination = context.createMediaStreamDestination();
      oscillator.connect(destination); oscillator.start();
      window.__voiceGate.media.push({ context, oscillator, stream: destination.stream });
      return destination.stream;
    };
    window.cc.chatTranscribeBlob = async payload => {
      window.__voiceGate.transcribed.push(payload);
      if (window.__voiceGate.holdTranscription) await new Promise(resolve => { window.__voiceGate.releaseTranscription = resolve; });
      if (window.__voiceGate.fail) return { ok: false, error_code: 'backend_unavailable' };
      return { ok: true, text: 'Hermetic voice transcript', duration_s: 1 };
    };
    window.cc.chatSendCorrelated = async (...args) => { window.__voiceGate.sent.push(args); return { ok: true }; };
    window.__voiceGate.restore = async () => {
      window.__voiceGate.stopObserving?.();
      window.__voiceGate.releaseTranscription?.();
      navigator.mediaDevices.getUserMedia = originalMedia;
      window.cc.chatTranscribeBlob = originalTranscribe; window.cc.chatSendCorrelated = originalSend; window.cc.createPty = originalPty;
      room.removeEventListener('click', listener);
      for (const media of window.__voiceGate.media) { media.oscillator.stop(); media.stream.getTracks().forEach(t => t.stop()); await media.context.close(); }
    };
  })()`);
  const retainedChecks = []; const errors = [];
  let recording; let pending; let result;
  try {
    await session.eval(`window.focusStreamId(${JSON.stringify(fixture.streamId)})`);
    await session.waitFor("!!document.querySelector('#header-0 [data-mode=\"chat\"]')");
    await session.click('#header-0 [data-mode="chat"]');
    const mic = '#cell-0 .slot-chat-compose-mic'; const panel = '#cell-0 .slot-chat-voice-take';
    await session.eval(`(${observeRecording.toString()})(document.querySelector(${JSON.stringify(panel)}), document.querySelector(${JSON.stringify(mic)}), window.__voiceGate)`);
    await session.send('Runtime.evaluate', { expression: `document.querySelector(${JSON.stringify(mic)}).click()`, userGesture: true });
    await session.waitFor(`document.querySelector(${JSON.stringify(panel)})?.textContent.includes('Recording')`);
    retainedChecks.push(['web mic shows recording duration and Cancel', await session.eval(`document.querySelector(${JSON.stringify(panel)}).textContent.includes('0:00') && document.querySelector(${JSON.stringify(panel)}).textContent.includes('Cancel')`)]);
    await cdp.sleep(1350);
    await session.eval(`window.__voiceGate.stopStyle = (${readStopControl.toString()})(document.querySelector(${JSON.stringify(mic)}))`);
    if (report.dir) await session.screenshot(require('path').join(report.dir, 'voice-recording.png'));
    await session.eval('window.__voiceGate.fail = true');
    await session.send('Runtime.evaluate', { expression: `(() => {
      const gate = window.__voiceGate;
      gate.stopObserving();
      gate.recordingAtStop = { ...gate.recording, transcribed: gate.transcribed.length, sent: gate.sent.length, stop: gate.stopStyle,
        composer: (() => { const style = getComputedStyle(document.querySelector(${JSON.stringify(mic)}).closest('.slot-chat-compose')); return { background: style.backgroundColor, border: style.borderTopColor }; })() };
      document.querySelector(${JSON.stringify(mic)}).click();
    })()`, userGesture: true });
    recording = await session.eval('window.__voiceGate.recordingAtStop');
    await session.waitFor('window.__voiceGate.transcribed.length === 1');
    pending = await session.eval(`(() => {
      const panel = document.querySelector(${JSON.stringify(panel)});
      return { visible: !panel.hidden && panel.getBoundingClientRect().height > 0,
        inTranscript: !!panel.closest('.slot-chat-scroll'), text: panel.textContent,
        bars: panel.querySelectorAll('.voice-bubble-bar').length, sent: window.__voiceGate.sent.length };
    })()`);
    if (report.dir) await session.screenshot(require('path').join(report.dir, 'voice-transcribing.png'));
    await session.eval('window.__voiceGate.holdTranscription = false; window.__voiceGate.releaseTranscription()');
    await session.waitFor(`document.querySelector(${JSON.stringify(panel)})?.textContent.includes('unavailable')`);
    const canRetry = await session.eval(`document.querySelector(${JSON.stringify(panel)}).textContent.includes('Retry')`);
    retainedChecks.push(['backend_unavailable is visible with Retry', canRetry]);
    if (!canRetry) throw new Error('Retry control is missing; delivery checks require an actionable failed take');
    await session.eval(`window.__voiceGate.fail = false; Array.from(document.querySelector(${JSON.stringify(panel)}).querySelectorAll('button')).find(b => b.textContent === 'Retry').click()`);
    await session.waitFor('window.__voiceGate.sent.length >= 1');
    await session.waitFor("[...document.querySelectorAll('#cell-0 .slot-chat-row.is-user')].some(row => row.textContent.includes('Hermetic voice transcript'))");
    result = await session.eval(`(() => {
      const gate = window.__voiceGate;
      const args = gate.sent[0];
      const row = [...document.querySelectorAll('#cell-0 .slot-chat-row.is-user')].find(row => row.textContent.includes('Hermetic voice transcript'));
      const caption = row?.querySelector('.slot-chat-voice-caption');
      const captionStyle = caption && getComputedStyle(caption);
      const captionRect = caption?.getBoundingClientRect();
      return { text: args[2], meta: args[6]?.meta, stream: args[6]?.stream_id,
        ids: gate.transcribed.map(p => p.request_id), mime: gate.transcribed[0]?.mime,
        sha: gate.transcribed[0]?.blob_sha, roomClicks: gate.roomClicks,
        tracksEnded: gate.media.every(m => m.stream.getTracks().every(t => t.readyState === 'ended')),
        sentCount: gate.sent.length,
        captionVisible: !!caption && captionRect.width > 0 && captionRect.height > 0 && captionStyle.display !== 'none' && captionStyle.visibility !== 'hidden',
        captionText: caption?.textContent.trim() || '',
        domRows: [...document.querySelectorAll('#cell-0 .slot-chat-row.is-user')].filter(row => row.textContent.includes('Hermetic voice transcript')).length,
        rowVoice: window.PentacleChatStore.selectSessionDetail(${JSON.stringify(fixture.streamId)}).transcriptItems.find(i => i.text === 'Hermetic voice transcript')?.voice || null,
        rows: window.PentacleChatStore.selectSessionDetail(${JSON.stringify(fixture.streamId)}).transcriptItems.filter(i => i.text === 'Hermetic voice transcript').length,
        pendingHidden: document.querySelector(${JSON.stringify(panel)}).hidden };
    })()`);
    retainedChecks.push(['record → upload → transcribe → ordinary send with voice metadata', result.text === 'Hermetic voice transcript' && result.stream === fixture.streamId && result.meta?.voice?.duration_s > 0 && /^[a-f0-9]{64}$/.test(result.sha) && ['audio/wav', 'audio/mp4'].includes(result.mime), result]);
    retainedChecks.push(['Retry keeps identity and replaces pending take with exactly one text row', result.ids.length === 2 && result.ids[0] === result.ids[1] && result.rows === 1 && result.pendingHidden, result]);
    retainedChecks.push(['chat mic leaves room mic untouched and releases media tracks', result.roomClicks === 0 && result.tracksEnded, result]);
  } catch (error) {
    errors.push(error);
    try {
      report.note('voice UI at failure: ' + JSON.stringify(await session.eval(`({ web: window.HOST.isWeb, panel: document.querySelector('#cell-0 .slot-chat-voice-take')?.textContent, disabled: document.querySelector('#cell-0 .slot-chat-compose-mic')?.disabled })`)));
    } catch (diagnosticError) { errors.push(diagnosticError); }
  } finally {
    // Assert everything already observed even if a retained assertion fails.
    // Missing prerequisites stop the journey above; no unavailable result is
    // fabricated, and recording/pending observations can still be reported.
    try {
      const unavailable = [!recording && 'recording UI', !pending && 'pending voice row', !result && 'completed delivery'].filter(Boolean);
      if (unavailable.length) report.note(`Unavailable voice checks: ${unavailable.join(', ')}; prerequisite failed: ${errors[0]?.message || 'observation missing'}`);
      reportChecks(report, [...retainedChecks, ...(recording ? recordingChecks(recording) : []), ...deliveryChecks(pending, result)]);
    } catch (assertionError) { errors.push(assertionError); }
    try { await session.eval('window.__voiceGate.restore()'); }
    catch (cleanupError) { errors.push(cleanupError); }
  }
  if (errors.length) throw new AggregateError(errors, errors.map(error => error.message).join('; '));
}
module.exports = { webVoice, recordingChecks, deliveryChecks, observeRecording, reportChecks, readStopControl };
