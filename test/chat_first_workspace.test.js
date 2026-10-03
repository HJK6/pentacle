'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { installRenderer, mountRaceSlot } = require('./helpers/renderer_chat');
const { normalizeWorkspace, visibleWorkspaceSlots, defaultSessionView } = require('../renderer/workspace_layout');

test('bundled configuration enables the chat workspace for new installations', () => {
  assert.equal(require('../pentacle.config.example').features.defaultChatView, true);
  assert.equal(require('../pentacle.config.example').features.chatUi, undefined);
});

test('corrupt or obsolete preferences are bounded; active hidden slots stay reachable', () => {
  for (const value of [null, [], false, { paneCount: 9, activeSlot: -1 }, { paneCount: '4', activeSlot: '2' }]) {
    assert.equal(normalizeWorkspace(value).paneCount, 1);
    assert.equal(normalizeWorkspace(value).activeSlot, 0);
  }
  for (let count = 1; count <= 4; count++) for (let active = 0; active < 4; active++) {
    const slots = visibleWorkspaceSlots(count, active);
    assert.equal(slots.length, count);
    assert.equal(new Set(slots).size, count);
    assert.ok(slots.includes(active));
  }
  assert.equal(defaultSessionView(true, { provider: 'claude' }), 'chat');
  assert.equal(defaultSessionView(false, { provider: 'claude' }), 'terminal');
  assert.equal(defaultSessionView(true, { provider: 'terminal' }), 'terminal');
  assert.deepEqual(normalizeWorkspace({ paneCount: 2, activeSlot: 0, visibleSlots: [0, 3] }).visibleSlots, [0, 3]);
  assert.deepEqual(normalizeWorkspace({ paneCount: 3, activeSlot: 3, visibleSlots: [-1, 9, 0, 0, '2'] }).visibleSlots, [0, 1, 3]);
  assert.deepEqual(normalizeWorkspace({ bindings: [{ name: '<untrusted>', hostId: 'local', mode: 'evil' }] }).bindings[0],
    { name: '<untrusted>', hostId: 'local', mode: 'chat', draft: '' });
});

test('fresh workspace is light, one pane, and normal attachment starts in chat', async () => {
  const h = installRenderer({ questionOverride: null });
  const value = vm.runInContext(`(() => {
    const pending = attachSession(0, 'claude-hostc-race', 'A real chat', 'local');
    pending.catch(() => {});
    return { mode: state.slotViewModes[0], theme: state.appearance.theme };
  })()`, h.context);
  assert.equal(value.mode, 'chat');
  assert.equal(value.theme, 'light');
  assert.equal(h.dom.window.document.querySelector('.grid').dataset.paneCount, '1');
});

test('reselecting an open chat focuses its composer, not hidden xterm', () => {
  const h = installRenderer({ questionOverride: null });
  mountRaceSlot(h.context);
  vm.runInContext(`state.terminals[0] = { term: { focus() { throw Error('hidden xterm focused'); } } };
    assignToSlot('claude-hostc-race', 'Race Fixture', 'local');`, h.context);
  assert.equal(h.dom.window.document.activeElement, h.dom.window.document.querySelector('.slot-chat-compose-input'));
});

test('status and chat scrolling are never swallowed by terminal wheel handling', () => {
  const h = installRenderer({ questionOverride: null });
  mountRaceSlot(h.context);
  vm.runInContext(`state.terminals[0] = { term: {} }; state.slots[0].paneId = '%fixture';`, h.context);
  h.dom.window.cc.scrollTmux = () => { throw Error('nonterminal wheel sent to tmux'); };
  for (const mode of ['chat', 'status', 'asset']) {
    vm.runInContext(`state.slotViewModes[0] = '${mode}';`, h.context);
    const event = new h.dom.window.WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: 100 });
    h.dom.window.document.getElementById('term-0').dispatchEvent(event);
    assert.equal(event.defaultPrevented, false, mode);
  }
});

test('pane-count controls persist and hide slots without closing sessions', () => {
  const h = installRenderer({ questionOverride: null });
  mountRaceSlot(h.context);
  for (const count of [4, 3, 2, 1]) {
    const control = h.dom.window.document.querySelector(`[data-pane-count-button="${count}"]`);
    assert.ok(control, `missing ${count}-pane control`);
    control.click();
    assert.equal(h.dom.window.document.querySelector('.grid').dataset.paneCount, String(count));
    assert.equal(control.getAttribute('aria-pressed'), 'true');
    assert.equal(JSON.parse(h.dom.window.localStorage.getItem('pentacle.workspace.v1')).paneCount, count);
    assert.equal(vm.runInContext('state.slots[0].name', h.context), 'claude-hostc-race');
  }
  assert.equal(h.closeCalls.length, 0);
  assert.equal(h.killCalls.length, 0);
});

test('latest session bindings and draft restore only from daemon inventory', async () => {
  const session = { host: 'hostc', stream_id: 'hostc:claude-hostc-race', session_name: 'claude-hostc-race', provider: 'claude', status: 'open', visibility: 'default' };
  const h = installRenderer({ questionOverride: null, initialSessions: [session], storage: {
    'pentacle.workspace.v1': JSON.stringify({ paneCount: 3, activeSlot: 2, bindings: [
      { name: session.session_name, hostId: 'local', mode: 'chat', draft: 'Unsent draft' },
      { name: 'closed-session', hostId: 'local', mode: 'chat' },
    ] }),
  } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(vm.runInContext('state.slots[0]?.name', h.context), session.session_name);
  assert.equal(vm.runInContext('state.slots[1]', h.context), null);
  assert.equal(vm.runInContext('state.workspace.paneCount', h.context), 3);
  assert.equal(vm.runInContext('state.workspace.activeSlot', h.context), 2);
  assert.equal(h.dom.window.document.querySelector('.slot-chat-compose-input').value, 'Unsent draft');
  assert.equal(h.closeCalls.length, 0);
});

test('terminal attachment failure leaves chat and its binding usable, with an explicit retry', async () => {
  class Terminal {
    constructor() { this.unicode = {}; this.cols = 80; this.rows = 24; }
    loadAddon() {} dispose() {} focus() {} onData() {} attachCustomKeyEventHandler() {}
    open(container) { this.element = container.ownerDocument.createElement('div'); container.appendChild(this.element); }
  }
  const h = installRenderer({ questionOverride: null, terminalClass: Terminal });
  mountRaceSlot(h.context);
  let attempts = 0;
  h.dom.window.cc.createPty = async () => { attempts++; throw new Error('fixture: terminal offline'); };
  await vm.runInContext("attachSession(0, 'claude-hostc-race', 'Race Fixture', 'local')", h.context);
  assert.equal(attempts, 0, 'chat must not attach a hidden PTY');
  vm.runInContext("updateSlotViewMode(0, 'terminal')", h.context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(attempts, 1);
  assert.match(h.dom.window.document.querySelector('.terminal-unavailable').textContent, /Retry terminal/);
  vm.runInContext("updateSlotViewMode(0, 'chat')", h.context);
  assert.equal(vm.runInContext('state.slots[0].name', h.context), 'claude-hostc-race');
  assert.equal(h.dom.window.document.querySelector('.slot-chat-layer').style.display, 'flex');
  assert.equal(h.closeCalls.length, 0);
});

test('composer forwards literal slash text, Shift+Enter does not send, and offline send is blocked', async () => {
  const h = installRenderer({ questionOverride: null });
  mountRaceSlot(h.context);
  const input = h.dom.window.document.querySelector('.slot-chat-compose-input');
  input.value = '/help';
  input.dispatchEvent(new h.dom.window.Event('input', { bubbles: true }));
  input.dispatchEvent(new h.dom.window.KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true }));
  assert.equal(h.sendCalls.length, 0);
  input.dispatchEvent(new h.dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.sendCalls.length, 1);
  assert.equal(h.sendCalls[0].text, '/help');
  vm.runInContext('state.chatStream.connected = false; renderSlotChat(0)', h.context);
  input.value = 'must not be sent offline';
  input.dispatchEvent(new h.dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.sendCalls.length, 1);
});

const tick = () => new Promise(resolve => setImmediate(resolve));
const run = (h, code) => vm.runInContext(code, h.context);
const visibleSlots = h => [0, 1, 2, 3].filter(slot => !h.dom.window.document.getElementById(`cell-${slot}`).classList.contains('workspace-hidden'));

test('focus does not replace visible panes after reducing four panes to two or three', async () => {
  for (const count of [2, 3]) {
    const h = installRenderer({ questionOverride: null });
    await tick();
    h.dom.window.document.querySelector('[data-pane-count-button="4"]').click();
    run(h, 'focusSlot(3)');
    h.dom.window.document.querySelector(`[data-pane-count-button="${count}"]`).click();
    const expected = count === 2 ? [0, 3] : [0, 1, 3];
    assert.deepEqual(visibleSlots(h), expected);
    for (const slot of expected) {
      h.dom.window.document.getElementById(`cell-${slot}`).dispatchEvent(new h.dom.window.Event('pointerdown', { bubbles: true }));
      assert.deepEqual(visibleSlots(h), expected, `pointer focus ${slot} in ${count} panes`);
      run(h, `focusSlot(${slot})`);
      assert.deepEqual(visibleSlots(h), expected, `sidebar focus ${slot} in ${count} panes`);
    }
    // Selecting a genuinely hidden slot, unlike focusing a visible pane, reveals it.
    run(h, 'focusSlot(2)');
    assert.ok(visibleSlots(h).includes(2));
    assert.equal(visibleSlots(h).length, count);
    assert.equal(h.closeCalls.length + h.killCalls.length, 0);
  }
});

test('batch restore keeps saved active/narrow/composer focus and persists all drafts', async () => {
  const sessions = [0, 3].map(slot => ({ host: 'hostc', stream_id: `hostc:session-${slot}`, session_name: `session-${slot}`, provider: 'claude', status: 'open', visibility: 'default' }));
  const bindings = [0, 1, 2, 3].map(slot => [0, 3].includes(slot) ? { name: `session-${slot}`, hostId: 'local', mode: 'chat', draft: `draft${slot}` } : null);
  const h = installRenderer({ questionOverride: null, initialSessions: sessions, storage: {
    'pentacle.workspace.v1': JSON.stringify({ paneCount: 1, activeSlot: 0, bindings }),
  } });
  await tick();
  assert.equal(run(h, 'state.workspace.activeSlot'), 0);
  assert.equal(run(h, 'narrowActiveSlot'), 0);
  assert.equal(h.dom.window.document.activeElement.dataset.slot, '0');
  assert.deepEqual(visibleSlots(h), [0]);
  assert.equal(h.dom.window.document.getElementById('cell-0').classList.contains('narrow-active'), true);
  assert.equal(h.dom.window.document.getElementById('cell-3').classList.contains('narrow-active'), false);
  const saved = JSON.parse(h.dom.window.localStorage.getItem('pentacle.workspace.v1'));
  assert.equal(saved.activeSlot, 0);
  assert.equal(saved.bindings[0].draft, 'draft0');
  assert.equal(saved.bindings[3].draft, 'draft3');
  assert.equal(h.closeCalls.length + h.killCalls.length, 0);
});

test('late restore never leaks a draft into a rebound slot, including same-name ABA', async () => {
  for (const host of ['local', 'other']) {
    const h = installRenderer({ questionOverride: null });
    await tick();
    mountRaceSlot(h.context);
    run(h, `
      detachSlot(0);
      state.workspaceRestored = false;
      state.workspace.bindings[0] = { name: 'claude-hostc-race', hostId: 'local', mode: 'chat', draft: 'old private draft' };
      var originalAttach = attachSession, releaseRestore;
      attachSession = function(...args) { originalAttach(...args); return new Promise(resolve => { releaseRestore = resolve; }); };
      var pendingRestore = restoreWorkspaceSessions();
      attachSession = originalAttach;
    `);
    await run(h, `attachSession(0, 'claude-hostc-race', 'New binding', '${host}')`);
    run(h, 'releaseRestore()');
    await run(h, 'pendingRestore');
    assert.equal(run(h, 'state.slotDrafts[0]'), '', host);
    assert.equal(h.dom.window.document.querySelector('.slot-chat-compose-input').value, '', host);
    assert.equal(JSON.parse(h.dom.window.localStorage.getItem('pentacle.workspace.v1')).bindings[0].draft, '', host);
  }
});
