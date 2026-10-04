'use strict';
// Headless render of the mic panel's silent toggle and waiting-for-answer
// indicator against status fixtures, using the SHIPPED renderer/mic-state.js
// view model (no logic duplicated). Proves the waiting state is shown and clears
// when the fixture clears, and the silent toggle reflects state. Runs inside the
// already-loaded web client page over CDP; it appends an isolated probe node and
// removes it afterwards so neighbouring scenarios are unaffected.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const MIC_STATE_SRC = fs.readFileSync(
  path.join(__dirname, '..', '..', '..', 'renderer', 'mic-state.js'), 'utf8');

async function micPanelAnswerWindow({ session, report }) {
  await session.eval(`(() => {
    const module = { exports: {} };
    (function (module, exports) {\n${MIC_STATE_SRC}\n})(module, module.exports);
    window.__micPanel = module.exports;
    let host = document.getElementById('mic-panel-render-probe');
    if (!host) { host = document.createElement('div'); host.id = 'mic-panel-render-probe'; document.body.appendChild(host); }
    host.innerHTML = '<button id="probe-silent"></button><div id="probe-answer"></div>';
    window.__paintMicPanel = (status) => {
      const v = window.__micPanel.computeMicPanelView({ status });
      const s = document.getElementById('probe-silent');
      s.textContent = v.silent.label;
      s.dataset.on = String(v.silent.on);
      s.dataset.next = String(v.silent.nextOn);
      const a = document.getElementById('probe-answer');
      a.dataset.waiting = String(v.answerWindow.waiting);
      a.dataset.ready = String(v.answerWindow.ready);
      a.textContent = v.answerWindow.text;
    };
    return typeof window.__micPanel.computeMicPanelView === 'function';
  })()`).then(ok => assert.equal(ok, true, 'mic-state view model loaded in the page'));

  const read = () => session.eval(`(() => ({
    silent: document.getElementById('probe-silent').dataset.on,
    silentNext: document.getElementById('probe-silent').dataset.next,
    silentLabel: document.getElementById('probe-silent').textContent,
    waiting: document.getElementById('probe-answer').dataset.waiting,
    ready: document.getElementById('probe-answer').dataset.ready,
    answerText: document.getElementById('probe-answer').textContent,
  }))()`);

  // Answer window open and ready → the waiting-for-answer state is shown.
  await session.eval(`window.__paintMicPanel({ speaker: { answer_window: { waiting: true, ready: true, conversation_id: 'C', line_id: 'L', expires_in: 18 } } })`);
  let r = await read();
  assert.equal(r.waiting, 'true', 'answer window waiting should render');
  assert.equal(r.ready, 'true');
  assert.match(r.answerText, /say over/i);

  // Fixture clears → the indicator clears.
  await session.eval(`window.__paintMicPanel({ speaker: { answer_window: { waiting: false } } })`);
  r = await read();
  assert.equal(r.waiting, 'false', 'answer window should clear when the fixture clears');
  assert.equal(r.answerText, '');

  // Silent toggle reflects state and the next requested value.
  await session.eval(`window.__paintMicPanel({ speaker: { silent: false } })`);
  r = await read();
  assert.equal(r.silent, 'false');
  assert.equal(r.silentNext, 'true');
  assert.equal(r.silentLabel, 'Silent');
  await session.eval(`window.__paintMicPanel({ speaker: { silent: true, silent_source: 'web' } })`);
  r = await read();
  assert.equal(r.silent, 'true');
  assert.equal(r.silentNext, 'false');
  assert.equal(r.silentLabel, 'Silent: On');

  await session.eval(`document.getElementById('mic-panel-render-probe')?.remove(); delete window.__paintMicPanel; delete window.__micPanel; true`);
  report.note('mic-panel render: waiting-for-answer shows and clears; silent toggle reflects state');
}

module.exports = { micPanelAnswerWindow };
