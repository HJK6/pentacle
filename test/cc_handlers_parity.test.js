const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const {
  createCcHandlers,
  createCollector,
  WEB_LOCAL,
  WEB_UNSUPPORTED,
  UNIMPLEMENTED,
  UNCALLED,
  PUSH_EVENTS,
  PRELOAD_LOCAL,
} = require('../main/cc_handlers');

// The table exactly as main.js and server/ build it.
function fullTable() {
  const stubClient = new Proxy({}, { get: () => async () => ({}) });
  const collector = createCollector();
  createCcHandlers({ CONFIG: { chatStream: {} }, chatStreamClient: stubClient, harness: true }).register(collector);
  return collector.table;
}

// Run preload.js for real and record which channel (and which IPC verb) each
// window.cc method reaches for. A regex over the source would miss the
// indirection; calling the methods cannot drift from what the renderer does.
function preloadSurface() {
  let recorded = [];
  const ipcRenderer = {
    invoke(channel) { recorded.push({ mode: 'invoke', channel }); return Promise.resolve(); },
    send(channel) { recorded.push({ mode: 'send', channel }); },
    on(channel) { recorded.push({ mode: 'on', channel }); },
    removeAllListeners() {},
  };
  const context = {
    require(name) {
      if (name === 'electron') return { clipboard: { writeText() {}, readText: () => '' }, ipcRenderer };
      if (name === './config-loader') return { loadConfig: () => ({ config: {} }) };
      return require(name);
    },
    window: {},
    process,
    console,
  };
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8'),
    context,
    { filename: 'preload.js' },
  );

  const surface = new Map();
  for (const [name, fn] of Object.entries(context.window.cc)) {
    if (typeof fn !== 'function') continue;
    recorded = [];
    try { fn(() => {}); } catch (_) { /* argument shape is irrelevant here */ }
    surface.set(name, recorded.slice());
  }
  return surface;
}

const TABLE = fullTable();
const SURFACE = preloadSurface();

test('every window.cc method is accounted for by exactly one contract', () => {
  const unresolved = [];
  for (const [name, calls] of SURFACE) {
    if (calls.length === 0) {
      if (!PRELOAD_LOCAL[name]) unresolved.push(`${name}: no IPC channel and not listed in PRELOAD_LOCAL`);
      continue;
    }
    for (const { mode, channel } of calls) {
      if (WEB_LOCAL[channel] || WEB_UNSUPPORTED[channel] || UNIMPLEMENTED[channel]) continue;
      if (mode === 'on') {
        if (!PUSH_EVENTS.includes(channel)) unresolved.push(`${name}: push channel '${channel}' is not in PUSH_EVENTS`);
        continue;
      }
      if (!TABLE[channel]) unresolved.push(`${name}: channel '${channel}' has no handler and is in no documented set`);
    }
  }
  assert.deepEqual(unresolved, [], 'window.cc drifted from main/cc_handlers.js');
});

test('invoke/send mode matches preload for every handled channel', () => {
  const mismatched = [];
  for (const [name, calls] of SURFACE) {
    for (const { mode, channel } of calls) {
      if (mode === 'on' || !TABLE[channel]) continue;
      if (TABLE[channel].mode !== mode) {
        mismatched.push(`${name}: preload uses ipcRenderer.${mode}('${channel}') but the table declares '${TABLE[channel].mode}'`);
      }
    }
  }
  // A send channel answered as invoke leaves a web caller waiting forever, and
  // an invoke channel fired as send silently drops the reply.
  assert.deepEqual(mismatched, []);
});

test('no handler is unreachable from window.cc except the documented ones', () => {
  const reachable = new Set();
  for (const calls of SURFACE.values()) {
    for (const { channel } of calls) reachable.add(channel);
  }
  const orphans = Object.keys(TABLE).filter((channel) => !reachable.has(channel) && !UNCALLED[channel]);
  assert.deepEqual(orphans, [], 'these handlers are registered but no window.cc method calls them');
});

test('the documented sets describe reality and nothing more', () => {
  const channels = new Set();
  for (const calls of SURFACE.values()) {
    for (const { channel } of calls) channels.add(channel);
  }
  for (const [label, set] of [['WEB_LOCAL', WEB_LOCAL], ['WEB_UNSUPPORTED', WEB_UNSUPPORTED], ['UNIMPLEMENTED', UNIMPLEMENTED]]) {
    const stale = Object.keys(set).filter((c) => !channels.has(c));
    assert.deepEqual(stale, [], `${label} lists channels preload no longer has`);
  }
  assert.deepEqual(PUSH_EVENTS.filter((c) => !channels.has(c)), []);
  assert.deepEqual(Object.keys(PRELOAD_LOCAL).filter((n) => !SURFACE.has(n)), []);
  assert.deepEqual(Object.keys(UNCALLED).filter((c) => !TABLE[c]), [], 'UNCALLED lists channels that are not registered');
  // UNIMPLEMENTED must never overlap a channel that IS served.
  assert.deepEqual(Object.keys(UNIMPLEMENTED).filter((c) => TABLE[c]), []);
});

test('the terminal channels the web host depends on are present and correctly moded', () => {
  const expected = {
    'pty:create': 'invoke',
    'pty:kill': 'invoke',
    'pty:paste': 'invoke',
    'pty:check-session': 'invoke',
    'pty:new-session': 'invoke',
    'pty:save-image': 'invoke',
    'pty:write': 'send',
    'pty:resize': 'send',
    'pty:scroll': 'send',
    'pty:tmux-send': 'send',
    'pty:exit-copy-mode': 'send',
    'get-config': 'invoke',
  };
  for (const [channel, mode] of Object.entries(expected)) {
    assert.ok(TABLE[channel], `${channel} missing from the handler table`);
    assert.equal(TABLE[channel].mode, mode, `${channel} has the wrong mode`);
  }
});

test('the clipboard is never routed over the wire', () => {
  // A clipboard round trip would read the HOST's clipboard, not the viewer's.
  for (const channel of ['clipboard:read-text', 'clipboard:write-text']) {
    assert.ok(WEB_LOCAL[channel], `${channel} must be WEB_LOCAL`);
    assert.equal(TABLE[channel], undefined, `${channel} must not be servable over the websocket`);
  }
});

test('every refusable channel records the mode preload actually uses', () => {
  // The bridge refuses by this mode; getting it wrong would push an
  // unsolicited error frame at a send-mode caller that never asked for a reply.
  const preloadMode = new Map();
  for (const calls of SURFACE.values()) {
    for (const { mode, channel } of calls) if (mode !== 'on') preloadMode.set(channel, mode);
  }
  const wrong = [];
  for (const [label, set] of [['WEB_LOCAL', WEB_LOCAL], ['WEB_UNSUPPORTED', WEB_UNSUPPORTED]]) {
    for (const [channel, entry] of Object.entries(set)) {
      assert.equal(typeof entry.reason, 'string', `${label}.${channel} needs a reason`);
      if (entry.mode !== preloadMode.get(channel)) {
        wrong.push(`${label}.${channel}: declared '${entry.mode}', preload uses '${preloadMode.get(channel)}'`);
      }
    }
  }
  assert.deepEqual(wrong, []);
});

test('main.js registers the native channels and nothing the shared module owns', () => {
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  const registered = [...main.matchAll(/ipcMain\.(?:handle|on)\('([^']+)'/g)].map((m) => m[1]).sort();
  const native = [...Object.keys(WEB_LOCAL), ...Object.keys(WEB_UNSUPPORTED)]
    .filter((c) => c !== 'clipboard:read-text' && c !== 'clipboard:write-text' && c !== 'context-menu')
    .sort();
  assert.deepEqual(registered, native,
    'main.js must register exactly the Electron-native channels; everything else belongs to main/cc_handlers.js');
  // The clipboard pair comes from its own bridge, and context-menu has no
  // handler on either transport.
  assert.match(main, /registerClipboardIpc\(ipcMain, clipboard\)/);
});
