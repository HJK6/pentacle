// Renders the SHIPPED updateMicUI against /status fixtures using the real served
// mic-panel control ids (including #mic-btn-silent), asserting the waiting-for-answer
// state is shown and clears when the fixture clears, and the silent toggle reflects
// state. This exercises the actual served controls, not synthetic proxies.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const { computeMicPanelView } = require('../renderer/mic-state');
const source = fs.readFileSync(require.resolve('../renderer/app.js'), 'utf8');
const update = source.slice(source.indexOf('function updateMicUI(data) {'), source.indexOf('async function fetchMicStatus()'));

// The real mic-panel control ids as served in renderer/index.html.
const IDS = ['mic-status-dot', 'mic-info', 'mic-btn-toggle', 'mic-btn-copy', 'mic-btn-meeting', 'mic-btn-silent', 'mic-transcript-preview'];

function render(status) {
  const dom = new JSDOM(IDS.map(id => `<button id="${id}"></button>`).join(''));
  const ctx = {
    document: dom.window.document, window: {}, CONFIG: { mic: { alwaysOnEnabled: true }, wakeWord: 'Hey Bart' },
    micState: {}, voiceState: {}, wakeDelivery: null,
    alwaysOnVisible: () => true, shouldRenderAlwaysOnUi: () => true, resolveLocalMicCaller: () => 'local',
    computeBusyBannerState: () => ({ visible: false }), computeMicPanelView, stopRemoteClipboardPoller: () => {}, esc: String,
  };
  vm.createContext(ctx);
  vm.runInContext(update, ctx);
  ctx.updateMicUI(status);
  const doc = dom.window.document;
  return {
    dot: doc.getElementById('mic-status-dot').className,
    silentText: doc.getElementById('mic-btn-silent').textContent,
    silentClass: doc.getElementById('mic-btn-silent').className,
    silentOn: doc.getElementById('mic-btn-silent').dataset.silentOn,
  };
}

const base = { mode: 'on', on_listener_state: 'LISTENING' };

test('waiting-for-answer shows on the served panel and clears when the fixture clears', () => {
  const waiting = render({ ...base, speaker: { answer_window: { waiting: true, ready: true, conversation_id: 'C', line_id: 'L', expires_in: 18 } } });
  assert.match(waiting.dot, /active-capturing/);
  const cleared = render({ ...base, speaker: { answer_window: { waiting: false } } });
  assert.doesNotMatch(cleared.dot, /active-capturing/);
});

test('the served silent toggle reflects silent state', () => {
  const off = render({ ...base, speaker: { silent: false } });
  assert.equal(off.silentText, 'Silent');
  assert.equal(off.silentOn, 'false');
  assert.doesNotMatch(off.silentClass, /active/);
  const on = render({ ...base, speaker: { silent: true, silent_source: 'web' } });
  assert.equal(on.silentText, 'Silent: On');
  assert.equal(on.silentOn, 'true');
  assert.match(on.silentClass, /active/);
});
