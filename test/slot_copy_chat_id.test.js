'use strict';
// Spec: pentacle__web_slot_header_copy_chat_id_2026_09
// A copy control sits at the top-left of every OCCUPIED slot header (left of the
// title), copies the full `<host>:<session>` stream id, and shows success ONLY
// after the clipboard write resolves; a failed/unavailable write shows truthful
// failure (no success check) and permits retry. Empty slots show no control.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');

// Slice a top-level `function name(...) { ... }` (or async) out of app.js the
// same way host_presentation.test.js extracts renderConfigWarnings/renderLimits.
function sliceFn(source, signature) {
  const start = source.indexOf(signature);
  assert.notEqual(start, -1, `expected to find: ${signature}`);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}

test('every static slot header carries a copy-id control, hidden until occupied', () => {
  const html = fs.readFileSync(require.resolve('../renderer/index.html'), 'utf8');
  const document = new JSDOM(html).window.document;
  for (let i = 0; i < 4; i++) {
    const header = document.getElementById(`header-${i}`);
    assert.ok(header, `header-${i} present`);
    const btn = header.querySelector('.cell-copyid');
    assert.ok(btn, `header-${i} has a .cell-copyid control`);
    // Left of the title: the copy control precedes .cell-label in DOM order.
    const label = header.querySelector('.cell-label');
    assert.ok(
      btn.compareDocumentPosition(label) & document.DOCUMENT_POSITION_FOLLOWING,
      `copy control precedes the title in header-${i}`,
    );
    // Empty slot => no visible icon.
    assert.equal(btn.getAttribute('style') || '', 'display:none');
  }
});

function loadCopyFns(extraContext = {}) {
  const source = fs.readFileSync(require.resolve('../renderer/app.js'), 'utf8');
  const code = sliceFn(source, 'function markCopyIdState(')
    + '\n' + sliceFn(source, 'async function copyChatIdFromButton(');
  const context = {
    console: { warn() {} },
    setTimeout: (fn) => { context._pendingTimer = fn; return 1; },
    clearTimeout: () => { context._pendingTimer = null; },
    ...extraContext,
  };
  vm.runInNewContext(code, context);
  return context;
}

function makeButton(streamId) {
  const document = new JSDOM('<button class="cell-copyid"></button>').window.document;
  const btn = document.querySelector('.cell-copyid');
  btn.dataset.streamId = streamId;
  btn.dataset.copyLabel = 'Copy chat id';
  return btn;
}

test('a resolved write copies the exact full stream id and shows success only after completion', async () => {
  const writes = [];
  let resolveWrite;
  const ctx = loadCopyFns({
    writeChatCopyText: (value) => { writes.push(value); return new Promise((r) => { resolveWrite = r; }); },
  });
  const btn = makeButton('amaterasu:v2-a1b2c3d4');
  const p = ctx.copyChatIdFromButton(btn);
  // Success must NOT appear before the clipboard write resolves.
  assert.equal(btn.classList.contains('is-copied'), false, 'no premature success');
  resolveWrite(true);
  const ok = await p;
  assert.equal(ok, true);
  assert.deepEqual(writes, ['amaterasu:v2-a1b2c3d4'], 'exact full host:session copied once');
  assert.equal(btn.classList.contains('is-copied'), true, 'success after resolution');
  assert.equal(btn.classList.contains('is-copy-failed'), false);
  // The icon visibly changes to a success checkmark only after resolution.
  assert.match(btn.innerHTML, /copyid-check/, 'success checkmark rendered after clipboard resolves');
});

test('an unavailable clipboard (helper returns false) shows failure, never success, and permits retry', async () => {
  let ok = false;
  const ctx = loadCopyFns({ writeChatCopyText: () => Promise.resolve(ok) });
  const btn = makeButton('thoth:v2-deadbeef');
  const first = await ctx.copyChatIdFromButton(btn);
  assert.equal(first, false);
  assert.equal(btn.classList.contains('is-copied'), false, 'never shows success on failure');
  assert.equal(btn.classList.contains('is-copy-failed'), true, 'truthful failure feedback');
  // Retry: clipboard now works.
  ok = true;
  const second = await ctx.copyChatIdFromButton(btn);
  assert.equal(second, true);
  assert.equal(btn.classList.contains('is-copied'), true, 'retry succeeds');
  assert.equal(btn.classList.contains('is-copy-failed'), false, 'failure state cleared on success');
});

test('a rejected clipboard write shows failure, not success', async () => {
  const ctx = loadCopyFns({ writeChatCopyText: () => Promise.reject(new Error('denied')) });
  const btn = makeButton('merlin:v2-abc123');
  const ok = await ctx.copyChatIdFromButton(btn);
  assert.equal(ok, false);
  assert.equal(btn.classList.contains('is-copied'), false);
  assert.equal(btn.classList.contains('is-copy-failed'), true);
});

function loadSlotStreamIdFn(ctx) {
  const source = fs.readFileSync(require.resolve('../renderer/app.js'), 'utf8');
  const code = sliceFn(source, 'function slotCopyIdStreamId(');
  vm.runInNewContext(code, ctx);
  return ctx.slotCopyIdStreamId;
}

test('slotCopyIdStreamId returns the CURRENT session id, never a stale bound-stream after a rebind', () => {
  // Slot rebound A->B; the retained bound-stream still holds A's id.
  const ctx = {
    state: {
      slots: [{ name: 'v2-newB', hostId: 'amaterasu' }],
      slotChatBoundStream: ['amaterasu:v2-STALE-A'],
    },
    findSession: (name, hostId) => ({ name, hostId, stream_id: `${hostId}:${name}` }),
    sidebarStreamIdForSession: (s) => (s && s.stream_id) ? s.stream_id : '',
  };
  const slotCopyIdStreamId = loadSlotStreamIdFn(ctx);
  assert.equal(slotCopyIdStreamId(0), 'amaterasu:v2-newB', 'copies B, not the stale bound A');
});

test('slotCopyIdStreamId resolves host:session in terminal-only mode (no chat paint / no summary)', () => {
  const ctx = {
    state: { slots: [{ name: 'assistant-b', hostId: 'thoth' }], slotChatBoundStream: ['thoth:OLD'] },
    findSession: (name, hostId) => ({ name, hostId }), // no stream_id (never chat-painted)
    sidebarStreamIdForSession: (s) => (s && s.stream_id) ? s.stream_id : '',
  };
  const slotCopyIdStreamId = loadSlotStreamIdFn(ctx);
  assert.equal(slotCopyIdStreamId(0), 'thoth:assistant-b', 'current host:session, not stale bound');
});

test('slotCopyIdStreamId returns empty for an unoccupied slot', () => {
  const ctx = { state: { slots: [null], slotChatBoundStream: [null] }, findSession: () => null, sidebarStreamIdForSession: () => '' };
  const slotCopyIdStreamId = loadSlotStreamIdFn(ctx);
  assert.equal(slotCopyIdStreamId(0), '');
});

test('a control with no stream id (empty slot) fails closed without calling the clipboard', async () => {
  let called = false;
  const ctx = loadCopyFns({ writeChatCopyText: () => { called = true; return Promise.resolve(true); } });
  const btn = makeButton('');
  delete btn.dataset.streamId;
  const ok = await ctx.copyChatIdFromButton(btn);
  assert.equal(ok, false);
  assert.equal(called, false, 'clipboard not called without a stream id');
  assert.equal(btn.classList.contains('is-copied'), false);
});
