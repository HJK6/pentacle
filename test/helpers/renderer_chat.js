'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const Module = require('node:module');
const esbuild = require('esbuild');
const { createRequire } = require('node:module');
const { JSDOM } = require('jsdom');

const root = path.join(__dirname, '..', '..');
const rendererRequire = createRequire(path.join(root, 'renderer', 'app.js'));
const STREAM = 'hostc:claude-hostc-race';
const answerModule = new Module(path.join(root, 'pentacle-chat-core/src/services/questionAnswerFormat.ts'));
answerModule._compile(esbuild.buildSync({ entryPoints: [answerModule.id], bundle: true, platform: 'node', format: 'cjs', write: false, logLevel: 'silent' }).outputFiles[0].text, answerModule.id);
const { buildPentacleQuestionAnswerText } = answerModule.exports;


function installRenderer({ dismissResult, questionOverride, assistantRole = '', initialSessions = [], popoutContext = null } = {}) {
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
    getChatStreamState: async () => ({ connected: true, events: [], sessions: initialSessions, schedules: [] }),
    chatPopoutContext: () => popoutContext,
    killPty() {},
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


module.exports = { installRenderer, mountRaceSlot, STREAM };
