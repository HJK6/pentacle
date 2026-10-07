'use strict';

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
      stop.label === 'Stop and send' && stop.background === 'rgb(61, 255, 102)' && Math.abs(stop.width - 40) < 1 && Math.abs(stop.height - 40) < 1 && stop.radius >= 20,
      stop],
    ['no partial or interim transcript characters are painted before stop',
      recording.textSamples > 0 && recording.unexpectedText.length === 0 && recording.transcribed === 0 && recording.sent === 0,
      { textSamples: recording.textSamples, unexpectedText: recording.unexpectedText, transcribed: recording.transcribed, sent: recording.sent }],
  ];
}

function deliveryChecks(pending, result) {
  return [
    ['stopping paints a pending TRANSCRIBING voice row before completion',
      pending.visible && pending.inTranscript && pending.text.includes('TRANSCRIBING') && pending.bars === 30 && pending.sent === 0,
      pending],
    ['completed voice take paints exactly one ordinary text row with positive voice duration',
      result.rows === 1 && result.domRows === 1 && result.sentCount === 1 && result.meta?.voice?.duration_s > 0 && result.rowVoice?.duration_s > 0,
      { rows: result.rows, domRows: result.domRows, sentCount: result.sentCount, meta: result.meta, rowVoice: result.rowVoice }],
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
  const observer = new env.MutationObserver(records => {
    if (!observing) return;
    // Examine added/removed/changed text too: a transient interim painted
    // and replaced in the same event-loop turn must not evade the oracle.
    for (const record of records) {
      if (record.type === 'characterData') { recordText(record.oldValue); recordText(record.target.textContent); }
      for (const node of [...record.addedNodes, ...record.removedNodes]) recordText(node.textContent);
    }
    sample();
  });
  observer.observe(panel, { subtree: true, childList: true, characterData: true, characterDataOldValue: true });
  const interval = env.setInterval(sample, 25);
  gate.stopObserving = () => { sample(); observing = false; observer.disconnect(); env.clearInterval(interval); };
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
  try {
    await session.eval(`window.focusStreamId(${JSON.stringify(fixture.streamId)})`);
    await session.waitFor("!!document.querySelector('#header-0 [data-mode=\"chat\"]')");
    await session.click('#header-0 [data-mode="chat"]');
    const mic = '#cell-0 .slot-chat-compose-mic'; const panel = '#cell-0 .slot-chat-voice-take';
    await session.eval(`(${observeRecording.toString()})(document.querySelector(${JSON.stringify(panel)}), document.querySelector(${JSON.stringify(mic)}), window.__voiceGate)`);
    await session.send('Runtime.evaluate', { expression: `document.querySelector(${JSON.stringify(mic)}).click()`, userGesture: true });
    await session.waitFor(`document.querySelector(${JSON.stringify(panel)})?.textContent.includes('Recording')`);
    report.ok('web mic shows recording duration and Cancel', await session.eval(`document.querySelector(${JSON.stringify(panel)}).textContent.includes('0:00') && document.querySelector(${JSON.stringify(panel)}).textContent.includes('Cancel')`));
    await cdp.sleep(1350);
    await session.eval(`(() => {
      const stop = document.querySelector(${JSON.stringify(mic)});
      const style = getComputedStyle(stop); const rect = stop.getBoundingClientRect();
      window.__voiceGate.stopStyle = { label: stop.getAttribute('aria-label'), background: style.backgroundColor,
        width: rect.width, height: rect.height, radius: parseFloat(style.borderRadius) };
    })()`);
    if (report.dir) await session.screenshot(require('path').join(report.dir, 'voice-recording.png'));
    await session.eval('window.__voiceGate.fail = true');
    await session.send('Runtime.evaluate', { expression: `(() => {
      const gate = window.__voiceGate;
      gate.stopObserving();
      gate.recordingAtStop = { ...gate.recording, transcribed: gate.transcribed.length, sent: gate.sent.length, stop: gate.stopStyle };
      document.querySelector(${JSON.stringify(mic)}).click();
    })()`, userGesture: true });
    const recording = await session.eval('window.__voiceGate.recordingAtStop');
    await session.waitFor('window.__voiceGate.transcribed.length === 1');
    const pending = await session.eval(`(() => {
      const panel = document.querySelector(${JSON.stringify(panel)});
      return { visible: !panel.hidden && panel.getBoundingClientRect().height > 0,
        inTranscript: !!panel.closest('.slot-chat-scroll'), text: panel.textContent,
        bars: panel.querySelectorAll('.voice-bubble-bar').length, sent: window.__voiceGate.sent.length };
    })()`);
    if (report.dir) await session.screenshot(require('path').join(report.dir, 'voice-transcribing.png'));
    await session.eval('window.__voiceGate.holdTranscription = false; window.__voiceGate.releaseTranscription()');
    await session.waitFor(`document.querySelector(${JSON.stringify(panel)})?.textContent.includes('unavailable')`);
    report.ok('backend_unavailable is visible with Retry', await session.eval(`document.querySelector(${JSON.stringify(panel)}).textContent.includes('Retry')`));
    await session.eval(`window.__voiceGate.fail = false; Array.from(document.querySelector(${JSON.stringify(panel)}).querySelectorAll('button')).find(b => b.textContent === 'Retry').click()`);
    await session.waitFor('window.__voiceGate.sent.length === 1');
    await session.waitFor("[...document.querySelectorAll('#cell-0 .slot-chat-row.is-user')].some(row => row.textContent.includes('Hermetic voice transcript'))");
    const result = await session.eval(`(() => {
      const gate = window.__voiceGate;
      const args = gate.sent[0];
      return { text: args[2], meta: args[6]?.meta, stream: args[6]?.stream_id,
        ids: gate.transcribed.map(p => p.request_id), mime: gate.transcribed[0]?.mime,
        sha: gate.transcribed[0]?.blob_sha, roomClicks: gate.roomClicks,
        tracksEnded: gate.media.every(m => m.stream.getTracks().every(t => t.readyState === 'ended')),
        sentCount: gate.sent.length,
        domRows: [...document.querySelectorAll('#cell-0 .slot-chat-row.is-user')].filter(row => row.textContent.includes('Hermetic voice transcript')).length,
        rowVoice: window.PentacleChatStore.selectSessionDetail(${JSON.stringify(fixture.streamId)}).transcriptItems.find(i => i.text === 'Hermetic voice transcript')?.voice || null,
        rows: window.PentacleChatStore.selectSessionDetail(${JSON.stringify(fixture.streamId)}).transcriptItems.filter(i => i.text === 'Hermetic voice transcript').length,
        pendingHidden: document.querySelector(${JSON.stringify(panel)}).hidden };
    })()`);
    report.ok('record → upload → transcribe → ordinary send with voice metadata', result.text === 'Hermetic voice transcript' && result.stream === fixture.streamId && result.meta?.voice?.duration_s > 0 && /^[a-f0-9]{64}$/.test(result.sha) && ['audio/wav', 'audio/mp4'].includes(result.mime), result);
    report.ok('Retry keeps identity and replaces pending take with exactly one text row', result.ids.length === 2 && result.ids[0] === result.ids[1] && result.rows === 1 && result.pendingHidden, result);
    report.ok('chat mic leaves room mic untouched and releases media tracks', result.roomClicks === 0 && result.tracksEnded, result);
    reportChecks(report, [...recordingChecks(recording), ...deliveryChecks(pending, result)]);
  } catch (error) {
    report.note('voice UI at failure: ' + JSON.stringify(await session.eval(`({ web: window.HOST.isWeb, panel: document.querySelector('#cell-0 .slot-chat-voice-take')?.textContent, disabled: document.querySelector('#cell-0 .slot-chat-compose-mic')?.disabled })`)));
    throw error;
  } finally { await session.eval('window.__voiceGate.restore()'); }
}
module.exports = { webVoice, recordingChecks, deliveryChecks, observeRecording, reportChecks };
