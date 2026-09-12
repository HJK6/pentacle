const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const { JSDOM } = require('jsdom');

const root = path.join(__dirname, '..');
const rendererRequire = createRequire(path.join(root, 'renderer', 'app.js'));
const STREAM = 'hostc:claude-hostc-duration';

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

function injectRendererStyles(document) {
  for (const fileName of ['styles.css', 'cosmic_theme.css', 'cosmic_chat_surface.css']) {
    const style = document.createElement('style');
    style.textContent = fs.readFileSync(path.join(root, 'renderer', fileName), 'utf8');
    document.head.appendChild(style);
  }
}

function resolveCssPx(win, element, value) {
  const trimmed = String(value || '').trim();
  const variable = trimmed.match(/^var\((--[-_a-zA-Z0-9]+)\)$/);
  if (!variable) return Number.parseFloat(trimmed);
  for (let node = element; node; node = node.parentElement) {
    const tokenValue = win.getComputedStyle(node).getPropertyValue(variable[1]).trim();
    if (tokenValue) return resolveCssPx(win, node, tokenValue);
  }
  return NaN;
}

function computedFontSizePx(win, selector) {
  const element = win.document.querySelector(selector);
  assert.ok(element, `${selector} exists`);
  return resolveCssPx(win, element, win.getComputedStyle(element).fontSize);
}

function installRenderer({ settingsRecord = null } = {}) {
  const html = fs.readFileSync(path.join(root, 'renderer', 'index.html'), 'utf8');
  const dom = new JSDOM(html, { url: 'https://example.local/renderer/index.html' });
  injectRendererStyles(dom.window.document);
  if (settingsRecord) {
    dom.window.localStorage.setItem('pentacle.settings.v1', JSON.stringify(settingsRecord));
  }
  const selectCalls = [];
  const renderCalls = [];
  const harnessEvents = [];
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

  dom.window.PentacleHarness = {
    emit(name, payload = {}) {
      harnessEvents.push({ name, ...payload });
    },
  };
  dom.window.PentacleChatStore = {
    selectSessionDetail(streamId, options = {}) {
      selectCalls.push({ streamId, options: { ...options } });
      const transcriptItems = [
        {
          id: 'assistant-1',
          timestampLabel: '',
          label: '',
          tone: 'assistant',
          provider: 'claude',
          source: '',
          text: 'Done',
          kind: 'ASSIST',
          isUser: false,
          eventCase: 'assistant-message',
          displayRule: 'bubble:assistant',
        },
        {
          id: 'user-1',
          timestampLabel: '',
          label: '',
          tone: 'user',
          provider: 'operator',
          source: '',
          text: 'Run it',
          kind: 'USER',
          isUser: true,
          eventCase: 'user-message',
          displayRule: 'bubble:user',
        },
      ];
      if (options.includeSystem === true) {
        transcriptItems.push(
          {
            id: 'summary-1',
            timestampLabel: '',
            label: 'Summary',
            tone: 'system',
            provider: 'claude',
            source: 'claude-jsonl',
            text: 'Turn complete',
            kind: 'SYSTEM',
            isUser: false,
            eventCase: 'turn-summary',
            displayRule: 'activity:turn-summary',
          },
          {
            id: 'system-1',
            timestampLabel: '',
            label: 'System',
            tone: 'system',
            provider: 'claude',
            source: 'claude-jsonl',
            text: 'Session resumed',
            kind: 'SYSTEM',
            isUser: false,
            eventCase: 'system',
            displayRule: 'activity:system',
          },
          {
            id: 'compacted-1',
            timestampLabel: '',
            label: 'Context',
            tone: 'system',
            provider: 'claude',
            source: '',
            text: 'Context Compacted to save tokens',
            kind: 'ASSIST',
            isUser: false,
            eventCase: 'context-compacted',
            displayRule: 'system:compacted',
          },
        );
      }
      return {
        streamId,
        title: 'Duration Fixture',
        providerLabel: 'claude',
        transcriptItems,
      };
    },
    getTurnPhase() {
      return 'idle';
    },
  };
  dom.window.PentacleChatView = {
    renderTranscriptTimelineHtml(detail, _chrome, options = {}) {
      renderCalls.push({
        showTurnDuration: options.showTurnDuration === true,
        rules: (detail?.transcriptItems || []).map((item) => item.displayRule),
      });
      return (detail?.transcriptItems || [])
        .map((item) => {
          if (item.displayRule === 'activity:turn-summary') {
            return options.showTurnDuration === true
              ? '<article class="slot-chat-row"><div class="slot-chat-activity">Turn complete</div></article>'
              : '';
          }
          if (item.displayRule === 'activity:system') {
            return '<article class="slot-chat-row"><div class="slot-chat-activity">Session resumed</div></article>';
          }
          if (item.displayRule === 'system:compacted') {
            return '<div class="slot-chat-compacted">Context Compacted to save tokens</div>';
          }
          if (item.displayRule === 'bubble:user') {
            return '<article class="slot-chat-row is-user"><div class="slot-chat-user-bubble">Run it</div></article>';
          }
          return '<article class="slot-chat-row"><div class="slot-chat-assistant-card">Done</div></article>';
        })
        .join('');
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
  return { context, dom, harnessEvents, renderCalls, selectCalls };
}

test('showTurnDuration settings switch reselects system timing rows and repaints active chat slots', async () => {
  const { context, dom, renderCalls, selectCalls } = installRenderer();
  await flush();
  await flush();

  vm.runInContext(`
    ensureSlotChatSurface(0);
    state.slots[0] = { name: 'claude-hostc-duration', displayName: 'Duration Fixture', hostId: 'local' };
    state.slotViewModes[0] = 'chat';
    state.chatStream.connected = true;
    state.chatStream.sessions = [{
      stream_id: ${JSON.stringify(STREAM)},
      host: 'hostc',
      provider: 'claude',
      session_id: 'claude-hostc-duration',
      session_name: 'claude-hostc-duration',
      name: 'claude-hostc-duration',
      title: 'Duration Fixture',
      last_text: '',
      last_kind: 'ASSIST',
      last_event_at: new Date().toISOString(),
      working: false,
    }];
    renderSlotChat(0);
  `, context);

  const document = dom.window.document;
  assert.equal(document.querySelector('#cell-0 .slot-chat-activity')?.textContent?.includes('Turn complete'), undefined);
  assert.equal(selectCalls.at(-1).options.includeSystem, false, 'default OFF keeps system rows out of the slot selector');
  assert.equal(selectCalls.at(-1).options.systemRows, undefined, 'default OFF does not request a system row mode');
  assert.equal(renderCalls.at(-1).showTurnDuration, false, 'default OFF passes render option false');
  assert.ok(!renderCalls.at(-1).rules.includes('activity:turn-summary'), 'default OFF detail has no turn-summary item');

  document.getElementById('settings-btn').click();
  const sw = document.querySelector('.settings-row[data-flag="showTurnDuration"] .settings-switch');
  assert.ok(sw, 'showTurnDuration switch exists');
  sw.click();

  assert.equal(sw.getAttribute('aria-checked'), 'true');
  assert.equal(selectCalls.at(-1).options.includeSystem, true, 'ON reselects with system rows available');
  assert.equal(selectCalls.at(-1).options.systemRows, 'timing-only', 'ON filters system rows before selector windowing');
  assert.equal(renderCalls.at(-1).showTurnDuration, true, 'ON passes render option true');
  assert.ok(renderCalls.at(-1).rules.includes('activity:turn-summary'), 'ON detail contains the turn-summary row');
  assert.ok(!renderCalls.at(-1).rules.includes('activity:system'), 'non-timing system rows stay filtered out');
  assert.ok(!renderCalls.at(-1).rules.includes('system:compacted'), 'context compaction markers are not timing rows');
  assert.ok(
    document.querySelector('#cell-0 .slot-chat-activity')?.textContent?.includes('Turn complete'),
    'active slot repainted with duration visuals after settings click',
  );
  assert.equal(document.querySelector('#cell-0 .slot-chat-compacted'), null);
});

test('theme and density settings apply live and persist outside feature flags', async () => {
  const { context, dom, harnessEvents } = installRenderer();
  await flush();
  await flush();

  const document = dom.window.document;
  vm.runInContext(`
    ensureSlotChatSurface(0);
    state.slots[0] = { name: 'claude-hostc-duration', displayName: 'Duration Fixture', hostId: 'local' };
    state.slotViewModes[0] = 'chat';
    state.chatStream.connected = true;
    state.chatStream.sessions = [{
      stream_id: ${JSON.stringify(STREAM)},
      host: 'hostc',
      provider: 'claude',
      session_id: 'claude-hostc-duration',
      session_name: 'claude-hostc-duration',
      name: 'claude-hostc-duration',
      title: 'Duration Fixture',
      last_text: '',
      updated_at: new Date().toISOString(),
    }];
    renderSlotChat(0);
  `, context);

  const comfortableFonts = {
    assistant: computedFontSizePx(dom.window, '#cell-0 .slot-chat-assistant-card'),
    user: computedFontSizePx(dom.window, '#cell-0 .slot-chat-user-bubble'),
    compose: computedFontSizePx(dom.window, '#cell-0 .slot-chat-compose-input'),
  };
  assert.deepEqual(comfortableFonts, { assistant: 14.5, user: 13, compose: 13.5 });

  document.getElementById('settings-btn').click();

  const themeRow = document.querySelector('.settings-row[data-setting="theme"]');
  const densityRow = document.querySelector('.settings-row[data-setting="density"]');
  assert.ok(themeRow, 'theme setting row exists');
  assert.ok(densityRow, 'density setting row exists');

  themeRow.querySelector('.settings-segment-btn[data-value="light"]').click();
  densityRow.querySelector('.settings-segment-btn[data-value="compact"]').click();

  assert.equal(document.documentElement.dataset.theme, 'light');
  assert.equal(document.documentElement.dataset.density, 'compact');
  assert.equal(themeRow.querySelector('.settings-segment-btn[data-value="light"]').getAttribute('aria-pressed'), 'true');
  assert.equal(densityRow.querySelector('.settings-segment-btn[data-value="compact"]').getAttribute('aria-pressed'), 'true');

  const compactFonts = {
    assistant: computedFontSizePx(dom.window, '#cell-0 .slot-chat-assistant-card'),
    user: computedFontSizePx(dom.window, '#cell-0 .slot-chat-user-bubble'),
    compose: computedFontSizePx(dom.window, '#cell-0 .slot-chat-compose-input'),
  };
  assert.deepEqual(compactFonts, { assistant: 12, user: 12, compose: 12 });
  assert.ok(compactFonts.assistant < comfortableFonts.assistant, 'compact reduces assistant card font size');
  assert.ok(compactFonts.user < comfortableFonts.user, 'compact reduces user bubble font size');
  assert.ok(compactFonts.compose < comfortableFonts.compose, 'compact reduces compose input font size');

  const stored = JSON.parse(dom.window.localStorage.getItem('pentacle.settings.v1'));
  assert.deepEqual(stored.appearance, { theme: 'light', density: 'compact' });
  assert.equal(stored.features, undefined, 'appearance settings do not create feature overrides');
  assert.ok(harnessEvents.some((event) => event.name === 'settings:appearance' && event.data?.theme === 'light'));
  assert.ok(harnessEvents.some((event) => event.name === 'settings:appearance' && event.data?.density === 'compact'));
});

test('theme and density settings restore from localStorage on renderer boot', async () => {
  const { dom } = installRenderer({
    settingsRecord: { appearance: { theme: 'light', density: 'compact' } },
  });
  await flush();
  await flush();

  const document = dom.window.document;
  assert.equal(document.documentElement.dataset.theme, 'light');
  assert.equal(document.documentElement.dataset.density, 'compact');

  document.getElementById('settings-btn').click();
  assert.equal(
    document.querySelector('.settings-row[data-setting="theme"] .settings-segment-btn[data-value="light"]').getAttribute('aria-pressed'),
    'true',
  );
  assert.equal(
    document.querySelector('.settings-row[data-setting="density"] .settings-segment-btn[data-value="compact"]').getAttribute('aria-pressed'),
    'true',
  );
});

test('column preference survives the real settings save/load normalizer without eager writes', async () => {
  const first = installRenderer();
  await flush(); await flush();
  assert.equal(first.dom.window.localStorage.getItem('pentacle.settings.v1'), null);
  vm.runInContext("saveAppearanceSetting('gridColSplit', .68)", first.context);
  const stored = JSON.parse(first.dom.window.localStorage.getItem('pentacle.settings.v1'));
  const next = installRenderer({ settingsRecord: stored });
  await flush(); await flush();
  assert.equal(vm.runInContext('state.appearance.gridColSplit', next.context), .68);
  vm.runInContext("state.appearance.theme = 'light'; applyAppearanceSettings()", next.context);
  assert.equal(vm.runInContext('state.appearance.gridColSplit', next.context), .68);
  assert.deepEqual(JSON.parse(next.dom.window.localStorage.getItem('pentacle.settings.v1')), stored);
  for (const invalid of [null, true, [], -1, 0, 1, 'invalid']) {
    assert.equal(vm.runInContext(`normalizeAppearance({gridColSplit:${JSON.stringify(invalid)}}).gridColSplit`, next.context), .5);
  }
  first.dom.window.close(); next.dom.window.close();
});

test('visible-fit policy skips dashboard, maximized-away and hidden terminal views', async () => {
  const { context, dom } = installRenderer();
  await flush(); await flush();
  vm.runInContext(`
    window.resizeCalls = [];
    window.fitCalls = [];
    window.cc.resizePty = (...args) => window.resizeCalls.push(args);
    for (let slot = 0; slot < 4; slot++) {
      const mount = document.getElementById('term-'+slot);
      Object.defineProperty(mount, 'clientWidth', { configurable:true, value:400 });
      Object.defineProperty(mount, 'clientHeight', { configurable:true, value:300 });
      mount.getClientRects = () => mount.style.display === 'none' ? [] : [{ width:400, height:300 }];
      const element = document.createElement('div'); mount.append(element);
      const term = { element, cols:80, rows:24 };
      state.terminals[slot] = { term, fitAddon:{ fit(){window.fitCalls.push(slot); term.cols=90;term.rows=30;} } };
    }
    for (let slot=0;slot<4;slot++) fitVisibleSlot(slot);
  `, context);
  assert.deepEqual(Array.from(dom.window.fitCalls), [0,1,2,3]);
  assert.equal(dom.window.resizeCalls.length, 4);
  vm.runInContext(`
    window.fitCalls.length = 0;
    state.maximizedSlot=2;
    for (let slot=0;slot<4;slot++) fitVisibleSlot(slot);
  `, context);
  assert.deepEqual(Array.from(dom.window.fitCalls), [2]);
  assert.equal(dom.window.resizeCalls.length, 4, 'unchanged cols/rows never resize the PTY');
  vm.runInContext(`
    window.fitCalls.length=0; state.currentView='dashboards'; fitVisibleSlot(2);
    state.currentView='chats'; document.getElementById('term-2').style.display='none'; fitVisibleSlot(2);
    state.maximizedSlot=null;
    Object.defineProperty(document.getElementById('term-0'),'clientWidth',{value:0}); fitVisibleSlot(0);
  `, context);
  assert.equal(dom.window.fitCalls.length, 0);
  dom.window.close();
});

test('usage panel toggle collapses and expands the sidebar section', async () => {
  const { dom } = installRenderer();
  await flush();
  await flush();

  const document = dom.window.document;
  const section = document.getElementById('usage-section');
  const toggle = document.getElementById('usage-section-toggle');
  const body = document.getElementById('usage-section-body');
  assert.ok(section, 'usage section exists');
  assert.ok(toggle, 'usage toggle exists');
  assert.ok(body, 'usage body exists');

  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(body.hidden, false);
  assert.equal(section.classList.contains('is-collapsed'), false);

  toggle.click();
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(body.hidden, true);
  assert.equal(section.classList.contains('is-collapsed'), true);

  toggle.click();
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(body.hidden, false);
  assert.equal(section.classList.contains('is-collapsed'), false);
});
