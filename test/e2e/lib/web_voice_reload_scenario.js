'use strict';

const fs = require('fs');
const { execFileSync } = require('child_process');
const path = require('path');
const { WORKSPACE_KEY, waitFor, reloadWith } = require('./workspace_scenario');
const json = JSON.stringify;
const TEXT = 'Hermetic server transcript';

// This is the host's current snapshot cache, not a historical welcome trace.
async function readCommitSnapshot() {
  try {
    const state = await window.cc.getChatStreamState();
    const hash = value => typeof value === 'string' && /^[a-f0-9]{7,40}$/i.test(value) ? value : null;
    const boolean = value => typeof value === 'boolean' ? value : null;
    return { source: 'getChatStreamState', connected: boolean(state?.connected),
      stateVersion: Number.isSafeInteger(state?.state_version) ? state.state_version : null,
      outboundCommitV1: boolean(state?.capabilities?.outbound_commit_v1),
      runtimeSha: hash(state?.runtime_sha), buildSha: hash(state?.build_sha) };
  } catch { return { source: 'getChatStreamState', readError: 'unavailable' }; }
}
function installCommitProbe(readSnapshot) {
  const original = window.cc.chatOutboundCommit;
  const boolean = value => typeof value === 'boolean' ? value : null;
  const safeCode = code => code == null ? null : new Set(['unsupported', 'unsupported_in_v2',
    'invalid_request', 'operator_required', 'credential_revoked', 'operation_forbidden',
    'unavailable', 'unknown_verb', 'timeout', 'disconnected']).has(code) ? code : 'unrecognized_error';
  window.cc.chatOutboundCommit = async unit => {
    const commit = window.__reloadVoice.commit = { phase: 'pending',
      operationId: unit.voice_operation.operation_id, requestId: unit.send_mandate.request_id,
      origin: unit.voice_operation.origin_stream_id, generation: unit.voice_operation.origin_generation,
      replyOperationId: null, replyRequestId: null, ok: null, serverSend: null, error: null };
    commit.snapshotBefore = await readSnapshot();
    try {
      const reply = await original(unit);
      Object.assign(commit, { phase: 'replied', replyOperationId: reply?.operation_id ?? null,
        replyRequestId: reply?.request_id ?? null, ok: boolean(reply?.ok),
        serverSend: boolean(reply?.server_send), error: safeCode(reply?.error_code) });
      return reply;
    } catch (error) {
      Object.assign(commit, { phase: 'threw', error: safeCode(error?.error_code || error?.code) });
      throw error;
    }
  };
}

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
  let lastCommit = null;
  const times = {};
  async function diagnose(stage) {
    let currentSnapshot = null, pageReadError;
    try {
      const observed = await session.eval(`(async () => ({ commit: window.__reloadVoice?.commit || null,
        snapshot: await (${readCommitSnapshot.toString()})() }))()`);
      if (observed.commit) lastCommit = observed.commit;
      currentSnapshot = observed.snapshot;
    } catch { pageReadError = 'unavailable'; }
    const database = { path: db };
    let operations = [], receipts = [];
    try {
      const info = fs.statSync(db);
      Object.assign(database, { path: fs.realpathSync(db), device: info.dev, inode: info.ino });
      if (/^[a-f0-9-]{36}$/i.test(lastCommit?.operationId || '')) {
        operations = query(`SELECT operation_id, origin_stream_id, origin_generation,
          json_extract(data,'$.state') AS state, json_extract(data,'$.server_finish_failed_code') AS failed_code,
          json_extract(data,'$.upload_committed') AS upload_committed,
          json_extract(data,'$.send_attempted') AS send_attempted,
          json_extract(data,'$.send_committed') AS send_committed,
          json_extract(data,'$.send_proved') AS send_proved FROM voice_operations
          WHERE operation_id='${lastCommit.operationId}'`);
      }
      if (/^[a-z0-9._:-]{1,160}$/i.test(lastCommit?.requestId || '')) {
        receipts = query(`SELECT request_id, to_stream_id, delivery, attempts FROM v2_send_receipts
          WHERE request_id='${lastCommit.requestId}' LIMIT 10`);
      }
    } catch { database.readError = 'unavailable'; }
    const detail = { stage, times: { ...times }, commit: lastCommit, currentSnapshot,
      ...(pageReadError ? { pageReadError } : {}), database, operations, receipts };
    report.diagnostic('web-voice-reload-after-commit', detail);
    return detail;
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
      (${installCommitProbe.toString()})(${readCommitSnapshot.toString()});
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
    await diagnose('precommit-control');
    report.ok('control: a reload before the commit loses the never-uploaded take', control.sends === 0 && !control.asrCalled, control);

    // Journey: the audio is on the daemon and ASR has started when the page reloads.
    fs.rmSync(release, { force: true });
    times.recordStarted = new Date().toISOString();
    lastCommit = null;
    await record(false);
    await untilFile(calls, 'daemon ASR to start');
    times.asrCalled = fs.statSync(calls).mtime.toISOString();
    const { commit } = await diagnose('before-reload');
    report.ok('the real commit ACK precedes reload', commit?.ok === true && commit.serverSend === true
      && commit.origin === streamId && commit.replyOperationId === commit.operationId, commit);
    await reset(); await open();
    times.reopened = new Date().toISOString();
    fs.writeFileSync(release, '');
    times.released = new Date().toISOString();
    const deadline = Date.now() + 20000;
    while (sends() === 0 && Date.now() < deadline) await cdp.sleep(200);
    await cdp.sleep(2000);
    const journey = { sends: sends(), asrCalls: fs.readFileSync(calls, 'utf8').trim().split('\n').length };
    const diagnostic = await diagnose('after-delivery');
    if (journey.sends !== 1) Object.assign(journey, diagnostic);
    report.ok('reload after the audio reaches the daemon: exactly one send carries the transcript', journey.sends === 1, journey);
  } finally {
    try { await diagnose('finally'); } finally { fs.writeFileSync(release, ''); }
  }
}

module.exports = { webVoiceReloadAfterCommit };
