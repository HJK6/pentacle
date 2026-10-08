'use strict';
const { fixtureRequest } = require('./closed_chat_scenario');
const { reportChecks } = require('./web_voice_scenario');
const { reloadDashboardPage } = require('./dashboard_scenario');

// Serialized unchanged into Chrome; unit tests supply geometry only because
// jsdom has no layout. No product DOM or recording state is synthesized here.
function readQuestionVoiceDom(doc, streamId) {
  const visible = node => !!node && !node.closest('[hidden]') && node.getBoundingClientRect().width > 0
    && node.getBoundingClientRect().height > 0 && doc.defaultView.getComputedStyle(node).visibility !== 'hidden';
  const text = node => node?.textContent?.trim() || '';
  const mic = doc.querySelector('[data-question-voice-mic]');
  const bar = doc.querySelector('[data-question-voice-bar]');
  const done = bar?.querySelector('[data-question-voice-done]');
  const pending = doc.querySelector('[data-voice-answers-count]');
  const plain = [...doc.querySelectorAll('button')].find(node => text(node) === 'Send as plain voice note');
  const row = [...doc.querySelectorAll('.session-item[data-stream-id]')].find(node => node.dataset.streamId === streamId);
  const badge = row?.querySelector('.s-question-badge');
  return {
    mic: { visible: visible(mic), label: mic?.getAttribute('aria-label') || '', disabled: !!mic?.disabled },
    bar: { visible: visible(bar), text: text(bar), role: bar?.getAttribute('role') || bar?.querySelector('[role="status"]')?.getAttribute('role') || '',
      timer: text(bar?.querySelector('.voice-recording-timer')), meterBars: [...(bar?.querySelectorAll('.voice-recording-bar') || [])].filter(visible).length,
      doneLabel: done?.getAttribute('aria-label') || text(done) },
    pageState: text(doc.querySelector('[data-question-voice-page-state]')),
    pending: { visible: visible(pending), text: text(pending), inTranscript: !!pending?.closest('.slot-chat-scroll') },
    status: [...doc.querySelectorAll('[data-voice-answers-status]')].map(text).join(' '),
    plain: { visible: visible(plain), disabled: !!plain?.disabled },
    badge: { text: text(badge), label: badge?.getAttribute('aria-label') || '' },
    dots: [...doc.querySelectorAll('.desktop-question-dot')].map(node => node.classList.contains('is-answered')),
    cardIds: [...doc.querySelectorAll('.slot-chat-question-card[data-notification-id]')].map(node => node.dataset.notificationId),
  };
}
// The store also lists local optimistic rows without a wire message_id, and
// composite USER events have no daemon_seq to rely on. After the receipt the
// client-origin row carries the wire message_id too and can briefly sit beside
// the daemon row: that pair is one turn. Two daemon rows stay a duplicate.
const userEvents = events => {
  const rows = (events || []).filter(event => event.kind === 'USER' && typeof event.message_id === 'string' && !!event.message_id);
  const confirmed = new Set(rows.filter(event => !event.client_origin).map(event => event.message_id));
  return rows.filter(event => !(event.client_origin && confirmed.has(event.message_id)));
};
const turns = (events, text) => userEvents(events).filter(event => event.text === text);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
function recordingChecks({ first, middle, last }) {
  return [
    ['voice-answer card has a visible labelled capture mic', !!first.mic?.visible && !!first.mic.label && !first.mic.disabled, first.mic],
    ['first dwell covers one of three questions', first.pageState === 'ANSWER RECORDED' && first.bar.text.includes('1 of 3 answered by voice'), first],
    ['brief second page is not covered', middle.pageState === 'RECORDING YOUR ANSWER…' && middle.bar.text.includes('1 of 3 answered by voice'), middle],
    ['third dwell covers exactly two of three questions', last.pageState === 'ANSWER RECORDED' && last.bar.text.includes('2 of 3 answered by voice'), last],
    ['recording bar has accessible status, advancing timer, meter and Done', !!last.bar.visible && last.bar.role === 'status' && /^\d+:[0-5]\d$/.test(last.bar.timer) && last.bar.timer !== '0:00' && last.bar.meterBars > 0 && !!last.bar.doneLabel, last.bar],
  ];
}
function pendingChecks({ ui, events, text }) {
  return [
    ['pending transcript bubble says ANSWERS 2 QUESTIONS', !!ui.pending.visible && ui.pending.inTranscript && ui.pending.text === 'ANSWERS 2 QUESTIONS', ui.pending],
    ['ASR hold has not sent the pending voice take', turns(events, text).length === 0, { matches: turns(events, text).length }],
  ];
}
function boundChecks(o) {
  const matching = turns(o.events, o.text); const event = matching[0]; const binding = event?.meta?.voice_answers; const status = event?.meta?.voice_answers_status;
  const items = binding?.items || [];
  return [
    ['real daemon USER readback contains exactly one voice-answer turn', matching.length === 1 && event.stream_id === o.surface && !!status && typeof status === 'object', matching],
    ['real host bridge preserves positive voice duration', Number.isFinite(event?.meta?.voice?.duration_s) && event.meta.voice.duration_s > 0, event?.meta?.voice],
    ['real composite binding carries only pages 1 and 3 with eligible ordered segments', binding?.version === 1 && !!binding.recording_id && binding.blob_sha === o.blobSha && Number.isFinite(binding.duration_s) && binding.duration_s > 0
      && same(items.map(item => item.question_id), o.ids) && same(items.map(item => item.notification_id), o.notificationIds)
      && items.every((item, index) => item.producer_stream_id === o.producer && item.surface_stream_id === o.surface
        && Number.isFinite(item.segment?.start_s) && item.segment.start_s >= 0 && Number.isFinite(item.segment?.end_s)
        && item.segment.end_s - item.segment.start_s >= 1.5 && (!index || item.segment.start_s >= items[index - 1].segment.end_s)), binding],
    ['real daemon echo acknowledges binding as bound without stale items', status?.state === 'bound' && Array.isArray(status.stale_keys) && status.stale_keys.length === 0, status],
    ['Done, upload and transcription leave question state, pager and sidebar count unchanged', o.states.length === 3 && o.states.every(state => state === 'open')
      && !!o.before.badge.label && same(o.before.badge, o.after.badge) && o.before.dots.length === 3 && same(o.before.dots, o.after.dots), { states: o.states, before: o.before, after: o.after }],
  ];
}
function refusalChecks(o) {
  return [
    ['tampered binding receives typed voice_answers_invalid from real host bridge', o.failure === 'voice_answers_invalid', { failure: o.failure }],
    ["host refusal visibly offers Couldn't attach questions and explicit plain conversion", o.ui.status.includes("Couldn't attach questions") && o.ui.plain.visible && !o.ui.plain.disabled, o.ui],
    ['refused binding sends nothing before user action', userEvents(o.before).length === userEvents(o.after).length && turns(o.after, o.text).length === 0, { before: userEvents(o.before).map(event => event.text), after: userEvents(o.after).map(event => event.text), ids: userEvents(o.after).map(event => event.message_id) }],
  ];
}
function plainChecks(o) {
  const matching = turns(o.after, o.text); const meta = matching[0]?.meta;
  return [
    ['explicit plain conversion sends exactly one USER turn', userEvents(o.after).length === userEvents(o.before).length + 1 && matching.length === 1, matching],
    ['explicit plain conversion carries voice and no voice_answers', Number.isFinite(meta?.voice?.duration_s) && meta.voice.duration_s > 0 && !Object.hasOwn(meta, 'voice_answers') && !Object.hasOwn(meta, 'voice_answers_status'), meta],
  ];
}
function cleanupChecks(o) {
  return [
    ['discard stops actual MediaStreamTracks', o.tracks.length > 0 && o.tracks.every(state => state === 'ended'), { tracks: o.tracks }],
    ['discard uploads no take for transcription and sends nothing', o.uploadsBefore === o.uploadsAfter && o.transcribedBefore === o.transcribedAfter && o.eventsBefore === o.eventsAfter, o],
  ];
}
function environmentChecks(o) {
  return [
    ['gate records its own loopback page origin and actual served response headers', /^http:\/\/127\.0\.0\.1:\d+$/.test(o.origin) && o.responseUrl?.startsWith(`${o.origin}/`) && o.status === 200 && !!o.headers && Object.keys(o.headers).length > 0, o],
    ['gate records secure-context and microphone permission state', o.secure === true && ['granted', 'prompt', 'denied'].includes(o.permission), { secure: o.secure, permission: o.permission }],
  ];
}

// The cleanup reload invalidates the execution context it then polls. Reuse the
// shared reload that retries only that navigation boundary and waits for the
// fresh document, instead of a bare Page.reload followed by waitFor.
const reloadAfterScenario = ({ session, cdp }) => reloadDashboardPage({ session, cdp });
// Capture is Chrome's synthetic media device through the unmodified product
// getUserMedia/MediaRecorder path. ASR alone is stubbed; sends and readbacks use
// window.cc and the real host + daemon. Never run this against --profile.
async function webVoiceAnswers({ session, report, fixture, runtime, cdp }) {
  if (!fixture || !runtime.fixtureTokens) { report.note('web-voice-answers excluded: requires the isolated composite fixture'); return; }
  const surface = 'local:web-gate-assistant'; const producer = 'local:web-gate-voice-producer';
  // The seeder derives a distinct producer credential from the scratch fixture
  // token; the daemon resolves exactly one stream per token hash.
  const authority = { ...runtime, fixtureTokens: { ...runtime.fixtureTokens, [producer]: require('node:crypto').createHash('sha256').update(`voice-producer:${runtime.fixtureTokens[fixture.streamId]}`).digest('hex') } };
  const json = JSON.stringify;
  const ui = () => session.eval(`(${readQuestionVoiceDom.toString()})(document, ${json(surface)})`);
  const events = () => session.eval(`(async () => {
    const reply = await window.cc.requestStreamEvents({ streamId: ${json(surface)}, limit: 100 });
    if (!reply.ok) throw new Error(reply.error || 'daemon readback failed');
    return window.PentacleChatStore.getState().events.filter(event => event.stream_id === ${json(surface)} && event.kind === 'USER');
  })()`);
  // Confirm a turn from daemon events (wire message_id) after a history
  // readback; the bound check additionally requires the daemon-authored status.
  const waitTurn = async text => {
    for (let attempt = 0; attempt < 60; attempt++) { if (turns(await events(), text).length) return; await cdp.sleep(250); }
    throw new Error(`daemon readback never returned USER turn: ${text}`);
  };
  const controller = 'window.__voiceAnswersController';
  const portal = '#desktop-question-portal-local-web-gate-assistant';
  const mic = '[data-question-voice-mic]'; const done = '[data-question-voice-done]';
  const clickGesture = selector => session.send('Runtime.evaluate', { expression: `document.querySelector(${json(selector)}).click()`, userGesture: true });
  const openDeck = async () => {
    await session.waitFor(`window.focusStreamId(${json(surface)}) === true`);
    await session.waitFor(`[...document.querySelectorAll('.grid-cell')].some(cell => cell.querySelector('.cell-label')?.textContent.trim() === 'Assistant Fixture')`);
    // A freshly attached slot hydrates its durable questions asynchronously;
    // wait for the opener (or an already open deck) instead of clicking once.
    await session.waitFor(`(() => { const cell = [...document.querySelectorAll('.grid-cell')].find(cell => cell.querySelector('.cell-label')?.textContent.trim() === 'Assistant Fixture'); if (!cell) return false; window.__voiceAnswersCell = cell.id; return !!document.querySelector(${json(portal)}) || !!cell.querySelector('.slot-chat-question-open'); })()`);
    await session.eval(`(() => { if (!document.querySelector(${json(portal)})) document.getElementById(window.__voiceAnswersCell).querySelector('.slot-chat-question-open').click(); })()`);
    await session.waitFor(`!!document.querySelector(${json(portal + ' ' + mic)}) && document.querySelectorAll(${json(portal + ' .desktop-question-dot')}).length === 3`);
    await session.eval(`window.__voiceAnswersController = [...document.querySelectorAll(${json(portal + ' *')})].find(node => node.__questionVoiceController)?.__questionVoiceController`);
  };
  const navigate = async index => {
    await session.click(`.desktop-question-dot[aria-label="Question ${index}"]`);
    await session.waitFor(`document.querySelector('.desktop-question-dot[aria-label="Question ${index}"]')?.classList.contains('is-active')`);
  };
  let installed = false; const errors = [];
  try {
    await session.waitFor("document.readyState === 'complete' && typeof window.focusStreamId === 'function'");
    await session.waitFor('window.cc.getChatStreamState().then(s => s.connected === true)');
    const ids = [], notificationIds = [];
    for (let index = 1; index <= 3; index++) {
      const questionId = `web-voice-answer-${index}`;
      const asked = await fixtureRequest(authority, producer, { type: 'prompt.ask', envelope: { schema_version: 1, question_id: questionId,
        title: `Synthetic question ${index}`, body: `Describe synthetic answer ${index}`, dedup_key: questionId, producer_stream_id: producer,
        response_mode: 'free_text', allow_custom: true, options: [] }, actions: [] });
      report.ok(`isolated bound producer asks durable question ${index}`, asked.ok === true, asked.error || asked.question);
      ids.push(questionId); notificationIds.push(asked.question.notification_id);
    }
    await openDeck();
    await session.eval(`(() => {
      const originalTranscribe = window.cc.chatTranscribeBlob;
      const originalUpload = window.cc.chatUploadBlob;
      const originalStop = MediaStreamTrack.prototype.stop;
      const gate = window.__voiceAnswersGate = { transcribed: [], uploads: 0, stoppedTracks: [], hold: true, take: 1 };
      // Observe calls while preserving the browser's actual stop implementation.
      MediaStreamTrack.prototype.stop = function(...args) { if (!gate.stoppedTracks.includes(this)) gate.stoppedTracks.push(this); return originalStop.apply(this, args); };
      window.cc.chatUploadBlob = (...args) => { gate.uploads++; return originalUpload(...args); };
      window.cc.chatTranscribeBlob = async payload => {
        gate.transcribed.push(payload);
        if (gate.hold) await new Promise(resolve => { gate.release = resolve; });
        return { ok: true, text: 'Synthetic voice answers take ' + gate.take, duration_s: 4 };
      };
      gate.restore = () => { gate.release?.(); window.cc.chatTranscribeBlob = originalTranscribe; window.cc.chatUploadBlob = originalUpload; MediaStreamTrack.prototype.stop = originalStop; };
    })()`); installed = true;
    await navigate(1); const before = await ui();
    await clickGesture(mic);
    await session.waitFor(`(${controller})?.snapshot().phase === 'recording'`);
    await cdp.sleep(1750); const first = await ui();
    await navigate(2); await cdp.sleep(100); const middle = await ui();
    await navigate(3); await cdp.sleep(1750); const last = await ui();
    reportChecks(report, recordingChecks({ first, middle, last }));
    if (report.dir) await session.screenshot(require('node:path').join(report.dir, 'voice-answers-synthetic-recording.png'));
    await clickGesture(done);
    await session.waitFor('window.__voiceAnswersGate.transcribed.length === 1');
    reportChecks(report, pendingChecks({ ui: await ui(), events: await events(), text: 'Synthetic voice answers take 1' }));
    await session.eval('window.__voiceAnswersGate.hold = false; window.__voiceAnswersGate.release()');
    await waitTurn('Synthetic voice answers take 1');
    const states = [];
    for (const questionId of ids) states.push((await fixtureRequest(authority, producer, { type: 'prompt.status', question_id: questionId })).question?.state);
    const boundEvents = await events();
    // The overlay may close when Done finishes; reopen to observe all pager dots.
    await openDeck();
    reportChecks(report, boundChecks({ events: boundEvents, text: 'Synthetic voice answers take 1', ids: [ids[0], ids[2]], notificationIds: [notificationIds[0], notificationIds[2]],
      producer, surface, blobSha: await session.eval('window.__voiceAnswersGate.transcribed[0].blob_sha'), before, after: await ui(), states }));

    await session.eval(`(${controller}).setBindingTransformForTest(binding => ({ ...binding, items: binding.items.map((item, index) => index ? item : { ...item, prompt: 'x'.repeat(2001) }) })); window.__voiceAnswersGate.take = 2`);
    await navigate(1); await clickGesture(mic); await session.waitFor(`(${controller})?.snapshot().phase === 'recording'`); await cdp.sleep(1750); await clickGesture(done);
    await session.waitFor(`Object.values(window.PentacleChatStore.getState().optimisticSends || {}).some(row => row.stream_id === ${json(surface)} && row.failure_reason === 'voice_answers_invalid')`);
    await cdp.sleep(350);
    const refused = await events();
    const failure = await session.eval(`Object.values(window.PentacleChatStore.getState().optimisticSends || {}).find(row => row.stream_id === ${json(surface)} && row.failure_reason === 'voice_answers_invalid')?.failure_reason`);
    reportChecks(report, refusalChecks({ before: boundEvents, after: refused, text: 'Synthetic voice answers take 2', failure, ui: await ui() }));
    if (report.dir) await session.screenshot(require('node:path').join(report.dir, 'voice-answers-synthetic-refused.png'));
    await session.eval(`[...document.querySelectorAll('button')].find(button => button.textContent.trim() === 'Send as plain voice note').click()`);
    await waitTurn('Synthetic voice answers take 2');
    await cdp.sleep(350);
    reportChecks(report, plainChecks({ before: refused, after: await events(), text: 'Synthetic voice answers take 2' }));
    await openDeck(); await session.eval(`(${controller}).setBindingTransformForTest(binding => binding)`);

    for (const mode of ['cancel', 'leave-slot']) {
      const uploadsBefore = await session.eval('window.__voiceAnswersGate.uploads');
      const transcribedBefore = await session.eval('window.__voiceAnswersGate.transcribed.length');
      const eventsBefore = (await events()).length;
      await session.eval('window.__voiceAnswersGate.stoppedTracks = []');
      await clickGesture(mic); await session.waitFor(`(${controller})?.snapshot().phase === 'recording'`);
      if (mode === 'cancel') await session.click('[data-question-voice-bar] .voice-recording-discard');
      else await session.eval(`document.getElementById(window.__voiceAnswersCell).querySelector('.cell-close').click()`);
      await session.waitFor(`!!document.querySelector('[data-question-voice-confirm]') && !document.querySelector('[data-question-voice-confirm]').hidden`);
      await session.click('[data-question-voice-discard]');
      await session.waitFor('window.__voiceAnswersGate.stoppedTracks.length > 0 && window.__voiceAnswersGate.stoppedTracks.every(track => track.readyState === "ended")');
      const observation = { uploadsBefore, uploadsAfter: await session.eval('window.__voiceAnswersGate.uploads'), tracks: await session.eval('window.__voiceAnswersGate.stoppedTracks.map(track => track.readyState)'), transcribedBefore,
        transcribedAfter: await session.eval('window.__voiceAnswersGate.transcribed.length'), eventsBefore, eventsAfter: (await events()).length };
      reportChecks(report, cleanupChecks(observation).map(([name, passed, detail]) => [`${mode}: ${name}`, passed, detail]));
      await openDeck();
    }
    const environment = await session.eval(`(async () => {
      const response = await fetch(location.href, { cache: 'no-store' });
      const permission = await navigator.permissions.query({ name: 'microphone' });
      return { origin: location.origin, responseUrl: response.url, status: response.status, headers: Object.fromEntries(response.headers), permission: permission.state, secure: isSecureContext };
    })()`);
    reportChecks(report, environmentChecks(environment));
    report.note('web-voice-answers collection: test/voice_answers_scenario.test.js; daemon USER event with voice_answers_status bound; loopback origin, response headers and permission are retained in verdict steps');
  } catch (error) { errors.push(error); }
  finally {
    if (installed) {
      try { await session.eval(`(async () => { await (${controller})?.cancel(); window.__voiceAnswersGate.restore(); })()`); }
      catch (error) { errors.push(error); }
    }
    // A reload removes only this scenario's synthetic UI state and any failed
    // optimistic row; all durable fixture questions/events remain daemon-owned.
    try { await reloadAfterScenario({ session, cdp }); }
    catch (error) { errors.push(error); }
  }
  if (errors.length) throw new AggregateError(errors, errors.map(error => error.message).join('; '));
}
module.exports = { webVoiceAnswers, reloadAfterScenario, readQuestionVoiceDom, recordingChecks, pendingChecks, boundChecks, refusalChecks, plainChecks, cleanupChecks, environmentChecks };
