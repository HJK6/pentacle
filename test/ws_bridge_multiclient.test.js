'use strict';

// ── Two browser connections over one web host ────────────────────────────────
// Drives the REAL shared handler table (main/cc_handlers.js + terminal_adapter)
// through server/ws_bridge.js with two fake sockets, using a fake pty and a
// fake tmux, to prove the multi-client contract lane 2 owns:
//   1. slot isolation      — one connection's pty bytes never reach another's
//   2. broadcast           — daemon frames reach every connection
//   3. disconnect cleanup  — closing a socket tears down only its own ptys
//   4. per-connection cap  — a connection cannot exceed its terminal ceiling
//
// The isolation and cleanup here are behaviours lane 1 already provides
// (terminal_adapter keys slots by event.sender.id and tears down on the
// socket's `destroyed` hook); this test pins them against regression. The cap
// is new in lane 2.

const test = require('node:test');
const assert = require('node:assert/strict');

const { createWsBridge } = require('../server/ws_bridge');
const { createCcHandlers, createCollector } = require('../main/cc_handlers');

function fakeSocket(name) {
  return { name, frames: [], readyState: 1, send(text) { this.frames.push(JSON.parse(text)); } };
}

// node-pty stand-in: write(data) echoes it straight back through onData, so a
// pty:write round-trips to a pty:data push on the owning connection's socket.
function makePty() {
  const procs = [];
  return {
    procs,
    spawn() {
      let onData = () => {};
      let onExit = () => {};
      const proc = {
        killed: false,
        onData(cb) { onData = cb; },
        onExit(cb) { onExit = cb; },
        write(data) { onData(String(data)); },
        resize() {},
        kill() { this.killed = true; onExit({ exitCode: 0 }); },
      };
      procs.push(proc);
      return proc;
    },
  };
}

// tmux is never really invoked: the pane lookup returns a pane id, and every
// other verb (set-option, copy-mode) succeeds silently.
async function fakeExecute(_file, args) {
  if (Array.isArray(args) && args.includes('display-message')) return { stdout: '%1\n' };
  return { stdout: '' };
}

function buildBridge({ maxPtysPerConnection = null } = {}) {
  const pty = makePty();
  const stubClient = new Proxy({}, { get: () => async () => ({}) });
  const collector = createCollector();
  createCcHandlers({
    CONFIG: { chatStream: {} },
    chatStreamClient: stubClient,
    terminalOptions: { pty, execute: fakeExecute, maxPtysPerConnection },
  }).register(collector);
  const bridge = createWsBridge({ table: collector.table, logger: { warn() {} } });
  return { bridge, pty };
}

async function waitFor(predicate, { timeoutMs = 1000, label = 'condition' } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

// invoke: resolves with the correlated response frame for `id`.
async function invoke(bridge, socket, id, method, ...args) {
  await bridge.handleMessage(socket, JSON.stringify({ id, method, args }));
  return socket.frames.find((frame) => frame.id === id);
}
// send: fire-and-forget, exactly as the browser does (no id).
function fire(bridge, socket, method, ...args) {
  return bridge.handleMessage(socket, JSON.stringify({ method, args }));
}
const ptyData = (socket, slot, token) => socket.frames.some(
  (f) => f.event === 'pty:data' && f.args[0] === slot && String(f.args[1]).includes(token));

test('each connection\'s pty bytes stay isolated to its own socket', async () => {
  const { bridge } = buildBridge();
  const a = fakeSocket('a');
  const b = fakeSocket('b');
  bridge.addSocket(a);
  bridge.addSocket(b);

  const ca = await invoke(bridge, a, 1, 'pty:create', 0, 'sessA', 'local', 80, 24);
  const cb = await invoke(bridge, b, 1, 'pty:create', 0, 'sessB', 'local', 80, 24);
  assert.equal(ca.ok, true, 'A attaches its slot');
  assert.match(String(ca.result), /^%\d+$/);
  assert.equal(cb.ok, true, 'B attaches its own slot on the same slot index');

  await fire(bridge, a, 'pty:write', 0, 'HELLO-A\r');
  await waitFor(() => ptyData(a, 0, 'HELLO-A'), { label: 'A receives its own echo' });
  assert.ok(!b.frames.some((f) => f.event === 'pty:data'), 'B must never see A\'s pty bytes');

  await fire(bridge, b, 'pty:write', 0, 'HELLO-B\r');
  await waitFor(() => ptyData(b, 0, 'HELLO-B'), { label: 'B receives its own echo' });
  assert.ok(!a.frames.some((f) => f.event === 'pty:data' && String(f.args[1]).includes('HELLO-B')),
    'A must never see B\'s pty bytes');
});

test('daemon frames broadcast to every connection', async () => {
  const { bridge } = buildBridge();
  const a = fakeSocket('a');
  const b = fakeSocket('b');
  bridge.addSocket(a);
  bridge.addSocket(b);

  bridge.broadcast('chat-stream:frame', { type: 'session.update' });

  const gotFrame = (s) => s.frames.some((f) => f.event === 'chat-stream:frame'
    && f.args[0] && f.args[0].type === 'session.update');
  assert.ok(gotFrame(a) && gotFrame(b), 'both connections receive the same frame');
});

test('closing one socket tears down only its ptys; the other stays live', async () => {
  const { bridge, pty } = buildBridge();
  const a = fakeSocket('a');
  const b = fakeSocket('b');
  bridge.addSocket(a);
  bridge.addSocket(b);

  await invoke(bridge, a, 1, 'pty:create', 0, 'sessA', 'local', 80, 24);
  await invoke(bridge, b, 1, 'pty:create', 0, 'sessB', 'local', 80, 24);
  const [procA, procB] = pty.procs;

  bridge.removeSocket(a);
  assert.equal(procA.killed, true, 'A\'s terminal is killed when A disconnects');
  assert.equal(procB.killed, false, 'B\'s terminal is untouched');

  // B keeps working after A is gone.
  await fire(bridge, b, 'pty:write', 0, 'STILL-B\r');
  await waitFor(() => ptyData(b, 0, 'STILL-B'), { label: 'B still echoes after A left' });
});

test('a connection cannot exceed its per-connection terminal cap', async () => {
  const { bridge } = buildBridge({ maxPtysPerConnection: 2 });
  const a = fakeSocket('a');
  const b = fakeSocket('b');
  bridge.addSocket(a);
  bridge.addSocket(b);

  assert.equal((await invoke(bridge, a, 1, 'pty:create', 0, 's0', 'local', 80, 24)).ok, true);
  assert.equal((await invoke(bridge, a, 2, 'pty:create', 1, 's1', 'local', 80, 24)).ok, true);

  const over = await invoke(bridge, a, 3, 'pty:create', 2, 's2', 'local', 80, 24);
  assert.equal(over.ok, false, 'the third pty on one connection is refused');
  assert.match(over.error.message, /terminal limit/i);

  // Re-creating an already-owned slot is a replacement, not a new allocation.
  assert.equal((await invoke(bridge, a, 4, 'pty:create', 0, 's0b', 'local', 80, 24)).ok, true,
    'replacing an existing slot is allowed at the cap');

  // The cap is per-connection: B is unaffected by A being full.
  assert.equal((await invoke(bridge, b, 1, 'pty:create', 0, 's0', 'local', 80, 24)).ok, true,
    'a different connection has its own budget');
});
