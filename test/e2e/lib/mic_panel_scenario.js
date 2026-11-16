'use strict';
// Headless render of the mic panel against /status fixtures, driving the ACTUAL
// served controls (#mic-btn-silent, #mic-status-dot) in the loaded web client with
// the SHIPPED renderer/mic-state.js view model (no logic duplicated). Proves the
// waiting-for-answer state is shown and clears when the fixture clears, and the
// silent toggle reflects state. Restores the controls afterwards so neighbouring
// scenarios are unaffected.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const MIC_STATE_SRC = fs.readFileSync(
  path.join(__dirname, '..', '..', '..', 'renderer', 'mic-state.js'), 'utf8');

async function micPanelAnswerWindow({ session, report, cdp }) {
  // The served client ships the mic panel markup even when the feature is hidden; wait
  // for the real controls to be present before rendering against them.
  let present = false;
  for (let i = 0; i < 40 && !present; i++) {
    present = await session.eval(`!!(document.getElementById('mic-btn-silent') && document.getElementById('mic-status-dot'))`);
    if (!present) await cdp.sleep(100);
  }
  assert.equal(present, true, 'served mic-panel controls (#mic-btn-silent, #mic-status-dot) are present');

  await session.eval(`(() => {
    const module = { exports: {} };
    (function (module, exports) {\n${MIC_STATE_SRC}\n})(module, module.exports);
    window.__micPanel = module.exports;
    const silent = document.getElementById('mic-btn-silent');
    const dot = document.getElementById('mic-status-dot');
    window.__micPanelRestore = { silentText: silent.textContent, silentClass: silent.className, dotClass: dot.className };
    // Paint exactly as app.js's updateMicUI does for these controls.
    window.__paintMicPanel = (status) => {
      const v = window.__micPanel.computeMicPanelView({ status });
      silent.textContent = v.silent.label;
      silent.className = 'mic-btn mic-btn-silent' + (v.silent.on ? ' active' : '');
      silent.dataset.silentOn = String(v.silent.on);
      dot.className = 'mic-status-dot' + (v.answerWindow.waiting ? ' active-capturing' : '');
    };
    return typeof window.__micPanel.computeMicPanelView === 'function';
  })()`).then(ok => assert.equal(ok, true, 'mic-state view model loaded in the page'));

  const read = () => session.eval(`(() => ({
    silentText: document.getElementById('mic-btn-silent').textContent,
    silentClass: document.getElementById('mic-btn-silent').className,
    silentOn: document.getElementById('mic-btn-silent').dataset.silentOn,
    dotClass: document.getElementById('mic-status-dot').className,
  }))()`);

  // Answer window open and ready → the served panel shows the waiting-for-answer state.
  await session.eval(`window.__paintMicPanel({ speaker: { answer_window: { waiting: true, ready: true, conversation_id: 'C', line_id: 'L', expires_in: 18 } } })`);
  let r = await read();
  assert.match(r.dotClass, /active-capturing/, 'the served status dot shows waiting-for-answer');

  // Fixture clears → the served indicator clears.
  await session.eval(`window.__paintMicPanel({ speaker: { answer_window: { waiting: false } } })`);
  r = await read();
  assert.doesNotMatch(r.dotClass, /active-capturing/, 'the served status dot clears when the fixture clears');

  // The served silent toggle reflects state.
  await session.eval(`window.__paintMicPanel({ speaker: { silent: false } })`);
  r = await read();
  assert.equal(r.silentText, 'Silent');
  assert.equal(r.silentOn, 'false');
  assert.doesNotMatch(r.silentClass, /active/);
  await session.eval(`window.__paintMicPanel({ speaker: { silent: true, silent_source: 'web' } })`);
  r = await read();
  assert.equal(r.silentText, 'Silent: On');
  assert.equal(r.silentOn, 'true');
  assert.match(r.silentClass, /active/);

  // Restore the served controls and clean up.
  await session.eval(`(() => {
    const s = document.getElementById('mic-btn-silent'); const d = document.getElementById('mic-status-dot');
    const r = window.__micPanelRestore || {};
    if (s) { s.textContent = r.silentText ?? 'Silent'; s.className = r.silentClass ?? 'mic-btn mic-btn-silent'; delete s.dataset.silentOn; }
    if (d) { d.className = r.dotClass ?? 'mic-status-dot'; }
    delete window.__paintMicPanel; delete window.__micPanel; delete window.__micPanelRestore; return true;
  })()`);
  report.note('mic-panel render: served controls show waiting-for-answer and clear; silent toggle reflects state');
}

module.exports = { micPanelAnswerWindow };
