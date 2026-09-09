'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const app = fs.readFileSync(path.join(__dirname, '../renderer/app.js'), 'utf8');
const start = app.indexOf('function updateSendControls(slot) {');
const end = app.indexOf('\nfunction maybeRestoreReturnedToPromptDraft', start);
assert.ok(start >= 0 && end > start);

function controls(initialTarget, pending = false) {
  let target = initialTarget;
  const refs = { sendEl: {}, inputEl: { value: 'draft survives' }, attachEl: {} };
  const state = { slotSendPending: [pending], slotChatRefs: [refs] };
  const context = vm.createContext({ state, chatControlTargetForSlot: () => target });
  vm.runInContext(app.slice(start, end), context);
  const update = () => vm.runInContext('updateSendControls(0)', context);
  return { refs, state, update, setTarget: value => { target = value; } };
}

for (const target of [null, { error: 'Waiting for websocket session detail.' }, { error: 'Chat stream offline.' }]) {
  test(`missing control target disables only send: ${JSON.stringify(target)}`, () => {
    const c = controls(target);
    c.update();
    assert.equal(c.refs.sendEl.disabled, true);
    assert.equal(c.refs.inputEl.disabled, false);
    assert.equal(c.refs.attachEl.disabled, false);
    assert.equal(c.refs.inputEl.value, 'draft survives');
  });
}

test('arrival enables and loss disables send without replacing the editable draft', () => {
  const c = controls(null);
  c.update();
  assert.equal(c.refs.sendEl.disabled, true);
  c.setTarget({ streamSession: { stream_id: 'hosta:test' } });
  c.update();
  assert.equal(c.refs.sendEl.disabled, false);
  c.setTarget({ error: 'Chat stream offline.' });
  c.update();
  assert.equal(c.refs.sendEl.disabled, true);
  assert.equal(c.refs.inputEl.value, 'draft survives');
});

test('a resolved working stream still accepts native mid-turn input', () => {
  const c = controls({ streamSession: { stream_id: 'hosta:test', working: true } });
  c.update();
  assert.equal(c.refs.sendEl.disabled, false);
  assert.equal(c.refs.inputEl.disabled, false);
});

test('existing pending operation still disables every control until it finishes', () => {
  const c = controls({ streamSession: { stream_id: 'hosta:test' } }, true);
  c.update();
  for (const ref of Object.values(c.refs)) assert.equal(ref.disabled, true);
  c.state.slotSendPending[0] = false;
  c.update();
  for (const ref of Object.values(c.refs)) assert.equal(ref.disabled, false);
});

