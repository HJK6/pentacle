'use strict';

const fs = require('fs');
const { execFileSync } = require('child_process');
const path = require('path');
const { WORKSPACE_KEY, waitFor, reloadWith } = require('./workspace_scenario');
const json = JSON.stringify;
const TEXT = 'Hermetic server transcript';

// Real composer, real daemon, real reload; only media and the ASR backend are
// fake (web_gate_daemon.py holds ASR until the release file exists). Once the
// audio is on the daemon a page reload must not lose the message. The fixture
// chat has no agent to echo a USER row, so delivery is read from the daemon's
// own send receipts.
async function webVoiceReloadAfterCommit(ctx) {
  const { session, report, cdp, fixture, runtime } = ctx;
  if (!fixture) { report.note('web-voice-reload-after-commit: hermetic-only scenario skipped'); return; }
  const release = path.join(runtime.fixtureScratch, 'asr-release');
  const calls = path.join(runtime.fixtureScratch, 'asr-calls.log');
  const streamId = fixture.streamId;
  const mic = '#cell-0 .slot-chat-compose-mic';
  const reset = () => reloadWith(ctx, { [WORKSPACE_KEY]: json({ paneCount: 4, activeSlot: 0 }) });
  async function open() {
    await waitFor(ctx, '!!window.PentacleChatStore && typeof window.focusStreamId === "function"', 'chat store ready');
    await waitFor(ctx, `window.focusStreamId(${json(streamId)}) === true`, 'fixture chat focused');
    await waitFor(ctx, '!!document.querySelector(\'#header-0 [data-mode="chat"]\')', 'chat toggle');
    await session.eval('document.querySelector(\'#header-0 [data-mode="chat"]\').click(), true');
    await waitFor(ctx, `document.querySelector('#cell-0 .slot-chat-list')?.dataset.streamId === ${json(streamId)}`, 'fixture chat in slot 0');
  }
  const db = path.join(runtime.fixtureScratch, 'sessions.db');
  const sends = () => Number(execFileSync('sqlite3', ['-readonly', db,
    `SELECT count(DISTINCT request_id) FROM v2_send_receipts WHERE to_stream_id='${streamId}' AND display_text='${TEXT}'`], { encoding: 'utf8' }).trim());
  const query = sql => JSON.parse(execFileSync('sqlite3', ['-readonly', '-json', db, sql], { encoding: 'utf8' }).trim() || '[]');
  // Failure-only evidence: state, codes and milestone times; never mandate or transcript text.
  function finisherEvidence(since) {
    const operations = query(`SELECT operation_id, created_at, json_extract(data,'$.state') AS state,
      json_extract(data,'$.server_finish_failed_code') AS failed_code, json_extract(data,'$.worker.started_at') AS worker_started,
      json_extract(data,'$.ready_at') AS ready_at, json_extract(data,'$.upload_committed') AS upload_committed,
      json_extract(data,'$.transcribe_started') AS transcribe_started, json_extract(data,'$.transcribe_result') AS transcribe_result,
      json_extract(data,'$.transcribe_failed') AS transcribe_failed, json_extract(data,'$.send_attempted') AS send_attempted,
      json_extract(data,'$.send_committed') AS send_committed, json_extract(data,'$.server_finish_failed') AS failed_at,
      origin_generation FROM voice_operations WHERE origin_stream_id='${streamId}' AND created_at >= '${since}'`);
    const [host, name] = [streamId.slice(0, streamId.indexOf(':')), streamId.slice(streamId.indexOf(':') + 1)];
    const session = query(`SELECT * FROM sessions WHERE host='${host}' AND session_name='${name}'`)[0] || {};
    const origin = Object.fromEntries(Object.entries(session).filter(([key]) => /^(status|.*generation.*)$/.test(key)));
    return { operations, origin };
  }
  async function record(holdBeforeCommit) {
    await reset(); await open();
    await session.eval(`(() => {
      window.__reloadVoice = { held: [] };
      navigator.mediaDevices.getUserMedia = async () => {
        const context = new AudioContext(); await context.resume();
        const oscillator = context.createOscillator(); const destination = context.createMediaStreamDestination();
        oscillator.connect(destination); oscillator.start(); return destination.stream;
      };
      if (${holdBeforeCommit}) for (const name of ['chatUploadBlob', 'chatOutboundCommit']) {
        if (typeof window.cc[name] === 'function') window.cc[name] = () => { window.__reloadVoice.held.push(name); return new Promise(() => {}); };
      }
      return true;
    })()`);
    await session.send('Runtime.evaluate', { expression: `document.querySelector(${json(mic)}).click()`, userGesture: true });
    await waitFor(ctx, `document.querySelector('#cell-0 .slot-chat-voice-take')?.textContent.includes('Recording')`, 'recording');
    await cdp.sleep(1350);
    await session.send('Runtime.evaluate', { expression: `document.querySelector(${json(mic)}).click()`, userGesture: true });
  }
  async function untilFile(file, label, timeoutMs = 20000) {
    const deadline = Date.now() + timeoutMs;
    while (!fs.existsSync(file)) {
      if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`);
      await cdp.sleep(100);
    }
  }
  try {
    fs.rmSync(release, { force: true }); fs.rmSync(calls, { force: true });
    // Control: the take never leaves the page, so a reload loses it on every build.
    await record(true);
    await waitFor(ctx, 'window.__reloadVoice.held.length > 0', 'upload or commit attempted');
    await reset(); await open();
    fs.writeFileSync(release, '');
    await cdp.sleep(3000);
    const control = { sends: sends(), asrCalled: fs.existsSync(calls) };
    report.ok('control: a reload before the commit loses the never-uploaded take', control.sends === 0 && !control.asrCalled, control);

    // Journey: the audio is on the daemon and ASR has started when the page reloads.
    fs.rmSync(release, { force: true });
    const since = new Date().toISOString().slice(0, 19);
    const times = { recordStarted: new Date().toISOString() };
    await record(false);
    await untilFile(calls, 'daemon ASR to start');
    times.asrCalled = fs.statSync(calls).mtime.toISOString();
    await reset(); await open();
    times.reopened = new Date().toISOString();
    fs.writeFileSync(release, '');
    times.released = new Date().toISOString();
    const deadline = Date.now() + 20000;
    while (sends() === 0 && Date.now() < deadline) await cdp.sleep(200);
    await cdp.sleep(2000);
    const journey = { sends: sends(), asrCalls: fs.readFileSync(calls, 'utf8').trim().split('\n').length };
    if (journey.sends !== 1) Object.assign(journey, { times, ...finisherEvidence(since) });
    report.ok('reload after the audio reaches the daemon: exactly one send carries the transcript', journey.sends === 1, journey);
  } finally {
    fs.writeFileSync(release, '');
  }
}

module.exports = { webVoiceReloadAfterCommit };
