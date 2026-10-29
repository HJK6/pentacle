'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// Exercise the actual renderer draft functions with a controlled pane environment.
function harness() {
  const source = fs.readFileSync(require.resolve('../renderer/app.js'), 'utf8');
  const code = source.slice(source.indexOf('function composerKey('), source.indexOf('const CHAT_ATTACHMENT_MIMES'));
  const state = { chatComposerDrafts: new Map(), slotComposerKeys: [null, null], slotComposerVersions: [0, 0], slotDrafts: ['', ''],
    slotDraftTouched: [false, false], slotReplies: [null, null], slotAttachments: [[], []],
    slotChatRefs: [{ inputEl: { value: '' } }, { inputEl: { value: '' } }], slotGen: [1, 1], slots: [null, null] };
  const errors = [];
  const context = vm.createContext({ state, JSON, window: {}, URL: { createObjectURL: () => 'blob:test', revokeObjectURL() {} },
    CHAT_ATTACHMENT_MIMES: new Set(['image/png']), chatAttachmentLimit: () => 5,
    setSlotSendError: (slot, error) => errors.push([slot, error]), updateSendControls() {},
    readFileAsDataBase64: async () => 'eA==', readImageDimensions: async () => ({ width: 1, height: 1 }), nextAttachmentDraftId: () => 'one',
    canonicalChatSessionStateForNameHost: (name, host) => ({ stream_id: `${host}:${name}`, session_generation: 'gen', session_kind: 'ordinary' }),
    isCompositeAssistant: s => s.session_kind === 'assistant',
    slotAttachmentDrafts: slot => state.slotAttachments[slot], renderSlotAttachmentTray() {}, renderSlotReply() {}, renderSlotChat() {} });
  const attachmentCode = source.slice(source.indexOf('async function addSlotAttachmentFiles('), source.indexOf('async function addSlotScreenshot('));
  vm.runInContext(`${code}\n${attachmentCode}\nthis.api = { composerKey, bindComposerDraft, saveComposerDraft, captureComposer, composerChanged, clearSubmittedComposer, addSlotAttachmentFiles };`, context);
  return { state, errors, api: context.api };
}

test('canonical host and generation isolate ordinary drafts; direct assistant uses source', () => {
  const { api } = harness();
  assert.notEqual(api.composerKey({ name: 'same', hostId: 'one' }), api.composerKey({ name: 'same', hostId: 'two' }));
  assert.equal(api.composerKey({ laneHistory: true }), null);
  assert.equal(api.composerKey({ assistantDirect: { sourceId: 'a:assistant', targetId: 'old' } }), api.composerKey({ assistantDirect: { sourceId: 'a:assistant', targetId: 'new' } }));
});

test('delayed completion clears its own draft, and never clears newer edits', () => {
  const { api, state } = harness();
  state.slots[0] = { name: 'a', hostId: 'one' }; api.bindComposerDraft(0);
  state.slotChatRefs[0].inputEl.value = 'old'; state.slotDraftTouched[0] = true; api.composerChanged(0);
  const owner = api.captureComposer(0); const version = owner.record.version;
  api.saveComposerDraft(0); state.slotGen[0]++; state.slots[0] = { name: 'b', hostId: 'one' }; api.bindComposerDraft(0);
  state.slotChatRefs[0].inputEl.value = 'other'; state.slotDraftTouched[0] = true; api.composerChanged(0);
  api.clearSubmittedComposer(0, owner, version);
  assert.equal(state.slotChatRefs[0].inputEl.value, 'other');
  assert.equal(owner.record.text, '');
  state.slots[1] = { name: 'a', hostId: 'one' }; api.bindComposerDraft(1);
  state.slotChatRefs[1].inputEl.value = 'new'; state.slotDraftTouched[1] = true; api.composerChanged(1);
  api.clearSubmittedComposer(0, owner, version);
  assert.equal(state.slotChatRefs[1].inputEl.value, 'new');
});

test('an exact unresolved stream keeps typed text when canonical identity arrives', () => {
  const { api, state } = harness();
  const unresolved = { name: 'a', hostId: 'one', draftStreamId: 'one:a' };
  // The retained exact stream is the pending identity until inventory resolves.
  assert.equal(api.composerKey({ ...unresolved, laneHistory: true }), null);
  const pendingKey = JSON.stringify(['pending', 'one:a']);
  const record = { text: 'typed while resolving', touched: true, reply: null, attachments: [], version: 2 };
  state.chatComposerDrafts.set(pendingKey, record);
  state.slots[0] = unresolved; api.bindComposerDraft(0);
  assert.equal(state.slotChatRefs[0].inputEl.value, 'typed while resolving');
  assert.equal(state.chatComposerDrafts.has(pendingKey), false);
});


test('late screenshot attachment does not alter the replacement pane error', async () => {
  const { api, state, errors } = harness();
  state.slots[0] = { name: 'a', hostId: 'one' }; api.bindComposerDraft(0);
  const owner = api.captureComposer(0);
  state.slotGen[0]++; state.slots[0] = { name: 'b', hostId: 'one' }; api.bindComposerDraft(0);
  await api.addSlotAttachmentFiles(0, [{ type: 'image/png', size: 1, name: 'image' }], owner);
  assert.equal(errors.length, 0);
  assert.equal(owner.record.attachments.length, 1);
  assert.equal(state.slotAttachments[0].length, 0);
});

test('retirement waits for authoritative inventory and cancels across reconnect or a temporary gap', () => {
  const source = fs.readFileSync(require.resolve('../renderer/app.js'), 'utf8');
  const code = source.slice(source.indexOf('// A full, connected inventory may retire'), source.indexOf('// The slot is a view onto a chat-owned draft'));
  const key = JSON.stringify(['chat', 'host:chat', 'generation']);
  const record = { attachments: [{ previewUrl: 'blob:owned' }] };
  const state = { chatStream: { connected: true, stateVersion: 1 }, chatComposerDrafts: new Map([[key, record]]) };
  const timers = new Map(); const revoked = []; let next = 0;
  const context = vm.createContext({ state, JSON, Map, URL: { revokeObjectURL: url => revoked.push(url) },
    setTimeout: fn => { const id = ++next; timers.set(id, fn); return id; }, clearTimeout: id => timers.delete(id) });
  vm.runInContext(`${code}\nthis.reconcile = reconcileComposerDraftInventory;`, context);
  const present = { sessions: [{ stream_id: 'host:chat', session_generation: 'generation' }] };
  context.reconcile(present);
  context.reconcile({}); // status-only updates cannot prove closure
  assert.equal(timers.size, 0);
  context.reconcile({ sessions: [] });
  assert.equal(timers.size, 1);
  state.chatStream.connected = false; context.reconcile({});
  assert.equal(timers.size, 0);
  state.chatStream.connected = true; state.chatStream.stateVersion = 2; context.reconcile(present);
  context.reconcile({ sessions: [] }); context.reconcile(present);
  assert.equal(timers.size, 0);
  assert.equal(state.chatComposerDrafts.has(key), true);
  context.reconcile({ sessions: [] });
  const expire = [...timers.values()][0]; expire();
  assert.equal(state.chatComposerDrafts.has(key), false);
  assert.deepEqual(revoked, ['blob:owned']);
});
