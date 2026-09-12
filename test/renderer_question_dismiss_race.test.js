'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const Module = require('node:module');
const esbuild = require('esbuild');
const { createRequire } = require('node:module');
const { JSDOM } = require('jsdom');

const root = path.join(__dirname, '..');
const rendererRequire = createRequire(path.join(root, 'renderer', 'app.js'));
const STREAM = 'hostc:claude-hostc-race';
const answerModule = new Module(path.join(root, 'pentacle-chat-core/src/services/questionAnswerFormat.ts'));
answerModule._compile(esbuild.buildSync({ entryPoints: [answerModule.id], bundle: true, platform: 'node', format: 'cjs', write: false, logLevel: 'silent' }).outputFiles[0].text, answerModule.id);
const { buildPentacleQuestionAnswerText } = answerModule.exports;


function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

function openDesktopQuestions(dom) {
  const open = dom.window.document.querySelector('.slot-chat-question-open');
  if (open) open.click();
  return dom.window.document.querySelector('.desktop-question-portal .slot-chat-question');
}

test('desktop question affordance opens a cosmic portal with desktop CSS contract', async () => {
  const { context, dom } = installRenderer();
  await flush();
  await flush();
  mountRaceSlot(context);

  const open = dom.window.document.querySelector('.slot-chat-question-open');
  assert.ok(open);
  assert.match(open.textContent, /unanswered/);
  open.click();

  const portal = dom.window.document.querySelector('.desktop-question-portal');
  assert.ok(portal);
  assert.equal(portal.parentElement, dom.window.document.body);
  assert.equal(portal.classList.contains('cosmic'), true);
  assert.ok(portal.querySelector('.desktop-question-portal__close'));
  assert.ok(portal.querySelector('.desktop-question-portal__body'));

  const css = fs.readFileSync(path.join(root, 'renderer', 'styles.css'), 'utf8');
  for (const selector of [
    '.desktop-question-portal',
    '.desktop-question-portal__body',
    '.desktop-question-dots',
    '.desktop-question-dot.is-active',
    '.desktop-question-dot.is-answered',
  ]) assert.match(css, new RegExp(selector.replaceAll('.', '\\.') + '\\s*\\{'));
  assert.match(css, /Desktop Questions Mono/);
  assert.match(css, /font-family:\s*'IBM Plex Sans'/);
  assert.match(css, /font-family:\s*'Desktop Questions Mono',\s*'JetBrains Mono'/);
});

test('desktop portal dots jump between questions and mark the active question', async () => {
  const { context, dom } = installRenderer({
    questionOverride: {
      multi: true,
      questions: [
        { index: 0, header: 'First', prompt: 'First choice', options: [{ index: 1, label: 'One' }] },
        { index: 1, header: 'Second', prompt: 'Second choice', options: [{ index: 1, label: 'Two' }] },
      ],
    },
  });
  await flush();
  await flush();
  mountRaceSlot(context);
  const portalQuestion = openDesktopQuestions(dom);
  const dots = [...dom.window.document.querySelectorAll('.desktop-question-dot')];
  assert.equal(dots.length, 2);
  assert.equal(dots[0].classList.contains('is-active'), true);
  dots[1].click();
  const refreshedDots = [...dom.window.document.querySelectorAll('.desktop-question-dot')];
  assert.equal(refreshedDots[1].classList.contains('is-active'), true);
  assert.match(portalQuestion.textContent, /First choice|Second choice/);
});

function installRenderer({ dismissResult, questionOverride, assistantRole = '' } = {}) {
  const html = fs.readFileSync(path.join(root, 'renderer', 'index.html'), 'utf8');
  const dom = new JSDOM(html, { url: 'http://pentacle.test/renderer/index.html' });
  const dismissCalls = [];
  const notificationResolveCalls = [];
  const sendCalls = [];
  const closeCalls = [];
  const killCalls = [];
  const renameCalls = [];
  const contextMenuCalls = [];
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
      ...(assistantRole ? { assistantRole } : {}),
    },
  };
  const question = {
    question_key: 'qkey-race',
    header: 'Pick',
    prompt: 'Pick a number',
    options: [
      { index: 1, label: 'One' },
      { index: 2, label: 'Two' },
    ],
  };
  const paneQuestion = questionOverride === undefined ? question : questionOverride;

  dom.window.PentacleChatCore = {
    buildPentacleQuestionAnswerText,
  };
  dom.window.PentacleChatStore = {
    selectSessionDetail(streamId) {
      return {
        streamId,
        title: 'Race Fixture',
        providerLabel: 'claude',
        transcriptItems: [],
      };
    },
    getQuestion(streamId) {
      return streamId === STREAM ? paneQuestion : null;
    },
    getTurnPhase() {
      return 'idle';
    },
    sendTurn(streamId, text, attachments) {
      sendCalls.push({ streamId, text, attachments });
      return 'optimistic-test';
    },
  };
  dom.window.PentacleChatView = {
    renderTranscriptTimelineHtml() {
      return '<article class="slot-chat-row"><div class="slot-chat-assistant-card">Ready</div></article>';
    },
  };
  dom.window.cc = {
    perfRecord() {},
    getConfig: async () => ({ isClient: false, hostIds: ['local'], ...config }),
    getChatStreamState: async () => ({ connected: true, events: [], sessions: [], schedules: [] }),
    listChatSessions: async () => ({ ok: true, active: [], trashed: [] }),
    getLimits: async () => [],
    getMachineStats: async () => ({}),
    requestStreamEvents: async () => ({ ok: true, events: [] }),
    chatDismissQuestion: async (hostId, sessionName, options) => {
      dismissCalls.push({ hostId, sessionName, options });
      return dismissResult || { ok: false, error_code: 'stale_question', error: 'stale' };
    },
    promptList: async () => ({ ok: true, questions: [] }),
    notificationResolve: async (notificationId, actionKind, options) => {
      notificationResolveCalls.push({ notificationId, actionKind, options });
      return {
        ok: true,
        notification: {
          notification_id: notificationId,
          producer: 'agent_question.v1',
          state: 'answered',
          question: { state: 'answered' },
        },
      };
    },
    onPtyData() {},
    onPtyExit() {},
    onAssignSlot() {},
    onAction() {},
    onChatStreamFrame() {},
    showContextMenu() {},
    chatRename: async (...args) => { renameCalls.push({ kind: 'chat', args }); return { ok: true }; },
    setWindowTitle: async (...args) => { renameCalls.push({ kind: 'terminal', args }); return { ok: true }; },
    chatClose: async (...args) => { closeCalls.push(args); return { ok: true }; },
    chatKill: async (...args) => { killCalls.push(args); return { ok: true }; },
    killTmuxSession: async (...args) => { killCalls.push(args); return { ok: true }; },
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
  dom.window.cc.showContextMenu = (...args) => contextMenuCalls.push(args);
  return { context, dismissCalls, notificationResolveCalls, sendCalls, closeCalls, killCalls, renameCalls, contextMenuCalls, dom };
}

function mountRaceSlot(context) {
  vm.runInContext(`
    ensureSlotChatSurface(0);
    state.slots[0] = { name: 'claude-hostc-race', displayName: 'Race Fixture', hostId: 'local' };
    state.slotViewModes[0] = 'chat';
    state.chatStream.connected = true;
    state.chatStream.sessions = [{
      stream_id: ${JSON.stringify(STREAM)},
      host: 'hostc',
      provider: 'claude',
      session_id: 'claude-hostc-race',
      session_name: 'claude-hostc-race',
      name: 'claude-hostc-race',
      title: 'Race Fixture',
      last_text: '',
      last_kind: 'ASSIST',
      last_event_at: new Date().toISOString(),
      working: false,
    }];
    renderSlotChat(0);
  `, context);
}

test('mobile parity: a pane group cannot submit until every page is answered', async () => {
  const h = installRenderer({ dismissResult: { ok: true }, questionOverride: {
    question_key: 'complete-group', multi: true,
    questions: [
      { index: 0, header: 'Color', prompt: 'Choose a color', options: [{ index: 1, label: 'Green' }] },
      { index: 1, header: 'Size', prompt: 'Choose a size', options: [{ index: 1, label: 'Large' }] },
    ],
  } });
  await flush(); await flush(); mountRaceSlot(h.context);
  openDesktopQuestions(h.dom);
  h.dom.window.document.querySelector('.slot-chat-question-option[data-option="1"]').click();
  assert.equal(h.dom.window.document.querySelector('.slot-chat-question-submit').disabled, true);
  assert.equal(h.dismissCalls.length, 0);
  h.dom.window.document.querySelectorAll('.desktop-question-dot')[1].click();
  h.dom.window.document.querySelector('.slot-chat-question-option[data-option="1"]').click();
  h.dom.window.document.querySelector('.slot-chat-question-submit').click();
  await flush(); await flush();
  assert.equal(h.dismissCalls.length, 1);
  assert.equal(h.dismissCalls[0].options.questionKey, 'complete-group');
  assert.match(h.dismissCalls[0].options.text, /Q1 \(Color\): Green/);
  assert.match(h.dismissCalls[0].options.text, /Q2 \(Size\): Large/);
  h.dom.window.close();
});

test('mobile parity: pending history is loading and an RPC failure remains retryable', async () => {
  const h = installRenderer({ questionOverride: null });
  await flush(); await flush();
  let finish;
  h.dom.window.cc.requestStreamEvents = () => new Promise(resolve => { finish = resolve; });
  h.dom.window.PentacleChatView.renderTranscriptTimelineHtml = () => '';
  mountRaceSlot(h.context);
  assert.match(h.dom.window.document.querySelector('.slot-chat-empty').textContent, /Loading/);
  finish({ ok: false, error: 'History temporarily unavailable' });
  await flush(); await flush();
  assert.match(h.dom.window.document.querySelector('.slot-chat-empty').textContent, /could not|unable/i);
  assert.ok(h.dom.window.document.querySelector('.slot-chat-history-retry'));
  h.dom.window.close();
});

test('configured assistant role removes UI mutation routes and blocks direct close or rename', async () => {
  const { context, dom, closeCalls, killCalls, renameCalls, contextMenuCalls } = installRenderer({ assistantRole: 'persistent-assistant' });
  await flush();
  await flush();
  vm.runInContext(`
    state.chatStream.connected = true;
    state.chatStream.sessions = [{
      stream_id: 'hostc:assistant', host: 'hostc', session_name: 'assistant',
      name: 'assistant', role: 'persistent-assistant', visibility: 'default', working: false,
    }];
    state.sessions = [{
      name: 'assistant', hostId: 'local', display_name: 'Unrelated title',
      role: 'persistent-assistant', visibility: 'default', working: false,
    }, {
      name: 'Ordinary collision', hostId: 'local', display_name: 'Ordinary collision',
      role: 'worker', visibility: 'default', working: false,
    }];
    state.slots[0] = { name: 'assistant', displayName: 'Unrelated title', hostId: 'local' };
    renderSidebar();
  `, context);

  const sidebarRow = dom.window.document.querySelector('.session-item.assistant-protected');
  assert.ok(sidebarRow);
  assert.equal(sidebarRow.querySelector('.s-trash-btn'), null);
  assert.equal(sidebarRow.querySelector('.s-edit-btn'), null);
  sidebarRow.dispatchEvent(new dom.window.MouseEvent('contextmenu', { bubbles: true, cancelable: true }));
  assert.equal(contextMenuCalls.length, 0);
  assert.equal(dom.window.document.querySelector('#header-0 .cell-trash').hidden, true);
  assert.equal(dom.window.document.querySelector('#header-0 .cell-trash').disabled, true);
  assert.equal(dom.window.document.querySelector('#header-0 .cell-edit').hidden, true);
  assert.equal(dom.window.document.querySelector('#header-0 .cell-edit').disabled, true);
  const css = fs.readFileSync(path.join(root, 'renderer', 'styles.css'), 'utf8');
  assert.match(css, /\.cell-edit\[hidden\],\s*\.cell-trash\[hidden\]\s*\{[^}]*display:\s*none;/s);
  vm.runInContext("state.chatStream.sessions[0].display_name = 'Ordinary collision'", context);
  assert.equal(vm.runInContext("isProtectedAssistantNameHost('Ordinary collision', 'local')", context), false);

  vm.runInContext("state.slots[0] = { name: 'Ordinary collision', displayName: 'Ordinary collision', hostId: 'local' }; syncSlotAssistantControls(0)", context);
  assert.equal(dom.window.document.querySelector('#header-0 .cell-trash').hidden, false);
  assert.equal(dom.window.document.querySelector('#header-0 .cell-trash').disabled, false);
  assert.equal(dom.window.document.querySelector('#header-0 .cell-edit').hidden, false);
  assert.equal(dom.window.document.querySelector('#header-0 .cell-edit').disabled, false);

  vm.runInContext("showRenameModal('assistant', 'Unrelated title', 'local')", context);
  assert.equal(dom.window.document.querySelector('#modal-overlay').style.display, 'none');
  vm.runInContext("renameTarget = { sessionName: 'assistant', hostId: 'local' }; document.getElementById('modal-input').value = 'blocked rename'", context);
  dom.window.document.querySelector('#modal-confirm').click();
  await flush();
  assert.deepEqual(renameCalls, []);

  await vm.runInContext("deleteSession('assistant', 'local')", context);
  assert.deepEqual(closeCalls, []);
  assert.deepEqual(killCalls, []);
});

test('stale_question with answer text moves the draft into the normal composer', async () => {
  const { context, dismissCalls, dom } = installRenderer();
  await flush();
  await flush();

  mountRaceSlot(context);

  const document = dom.window.document;
  const questionEl = openDesktopQuestions(dom);
  assert.equal(questionEl?.style.display, '');
  questionEl.querySelector('[data-option="2"]').click();
  questionEl.querySelector('.slot-chat-question-submit').click();
  await flush();
  await flush();

  assert.equal(dismissCalls.length, 1);
  assert.equal(dismissCalls[0].options.text, 'Answering your question:\n\nQ1 (Pick): Two');
  assert.equal(questionEl.style.display, 'none');
  assert.equal(document.querySelector('#cell-0 .slot-chat-compose-input').value, dismissCalls[0].options.text);
  assert.equal(vm.runInContext('state.slotDrafts[0]', context), dismissCalls[0].options.text);
});

test('composer text answers an open pane question instead of normal send', async () => {
  const { context, dismissCalls, sendCalls, dom } = installRenderer({ dismissResult: { ok: true } });
  await flush();
  await flush();

  mountRaceSlot(context);

  const input = dom.window.document.querySelector('#cell-0 .slot-chat-compose-input');
  input.value = 'typed answer from composer';
  await vm.runInContext('sendChatComposer(0)', context);
  await flush();
  await flush();

  assert.equal(dismissCalls.length, 1);
  assert.equal(dismissCalls[0].hostId, 'local');
  assert.equal(dismissCalls[0].sessionName, 'claude-hostc-race');
  assert.equal(dismissCalls[0].options.questionKey, 'qkey-race');
  assert.equal(dismissCalls[0].options.text, 'typed answer from composer');
  assert.equal(sendCalls.length, 0);
  assert.equal(input.value, '');
  assert.equal(vm.runInContext('state.slotDrafts[0]', context), '');
});

test('open pane question prevents attachment sends from bypassing answer routing', async () => {
  const { context, dismissCalls, sendCalls, dom } = installRenderer({ dismissResult: { ok: true } });
  await flush();
  await flush();

  mountRaceSlot(context);
  vm.runInContext(`
    state.slotAttachments[0] = [{ key: 'blob:fixture', mime: 'image/png', name: 'fixture.png' }];
  `, context);

  const input = dom.window.document.querySelector('#cell-0 .slot-chat-compose-input');
  input.value = 'answer while image is staged';
  await vm.runInContext('sendChatComposer(0)', context);
  await flush();
  await flush();

  assert.equal(dismissCalls.length, 1);
  assert.equal(dismissCalls[0].options.text, 'answer while image is staged');
  assert.equal(sendCalls.length, 0);
  assert.equal(vm.runInContext('state.slotAttachments[0].length', context), 1);

  dismissCalls.length = 0;
  input.value = '';
  await vm.runInContext('sendChatComposer(0)', context);
  await flush();
  await flush();

  assert.equal(dismissCalls.length, 0);
  assert.equal(sendCalls.length, 0);
  assert.match(vm.runInContext('state.slotSendErrors[0]', context), /open question/);
});

test('durable agent question renders in the slot and submits through notificationResolve', async () => {
  const { context, dismissCalls, notificationResolveCalls, dom } = installRenderer({ questionOverride: null });
  await flush();
  await flush();

  mountRaceSlot(context);

  const wrongStreamFrame = {
    type: 'notification',
    notification: {
      notification_id: 'n-other',
      producer: 'agent_question.v1',
      state: 'open',
      title: 'Wrong stream',
      body: 'Should not render here',
      answer_to_stream_id: 'hostc:claude-other',
      question: {
        question_id: 'q-other',
        producer_stream_id: 'hostc:claude-other',
        response_mode: 'single_choice',
        options: [{ label: 'Other', value: 'other' }],
        state: 'open',
        answer: null,
      },
    },
  };
  vm.runInContext(`applyChatStreamPayload(${JSON.stringify(wrongStreamFrame)})`, context);
  const document = dom.window.document;
  let questionEl = document.querySelector('#cell-0 .slot-chat-question');
  assert.equal(questionEl?.style.display, 'none');
  assert.equal(vm.runInContext(`window.PentacleDurableQuestions.getOpenQuestionsForStream(${JSON.stringify(STREAM)}).length`, context), 0);

  const expiredFrame = {
    type: 'notification',
    notification: {
      notification_id: 'n-expired',
      producer: 'agent_question.v1',
      state: 'expired',
      title: 'Expired',
      body: 'Should not prompt.',
      answer_to_stream_id: STREAM,
      question: {
        question_id: 'q-expired',
        producer_stream_id: STREAM,
        response_mode: 'single_choice',
        options: [{ label: 'Ignore', value: 'ignore' }],
        state: 'expired',
        answer: null,
      },
    },
  };
  vm.runInContext(`applyChatStreamPayload(${JSON.stringify(expiredFrame)})`, context);
  assert.equal(vm.runInContext(`window.PentacleDurableQuestions.getOpenQuestionsForStream(${JSON.stringify(STREAM)}).length`, context), 0);
  assert.equal(questionEl.style.display, 'none');

  const frame = {
    type: 'notification',
    notification: {
      notification_id: 'n-durable',
      producer: 'agent_question.v1',
      state: 'open',
      title: 'Choose targets',
      body: 'Pick the deploy targets.',
      answer_to_stream_id: STREAM,
      actions: [{ kind: 'yes_no' }],
      question: {
        question_id: 'q-durable',
        producer_stream_id: STREAM,
        response_mode: 'multi_choice',
        options: [
          { label: 'Alpha', value: 'alpha' },
          { label: 'Beta', value: 'beta' },
        ],
        state: 'open',
        answer: null,
      },
    },
  };
  vm.runInContext(`applyChatStreamPayload(${JSON.stringify(frame)})`, context);
  assert.equal(vm.runInContext(`window.PentacleDurableQuestions.getOpenQuestionsForStream(${JSON.stringify(STREAM)}).length`, context), 1);
  questionEl = openDesktopQuestions(dom);
  assert.equal(questionEl.style.display, '');
  assert.equal(questionEl.querySelector('.slot-chat-question-freetext'), null);
  assert.ok(dom.window.document.querySelector('.desktop-question-portal__close'));
  const options = [...questionEl.querySelectorAll('.slot-chat-question-option.is-checkbox[data-option]')];
  assert.deepEqual(options.map((btn) => btn.textContent), ['Alpha', 'Beta']);

  options[0].click();
  await flush();
  assert.equal(notificationResolveCalls.length, 0);
  options[1].click();
  const note = questionEl.querySelector('.slot-chat-question-note');
  note.value = 'take both';
  note.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  questionEl.querySelector('.slot-chat-question-submit').click();
  await flush();
  await flush();

  assert.equal(dismissCalls.length, 0);
  assert.equal(notificationResolveCalls.length, 1);
  assert.equal(notificationResolveCalls[0].notificationId, 'n-durable');
  assert.equal(notificationResolveCalls[0].actionKind, 'yes_no');
  assert.deepEqual(notificationResolveCalls[0].options.selections, ['alpha', 'beta']);
  assert.equal(notificationResolveCalls[0].options.note, 'take both');
  assert.equal(notificationResolveCalls[0].options.submit, true);
  assert.equal(questionEl.style.display, 'none');

  notificationResolveCalls.length = 0;
  const cancelFrame = {
    ...frame,
    notification: {
      ...frame.notification,
      notification_id: 'n-cancel-durable',
      body: 'Cancel this prompt.',
    },
  };
  vm.runInContext(`applyChatStreamPayload(${JSON.stringify(cancelFrame)})`, context);
  questionEl = openDesktopQuestions(dom);
  dom.window.document.querySelector('.desktop-question-portal__close').click();
  await flush();
  await flush();

  assert.equal(notificationResolveCalls.length, 0);
  assert.equal(dom.window.document.querySelector('.desktop-question-portal'), null);
});

test('durable allow-custom question renders descriptions and submits custom_text', async () => {
  const { context, notificationResolveCalls, dom } = installRenderer({ questionOverride: null });
  await flush();
  await flush();

  mountRaceSlot(context);
  const frame = {
    type: 'notification',
    notification: {
      notification_id: 'n-custom-durable',
      producer: 'agent_question.v1',
      state: 'open',
      title: 'Choose path',
      body: 'Pick or type another path.',
      answer_to_stream_id: STREAM,
      actions: [{ kind: 'yes_no' }],
      question: {
        question_id: 'q-custom-durable',
        producer_stream_id: STREAM,
        response_mode: 'single_choice',
        allow_custom: true,
        options: [
          { label: 'Alpha', value: 'alpha', description: 'First option' },
          { label: 'Beta', value: 'beta' },
        ],
        state: 'open',
        answer: null,
      },
    },
  };
  vm.runInContext(`applyChatStreamPayload(${JSON.stringify(frame)})`, context);
  await flush();
  await flush();

  const questionEl = openDesktopQuestions(dom);
  assert.equal(questionEl.style.display, '');
  assert.equal(questionEl.querySelector('.slot-chat-question-option-desc').textContent, 'First option');
  const free = questionEl.querySelector('.slot-chat-question-freetext');
  assert.ok(free);
  free.value = 'custom only';
  free.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  questionEl.querySelector('.slot-chat-question-submit').click();
  await flush();
  await flush();

  assert.equal(notificationResolveCalls.length, 1);
  assert.equal(notificationResolveCalls[0].notificationId, 'n-custom-durable');
  assert.equal(notificationResolveCalls[0].actionKind, 'yes_no');
  assert.equal(notificationResolveCalls[0].options.custom_text, 'custom only');
  assert.equal(notificationResolveCalls[0].options.submit, true);
  assert.equal(notificationResolveCalls[0].options.selections, undefined);
});

test('durable hidden-child question renders under nearest visible parent with child attribution', async () => {
  const { context, notificationResolveCalls, dom } = installRenderer({ questionOverride: null });
  await flush();
  await flush();

  mountRaceSlot(context);
  vm.runInContext(`
    state.chatStream.sessions = [
      {
        stream_id: ${JSON.stringify(STREAM)},
        host: 'hostc',
        session_name: 'claude-hostc-race',
        visibility: 'default',
        parent_stream_id: null,
      },
      {
        stream_id: 'hostc:codex-hidden-child',
        host: 'hostc',
        session_name: 'codex-hidden-child',
        visibility: 'hidden',
        parent_stream_id: ${JSON.stringify(STREAM)},
      },
    ];
  `, context);

  const frame = {
    type: 'notification',
    notification: {
      notification_id: 'n-child',
      producer: 'agent_question.v1',
      state: 'open',
      title: 'Child question',
      body: 'Hidden child needs a decision.',
      answer_to_stream_id: 'hostc:codex-hidden-child',
      actions: [{ kind: 'yes_no' }],
      question: {
        question_id: 'q-child',
        producer_stream_id: 'hostc:codex-hidden-child',
        response_mode: 'single_choice',
        options: [{ label: 'Approve', value: 'approve' }],
        state: 'open',
        answer: null,
      },
    },
  };
  vm.runInContext(`applyChatStreamPayload(${JSON.stringify(frame)})`, context);
  await flush();
  await flush();

  const questionEl = openDesktopQuestions(dom);
  assert.equal(vm.runInContext(`window.PentacleDurableQuestions.getOpenQuestionsForStream(${JSON.stringify(STREAM)}).length`, context), 1);
  assert.equal(questionEl.style.display, '');
  assert.match(questionEl.textContent, /Hidden child needs a decision/);
  questionEl.querySelector('.slot-chat-question-option[data-option="1"]').click();
  questionEl.querySelector('.slot-chat-question-submit').click();
  await flush();
  await flush();

  assert.equal(notificationResolveCalls.length, 1);
  assert.equal(notificationResolveCalls[0].notificationId, 'n-child');
  assert.deepEqual(notificationResolveCalls[0].options.selections, ['approve']);
});

test('promptList hydration can surface a closed hidden child without a session summary', async () => {
  const { context } = installRenderer({ questionOverride: null });
  await flush();
  await flush();

  vm.runInContext(`
    state.chatStream.sessions = [{
      stream_id: ${JSON.stringify(STREAM)},
      host: 'hostc',
      session_name: 'claude-hostc-race',
      visibility: 'default',
      parent_stream_id: null,
    }];
    applyPromptListQuestions({
      ok: true,
      surfaced_to_stream_id: ${JSON.stringify(STREAM)},
      producer_stream_ids: [${JSON.stringify(STREAM)}, 'hostc:codex-closed-child'],
      questions: [{
        question_id: 'q-closed-child',
        notification_id: 'n-closed-child',
        state: 'open',
        producer_stream_id: 'hostc:codex-closed-child',
        created_at: '2026-07-07T00:00:00Z',
        answer: null,
        envelope: {
          title: 'Closed child',
          body: 'Need a parent-visible answer.',
          response_mode: 'single_choice',
          options: [{ label: 'Answer', value: 'answer' }],
        },
      }],
    }, ${JSON.stringify(STREAM)});
  `, context);

  const questions = vm.runInContext(`window.PentacleDurableQuestions.getOpenQuestionsForStream(${JSON.stringify(STREAM)})`, context);
  assert.equal(questions.length, 1);
  assert.equal(questions[0].question.producer_stream_id, 'hostc:codex-closed-child');
  assert.equal(questions[0].surfaced_to_stream_id, STREAM);
});

test('current visibility inventory overrides stale promptList surfaced parent metadata', async () => {
  const { context } = installRenderer({ questionOverride: null });
  await flush();
  await flush();

  vm.runInContext(`
    state.chatStream.sessions = [
      {
        stream_id: 'hostc:orchestrator',
        host: 'hostc',
        session_name: 'orchestrator',
        visibility: 'default',
        parent_stream_id: null,
      },
      {
        stream_id: 'hostc:lead',
        host: 'hostc',
        session_name: 'lead',
        visibility: 'hidden',
        parent_stream_id: 'hostc:orchestrator',
      },
      {
        stream_id: 'hostc:child',
        host: 'hostc',
        session_name: 'child',
        visibility: 'hidden',
        parent_stream_id: 'hostc:lead',
      },
    ];
    applyPromptListQuestions({
      ok: true,
      surfaced_to_stream_id: 'hostc:orchestrator',
      producer_stream_ids: ['hostc:orchestrator', 'hostc:lead', 'hostc:child'],
      questions: [{
        question_id: 'q-reparented',
        notification_id: 'n-reparented',
        state: 'open',
        producer_stream_id: 'hostc:child',
        created_at: '2026-07-07T00:00:00Z',
        answer: null,
        envelope: {
          title: 'Child',
          body: 'Need answer.',
          response_mode: 'single_choice',
          options: [{ label: 'Answer', value: 'answer' }],
        },
      }],
    }, 'hostc:orchestrator');
  `, context);

  assert.equal(vm.runInContext(`window.PentacleDurableQuestions.getOpenQuestionsForStream('hostc:orchestrator').length`, context), 1);

  vm.runInContext(`
    state.chatStream.sessions = state.chatStream.sessions.map((summary) => (
      summary.stream_id === 'hostc:lead' ? { ...summary, visibility: 'default' } : summary
    ));
  `, context);

  assert.equal(vm.runInContext(`window.PentacleDurableQuestions.getOpenQuestionsForStream('hostc:orchestrator').length`, context), 0);
  assert.equal(vm.runInContext(`window.PentacleDurableQuestions.getOpenQuestionsForStream('hostc:lead').length`, context), 1);
});

test('durable ack question renders Acknowledge and submits its option value', async () => {
  const { context, notificationResolveCalls, dom } = installRenderer({ questionOverride: null });
  await flush();
  await flush();
  mountRaceSlot(context);

  const frame = {
    type: 'notification',
    notification: {
      notification_id: 'n-ack',
      producer: 'agent_question.v1',
      state: 'open',
      title: 'Continue?',
      body: 'Confirm the handoff.',
      answer_to_stream_id: STREAM,
      actions: [{ kind: 'ack' }],
      question: {
        question_id: 'q-ack',
        producer_stream_id: STREAM,
        response_mode: 'ack',
        options: [{ label: 'Acknowledge', value: 'acknowledged' }],
        state: 'open',
        answer: null,
      },
    },
  };
  vm.runInContext(`applyChatStreamPayload(${JSON.stringify(frame)})`, context);
  const questionEl = openDesktopQuestions(dom);
  const option = questionEl.querySelector('.slot-chat-question-option[data-option="1"]');
  assert.equal(option?.textContent, 'Acknowledge');
  option.click();
  questionEl.querySelector('.slot-chat-question-submit').click();
  await flush();
  await flush();

  assert.equal(notificationResolveCalls.length, 1);
  assert.equal(notificationResolveCalls[0].actionKind, 'ack');
  assert.deepEqual(notificationResolveCalls[0].options.selections, ['acknowledged']);
});

test('mobile parity: pane and durable questions share one complete submission flow', async () => {
  const h = installRenderer({ dismissResult: { ok: true } });
  await flush(); await flush(); mountRaceSlot(h.context);
  vm.runInContext(`indexDurableQuestionNotification({
    notification_id: 'mixed-durable', producer: 'agent_question.v1', state: 'open',
    title: 'Durable', body: 'Choose durable option', created_at: '2026-09-12T00:00:00Z',
    question: { producer_stream_id: '${STREAM}', question_id: 'durable-question', state: 'open',
      response_mode: 'single_choice', options: [{ label: 'Keep', value: 'keep' }] }
  }); renderSlotChat(0);`, h.context);
  openDesktopQuestions(h.dom);
  const doc = h.dom.window.document;
  assert.equal(doc.querySelectorAll('.desktop-question-dot').length, 2, 'pane and durable are both reachable');
  doc.querySelector('.slot-chat-question-option[data-option="1"]').click();
  assert.equal(doc.querySelector('.slot-chat-question-submit').disabled, true);
  doc.querySelectorAll('.desktop-question-dot')[1].click();
  doc.querySelector('.slot-chat-question-option[data-option="1"]').click();
  assert.equal(doc.querySelector('.slot-chat-question-submit').disabled, false);
  doc.querySelector('.slot-chat-question-submit').click();
  await flush(); await flush();
  assert.equal(h.dismissCalls.length, 1);
  assert.equal(h.notificationResolveCalls.length, 1);
  assert.equal(h.notificationResolveCalls[0].notificationId, 'mixed-durable');
});

test('mobile parity: composer cannot bypass incomplete multi-page questions', async () => {
  const h = installRenderer({ dismissResult: { ok: true }, questionOverride: {
    question_key: 'group-composer', multi: true, questions: [
      { index: 0, prompt: 'First', options: [{ index: 1, label: 'One' }] },
      { index: 1, prompt: 'Second', options: [{ index: 1, label: 'Two' }] },
    ],
  } });
  await flush(); await flush(); mountRaceSlot(h.context);
  const input = h.dom.window.document.querySelector('#cell-0 .slot-chat-compose-input');
  input.value = 'Only one answer';
  await vm.runInContext('sendChatComposer(0)', h.context);
  assert.equal(h.dismissCalls.length, 0);
  assert.equal(h.sendCalls.length, 0);
  assert.equal(input.value, 'Only one answer');
  assert.ok(h.dom.window.document.querySelector('.desktop-question-portal'));
});

function durableFixture(id) {
  return { notification_id: id, producer: 'agent_question.v1', state: 'open', title: id,
    answer_to_stream_id: STREAM, question: { question_id: id, producer_stream_id: STREAM,
      state: 'open', response_mode: 'single_choice', options: [{ label: 'Keep', value: 'keep' }] } };
}

test('mobile parity: partial durable failure retains drafts and never repeats a successful answer', async () => {
  const h = installRenderer({ questionOverride: null });
  await flush(); await flush(); mountRaceSlot(h.context);
  const first = durableFixture('a-first'); const second = durableFixture('b-second');
  const calls = [];
  h.dom.window.cc.notificationResolve = async id => {
    calls.push(id);
    if (id === 'b-second' && calls.filter(x => x === id).length === 1) return { ok: false, error: 'temporary failure' };
    return { ok: true, notification: { ...(id === 'a-first' ? first : second), state: 'answered', question: { ...(id === 'a-first' ? first : second).question, state: 'answered', answer: { selections: ['keep'] } } } };
  };
  h.dom.window.cc.promptList = async () => ({ ok: true, questions: [{ notification_id: second.notification_id,
    question_id: second.question.question_id, producer_stream_id: STREAM, state: 'open',
    envelope: { title: second.title, response_mode: 'single_choice', options: second.question.options } }] });
  vm.runInContext(`indexDurableQuestionNotification(${JSON.stringify(first)});indexDurableQuestionNotification(${JSON.stringify(second)});renderSlotChat(0);`, h.context);
  const doc = h.dom.window.document;
  openDesktopQuestions(h.dom);
  doc.querySelector('.slot-chat-question-option').click();
  doc.querySelectorAll('.desktop-question-dot')[1].click();
  doc.querySelector('.slot-chat-question-option').click();
  doc.querySelector('.slot-chat-question-submit').click();
  await flush(); await flush();
  assert.deepEqual(calls, ['a-first', 'b-second']);
  assert.ok(doc.querySelector('.slot-chat-question-option.is-selected'));
  assert.equal(doc.querySelector('.slot-chat-question-submit').disabled, false);
  doc.querySelector('.slot-chat-question-submit').click();
  await flush(); await flush();
  assert.deepEqual(calls, ['a-first', 'b-second', 'b-second']);
});

test('mobile parity: a lost durable reply blocks resubmission until status is reconciled', async () => {
  const h = installRenderer({ questionOverride: null });
  await flush(); await flush(); mountRaceSlot(h.context);
  h.dom.window.cc.notificationResolve = async () => { throw new Error('reply lost'); };
  h.dom.window.cc.promptList = async () => { throw new Error('offline'); };
  vm.runInContext(`indexDurableQuestionNotification(${JSON.stringify(durableFixture('uncertain'))});renderSlotChat(0);`, h.context);
  openDesktopQuestions(h.dom);
  h.dom.window.document.querySelector('.slot-chat-question-option').click();
  h.dom.window.document.querySelector('.slot-chat-question-submit').click();
  await flush(); await flush();
  assert.equal(h.dom.window.document.querySelector('.slot-chat-question-submit').disabled, true);
  assert.ok(h.dom.window.document.querySelector('.slot-chat-question-reconcile'));
});


test('mobile parity: deferred bottom follow respects scrolling after a view render', async () => {
  const { context, dom } = installRenderer();
  await flush(); await flush(); mountRaceSlot(context);
  const frames = [];
  context.requestAnimationFrame = fn => { frames.push(fn); return frames.length; };
  const scroll = dom.window.document.querySelector('.slot-chat-scroll');
  Object.defineProperty(scroll, 'scrollHeight', { configurable: true, value: 1000 });
  Object.defineProperty(scroll, 'clientHeight', { configurable: true, value: 200 });
  scroll.scrollTop = 800;
  vm.runInContext('renderSlotChat(0)', context);
  assert.ok(frames.length > 0, 'near-bottom render schedules following');
  scroll.scrollTop = 100; // Reading earlier history before the next animation frame.
  frames.splice(0).forEach(fn => fn());
  assert.equal(scroll.scrollTop, 100, 'a stale render must not overwrite the reading position');
  scroll.scrollTop = 800;
  vm.runInContext('scrollSlotChatToBottom(state.slotChatRefs[0])', context);
  frames.splice(0).forEach(fn => fn());
  assert.equal(scroll.scrollTop, 1000, 'following still works when the reader did not move');
  await flush(); await flush();
  frames.splice(0).forEach(fn => fn());
  dom.window.close();
});
