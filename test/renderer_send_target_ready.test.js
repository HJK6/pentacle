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
  const state = { slotSendPending: [pending], slotChatRefs: [refs], slotDrafts: ['draft survives'], slotAttachments: [[]], slotChatBoundStream: [null] };
  const context = vm.createContext({ state, window: {}, chatControlTargetForSlot: () => target, slotAttachmentDrafts: () => [], isCompositeSlot: () => false });
  vm.runInContext(app.slice(start, end), context);
  const update = () => vm.runInContext('updateSendControls(0)', context);
  return { refs, state, context, update, setTarget: value => { target = value; } };
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

test('queued-row readiness refreshes without replacing identical transcript HTML', () => {
  const c = controls({ streamSession: { stream_id: 'hosta:test' } });
  const button = { dataset: { streamId: 'hosta:test', optimisticId: 'row-1' } };
  let eligible = true;
  c.refs.listEl = { querySelectorAll: () => [button] };
  c.context.window.PentacleChatStore = { canSendQueuedNow: (stream, row) => {
    assert.equal(stream, 'hosta:test');
    assert.equal(row, 'row-1');
    return eligible;
  } };
  c.update();
  assert.equal(button.disabled, false);
  eligible = false; // reserved row or retired generation, same text/HTML
  c.update();
  assert.equal(button.disabled, true);
  eligible = true;
  c.update();
  assert.equal(button.disabled, false);
  c.setTarget({ streamSession: { stream_id: 'hosta:replacement' } });
  c.update();
  assert.equal(button.disabled, true);
});

test('existing pending operation still disables every control until it finishes', () => {
  const c = controls({ streamSession: { stream_id: 'hosta:test' } }, true);
  c.update();
  for (const ref of Object.values(c.refs)) assert.equal(ref.disabled, true);
  c.state.slotSendPending[0] = false;
  c.update();
  for (const ref of Object.values(c.refs)) assert.equal(ref.disabled, false);
});

// Ordinary Send / Stop controls (agent chats only).
function agentControls({ phase, composite = false, draft = 'draft survives', notice = '' }) {
  const refs = {
    sendEl: { dataset: {}, classList: { toggle() {} }, setAttribute() {} }, inputEl: { value: draft }, attachEl: {},
  };
  const extras = { stopEl: { hidden: true }, noticeEl: { hidden: true, textContent: '' } };
  Object.assign(refs, extras);
  const store = { getTurnPhase: () => phase, getComposerNotice: () => notice };
  const state = { slotSendPending: [false], slotChatRefs: [refs], slotDrafts: [draft], slotChatBoundStream: ['hosta:test'] };
  const context = vm.createContext({ state, window: { PentacleChatStore: store }, chatControlTargetForSlot: () => ({ streamSession: { stream_id: 'hosta:test' } }), slotAttachmentDrafts: () => [], isCompositeSlot: () => composite });
  vm.runInContext(app.slice(start, end), context);
  vm.runInContext('updateSendControls(0)', context);
  return { refs, extras, state };
}

test('a working agent keeps Stop and ordinary Send available with a draft', () => {
  const c = agentControls({ phase: 'working' });
  assert.equal(c.extras.stopEl.hidden, false);
  assert.equal(c.refs.sendEl.disabled, false);
  assert.equal(c.refs.sendEl.dataset.action, 'send');
});

test('an idle agent hides Stop and retains ordinary Send readiness', () => {
  const c = agentControls({ phase: 'idle' });
  assert.equal(c.extras.stopEl.hidden, true);
  assert.equal(c.refs.sendEl.disabled, false);
  assert.equal(c.refs.sendEl.dataset.action, 'send');
});

test('the composite chat shows ordinary Send without Stop or an agent queue notice', () => {
  const c = agentControls({ phase: 'working', composite: true, notice: 'Send now requested. Waiting for the queued message to start.' });
  assert.equal(c.extras.stopEl.hidden, true);
  assert.equal(c.extras.noticeEl.hidden, true);
  assert.equal(c.refs.sendEl.dataset.action, 'send');
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

test('Enter and Alt+Enter invoke the same ordinary send; Shift+Enter preserves the draft', () => {
  const keydown = app.slice(app.indexOf("inputEl.addEventListener('keydown'"), app.indexOf("attachmentTrayEl.addEventListener('click'"));
  let handler;
  const sends = [];
  const context = vm.createContext({ slot: 2, inputEl: { addEventListener: (_event, callback) => { handler = callback; } },
    sendChatComposer: (...args) => sends.push(args), cancelChatComposer: () => false });
  vm.runInContext(keydown, context);
  for (const altKey of [false, true]) {
    let prevented = false;
    handler({ key: 'Enter', altKey, shiftKey: false, preventDefault: () => { prevented = true; } });
    assert.equal(prevented, true);
  }
  assert.deepEqual(sends, [[2], [2]], 'neither key carries an urgent or mode option');
  let prevented = false;
  handler({ key: 'Enter', shiftKey: true, preventDefault: () => { prevented = true; } });
  assert.equal(prevented, false);
  assert.equal(sends.length, 2);
});

// Exercise the real row handler, including stale DOM after a slot changes.
function queuedAction({ target, composite = false, streamId = 'hosta:test', optimisticId = 'row-1' }) {
  const calls = [];
  let click;
  let renders = 0;
  const button = { dataset: { streamId, optimisticId }, addEventListener: (_event, callback) => { click = callback; } };
  const actionStart = app.indexOf("  refs.listEl.querySelectorAll('.slot-chat-send-now')");
  const actionEnd = app.indexOf('  // Commit the binding ONLY', actionStart);
  assert.ok(actionStart >= 0 && actionEnd > actionStart);
  const context = vm.createContext({ slot: 0, refs: { listEl: { querySelectorAll: () => [button] } },
    window: { PentacleChatStore: { sendQueuedNow: (...args) => { calls.push(args); return true; } } },
    chatControlTargetForSlot: () => target, isCompositeSlot: () => composite,
    renderSlotChat: () => { renders++; } });
  vm.runInContext(app.slice(actionStart, actionEnd), context);
  click();
  return { calls, renders };
}

test('queued Send now requires a ready matching stream and row, including after a slot switch', () => {
  for (const target of [null, { error: 'offline' }, {}, { streamSession: { stream_id: 'hosta:replacement' } }]) {
    assert.equal(queuedAction({ target }).calls.length, 0);
  }
  const target = { streamSession: { stream_id: 'hosta:test' } };
  assert.equal(queuedAction({ target, optimisticId: '' }).calls.length, 0);
  assert.equal(queuedAction({ target, streamId: '' }).calls.length, 0);
  const ready = queuedAction({ target });
  assert.deepEqual(ready.calls, [['hosta:test', 'row-1']]);
  assert.equal(ready.renders, 1);
});

test('a composite cannot invoke a queued-row interrupt even from a stale button', () => {
  const result = queuedAction({ target: { streamSession: { stream_id: 'hosta:test' } }, composite: true });
  assert.deepEqual(result.calls, []);
  assert.equal(result.renders, 0);
});

for (const eligibility of ['reserved', 'retired', 'ready']) {
test(`selection-release deferred commit refreshes ${eligibility} queued-row readiness`, () => {
  const { JSDOM } = require('jsdom');
  const document = new JSDOM('<div id="list">old selected transcript</div>').window.document;
  const c = controls({ streamSession: { stream_id: 'hosta:test' } });
  c.refs.listEl = document.querySelector('#list');
  c.state.slotChatPendingListRender = [{
    html: '<button class="slot-chat-send-now" data-stream-id="hosta:test" data-optimistic-id="row-1">Send now</button>',
    cacheKey: 'queued row', paintedStreamId: 'hosta:test', detailPresent: false,
  }];
  c.state.slotChatLastListHtml = [null];
  c.state.slotChatPendingPrepend = [null];
  c.context.window.PentacleChatStore = { canSendQueuedNow: () => eligibility === 'ready' };
  let selected = true;
  Object.assign(c.context, { document, activeSelectionInside: () => selected,
    hydrateChatAttachmentMedia() {}, syncActivitySpinnerPhase() {} });
  const applyStart = app.indexOf('function applySlotChatListRender(');
  vm.runInContext(app.slice(applyStart, app.indexOf('\nasync function writeChatCopyText', applyStart)), c.context);
  const deferStart = app.indexOf('function maybeApplyDeferredSlotChatRenders()');
  vm.runInContext(app.slice(deferStart, app.indexOf('\nfunction scrollSlotChatToBottom', deferStart)), c.context);
  vm.runInContext('maybeApplyDeferredSlotChatRenders()', c.context);
  assert.equal(c.refs.listEl.querySelector('.slot-chat-send-now'), null);
  selected = false;
  vm.runInContext('maybeApplyDeferredSlotChatRenders()', c.context);
  const button = c.refs.listEl.querySelector('.slot-chat-send-now');
  assert.ok(button, 'deferred HTML actually committed');
  assert.equal(c.state.slotChatPendingListRender[0], null);
  assert.equal(button.disabled, eligibility !== 'ready');
});
}

test('an explicit urgent action cannot dispatch a stale composite target', async () => {
  const start=app.indexOf('async function sendChatComposer('),end=app.indexOf('\nfunction renderSlotReply',start);
  const context=vm.createContext({isCompositeSlot:()=>true});vm.runInContext(app.slice(start,end),context);
  // The composite exclusion runs before touching a draft or any bridge.
  await context.sendChatComposer(0,{urgent:true});
});
