const test = require('node:test');
const assert = require('node:assert/strict');
const { buildCc } = require('../renderer/web_cc');

const {
  createCcHandlers,
  createCollector,
  WEB_LOCAL,
  WEB_UNSUPPORTED,
  UNIMPLEMENTED,
  UNCALLED,
  PUSH_EVENTS,
} = require('../main/cc_handlers');

// The table exactly as the web host builds it.
function fullTable() {
  const stubClient = new Proxy({}, { get: () => async () => ({}) });
  const collector = createCollector();
  createCcHandlers({ CONFIG: { chatStream: {} }, chatStreamClient: stubClient, harness: true }).register(collector);
  return collector.table;
}

// Run the browser surface with an in-process recorder; no socket is created.
// Calling the real methods preserves channel/mode coverage without a preload.
const BROWSER_LOCAL = new Set([
  'writeClipboard', 'readClipboard', 'openExternal', 'reloadApp',
  'openMeeting', 'closeMeeting', 'saveImage', 'showContextMenu',
  'chatPopoutContext', 'onReconnect', 'onDisconnect', 'webConnectionState',
]);
function browserSurface() {
  let recorded = [];
  const transport = {
    call(channel) { recorded.push({ mode: 'invoke', channel }); return Promise.resolve(); },
    fire(channel) { recorded.push({ mode: 'send', channel }); },
    on(channel) { recorded.push({ mode: 'on', channel }); },
    onReconnect() {}, onDisconnect() {},
  };
  const cc = buildCc(transport, {
    clipboard: { writeText() {}, readText: () => '' },
    reload() {}, config: { features: { mic: true } },
  });
  const surface = new Map();
  for (const [name, fn] of Object.entries(cc)) {
    if (typeof fn !== 'function') continue;
    recorded = [];
    fn(() => {});
    surface.set(name, recorded.slice());
  }
  return surface;
}

const TABLE = fullTable();
const SURFACE = browserSurface();

test('every window.cc method is accounted for by exactly one contract', () => {
  const unresolved = [];
  for (const [name, calls] of SURFACE) {
    if (calls.length === 0) {
      if (!BROWSER_LOCAL.has(name)) unresolved.push(`${name}: no transport channel and not listed in BROWSER_LOCAL`);
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

test('invoke/send mode matches the browser for every handled channel', () => {
  const mismatched = [];
  for (const [name, calls] of SURFACE) {
    for (const { mode, channel } of calls) {
      if (mode === 'on' || !TABLE[channel]) continue;
      if (TABLE[channel].mode !== mode) {
        mismatched.push(`${name}: browser uses ${mode}('${channel}') but the table declares '${TABLE[channel].mode}'`);
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
  // saveImage is a browser-local download; the host retains its existing handler.
  const orphans = Object.keys(TABLE).filter((channel) => !reachable.has(channel) && !UNCALLED[channel] && channel !== 'pty:save-image');
  assert.deepEqual(orphans, [], 'these handlers are registered but no window.cc method calls them');
});

test('the documented sets describe reality and nothing more', () => {
  const channels = new Set();
  for (const calls of SURFACE.values()) {
    for (const { channel } of calls) channels.add(channel);
  }
  for (const [label, set] of [['UNIMPLEMENTED', UNIMPLEMENTED]]) {
    const stale = Object.keys(set).filter((c) => !channels.has(c));
    assert.deepEqual(stale, [], `${label} lists channels the browser no longer has`);
  }
  assert.deepEqual(PUSH_EVENTS.filter((c) => !channels.has(c)), []);
  assert.deepEqual([...BROWSER_LOCAL].filter((name) => !SURFACE.has(name)), []);
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

test('every refusable channel records a reason', () => {
  // Browser-local methods do not send these channels. Keep the shared refusal
  // metadata assertion; the removed preload's IPC modes are no longer a surface.
  for (const [label, set] of [['WEB_LOCAL', WEB_LOCAL], ['WEB_UNSUPPORTED', WEB_UNSUPPORTED]]) {
    for (const [channel, entry] of Object.entries(set)) {
      assert.equal(typeof entry.reason, 'string', `${label}.${channel} needs a reason`);
    }
  }
});
