'use strict';

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
    window.__voiceGate = { sent: [], transcribed: [], media: [], roomClicks: 0 };
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
      if (window.__voiceGate.fail) return { ok: false, error_code: 'backend_unavailable' };
      return { ok: true, text: 'Hermetic voice transcript', duration_s: 1 };
    };
    window.cc.chatSendCorrelated = async (...args) => { window.__voiceGate.sent.push(args); return { ok: true }; };
    window.__voiceGate.restore = async () => {
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
    await session.send('Runtime.evaluate', { expression: `document.querySelector(${JSON.stringify(mic)}).click()`, userGesture: true });
    await session.waitFor(`document.querySelector(${JSON.stringify(panel)})?.textContent.includes('Recording')`);
    report.ok('web mic shows recording duration and Cancel', await session.eval(`document.querySelector(${JSON.stringify(panel)}).textContent.includes('0:00') && document.querySelector(${JSON.stringify(panel)}).textContent.includes('Cancel')`));
    await cdp.sleep(200);
    await session.eval('window.__voiceGate.fail = true');
    await session.send('Runtime.evaluate', { expression: `document.querySelector(${JSON.stringify(mic)}).click()`, userGesture: true });
    await session.waitFor(`document.querySelector(${JSON.stringify(panel)})?.textContent.includes('unavailable')`);
    report.ok('backend_unavailable is visible with Retry', await session.eval(`document.querySelector(${JSON.stringify(panel)}).textContent.includes('Retry')`));
    await session.eval(`window.__voiceGate.fail = false; Array.from(document.querySelector(${JSON.stringify(panel)}).querySelectorAll('button')).find(b => b.textContent === 'Retry').click()`);
    await session.waitFor('window.__voiceGate.sent.length === 1');
    const result = await session.eval(`(() => {
      const gate = window.__voiceGate;
      const args = gate.sent[0];
      return { text: args[2], meta: args[6]?.meta, stream: args[6]?.stream_id,
        ids: gate.transcribed.map(p => p.request_id), mime: gate.transcribed[0]?.mime,
        sha: gate.transcribed[0]?.blob_sha, roomClicks: gate.roomClicks,
        tracksEnded: gate.media.every(m => m.stream.getTracks().every(t => t.readyState === 'ended')),
        rows: window.PentacleChatStore.selectSessionDetail(${JSON.stringify(fixture.streamId)}).transcriptItems.filter(i => i.text === 'Hermetic voice transcript').length,
        pendingHidden: document.querySelector(${JSON.stringify(panel)}).hidden };
    })()`);
    report.ok('record → upload → transcribe → ordinary send with voice metadata', result.text === 'Hermetic voice transcript' && result.stream === fixture.streamId && result.meta?.voice?.duration_s > 0 && /^[a-f0-9]{64}$/.test(result.sha) && ['audio/wav', 'audio/mp4'].includes(result.mime), result);
    report.ok('Retry keeps identity and replaces pending take with exactly one text row', result.ids.length === 2 && result.ids[0] === result.ids[1] && result.rows === 1 && result.pendingHidden, result);
    report.ok('chat mic leaves room mic untouched and releases media tracks', result.roomClicks === 0 && result.tracksEnded, result);
  } catch (error) {
    report.note('voice UI at failure: ' + JSON.stringify(await session.eval(`({ web: window.HOST.isWeb, panel: document.querySelector('#cell-0 .slot-chat-voice-take')?.textContent, disabled: document.querySelector('#cell-0 .slot-chat-compose-mic')?.disabled })`)));
    throw error;
  } finally { await session.eval('window.__voiceGate.restore()'); }
}
module.exports = { webVoice };
