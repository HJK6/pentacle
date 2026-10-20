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
  const refs = { sendEl: { dataset: {}, classList: { toggle() {} }, setAttribute() {} }, inputEl: { value: 'draft survives' }, attachEl: {} };
  const state = { slotSendPending: [pending], slotChatRefs: [refs], slotDrafts: ['draft survives'], slotAttachments: [[]], slotChatBoundStream: [null], slotSendMode: ['queue'] };
  const context = vm.createContext({ state, window: {}, chatControlTargetForSlot: () => target, slotAttachmentDrafts: () => [], isCompositeSlot: () => false });
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

// Steer / queue / Stop controls (agent chats only).
function agentControls({ phase, composite = false, draft = 'draft survives', notice = '' }) {
  const modeButtons = ['queue', 'steer'].map(mode => ({ dataset: { sendMode: mode }, pressed: null, setAttribute(name, value) { if (name === 'aria-pressed') this.pressed = value; } }));
  const refs = {
    sendEl: { dataset: {}, classList: { toggle() {} }, setAttribute() {} }, inputEl: { value: draft }, attachEl: {},
  };
  const extras = { stopEl: { hidden: true }, modeEl: { hidden: true, querySelectorAll: () => modeButtons }, noticeEl: { hidden: true, textContent: '' } };
  Object.assign(refs, extras);
  const store = { getTurnPhase: () => phase, getComposerNotice: () => notice };
  const state = { slotSendPending: [false], slotChatRefs: [refs], slotDrafts: [draft], slotChatBoundStream: ['hosta:test'], slotSendMode: ['steer'] };
  const context = vm.createContext({ state, window: { PentacleChatStore: store }, chatControlTargetForSlot: () => ({ streamSession: { stream_id: 'hosta:test' } }), slotAttachmentDrafts: () => [], isCompositeSlot: () => composite });
  vm.runInContext(app.slice(start, end), context);
  vm.runInContext('updateSendControls(0)', context);
  return { refs, extras, state, modeButtons };
}

test('a working agent chat shows Stop beside Send (also with a draft) and the Queue | Steer now choice', () => {
  const c = agentControls({ phase: 'working' });
  assert.equal(c.extras.stopEl.hidden, false);
  assert.equal(c.extras.modeEl.hidden, false);
  assert.equal(c.refs.sendEl.disabled, false);
  assert.equal(c.refs.sendEl.dataset.action, 'send');
  assert.deepEqual(c.modeButtons.map(button => button.pressed), ['false', 'true']);
});

test('an idle agent chat hides Stop and the mode control and resets the mode to Queue', () => {
  const c = agentControls({ phase: 'idle' });
  assert.equal(c.extras.stopEl.hidden, true);
  assert.equal(c.extras.modeEl.hidden, true);
  assert.equal(c.state.slotSendMode[0], 'queue');
});

test('the composite chat never shows Stop, the mode control or a notice', () => {
  const c = agentControls({ phase: 'working', composite: true, notice: 'Steer did not take; queued instead' });
  assert.equal(c.extras.stopEl.hidden, true);
  assert.equal(c.extras.modeEl.hidden, true);
  assert.equal(c.extras.noticeEl.hidden, true);
  assert.equal(c.state.slotSendMode[0], 'queue');
});

test('a store notice shows in the composer', () => {
  const c = agentControls({ phase: 'working', notice: 'Stopped. Queued messages run next' });
  assert.equal(c.extras.noticeEl.hidden, false);
  assert.equal(c.extras.noticeEl.textContent, 'Stopped. Queued messages run next');
});

test('Send is still disabled without a draft even while the agent works', () => {
  const c = agentControls({ phase: 'working', draft: '' });
  assert.equal(c.refs.sendEl.disabled, true);
  assert.equal(c.extras.stopEl.hidden, false);
});

test('Alt+Enter sends as Steer now and plain Enter uses the selected mode', () => {
  const keydown = app.slice(app.indexOf("inputEl.addEventListener('keydown'"), app.indexOf("attachmentTrayEl.addEventListener('click'"));
  assert.match(keydown, /sendChatComposer\(slot, e\.altKey \? \{ steer: true \} : undefined\)/);
  const send = app.slice(app.indexOf('async function sendChatComposer(slot, options = {})'), app.indexOf('function renderSlotReply'));
  assert.match(send, /!isCompositeSlot\(slot\) && \(options\.steer === true \|\| state\.slotSendMode\[slot\] === 'steer'\)/);
  assert.match(send, /state\.slotSendMode\[slot\] = 'queue'/);
});
