'use strict';
const { fixtureRequest } = require('./closed_chat_scenario');
const { reportChecks } = require('./web_voice_scenario');
const { reloadDashboardPage } = require('./dashboard_scenario');

// The MIC panel Source selector in the shipped web client, under a
// scenario-scoped, service-less mic profile (features.mic on, an unused
// loopback micServerUrl, no start command). Only the media devices are fake:
// enumerateDevices lists the inputs this scenario chooses and getUserMedia
// records the audio constraint it receives. Capture, the composer and the
// question voice bar are the shipped code paths.
const STORAGE_KEY = 'pentacle.micSource';
const INPUTS_KEY = '__micSourceGateInputs';
const INPUT_1 = { deviceId: 'gate-input-1', label: 'Gate Input 1' };
const INPUT_2 = { deviceId: 'gate-input-2', label: 'Gate Input 2' };
// web_gate's mic profile names the web host; the room Source shows that name.
const ROOM_LABEL = 'Gate Room';

// Installed before every document load so a reload keeps the same fake devices.
function installFakeMedia(inputsKey, defaults) {
  const gate = window.__micSourceGate = { constraints: [], media: [] };
  gate.inputs = () => {
    try { const stored = JSON.parse(sessionStorage.getItem(inputsKey)); if (Array.isArray(stored)) return stored; } catch (_) { /* defaults */ }
    return defaults;
  };
  navigator.mediaDevices.enumerateDevices = async () => gate.inputs().map(input => ({ kind: 'audioinput', groupId: 'gate', ...input }));
  navigator.mediaDevices.getUserMedia = async constraints => {
    gate.constraints.push(JSON.parse(JSON.stringify(constraints)));
    const context = new AudioContext(); await context.resume();
    const oscillator = context.createOscillator(); const destination = context.createMediaStreamDestination();
    oscillator.connect(destination); oscillator.start();
    gate.media.push({ context, oscillator, stream: destination.stream });
    return destination.stream;
  };
}

// Serialized into the page: what the served panel shows.
function readMicSource(doc, storageKey) {
  const visible = node => !!node && !node.closest('[hidden]') && node.getBoundingClientRect().height > 0;
  const options = [...doc.querySelectorAll('#mic-source [data-mic-source]')];
  const select = doc.getElementById('mic-device-select');
  const note = doc.getElementById('mic-source-note');
  return {
    sourceVisible: visible(doc.getElementById('mic-source')),
    options: options.filter(visible).map(button => button.dataset.micSource),
    roomLabel: doc.querySelector('#mic-source [data-mic-source="room"]')?.textContent || '',
    active: options.find(button => button.getAttribute('aria-checked') === 'true')?.dataset.micSource || null,
    localVisible: visible(doc.getElementById('mic-local-section')),
    picker: [...(select?.options || [])].map(option => ({ value: option.value, label: option.textContent })),
    selected: select?.value ?? null,
    activeLabel: doc.getElementById('mic-local-active')?.textContent || '',
    noteVisible: visible(note),
    note: note?.textContent || '',
    stored: localStorage.getItem(storageKey),
    roomControls: ['mic-btn-copy', 'mic-btn-meeting', 'mic-btn-silent'].every(id => !!doc.getElementById(id)),
  };
}

const sameJson = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const exactInput2 = constraint => sameJson(constraint, { audio: { deviceId: { exact: INPUT_2.deviceId } } });

async function micSourceSelector({ session, report, fixture, runtime, cdp, configureMic }) {
  if (!fixture || !runtime.fixtureTokens || !configureMic) { report.note('mic-source-selector excluded: requires the hermetic fixture'); return; }
  const json = JSON.stringify;
  const surface = 'local:web-gate-assistant'; const producer = 'local:web-gate-voice-producer';
  const questionId = 'mic-source-selector-question';
  const authority = { ...runtime, fixtureTokens: { ...runtime.fixtureTokens, [producer]: require('node:crypto').createHash('sha256').update(`voice-producer:${runtime.fixtureTokens[fixture.streamId]}`).digest('hex') } };
  const read = () => session.eval(`(${readMicSource.toString()})(document, ${json(STORAGE_KEY)})`);
  const waitPanel = predicate => session.waitFor(`(() => { const v = (${readMicSource.toString()})(document, ${json(STORAGE_KEY)}); return ${predicate}; })()`);
  const reload = () => reloadDashboardPage({ session, cdp });
  const constraints = () => session.eval('window.__micSourceGate.constraints');
  const gesture = expression => session.send('Runtime.evaluate', { expression, userGesture: true });
  const errors = []; let scriptId = null; let asked = false;
  try {
    await configureMic(true);
    scriptId = (await session.send('Page.addScriptToEvaluateOnNewDocument', { source: `(${installFakeMedia.toString()})(${json(INPUTS_KEY)}, ${json([INPUT_1, INPUT_2])})` })).identifier;
    await session.eval(`localStorage.removeItem(${json(STORAGE_KEY)}); sessionStorage.removeItem(${json(INPUTS_KEY)})`);
    await reload();

    // (a) Two inputs, nothing stored: Source renders with This device available and the room Source active.
    await waitPanel('v.sourceVisible && v.options.length === 2');
    const fresh = await read();
    reportChecks(report, [
      ['(a) fresh browser: Source shows the host-named room Source and This device, room active, room controls present',
        fresh.sourceVisible && sameJson(fresh.options, ['room', 'device']) && fresh.active === 'room' && fresh.roomLabel === ROOM_LABEL && !fresh.localVisible && fresh.stored === null && fresh.roomControls, fresh],
    ]);

    // (b) Select This device, then input 2.
    await session.click('#mic-source [data-mic-source="device"]');
    await waitPanel("v.active === 'device' && v.localVisible");
    await session.eval(`(() => { const select = document.getElementById('mic-device-select'); select.value = ${json(INPUT_2.deviceId)}; select.dispatchEvent(new Event('change', { bubbles: true })); })()`);
    await waitPanel(`v.selected === ${json(INPUT_2.deviceId)}`);
    const picked = await read();
    reportChecks(report, [
      ['(b) picker lists System default then both inputs with input 2 active',
        sameJson(picked.picker, [{ value: '', label: 'System default' }, { value: INPUT_1.deviceId, label: INPUT_1.label }, { value: INPUT_2.deviceId, label: INPUT_2.label }])
          && picked.active === 'device' && picked.selected === INPUT_2.deviceId && picked.activeLabel === `Active: ${INPUT_2.label}`
          && sameJson(JSON.parse(picked.stored || 'null'), { source: 'device', deviceId: INPUT_2.deviceId }), picked],
    ]);
    if (report.dir) await session.screenshot(require('node:path').join(report.dir, 'mic-source-this-device.png'));

    // (c) A composer voice take captures from input 2. Discard it: delivery is web-chat-voice's job.
    await session.eval(`window.focusStreamId(${json(fixture.streamId)})`);
    await session.waitFor("!!document.querySelector('#header-0 [data-mode=\"chat\"]')");
    await session.click('#header-0 [data-mode="chat"]');
    const mic = '#cell-0 .slot-chat-compose-mic';
    await session.waitFor(`!!document.querySelector(${json(mic)}) && !document.querySelector(${json(mic)}).disabled`);
    const composerBefore = (await constraints()).length;
    await gesture(`document.querySelector(${json(mic)}).click()`);
    await session.waitFor(`document.querySelector(${json(mic)}).classList.contains('is-recording')`);
    const composer = (await constraints()).slice(composerBefore);
    await session.click('#cell-0 .voice-recording-discard');
    await session.waitFor(`!document.querySelector(${json(mic)}).classList.contains('is-recording')`);
    reportChecks(report, [['(c) composer voice take getUserMedia constraint carries deviceId exact input 2', composer.length === 1 && exactInput2(composer[0]), composer]]);

    // (d) The question voice bar captures from the same input.
    const question = await fixtureRequest(authority, producer, { type: 'prompt.ask', envelope: { schema_version: 1, question_id: questionId,
      title: 'Mic source question', body: 'Synthetic question for the mic source gate', dedup_key: questionId, producer_stream_id: producer,
      response_mode: 'free_text', allow_custom: true, options: [] }, actions: [] });
    asked = question.ok === true;
    report.ok('(d) isolated producer asks one durable question', asked, question.error || question.question);
    const portal = '#desktop-question-portal-local-web-gate-assistant';
    await session.waitFor(`window.focusStreamId(${json(surface)}) === true`);
    await session.waitFor(`(() => { const cell = [...document.querySelectorAll('.grid-cell')].find(cell => cell.querySelector('.cell-label')?.textContent.trim() === 'Assistant Fixture'); if (!cell) return false; window.__micSourceCell = cell.id; return !!document.querySelector(${json(portal)}) || !!cell.querySelector('.slot-chat-question-open'); })()`);
    await session.eval(`(() => { if (!document.querySelector(${json(portal)})) document.getElementById(window.__micSourceCell).querySelector('.slot-chat-question-open').click(); })()`);
    await session.waitFor(`(() => { const button = document.querySelector(${json(portal + ' [data-question-voice-mic]')}); return !!button && !button.hidden; })()`);
    await session.eval(`window.__micSourceController = [...document.querySelectorAll(${json(portal + ' *')})].find(node => node.__questionVoiceController)?.__questionVoiceController`);
    const deckBefore = (await constraints()).length;
    await gesture(`document.querySelector(${json(portal + ' [data-question-voice-mic]')}).click()`);
    await session.waitFor("window.__micSourceController?.snapshot().phase === 'recording'");
    const deck = (await constraints()).slice(deckBefore);
    await session.eval('window.__micSourceController.cancel()');
    await session.waitFor("window.__micSourceController.snapshot().phase === 'idle'");
    reportChecks(report, [['(d) question voice bar getUserMedia constraint carries the same deviceId', deck.length === 1 && exactInput2(deck[0]), deck]]);

    // (e) Reload keeps This device and input 2.
    await reload();
    await waitPanel("v.sourceVisible && v.options.length === 2");
    const reloaded = await read();
    reportChecks(report, [['(e) after reload This device and input 2 are still active',
      reloaded.active === 'device' && reloaded.localVisible && reloaded.selected === INPUT_2.deviceId && reloaded.activeLabel === `Active: ${INPUT_2.label}`, reloaded]]);

    // (f) Input 2 disappears: Source falls back to the room Source with a note; the stored choice is cleared.
    await session.eval(`sessionStorage.setItem(${json(INPUTS_KEY)}, ${json(JSON.stringify([INPUT_1]))}); navigator.mediaDevices.dispatchEvent(new Event('devicechange'))`);
    await waitPanel("v.active === 'room'");
    const lost = await read();
    reportChecks(report, [['(f) device loss: Source shows the room Source, inline note names it, stored choice cleared',
      lost.active === 'room' && !lost.localVisible && lost.noteVisible && lost.note === `The selected microphone is no longer available. Switched to ${ROOM_LABEL}.` && lost.stored === null && lost.roomControls, lost]]);
    if (report.dir) await session.screenshot(require('node:path').join(report.dir, 'mic-source-device-lost.png'));

    // (g) No inputs: only the room Source renders.
    await session.eval(`sessionStorage.setItem(${json(INPUTS_KEY)}, '[]')`);
    await reload();
    await waitPanel('v.sourceVisible');
    await cdp.sleep(300);
    const none = await read();
    reportChecks(report, [['(g) zero inputs: only the room Source renders', none.sourceVisible && sameJson(none.options, ['room']) && none.active === 'room' && !none.localVisible, none]]);
  } catch (error) {
    errors.push(error);
    try { report.note('mic source UI at failure: ' + JSON.stringify(await read())); } catch (diagnostic) { errors.push(diagnostic); }
  } finally {
    if (asked) {
      try { await fixtureRequest(authority, producer, { type: 'prompt.cancel', question_id: questionId }); } catch (error) { errors.push(error); }
    }
    try { if (scriptId) await session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier: scriptId }); } catch (error) { errors.push(error); }
    try { await session.eval(`localStorage.removeItem(${json(STORAGE_KEY)}); sessionStorage.removeItem(${json(INPUTS_KEY)})`); } catch (error) { errors.push(error); }
    // Restore the default profile (features.mic off) for every later scenario.
    try { await configureMic(false); } catch (error) { errors.push(error); }
  }
  if (errors.length) throw new AggregateError(errors, errors.map(error => error.message).join('; '));
}

module.exports = { micSourceSelector, readMicSource, installFakeMedia };
