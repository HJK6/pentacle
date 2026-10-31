'use strict';
// Pane-count workspace (default on) and the optional chat-first look
// (features.chatFirstStyle, default off). Derived from PR #55 by Sai Yeluru.
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { installRenderer, mountRaceSlot, STREAM } = require('./helpers/renderer_chat');
// The helper's fixture host and race session, so this file names neither.
const [HOST, RACE] = STREAM.split(':');
const {
  WORKSPACE_STORAGE_KEY, normalizeWorkspace, loadWorkspace, visibleWorkspaceSlots,
  placementSlot, defaultSessionView, uniqueAssistantComposite,
} = require('../renderer/workspace_layout');

const settle = () => new Promise(resolve => setTimeout(resolve, 20));
const composite = (name = 'assistant', extra = {}) => ({
  host: HOST, stream_id: `${HOST}:${name}`, session_name: name, name, display_name: 'Assistant',
  session_kind: 'assistant_composite', provider: 'composite', status: 'open', visibility: 'default', ...extra,
});
const ordinary = name => ({
  host: HOST, stream_id: `${HOST}:${name}`, session_name: name, name, provider: 'claude', status: 'open', visibility: 'default',
});

test('workspace preferences are bounded and default to one pane', () => {
  for (const value of [undefined, null, [], false, { paneCount: 9, activeSlot: -1 }, { paneCount: '4', activeSlot: '2' }]) {
    assert.equal(normalizeWorkspace(value).paneCount, 1);
    assert.equal(normalizeWorkspace(value).activeSlot, 0);
  }
  for (let count = 1; count <= 4; count++) for (let active = 0; active < 4; active++) {
    const slots = visibleWorkspaceSlots(count, active);
    assert.equal(slots.length, count);
    assert.equal(new Set(slots).size, count);
    assert.ok(slots.includes(active));
  }
  assert.deepEqual(normalizeWorkspace({ paneCount: 2, activeSlot: 0, visibleSlots: [0, 3] }).visibleSlots, [0, 3]);
  assert.deepEqual(normalizeWorkspace({ paneCount: 3, activeSlot: 3, visibleSlots: [-1, 9, 0, 0, '2'] }).visibleSlots, [0, 1, 3]);
  assert.deepEqual(normalizeWorkspace({ bindings: [{ name: '<untrusted>', hostId: 'local', mode: 'evil' }] }).bindings[0],
    { name: '<untrusted>', hostId: 'local', mode: 'chat', draft: '' });
  const storage = values => ({ getItem: key => (key in values ? values[key] : null) });
  assert.equal(loadWorkspace(storage({})).saved, false);
  assert.equal(loadWorkspace(storage({ [WORKSPACE_STORAGE_KEY]: '{bad json' })).saved, false);
  assert.equal(loadWorkspace(storage({ [WORKSPACE_STORAGE_KEY]: '{"paneCount":4}' })).workspace.paneCount, 4);
  assert.equal(loadWorkspace({ getItem() { throw Error('blocked'); } }).saved, false);
});

test('placement fills an empty visible pane, else the focused pane; plain terminals open in Terminal', () => {
  const workspace = normalizeWorkspace({ paneCount: 2, activeSlot: 1, visibleSlots: [0, 1] });
  assert.equal(placementSlot(workspace, [null, null, null, null]), 0);
  assert.equal(placementSlot(workspace, [{}, null, null, null]), 1);
  assert.equal(placementSlot(workspace, [{}, {}, null, null]), 1, 'hidden empty panes are never chosen');
  assert.equal(defaultSessionView('chat', { provider: 'claude' }), 'chat');
  assert.equal(defaultSessionView('terminal', { provider: 'claude' }), 'terminal');
  assert.equal(defaultSessionView('chat', null), 'terminal');
});

test('the default assistant is the single visible composite, never a guess', () => {
  assert.equal(uniqueAssistantComposite([]), null);
  assert.equal(uniqueAssistantComposite([ordinary('a')]), null);
  assert.equal(uniqueAssistantComposite([composite(), ordinary('a')]).session_name, 'assistant');
  assert.equal(uniqueAssistantComposite([composite('one'), composite('two')]), null);
  assert.equal(uniqueAssistantComposite([composite('one'), composite('two', { visibility: 'hidden' })]).session_name, 'one');
  assert.equal(uniqueAssistantComposite([composite('one', { status: 'closed' })]), null);
});

test('flag off keeps the classic look: palette, copy, title and pill styling', () => {
  const h = installRenderer({ questionOverride: null });
  const { document } = h.dom.window;
  assert.equal(document.documentElement.classList.contains('chat-first-style'), false);
  assert.equal(document.getElementById('view-chats').textContent, 'Agents');
  assert.equal(document.getElementById('session-search').placeholder, 'Search chats');
  assert.equal(document.querySelector('#term-1 .cell-empty').textContent, 'Click a session or bot to attach');
  assert.equal(document.documentElement.dataset.theme, 'dark');
  assert.equal(document.documentElement.style.getPropertyValue('--bg'), '#0c1310');
  assert.equal(document.querySelector('.workspace-brand, .workspace-sidebar-footer'), null);
  mountRaceSlot(h.context);
  vm.runInContext("ensureSlotModeToggle(0)", h.context);
  assert.equal(document.querySelector('.slot-chat-compose-input').placeholder, '');
  assert.match(document.querySelector('#header-0 .cell-view-toggle').getAttribute('style') || '', /border-radius: ?999px/);
});

test('flag on applies the chat-first look and defaults it to Light', () => {
  const h = installRenderer({ questionOverride: null, features: { chatFirstStyle: true } });
  const { document } = h.dom.window;
  assert.equal(document.documentElement.classList.contains('chat-first-style'), true);
  assert.equal(document.getElementById('view-chats').textContent, 'Chats');
  assert.equal(document.getElementById('session-search').placeholder, 'Search conversations');
  assert.ok(document.querySelector('.workspace-brand #sidebar-toggle'));
  assert.ok(document.querySelector('.workspace-sidebar-footer'));
  assert.ok(document.querySelector('#term-1 .workspace-empty [data-workspace-new]'));
  assert.equal(document.documentElement.dataset.theme, 'light');
  assert.equal(document.documentElement.style.getPropertyValue('--bg'), '#fcfbf8');
  mountRaceSlot(h.context);
  vm.runInContext("ensureSlotModeToggle(0)", h.context);
  assert.equal(document.querySelector('.slot-chat-compose-input').placeholder, 'Message your agent, or type a / command…');
  assert.equal(document.querySelector('#header-0 .cell-view-toggle').getAttribute('style'), null);
});

test('a saved theme wins over the chat-first Light default', () => {
  const h = installRenderer({ questionOverride: null, features: { chatFirstStyle: true },
    storage: { 'pentacle.settings.v1': JSON.stringify({ appearance: { theme: 'dark' } }) } });
  assert.equal(h.dom.window.document.documentElement.dataset.theme, 'dark');
  assert.equal(h.dom.window.document.documentElement.style.getPropertyValue('--bg'), '#22221f');
});

test('first visit opens the single assistant composite in one pane without spawning', async () => {
  const h = installRenderer({ questionOverride: null, freshWorkspace: true, initialSessions: [composite(), ordinary('worker')] });
  await settle();
  const { document } = h.dom.window;
  assert.equal(document.querySelector('.grid').dataset.paneCount, '1');
  assert.equal(document.querySelector('[data-pane-count-button="1"]').getAttribute('aria-pressed'), 'true');
  assert.equal(vm.runInContext('state.slots[0]?.name', h.context), 'assistant');
  assert.equal(vm.runInContext('state.slotViewModes[0]', h.context), 'chat');
  assert.equal(vm.runInContext('JSON.stringify(state.slots.slice(1).map(Boolean))', h.context), '[false,false,false]');
  assert.equal(document.getElementById('cell-1').classList.contains('workspace-hidden'), true);
});

test('without a unique composite the first visit is one empty pane', async () => {
  for (const sessions of [[ordinary('worker')], [composite('one'), composite('two')]]) {
    const h = installRenderer({ questionOverride: null, freshWorkspace: true, initialSessions: sessions });
    await settle();
    assert.equal(h.dom.window.document.querySelector('.grid').dataset.paneCount, '1');
    assert.equal(vm.runInContext('JSON.stringify(state.slots.map(Boolean))', h.context), '[false,false,false,false]');
  }
});

test('a saved choice always wins over the assistant default', async () => {
  const h = installRenderer({ questionOverride: null, initialSessions: [composite(), ordinary('worker')],
    storage: { [WORKSPACE_STORAGE_KEY]: JSON.stringify({ paneCount: 4, activeSlot: 0 }) } });
  await settle();
  assert.equal(h.dom.window.document.querySelector('.grid').dataset.paneCount, '4');
  assert.equal(vm.runInContext('JSON.stringify(state.slots.map(Boolean))', h.context), '[false,false,false,false]');
});

test('saved bindings and drafts restore only from the daemon inventory', async () => {
  const session = ordinary(RACE);
  const h = installRenderer({ questionOverride: null, initialSessions: [session], storage: {
    [WORKSPACE_STORAGE_KEY]: JSON.stringify({ paneCount: 3, activeSlot: 2, bindings: [
      { name: session.session_name, hostId: 'local', mode: 'chat', draft: 'Unsent draft' },
      { name: 'closed-session', hostId: 'local', mode: 'chat' },
    ] }),
  } });
  await settle();
  assert.equal(vm.runInContext('state.slots[0]?.name', h.context), session.session_name);
  assert.equal(vm.runInContext('state.slots[1]', h.context), null);
  assert.equal(vm.runInContext('state.workspace.paneCount', h.context), 3);
  assert.equal(vm.runInContext('state.workspace.activeSlot', h.context), 2);
  assert.equal(h.dom.window.document.querySelector('#cell-0 .slot-chat-compose-input').value, 'Unsent draft');
  assert.equal(h.closeCalls.length, 0);
});

test('pane-count controls persist and hide panes without closing sessions', () => {
  const h = installRenderer({ questionOverride: null });
  mountRaceSlot(h.context);
  const { document, localStorage } = h.dom.window;
  for (const count of [4, 3, 2, 1]) {
    const control = document.querySelector(`[data-pane-count-button="${count}"]`);
    control.click();
    assert.equal(document.querySelector('.grid').dataset.paneCount, String(count));
    assert.equal(control.getAttribute('aria-pressed'), 'true');
    assert.equal(JSON.parse(localStorage.getItem(WORKSPACE_STORAGE_KEY)).paneCount, count);
    assert.equal(document.querySelectorAll('.grid-cell:not(.workspace-hidden)').length, count);
    assert.equal(vm.runInContext('state.slots[0].name', h.context), RACE);
  }
  assert.equal(h.closeCalls.length, 0);
  assert.equal(h.killCalls.length, 0);
});

test('reselecting an open chat focuses its composer, not the hidden terminal', () => {
  const h = installRenderer({ questionOverride: null });
  mountRaceSlot(h.context);
  vm.runInContext(`state.terminals[0] = { term: { focus() { throw Error('hidden xterm focused'); } } };
    assignToSlot(${JSON.stringify(RACE)}, 'Race Fixture', 'local');`, h.context);
  assert.equal(h.dom.window.document.activeElement, h.dom.window.document.querySelector('.slot-chat-compose-input'));
});

test('only Terminal view hands the wheel to tmux', () => {
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

test('the sidebar toggle collapses the sidebar and reports its state', () => {
  const h = installRenderer({ questionOverride: null });
  const { document } = h.dom.window;
  const toggle = document.getElementById('sidebar-toggle');
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  toggle.click();
  assert.equal(document.body.classList.contains('sidebar-collapsed'), true);
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
});

const directTarget = { sourceStreamId: `${HOST}:assistant`, streamId: `${HOST}:worker`, generation: 'g1' };
const directWorker = (generation = 'g1') => ({ ...ordinary('worker'), session_generation: generation, online: true, host_status: 'online' });
const inventory = (h, sessions) => vm.runInContext(`applyChatStreamState(${JSON.stringify({ connected: true, sessions })})`, h.context);
const savedBindings = h => JSON.parse(h.dom.window.localStorage.getItem(WORKSPACE_STORAGE_KEY)).bindings;

const unmappedComposite = extra => composite('assistant', {
  host: 'composite-host', stream_id: 'composite-host:assistant', ...extra,
});
const legacyWorkspace = (bindings = [{ name: 'assistant', hostId: 'local', mode: 'chat', draft: 'Unsent composite draft' }]) => ({
  [WORKSPACE_STORAGE_KEY]: JSON.stringify({ paneCount: 2, activeSlot: 1, visibleSlots: [1, 3],
    bindings: [null, ...bindings] }),
});

test('legacy composite bindings retain slot identity, chat and draft across reloads', async () => {
  for (const source of [composite(), unmappedComposite()]) {
    let storage = legacyWorkspace();
    for (let reload = 0; reload < 3; reload++) {
      const h = installRenderer({ questionOverride: null, storage,
        features: { assistantDirectTarget: { ...directTarget, sourceStreamId: source.stream_id } },
        initialSessions: [source, directWorker()] });
      await settle();
      assert.equal(vm.runInContext('chatSessionStateForSession(state.slots[1])?.stream_id', h.context), source.stream_id);
      assert.equal(vm.runInContext('state.slots[1]?.assistantDirect', h.context), null, 'legacy binding stays composite');
      assert.equal(vm.runInContext('state.slotViewModes[1]', h.context), 'chat');
      assert.equal(vm.runInContext('JSON.stringify(state.workspace.visibleSlots)', h.context), '[1,3]');
      assert.equal(vm.runInContext('state.workspace.activeSlot', h.context), 1);
      assert.equal(vm.runInContext('state.workspace.paneCount', h.context), 2);
      assert.equal(vm.runInContext('state.slots.filter(Boolean).length', h.context), 1);
      assert.equal(h.dom.window.document.querySelector('#cell-1 .slot-chat-compose-input').value, 'Unsent composite draft');
      assert.ok(h.dom.window.document.querySelector('#cell-1 .slot-chat-list').textContent.trim());
      assert.deepEqual(savedBindings(h)[1], { name: 'assistant', hostId: 'local', mode: 'chat', draft: 'Unsent composite draft' });
      storage = { [WORKSPACE_STORAGE_KEY]: h.dom.window.localStorage.getItem(WORKSPACE_STORAGE_KEY) };
      assert.equal(h.sendCalls.length, 0);
      assert.equal(h.closeCalls.length, 0);
    }
  }
});

test('composite restore rejects missing, hidden, closed and ambiguous inventory', async () => {
  for (const sessions of [
    [], [unmappedComposite({ visibility: 'hidden' })], [unmappedComposite({ visibility: 'unknown' })],
    [composite('assistant', { visibility: 'hidden' })], [composite('assistant', { status: 'closed' })],
    [unmappedComposite({ status: 'closed' })], [unmappedComposite({ closed_at: '2026-01-01' })],
    ...[{}, { visibility: 'hidden' }, { status: 'closed' }].map(extra => [unmappedComposite(),
      composite('assistant', { host: 'other-composite', stream_id: 'other-composite:assistant', ...extra })]),
    [composite(), unmappedComposite({ visibility: 'hidden' })],
    [ordinary('assistant')].map(session => ({ ...session, host: 'unmapped', stream_id: 'unmapped:assistant' })),
  ]) {
    const h = installRenderer({ questionOverride: null, storage: legacyWorkspace(),
      initialSessions: [...sessions, ordinary('control')] });
    await settle();
    assert.equal(vm.runInContext('state.slots.filter(Boolean).length', h.context), 0, JSON.stringify(sessions));
    assert.equal(savedBindings(h)[1], null);
  }
});

test('duplicate legacy composite bindings open only once', async () => {
  const binding = { name: 'assistant', hostId: 'local', mode: 'chat' };
  const h = installRenderer({ questionOverride: null, initialSessions: [unmappedComposite()],
    storage: legacyWorkspace([binding, binding]) });
  await settle();
  assert.equal(vm.runInContext('chatSessionStateForSession(state.slots[1])?.stream_id', h.context), 'composite-host:assistant');
  assert.equal(vm.runInContext('state.slots.filter(Boolean).length', h.context), 1);
  assert.equal(savedBindings(h)[2], null);
});

test('an explicit unmapped assistant source still validates its direct target generation', async () => {
  for (const generation of ['g1', 'g2']) {
    const source = unmappedComposite();
    const h = installRenderer({ questionOverride: null, initialSessions: [source, directWorker(generation)],
      features: { assistantDirectTarget: { ...directTarget, sourceStreamId: source.stream_id } },
      storage: legacyWorkspace([{ name: 'worker', hostId: 'local', mode: 'chat', assistantSourceId: source.stream_id }]) });
    await settle();
    assert.equal(vm.runInContext('chatSessionStateForSession(state.slots[1])?.stream_id', h.context),
      generation === 'g1' ? directTarget.streamId : undefined);
    assert.equal(savedBindings(h)[1]?.assistantSourceId, generation === 'g1' ? source.stream_id : undefined);
  }
});

test('first visit opens a configured direct assistant exactly as its sidebar row does', async () => {
  const h = installRenderer({ questionOverride: null, freshWorkspace: true,
    features: { assistantDirectTarget: directTarget }, initialSessions: [composite(), directWorker()] });
  await settle();
  assert.equal(vm.runInContext('state.slots[0]?.name', h.context), 'worker');
  assert.equal(vm.runInContext('JSON.stringify(state.slots[0]?.assistantDirect)', h.context),
    JSON.stringify({ sourceId: `${HOST}:assistant`, targetId: `${HOST}:worker`, generation: 'g1' }));
  assert.equal(vm.runInContext('state.slotViewModes[0]', h.context), 'chat');
});

test('the first-visit default waits for a composite that arrives after other sessions', async () => {
  const h = installRenderer({ questionOverride: null, freshWorkspace: true, initialSessions: [ordinary('worker')] });
  await settle();
  assert.equal(vm.runInContext('state.slots[0]', h.context), null);
  inventory(h, [ordinary('worker'), composite()]);
  await settle();
  assert.equal(vm.runInContext('state.slots[0]?.name', h.context), 'assistant');
  assert.equal(h.dom.window.document.querySelector('.grid').dataset.paneCount, '1');
});

test('a user choice before the composite arrives cancels the first-visit default', async () => {
  for (const act of ["assignToSlot('worker', 'Worker', 'local')", 'setPaneCount(2)']) {
    const h = installRenderer({ questionOverride: null, freshWorkspace: true, initialSessions: [ordinary('worker')] });
    await settle();
    vm.runInContext(act, h.context);
    inventory(h, [ordinary('worker'), composite()]);
    await settle();
    assert.equal(vm.runInContext("state.slots.some(slot => slot?.name === 'assistant')", h.context), false, act);
  }
});

test('a direct-assistant pane persists through its assistant entry with its draft', async () => {
  const h = installRenderer({ questionOverride: null, freshWorkspace: true,
    features: { assistantDirectTarget: directTarget }, initialSessions: [composite(), directWorker()] });
  await settle();
  const input = h.dom.window.document.querySelector('#cell-0 .slot-chat-compose-input');
  input.value = 'Unsent to the assistant';
  input.dispatchEvent(new h.dom.window.Event('input', { bubbles: true }));
  assert.deepEqual(savedBindings(h)[0],
    { name: 'worker', hostId: 'local', mode: 'chat', draft: 'Unsent to the assistant', assistantSourceId: `${HOST}:assistant` });
});

test('a saved direct-assistant pane reopens only through a valid binding; otherwise it is retired', async () => {
  const storage = { [WORKSPACE_STORAGE_KEY]: JSON.stringify({ paneCount: 1, activeSlot: 0, bindings: [
    { name: 'worker', hostId: 'local', mode: 'chat', draft: 'Keep me', assistantSourceId: `${HOST}:assistant` }] }) };
  const ok = installRenderer({ questionOverride: null, storage,
    features: { assistantDirectTarget: directTarget }, initialSessions: [composite(), directWorker()] });
  await settle();
  assert.equal(vm.runInContext('state.slots[0]?.assistantDirect?.sourceId', ok.context), `${HOST}:assistant`);
  assert.equal(ok.dom.window.document.querySelector('#cell-0 .slot-chat-compose-input').value, 'Keep me');

  // The target moved to another generation: no stale target opens, the pane
  // is retired with its draft, and a later valid inventory does not reopen it.
  const stale = installRenderer({ questionOverride: null, storage,
    features: { assistantDirectTarget: directTarget }, initialSessions: [composite(), directWorker('g2')] });
  await settle();
  assert.equal(vm.runInContext('state.slots[0]', stale.context), null);
  assert.equal(savedBindings(stale)[0], null);
  inventory(stale, [composite(), directWorker('g1')]);
  await settle();
  assert.equal(vm.runInContext('state.slots[0]', stale.context), null, 'restore runs once');

  // The assistant entry itself is gone from a non-empty inventory: retired.
  const gone = installRenderer({ questionOverride: null, storage,
    features: { assistantDirectTarget: directTarget }, initialSessions: [directWorker()] });
  await settle();
  assert.equal(vm.runInContext('state.slots[0]', gone.context), null);
  assert.equal(savedBindings(gone)[0], null);
});

test('a session the user opened before restore is never opened twice', async () => {
  // A hidden saved pane, then the user opens the same session in the visible
  // pane before the first non-empty inventory arrives: the user's pane wins.
  const session = ordinary(RACE);
  const plain = installRenderer({ questionOverride: null, initialSessions: [], storage: {
    [WORKSPACE_STORAGE_KEY]: JSON.stringify({ paneCount: 1, activeSlot: 1, visibleSlots: [1], bindings: [
      { name: session.session_name, hostId: 'local', mode: 'chat', draft: 'Old draft' }] }) } });
  await settle();
  vm.runInContext(`assignToSlot(${JSON.stringify(session.session_name)}, 'Race', 'local')`, plain.context);
  inventory(plain, [session]);
  await settle();
  assert.equal(vm.runInContext(`state.slots.filter(slot => slot?.name === ${JSON.stringify(session.session_name)}).length`, plain.context), 1);
  assert.equal(vm.runInContext('state.slots[0]', plain.context), null);
  assert.equal(vm.runInContext('state.slots[1]?.name', plain.context), session.session_name);
  assert.equal(savedBindings(plain)[0], null);

  // The same journey for a direct-assistant pane: its resolved target is
  // already open, so the saved pane is retired rather than pinned again.
  const direct = installRenderer({ questionOverride: null, initialSessions: [],
    features: { assistantDirectTarget: directTarget }, storage: {
      [WORKSPACE_STORAGE_KEY]: JSON.stringify({ paneCount: 1, activeSlot: 1, visibleSlots: [1], bindings: [
        { name: 'worker', hostId: 'local', mode: 'chat', draft: 'Old draft', assistantSourceId: `${HOST}:assistant` }] }) } });
  await settle();
  vm.runInContext("assignToSlot('worker', 'Worker', 'local')", direct.context);
  inventory(direct, [composite(), directWorker()]);
  await settle();
  assert.equal(vm.runInContext("state.slots.filter(slot => slot?.name === 'worker').length", direct.context), 1);
  assert.equal(vm.runInContext('state.slots[0]', direct.context), null);
  assert.equal(vm.runInContext('state.slots[1]?.assistantDirect || null', direct.context), null, "the user's ordinary pane is untouched");

  // Two saved panes for one session open it once, in the first.
  const twice = installRenderer({ questionOverride: null, initialSessions: [session], storage: {
    [WORKSPACE_STORAGE_KEY]: JSON.stringify({ paneCount: 2, activeSlot: 0, bindings: [
      { name: session.session_name, hostId: 'local', mode: 'chat' }, { name: session.session_name, hostId: 'local', mode: 'chat' }] }) } });
  await settle();
  assert.equal(vm.runInContext('state.slots[0]?.name', twice.context), session.session_name);
  assert.equal(vm.runInContext('state.slots[1]', twice.context), null);
});

test('an empty first inventory does not erase saved panes', async () => {
  const session = ordinary(RACE);
  const h = installRenderer({ questionOverride: null, initialSessions: [], storage: {
    [WORKSPACE_STORAGE_KEY]: JSON.stringify({ paneCount: 2, activeSlot: 0, bindings: [
      { name: session.session_name, hostId: 'local', mode: 'chat', draft: 'Still here' }] }),
  } });
  await settle();
  assert.equal(vm.runInContext('state.slots[0]', h.context), null);
  vm.runInContext('setPaneCount(3)', h.context);
  assert.equal(savedBindings(h)[0].draft, 'Still here');
  inventory(h, [session]);
  await settle();
  assert.equal(vm.runInContext('state.slots[0]?.name', h.context), session.session_name);
  assert.equal(h.dom.window.document.querySelector('#cell-0 .slot-chat-compose-input').value, 'Still here');
});
