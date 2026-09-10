'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const root = path.resolve(__dirname, '..');
const realRequire = createRequire(path.join(root, 'main.js'));
function harness(client, config = {}) {
  const handlers = new Map();
  const app = { setName() {}, on() {}, whenReady: () => ({ then() {} }) };
  const ipcMain = { handle: (name, handler) => { assert.ok(!handlers.has(name), name); handlers.set(name, handler); }, on() {} };
  const electron = { app, ipcMain, BrowserWindow: class { static getAllWindows() { return []; } }, shell: {} };
  vm.runInNewContext(fs.readFileSync(path.join(root, 'main.js'), 'utf8'), {
    require(name) { if (name === 'electron') return electron; if (name === './main/chat_stream_client') return { init() {}, ...client };
      if (name === './config-loader') return { loadConfig: () => ({ config }) }; return realRequire(name); },
    __dirname: root, process: { ...process, env: {} }, console, Buffer, URL,
  });
  return (name, ...args) => handlers.get(name)({ sender: { id: 1 } }, ...args);
}
test('real main IPC returns the renderer catalog and preserves spawn host and tuple', async () => {
  const calls = [];
  const catalog = { profiles: { desktop_manual: {} }, catalog_version: 'public-v1' };
  const session = { stream_id: 'workstation:s1', host: 'workstation', session_name: 's1', requested_launch_tuple: { provider: 'claude' } };
  const invoke = harness({ getSpawnCatalog: async () => catalog, spawnSession: async (value) => { calls.push(value); return session; } });
  const catalogResult = await invoke('chat-stream:spawn-catalog');
  assert.equal(catalogResult.ok, true);
  assert.equal(catalogResult.catalog, catalog);
  const result = await invoke('chat-stream:spawn', { hostId: 'workstation', provider: 'claude', model: 'opus', effort: 'high', spawnProfile: 'desktop_manual', catalogVersion: 'public-v1' });
  assert.equal(result.session, session);
  assert.equal(result.requested, session.requested_launch_tuple);
  assert.equal(result.streamId, session.stream_id);
  assert.equal(calls[0].host, 'workstation');
  assert.equal(calls[0].catalogVersion, 'public-v1');
});
test('real main forwards send correlations and reports disconnected failures without synthetic events', async () => {
  let sent;
  const invoke = harness({ sendMessage: async (value) => { sent = value; throw new Error('Stream disconnected'); } });
  const result = await invoke('chat-stream:send', 'custom-host', 'session', 'hello', 'request-1', 'optimistic-1', [{ blob_sha: 'x' }]);
  assert.equal(result.ok, false);
  assert.equal(result.error, 'Stream disconnected');
  assert.equal(result.event, undefined);
  assert.equal(sent.host, 'custom-host');
  assert.equal(sent.requestId, 'request-1');
  assert.equal(sent.optimisticId, 'optimistic-1');
  assert.equal(sent.attachments[0].blob_sha, 'x');
});
test('renderer config retains feature and transport settings without credential material', async () => {
  const invoke = harness({}, { features: { chatUi: true }, chatStream: { url: 'ws://localhost:7777', hosts: ['workstation'], token: 'private', tokenPath: '/private/credential' } });
  const result = await invoke('get-config');
  assert.equal(result.features.chatUi, true);
  assert.equal(result.chatStream.token, undefined);
  assert.equal(result.chatStream.tokenPath, undefined);
  assert.deepEqual(result.hostIds, ['workstation']);
});
