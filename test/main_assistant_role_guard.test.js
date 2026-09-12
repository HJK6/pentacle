'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { configuredAssistantRole, isProtectedAssistantRename } = require('../main/assistant_role_guard');

const config = {
  features: { assistantRole: 'assistant' },
  chatStream: { hostMap: { local: 'daemon-host' } },
};
const snapshot = {
  sessions: [{ host: 'daemon-host', session_name: 'assistant-pane', display_name: 'Different title', role: 'assistant' }],
};

test('main rename guard is default-off and requires the exact configured role', () => {
  assert.equal(configuredAssistantRole({ features: {} }), '');
  assert.equal(isProtectedAssistantRename({ features: {} }, snapshot, 'local', 'assistant-pane'), false);
  assert.equal(isProtectedAssistantRename(config, snapshot, 'local', 'assistant-pane'), true);
  assert.equal(isProtectedAssistantRename(config, snapshot, 'local', 'Different title'), true);
  assert.equal(isProtectedAssistantRename(config, { sessions: [{ ...snapshot.sessions[0], role: 'worker' }] }, 'local', 'assistant-pane'), false);
  assert.equal(isProtectedAssistantRename(config, snapshot, 'other-host', 'assistant-pane'), false);
  assert.equal(isProtectedAssistantRename({ features: { assistantRole: 'assistant' }, localHostId: 'daemon-host' }, snapshot, 'local', 'assistant-pane'), true);
});

test('both public main rename IPC handlers reject before their rename mutation', () => {
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  for (const channel of ['chat-stream:rename', 'tmux:set-window-title']) {
    const start = main.indexOf(`ipcMain.handle('${channel}'`);
    assert.notEqual(start, -1, `${channel} handler exists`);
    const end = main.indexOf("ipcMain.handle('", start + 1);
    const handler = main.slice(start, end === -1 ? main.length : end);
    assert.match(handler, /protectedAssistantRenameError\(host, sessionName\)/);
    assert.ok(handler.indexOf('protectedAssistantRenameError') < handler.indexOf('renameSession'), `${channel} guards before renameSession`);
  }
});
