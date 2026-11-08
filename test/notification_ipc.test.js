const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const { registerNotificationIpcHandlers } = require('../main/notification_ipc_bridge');

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

