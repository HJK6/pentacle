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
test('real account status enriches the catalog without mutating its source', async () => {
  const catalog = { profiles: { desktop_manual: {} }, catalog_version: 'public-v1' };
  const accounts = { accounts: [{ provider: 'codex', host: 'workstation', alias: 'spare' }] };
  const invoke = harness({ getSpawnCatalog: async () => catalog, getProviderAccounts: async () => accounts });
  const result = await invoke('chat-stream:spawn-catalog');
  assert.equal(result.ok, true);
  assert.notEqual(result.catalog, catalog);
  assert.equal(result.catalog.account_status, accounts);
  assert.equal(Object.hasOwn(catalog, 'account_status'), false);
});
test('an unavailable account lookup preserves the legacy catalog object', async () => {
  const catalog = { profiles: { desktop_manual: {} }, catalog_version: 'public-v1' };
  const invoke = harness({ getSpawnCatalog: async () => catalog,
    getProviderAccounts: async () => { throw new Error('fixture unavailable'); } });
  const result = await invoke('chat-stream:spawn-catalog');
  assert.equal(result.ok, true);
  assert.equal(result.catalog, catalog);
});
test('account inventory authorization failures remain visible to the picker', async () => {
  const invoke = harness({ getSpawnCatalog: async () => ({ profiles: { desktop_manual: {} } }),
    getProviderAccounts: async () => { throw { type: 'account.error',
      error_code: 'account_control_unauthorized', error: 'account_control_unauthorized' }; } });
  const result = await invoke('chat-stream:spawn-catalog');
  assert.equal(result.ok, false);
  assert.equal(result.code, 'account_control_unauthorized');
  assert.equal(result.error, 'account_control_unauthorized');
});
test('signed operator RPC inventory reaches the real account picker', async () => {
  const os = require('node:os');
  const { promisify } = require('node:util');
  const execFile = promisify(require('node:child_process').execFile);
  const { JSDOM } = require('jsdom');
  const client = require('../main/chat_stream_client');
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'picker-rpc-'));
  try {
    const requestPath = path.join(scratch, 'request.json');
    const responsePath = path.join(scratch, 'response.json');
    const invoke = harness({ getSpawnCatalog: async () => ({ profiles: { desktop_manual: {} } }),
      getProviderAccounts: () => client.getProviderAccounts.call({
        async sendCommand(request, prefix) {
          assert.equal(prefix, 'account');
          fs.writeFileSync(requestPath, JSON.stringify(request));
          await execFile(process.env.PENTACLE_PYTHON || 'python3', ['-m', 'pytest',
            'services/chat-stream-v2/tests/test_operator_picker_inventory.py', '-q',
            '--basetemp', path.join(scratch, 'pytest')], {
            cwd: root, timeout: 20000,
            env: { ...process.env, PENTACLE_TEST_PICKER_REQUEST: requestPath,
              PENTACLE_TEST_PICKER_RESPONSE: responsePath },
          });
          return JSON.parse(fs.readFileSync(responsePath, 'utf8'));
        },
      }) });
    const result = await invoke('chat-stream:spawn-catalog');
    assert.equal(result.ok, true, result.error);
    const app = fs.readFileSync(path.join(root, 'renderer/app.js'), 'utf8');
    const source = app.match(/function spawnAccountControl\([^]*?\n}/)[0];
    const html = new Function('newSessionCatalog', 'esc', `${source}; return spawnAccountControl('codex', 'fixture-host');`)(result.catalog, String);
    const dom = new JSDOM(html);
    const picker = dom.window.document.querySelector('select');
    assert.deepEqual([...picker.options].map(option => option.value), ['', 'primary', 'spare']);
    assert.equal(picker.options[2].disabled, false);
    assert.match(picker.options[0].textContent, /primary/);
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
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
