'use strict';
// Regression for issue #7: the Settings "Reload now" button did nothing.
// main.js cancels every will-navigate, and the renderer used location.reload(),
// which Electron routes through will-navigate — so the reload was swallowed.
// The fix reloads from the main process over an 'app:reload' IPC channel,
// exposed to the renderer through the preload bridge as window.cc.reloadApp.
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');

const root = path.resolve(__dirname, '..');
const realRequire = createRequire(path.join(root, 'main.js'));

function loadMainIpc() {
  const handlers = new Map();
  const app = { setName() {}, on() {}, whenReady: () => ({ then() {} }) };
  const ipcMain = {
    handle: (name, handler) => handlers.set(name, handler),
    on: (name, handler) => handlers.set(name, handler),
  };
  const electron = { app, ipcMain, BrowserWindow: class { static getAllWindows() { return []; } }, shell: {} };
  vm.runInNewContext(fs.readFileSync(path.join(root, 'main.js'), 'utf8'), {
    require(name) {
      if (name === 'electron') return electron;
      if (name === './main/chat_stream_client') return { init() {} };
      if (name === './config-loader') return { loadConfig: () => ({ config: {} }) };
      return realRequire(name);
    },
    __dirname: root, process: { ...process, env: {} }, console, Buffer, URL,
  });
  return handlers;
}

test('main registers an app:reload handler that reloads the requesting window', () => {
  const handlers = loadMainIpc();
  const handler = handlers.get('app:reload');
  assert.equal(typeof handler, 'function', 'app:reload handler is registered');
  let reloaded = 0;
  handler({ sender: { reload: () => { reloaded += 1; } } });
  assert.equal(reloaded, 1, 'the sender webContents is reloaded from the main process');
});

test('preload exposes window.cc.reloadApp that sends app:reload', () => {
  const sent = [];
  const ipcRenderer = { send: (channel, ...args) => sent.push([channel, ...args]), on() {}, invoke() {}, removeAllListeners() {} };
  const win = {};
  vm.runInNewContext(fs.readFileSync(path.join(root, 'preload.js'), 'utf8'), {
    require(name) {
      if (name === 'electron') return { ipcRenderer };
      if (name === './config-loader') return { loadConfig: () => ({ config: {} }) };
      return realRequire(name);
    },
    __dirname: root, process: { ...process, argv: [] }, console, window: win, document: { addEventListener() {} },
  });
  assert.equal(typeof win.cc.reloadApp, 'function', 'window.cc.reloadApp is exposed');
  win.cc.reloadApp();
  assert.deepEqual(sent.at(-1), ['app:reload'], 'reloadApp sends the app:reload IPC message');
});

test('the Settings reload button is wired to window.cc.reloadApp, not a bare location.reload()', () => {
  // Regression guard for the fix: a revert of the click handler back to a plain
  // location.reload() would be swallowed again by the will-navigate guard. The
  // main/preload behaviour is covered above; this pins the renderer wiring
  // (a full JSDOM click test would depend on the env-fragile jsdom harness).
  const source = fs.readFileSync(path.join(root, 'renderer', 'app.js'), 'utf8');
  const start = source.indexOf("reloadBtn?.addEventListener('click'");
  assert.ok(start >= 0, 'the reload button click handler is present');
  const handler = source.slice(start, source.indexOf('});', start) + 3);
  assert.match(handler, /window\.cc\??\.reloadApp/,
    'the reload button routes through window.cc.reloadApp');
});
