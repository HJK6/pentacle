'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const { computeBusyBannerState, computeMicPanelView } = require('../renderer/mic-state');
const { createWakeDelivery } = require('../renderer/wake_delivery');
const source = fs.readFileSync(require.resolve('../renderer/app.js'), 'utf8');
const update = source.slice(source.indexOf('function updateMicUI(data) {'), source.indexOf('async function fetchMicStatus()'));
const callback = source.match(/onStatus: (\([^)]*\) => \{[\s\S]*?\n      \}),/);
assert.ok(callback, 'actual app wake callback is available');
const base = { mode: 'on', on_listener_state: 'LISTENING', wake: { enabled: true, generation: 'one', pending_count: 0 }, audio: { health_state: 'ok' } };
function panel(t) {
  const dom = new JSDOM(['mic-status-dot', 'mic-info', 'mic-btn-toggle', 'mic-btn-copy', 'mic-btn-meeting', 'mic-btn-silent', 'mic-transcript-preview'].map(id => `<div id="${id}"></div>`).join(''));
  t.after(() => dom.window.close());
  const ctx = { document: dom.window.document, window: {}, CONFIG: { mic: { alwaysOnEnabled: true }, wakeWord: 'Hey Zephyr' },
    micState: {}, voiceState: {}, wakeDelivery: { message: () => 'Say Hey Zephyr to speak.' },
    alwaysOnVisible: () => true, shouldRenderAlwaysOnUi: () => true, resolveLocalMicCaller: () => 'local',
    computeBusyBannerState, computeMicPanelView, stopRemoteClipboardPoller() {}, stopRemoteSlotVoiceSession() {},
    formatMicCaller: String, esc: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;') };
  vm.createContext(ctx); vm.runInContext(update + `\nvar wakeStatus = ${callback[1]};`, ctx);
  return { ctx, text: () => dom.window.document.getElementById('mic-info').textContent };
}
for (const [name, patch, expected] of [
  ['structured error', { last_error: { type: 'audio_health', health_message: 'Input stream could not open.' } }, 'Input stream could not open.'],
  ['missing input before last_error', { last_error: null, audio: { health_state: 'preferred_missing', health_message: 'Preferred microphone is not present.' } }, 'Preferred microphone is not present.'],
  ['unknown error object', { last_error: { unknown: true } }, 'Microphone error. Check the microphone connection and try again.'],
  ['legacy error string', { last_error: 'Device disconnected.' }, 'Device disconnected.'],
]) test(`${name} stays readable through polling and wake completion, then recovers`, t => {
  const p = panel(t); const status = { ...base, ...patch };
  for (let i = 0; i < 3; i++) { p.ctx.updateMicUI(status); assert.equal(p.text(), expected); p.ctx.wakeStatus('Say Hey Zephyr to speak.'); assert.equal(p.text(), expected); }
  p.ctx.updateMicUI(base); p.ctx.wakeStatus('Say Hey Zephyr to speak.'); assert.match(p.text(), /Say Hey/);
});
test('a delayed real wake tick cannot replace a newer missing-device status', async t => {
  const p = panel(t); let finish;
  p.ctx.wakeDelivery = createWakeDelivery({ config: { features: { mic: true }, mic: { wakeTargetStreamId: 'local:assistant', wakeTargetHost: 'local' }, chatStream: {} },
    getState: () => new Promise(resolve => { finish = resolve; }), api: async () => { throw Error('idle must not claim audio'); },
    sendTurn: () => { throw Error('idle must not send'); }, onStatus: p.ctx.wakeStatus });
  p.ctx.updateMicUI(base);
  const pending = p.ctx.wakeDelivery.tick(base);
  p.ctx.updateMicUI({ ...base, audio: { health_state: 'preferred_missing', health_message: 'Reconnect the microphone.' } });
  finish({ connected: true, sessions: [] }); await pending;
  assert.equal(p.text(), 'Reconnect the microphone.');
});
test('an old wake callback cannot erase offline status', t => {
  const p = panel(t); p.ctx.updateMicUI(base); p.ctx.updateMicUI(null); p.ctx.wakeStatus('Say Hey Zephyr to speak.');
  assert.equal(p.text(), 'Mic server offline');
});
test('health text is rendered as text, never markup', t => {
  const p = panel(t); p.ctx.updateMicUI({ ...base, last_error: { health_message: '<b>Input disconnected</b>' } });
  assert.equal(p.text(), '<b>Input disconnected</b>'); assert.equal(p.ctx.document.querySelector('#mic-info b'), null);
});
