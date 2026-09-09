'use strict';

// Slot-chat DOM render test for the session status card
// (public_behavior_spec). Modeled on
// renderer_question_dismiss_race.test.js: load renderer/app.js into JSDOM,
// bind a synthetic stream session to slot 0, call renderSlotChat directly,
// and assert the card DOM between the hero and the transcript.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const { JSDOM } = require('jsdom');

const root = path.join(__dirname, '..');
const rendererRequire = createRequire(path.join(root, 'renderer', 'app.js'));
const STREAM = 'hostc:claude-hostc-card';

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

function installRenderer() {
  const html = fs.readFileSync(path.join(root, 'renderer', 'index.html'), 'utf8');
  const dom = new JSDOM(html, { url: 'https://example.local/renderer/index.html' });
  const config = {
    appName: 'Pentacle',
    terminal: {},
    chatStream: { recentLimit: 5000, hostMap: { local: 'hostc' } },
    hostNames: { local: 'hostc' },
    hostColors: { local: 'royal-blue' },
    features: {
      chatUi: true,
      inputBar: true,
      dashboards: false,
      usage: false,
      mic: false,
      sourceTags: false,
      showTurnDuration: false,
    },
  };

  dom.window.PentacleChatStore = {
    selectSessionDetail(streamId) {
      return {
        streamId,
        title: 'Card Fixture',
        providerLabel: 'claude',
        transcriptItems: [],
      };
    },
    getQuestion() {
      return null;
    },
    getTurnPhase() {
      return 'idle';
    },
    sendTurn() {
      return 'optimistic-test';
    },
  };
  dom.window.PentacleChatView = {
    renderTranscriptTimelineHtml() {
      return '<article class="slot-chat-row"><div class="slot-chat-assistant-card">Ready</div></article>';
    },
  };
  dom.window.PentacleChatCore = { buildPentacleQuestionAnswerText: () => 'ok' };
  dom.window.cc = {
    perfRecord() {},
    getConfig: async () => ({ isClient: false, hostIds: ['local'], ...config }),
    getChatStreamState: async () => ({ connected: true, events: [], sessions: [], schedules: [] }),
    getLimits: async () => [],
    getMachineStats: async () => ({}),
    requestStreamEvents: async () => ({ ok: true, events: [] }),
    promptList: async () => ({ ok: true, questions: [] }),
    onPtyData() {},
    onPtyExit() {},
    onAssignSlot() {},
    onAction() {},
    onChatStreamFrame() {},
    showContextMenu() {},
  };
  dom.window.HOST = {
    hostname: 'example.local',
    platform: 'darwin',
    isClient: false,
    hasRemote: false,
    hasDashboardHub: false,
  };
  dom.window.DASHBOARDS = [];

  const context = {
    Buffer,
    console,
    document: dom.window.document,
    global: dom.window,
    localStorage: dom.window.localStorage,
    Node: dom.window.Node,
    process: { ...process, env: { ...process.env, NODE_ENV: 'test' } },
    requestAnimationFrame: (fn) => {
      fn();
      return 0;
    },
    setInterval: () => 0,
    clearInterval() {},
    setTimeout: (fn) => {
      fn();
      return 0;
    },
    clearTimeout() {},
    window: dom.window,
    __dirname: path.join(root, 'renderer'),
    __filename: path.join(root, 'renderer', 'app.js'),
    require(name) {
      if (name === '@xterm/xterm') return { Terminal: class { loadAddon() {} open() {} write() {} dispose() {} } };
      if (name === '@xterm/addon-fit') return { FitAddon: class { fit() {} } };
      if (name === '@xterm/addon-unicode11') return { Unicode11Addon: class {} };
      if (name === '@xterm/addon-webgl') return { WebglAddon: class {} };
      if (name === 'electron') return { clipboard: { readText: () => '', writeText() {} } };
      if (name === '../config-loader') return { loadConfig: () => ({ config }) };
      return rendererRequire(name);
    },
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, 'renderer', 'app.js'), 'utf8'), context, {
    filename: 'renderer/app.js',
  });
  return { context, dom };
}

function mountCardSlot(context, sessionExtras) {
  vm.runInContext(`
    ensureSlotChatSurface(0);
    state.slots[0] = { name: 'claude-hostc-card', displayName: 'Card Fixture', hostId: 'local' };
    state.slotViewModes[0] = 'chat';
    state.chatStream.connected = true;
    state.chatStream.sessions = [{
      stream_id: ${JSON.stringify(STREAM)},
      host: 'hostc',
      provider: 'claude',
      session_id: 'claude-hostc-card',
      session_name: 'claude-hostc-card',
      name: 'claude-hostc-card',
      title: 'Card Fixture',
      last_text: '',
      last_kind: 'ASSIST',
      last_event_at: new Date().toISOString(),
      working: false,
      ...${JSON.stringify(sessionExtras)},
    }];
    renderSlotChat(0);
  `, context);
}

test('desktop mixed-provider chat labels follow each session canonical provider', async () => {
  const { context, dom } = installRenderer();
  await flush(); await flush();

  vm.runInContext(`
    state.chatStream.connected = true;
    state.chatStream.sessions = [];
    state.slots[0] = { name: 'session-a', displayName: 'Session A', hostId: 'local' };
    state.slots[1] = { name: 'session-b', displayName: 'Session B', hostId: 'local' };
    state.slotViewModes[0] = 'chat';
    state.slotViewModes[1] = 'chat';
    ensureSlotChatSurface(0);
    ensureSlotChatSurface(1);
    updateSlotProviderTag(0);
    updateSlotProviderTag(1);
    state.chatStream.sessions = [
      { stream_id: 'hostc:session-a', host: 'hostc', session_name: 'session-a', name: 'session-a', provider: 'codex', working: false, last_text: '', last_kind: 'ASSIST' },
      { stream_id: 'hostc:session-b', host: 'hostc', session_name: 'session-b', name: 'session-b', provider: 'claude', working: false, last_text: '', last_kind: 'ASSIST' },
    ];
    renderSlotChat(0);
    renderSlotChat(1);
  `, context);

  assert.equal(dom.window.document.querySelector('#header-0 .cell-provider-tag').textContent, 'Codex');
  assert.equal(dom.window.document.querySelector('#header-1 .cell-provider-tag').textContent, 'Claude');
  assert.equal(dom.window.document.querySelector('#cell-0 .slot-chat-session-provider').textContent, 'Codex');
  assert.equal(dom.window.document.querySelector('#cell-1 .slot-chat-session-provider').textContent, 'Claude');
});

// ── Session Status card-view surface (public_behavior_spec) ──
// The card overlay hides the chat body via the shell's `is-status-card-open`
// class (CSS display:none !important), never per-element inline display — so a
// direct display mutation on any body element cannot leak through or corrupt a
// restore. These tests assert the class + that inline displays are untouched.

const glyphOf = (document) => document.querySelector('#header-0 .cell-status');
const shellOf = (document) => document.querySelector('#cell-0 .slot-chat-shell');
const cardOf = (document) => document.querySelector('#cell-0 .slot-chat-status-card-view .session-status-card');
const isOpen = (document) => !!shellOf(document) && shellOf(document).classList.contains('is-status-card-open');

const FULL_CARD = {
  status_card: {
    goal: 'Demonstrate the session status card',
    plan: [
      { text: 'sample update', status: 'done' },
      { text: 'public UI', status: 'active' },
      { text: 'run checks', status: 'pending' },
    ],
    update: 'sample update merged; wiring the desktop toggle',
    handoff_planned: true,
    updated_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
  },
  context_tokens: 271500,
  model_context_window: 1000000,
  context_level: 'advisory',
  spec_issues: [{ obligation_id: 'ob-1', detail: 'metadata mismatch', set_at: new Date(Date.now() - 20 * 60 * 1000).toISOString() }],
};

function bodyDisplays(document) {
  const out = {};
  for (const sel of ['.slot-chat-scroll', '.slot-chat-status', '.slot-chat-draft-preview-host', '.slot-chat-error', '.slot-chat-question', '.slot-chat-attachment-tray', '.slot-chat-compose']) {
    const el = document.querySelector(`#cell-0 ${sel}`);
    out[sel] = el ? el.style.display : '<absent>';
  }
  return out;
}

test('the shipped CSS hides the chat body via the is-status-card-open class', () => {
  const css = fs.readFileSync(path.join(root, 'renderer', 'styles.css'), 'utf8');
  for (const cls of ['slot-chat-scroll', 'slot-chat-status', 'slot-chat-draft-preview-host', 'slot-chat-error', 'slot-chat-question', 'slot-chat-attachment-tray', 'slot-chat-compose']) {
    assert.match(css, new RegExp(`\\.slot-chat-shell\\.is-status-card-open > \\.${cls}`), `CSS hides .${cls} while open`);
  }
  assert.match(css, /is-status-card-open > \.slot-chat-scroll[\s\S]*?display:\s*none\s*!important/);
  assert.match(css, /\.slot-chat-shell\.is-status-card-open > \.slot-chat-status-card-view\s*\{\s*display:\s*block/);
});

test('header glyph toggles the full card view; every section renders; body hidden via class', async () => {
  const { context, dom } = installRenderer();
  await flush(); await flush();
  mountCardSlot(context, FULL_CARD);
  const document = dom.window.document;

  const glyph = glyphOf(document);
  assert.ok(glyph && glyph.style.display !== 'none', 'glyph visible with content');
  assert.equal(glyph.disabled, false);
  assert.ok(glyph.classList.contains('has-attention'), 'closed glyph shows attention');
  assert.equal(isOpen(document), false, 'closed initially');
  assert.equal(cardOf(document), null, 'no card content while closed');

  glyph.click();
  const card = cardOf(document);
  assert.ok(card, 'card renders on toggle');
  assert.equal(isOpen(document), true, 'shell marked open (CSS hides body)');
  assert.ok(glyph.classList.contains('is-open'));
  assert.ok(!glyph.classList.contains('has-attention'), 'no attention dot while open');
  assert.equal(glyph.getAttribute('aria-pressed'), 'true');

  assert.match(card.querySelector('.is-goal .session-status-goal-text').textContent, /Ship the session status card/);
  assert.equal(card.querySelector('.session-status-rollup').textContent, 'Step 2 of 3');
  assert.equal(card.querySelector('.session-status-progress-fill').style.width, '33%');
  const steps = card.querySelectorAll('.session-status-step');
  assert.equal(steps.length, 3);
  assert.ok(steps[0].querySelector('.session-status-step-glyph.is-done'));
  assert.ok(steps[1].classList.contains('is-active') && steps[1].querySelector('.session-status-step-glyph.is-active'));
  assert.ok(steps[2].querySelector('.session-status-step-glyph.is-pending'));
  assert.match(card.querySelector('.is-update .session-status-update-panel').textContent, /sample update merged/);
  assert.ok(card.querySelector('.session-status-context.is-ctx-advisory'));
  assert.match(card.querySelector('.session-status-context').textContent, /272k · 27%/);
  assert.ok(card.querySelector('.session-status-handoff'));
  const issue = card.querySelector('.is-spec-issues .session-status-issue-card');
  assert.match(issue.querySelector('.session-status-issue-id').textContent, /ob-1/);
  assert.match(issue.querySelector('.session-status-issue-detail').textContent, /metadata mismatch/);

  glyph.click();
  assert.equal(isOpen(document), false, 'closed after toggle');
  assert.equal(cardOf(document), null, 'card content cleared on close');
  assert.ok(!glyph.classList.contains('is-open'));
});

test('overlay hides via class only — never sets inline display on body elements (no stash to go stale)', async () => {
  const { context, dom } = installRenderer();
  await flush(); await flush();
  mountCardSlot(context, FULL_CARD);
  const document = dom.window.document;
  glyphOf(document).click(); // open
  assert.equal(isOpen(document), true);
  // The overlay must NOT have written display:none onto any body element — hiding
  // is entirely via the shell class, so there is no per-element snapshot that a
  // later async mutation (renderSlotAttachments / setSlotSendError) could stale.
  for (const sel of ['.slot-chat-scroll', '.slot-chat-status', '.slot-chat-draft-preview-host', '.slot-chat-error', '.slot-chat-question', '.slot-chat-attachment-tray', '.slot-chat-compose']) {
    const el = document.querySelector(`#cell-0 ${sel}`);
    // No per-element overlay snapshot exists (the old stash mechanism is gone).
    assert.equal(el ? el.dataset.cardPrevDisplay : undefined, undefined, `${sel} carries no overlay snapshot`);
  }
  // The always-visible body elements are NOT inline-hidden by the overlay
  // (hiding is via the shell class), so nothing can be stale-restored.
  for (const sel of ['.slot-chat-scroll', '.slot-chat-compose']) {
    assert.notEqual(document.querySelector(`#cell-0 ${sel}`).style.display, 'none', `${sel} not inline-hidden by the overlay`);
  }
  // A direct async mutation while open cannot clear/flip the overlay.
  vm.runInContext("state.slotChatRefs[0].attachmentTrayEl.style.display = 'flex';", context);
  assert.equal(isOpen(document), true, 'overlay still open after a direct body mutation');
  glyphOf(document).click(); // close
  assert.equal(isOpen(document), false);
});

test('close restores a subregion that became visible WHILE open (render-driven, no stale re-hide)', async () => {
  // Baseline: same session WITH the question present, card never opened.
  const baseInstall = installRenderer();
  await flush(); await flush();
  baseInstall.dom.window.PentacleChatStore.getQuestion = () => ({ question_key: 'k1', prompt: 'Pick', options: [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }] });
  mountCardSlot(baseInstall.context, FULL_CARD);
  const baseline = bodyDisplays(baseInstall.dom.window.document);
  assert.notEqual(baseline['.slot-chat-question'], 'none', 'baseline: question visible inline');

  const { context, dom } = installRenderer();
  await flush(); await flush();
  const document = dom.window.document;
  dom.window.PentacleChatStore.getQuestion = () => null;
  mountCardSlot(context, FULL_CARD);
  glyphOf(document).click(); // open before the question exists
  assert.equal(isOpen(document), true);
  dom.window.PentacleChatStore.getQuestion = () => ({ question_key: 'k1', prompt: 'Pick', options: [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }] });
  vm.runInContext('renderSlotChat(0);', context); // question arrives while open
  assert.equal(isOpen(document), true, 'still open (question hidden by class)');
  glyphOf(document).click(); // close
  assert.equal(isOpen(document), false);
  assert.deepEqual(bodyDisplays(document), baseline, 'every body element matches the never-opened baseline after close');
});

test('all-done plan shows n/n rollup and a full progress bar', async () => {
  const { context, dom } = installRenderer();
  await flush(); await flush();
  mountCardSlot(context, { status_card: { goal: 'g', plan: [{ text: 'a', status: 'done' }, { text: 'b', status: 'done' }], updated_at: new Date().toISOString() } });
  glyphOf(dom.window.document).click();
  const card = cardOf(dom.window.document);
  assert.equal(card.querySelector('.session-status-rollup').textContent, '2/2 done');
  assert.equal(card.querySelector('.session-status-progress-fill').style.width, '100%');
});

test('plan roll-up uses the active step index, not done+1', async () => {
  const { context, dom } = installRenderer();
  await flush(); await flush();
  mountCardSlot(context, { status_card: { goal: 'g', plan: [{ text: 'a', status: 'pending' }, { text: 'b', status: 'active' }], updated_at: new Date().toISOString() } });
  glyphOf(dom.window.document).click();
  assert.equal(cardOf(dom.window.document).querySelector('.session-status-rollup').textContent, 'Step 2 of 2');
});

test('partial card hides absent plan/update/spec sections (no placeholders)', async () => {
  const { context, dom } = installRenderer();
  await flush(); await flush();
  mountCardSlot(context, { status_card: { goal: 'just a goal', updated_at: new Date().toISOString() } });
  glyphOf(dom.window.document).click();
  const card = cardOf(dom.window.document);
  assert.ok(card.querySelector('.is-goal'));
  assert.equal(card.querySelector('.is-plan'), null);
  assert.equal(card.querySelector('.is-update'), null);
  assert.equal(card.querySelector('.is-spec-issues'), null);
});

test('indicator-only session (no card) still toggles a card with just the indicators', async () => {
  const { context, dom } = installRenderer();
  await flush(); await flush();
  mountCardSlot(context, { context_tokens: 300000, model_context_window: 1000000, context_level: 'handoff', spec_issues: [{ obligation_id: 'ob-9', detail: 'obligation past expiry' }] });
  const document = dom.window.document;
  const glyph = glyphOf(document);
  assert.equal(glyph.disabled, false);
  assert.ok(glyph.classList.contains('has-attention'));
  glyph.click();
  const card = cardOf(document);
  assert.ok(card.querySelector('.session-status-context.is-ctx-handoff'));
  assert.ok(card.querySelector('.is-spec-issues'));
  assert.equal(card.querySelector('.is-goal'), null);
  assert.equal(card.querySelector('.is-plan'), null);
});

test('neither card nor indicators → neutral disabled glyph and no card', async () => {
  const { context, dom } = installRenderer();
  await flush(); await flush();
  mountCardSlot(context, {});
  const document = dom.window.document;
  const glyph = glyphOf(document);
  assert.equal(glyph.disabled, true);
  assert.ok(!glyph.classList.contains('has-attention'));
  glyph.click();
  assert.equal(cardOf(document), null);
  assert.equal(isOpen(document), false);
});

test('view-switch to terminal while open clears the overlay and hides the glyph', async () => {
  const { context, dom } = installRenderer();
  await flush(); await flush();
  mountCardSlot(context, FULL_CARD);
  const document = dom.window.document;
  glyphOf(document).click();
  assert.equal(isOpen(document), true);
  vm.runInContext("updateSlotViewMode(0, 'terminal');", context);
  assert.equal(isOpen(document), false, 'overlay cleared on view switch');
  assert.equal(glyphOf(document).style.display, 'none', 'glyph hidden outside chat view');
});

test('in-card close control closes the card', async () => {
  const { context, dom } = installRenderer();
  await flush(); await flush();
  mountCardSlot(context, FULL_CARD);
  const document = dom.window.document;
  glyphOf(document).click();
  const close = document.querySelector('#cell-0 .session-status-close');
  assert.ok(close, 'in-card close control present');
  close.click();
  assert.equal(isOpen(document), false);
  assert.ok(!glyphOf(document).classList.contains('is-open'));
});

test('attention affordance — one fixture per trigger + negatives', async () => {
  const { context, dom } = installRenderer();
  await flush(); await flush();
  const document = dom.window.document;
  const attn = () => glyphOf(document).classList.contains('has-attention');
  const enabled = () => glyphOf(document).disabled === false;
  mountCardSlot(context, { spec_issues: [{ obligation_id: 'x', detail: 'd' }] });
  assert.ok(enabled() && attn(), 'spec-issue -> attention');
  mountCardSlot(context, { status_card: { goal: 'g', handoff_planned: true, updated_at: new Date().toISOString() } });
  assert.ok(enabled() && attn(), 'handoff_planned -> attention');
  mountCardSlot(context, { context_tokens: 300000, model_context_window: 1000000, context_level: 'handoff' });
  assert.ok(enabled() && attn(), 'handoff level -> attention');
  mountCardSlot(context, { context_level: 'advisory' });
  assert.ok(enabled() && attn(), 'advisory level alone -> enabled + attention');
  mountCardSlot(context, { status_card: { goal: 'g', updated_at: new Date().toISOString() }, context_tokens: 40000, model_context_window: 1000000, context_level: 'none' });
  assert.ok(enabled() && !attn(), 'benign -> enabled, no attention');
});

