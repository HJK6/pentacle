const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { registerNotificationIpcHandlers } = require('../main/notification_ipc_bridge');

test('preload promptList forwards renderer calls to prompt-list IPC channel', async () => {
  const calls = [];
  const ipcRenderer = {
    invoke(channel, ...args) {
      calls.push({ channel, args });
      return Promise.resolve({ ok: true, channel, args });
    },
    send() {},
    on() {},
    removeAllListeners() {},
  };
  const context = {
    require(name) {
      if (name === 'electron') return { clipboard: { readText: () => '', writeText() {} }, ipcRenderer };
      if (name === './config-loader') return { loadConfig: () => ({ config: {} }) };
      return require(name);
    },
    window: {},
    process,
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8'), context, {
    filename: 'preload.js',
  });

  await context.window.cc.promptList({ producer_stream_id: 'hosta:parent', open: true });

  assert.deepEqual(calls, [
    { channel: 'chat-stream:prompt-list', args: [{ producer_stream_id: 'hosta:parent', open: true }] },
  ]);
});

test('preload notificationResolve forwards custom text to IPC channel', async () => {
  const calls = [];
  const ipcRenderer = {
    invoke(channel, ...args) {
      calls.push({ channel, args });
      return Promise.resolve({ ok: true, channel, args });
    },
    send() {},
    on() {},
    removeAllListeners() {},
  };
  const context = {
    require(name) {
      if (name === 'electron') return { clipboard: { readText: () => '', writeText() {} }, ipcRenderer };
      if (name === './config-loader') return { loadConfig: () => ({ config: {} }) };
      return require(name);
    },
    window: {},
    process,
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8'), context, {
    filename: 'preload.js',
  });

  await context.window.cc.notificationResolve('n-1', 'yes_no', {
    selections: ['alpha'],
    customText: 'custom answer',
    submit: true,
  });

  assert.equal(calls[0].channel, 'chat-stream:notification-resolve');
  assert.equal(calls[0].args[0].notificationId, 'n-1');
  assert.equal(calls[0].args[0].actionKind, 'yes_no');
  assert.deepEqual(Array.from(calls[0].args[0].selections), ['alpha']);
  assert.equal(calls[0].args[0].custom_text, 'custom answer');
  assert.equal(calls[0].args[0].submit, true);
  assert.equal(calls[0].args[0].by, 'operator');
});

test('main notification IPC forwards promptList arguments to chatStreamClient', async () => {
  const handlers = new Map();
  const calls = [];
  const ipcMain = {
    handle(channel, handler) {
      handlers.set(channel, handler);
    },
  };
  const chatStreamClient = {
    promptList(args) {
      calls.push(args);
      return Promise.resolve({ questions: [{ question_id: 'q1' }] });
    },
    notificationList() {
      return Promise.resolve({ notifications: [] });
    },
    notificationResolve() {
      return Promise.resolve({ notification: {} });
    },
    notificationCreate() {
      return Promise.resolve({ notification: {} });
    },
  };

  registerNotificationIpcHandlers(ipcMain, chatStreamClient);
  const reply = await handlers.get('chat-stream:prompt-list')(null, {
    producer_stream_id: 'hosta:parent',
    open: true,
  });

  assert.deepEqual(calls, [{ producer_stream_id: 'hosta:parent', open: true }]);
  assert.deepEqual(reply.questions, [{ question_id: 'q1' }]);
  assert.equal(reply.ok, true);
});

test('main notification IPC forwards custom text to chatStreamClient', async () => {
  const handlers = new Map();
  const calls = [];
  const ipcMain = {
    handle(channel, handler) {
      handlers.set(channel, handler);
    },
  };
  const chatStreamClient = {
    promptList() {
      return Promise.resolve({ questions: [] });
    },
    notificationList() {
      return Promise.resolve({ notifications: [] });
    },
    notificationResolve(args) {
      calls.push(args);
      return Promise.resolve({ notification: {} });
    },
    notificationCreate() {
      return Promise.resolve({ notification: {} });
    },
  };

  registerNotificationIpcHandlers(ipcMain, chatStreamClient);
  const reply = await handlers.get('chat-stream:notification-resolve')(null, {
    notificationId: 'n-1',
    actionKind: 'yes_no',
    selections: ['alpha'],
    custom_text: 'custom answer',
    submit: true,
  });

  assert.equal(reply.ok, true);
  assert.equal(calls[0].notification_id, 'n-1');
  assert.equal(calls[0].action_kind, 'yes_no');
  assert.deepEqual(calls[0].selections, ['alpha']);
  assert.equal(calls[0].custom_text, 'custom answer');
  assert.equal(calls[0].submit, true);
});

