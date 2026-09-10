'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { registerTerminalIpc } = require('../main/terminal_adapter');

test('terminal attachment targets a real pane and fences stale output after replacement', async () => {
  const handlers = new Map(), listeners = new Map(), processes = [], calls = [], output = [];
  const sender = new EventEmitter(); sender.id = 7; sender.isDestroyed = () => false; sender.send = (...args) => output.push(args);
  const native = { spawn(file, args) { const proc = { killed: false, onData(fn) { this.data = fn; }, onExit(fn) { this.exit = fn; }, kill() { this.killed = true; }, write() {}, resize() {} }; processes.push(proc); calls.push({ file, args }); return proc; } };
  const cleanup = registerTerminalIpc({ handle: (name, fn) => handlers.set(name, fn), on: (name, fn) => listeners.set(name, fn) }, { tmux: 'fixture-tmux' }, {},
    { pty: native, execute: async () => ({ stdout: '%42\n' }) });
  const event = { sender };
  assert.equal(await handlers.get('pty:create')(event, 0, 'first', 'local'), '%42');
  processes[0].data('first output');
  await handlers.get('pty:create')(event, 0, 'second', 'local');
  processes[0].data('stale output');
  processes[0].exit({ exitCode: 0 });
  processes[1].data('second output');
  assert.deepEqual(output, [['pty:data', 0, 'first output'], ['pty:data', 0, 'second output']]);
  assert.equal(processes[0].killed, true);
  assert.deepEqual(calls[1].args, ['-u', 'attach-session', '-t', '=second']);
  cleanup(); assert.equal(processes[1].killed, true);
});


test('Windows remote terminal uses an executable filename that ConPTY can resolve', async () => {
  const handlers = new Map(), calls = [];
  const sender = new EventEmitter(); sender.id = 9; sender.isDestroyed = () => false; sender.send = () => {};
  const native = { spawn(file) {
    if (!file.endsWith('.exe')) throw new Error('File not found: ');
    calls.push(file);
    return { onData() {}, onExit() {}, kill() {} };
  } };
  const cleanup = registerTerminalIpc({ handle: (name, fn) => handlers.set(name, fn), on() {} },
    { hosts: { workstation: { host: 'workstation.example', user: 'operator' } } }, {},
    { platform: 'win32', pty: native, execute: async (file) => { calls.push(file); return { stdout: '%42\n' }; } });
  assert.equal(await handlers.get('pty:create')({ sender }, 0, 'session', 'workstation'), '%42');
  assert.deepEqual(calls, ['ssh.exe', 'ssh.exe']);
  cleanup();
});

 test('SSH attachment forces UTF-8 even when the server does not accept locale forwarding', async () => {
  const handlers = new Map(); let spawned;
  const sender = new EventEmitter(); sender.id = 11; sender.isDestroyed = () => false;
  const cleanup = registerTerminalIpc({ handle: (n, fn) => handlers.set(n, fn), on() {} },
    { hosts: { peer: { host: 'peer.example' } } }, {},
    { execute: async () => ({ stdout: '%3' }), pty: { spawn(file, args, options) { spawned = { args, options }; return { onData() {}, onExit() {}, kill() {} }; } } });
  await handlers.get('pty:create')({ sender }, 0, 'utf8-fixture', 'peer');
  assert.match(spawned.args.at(-1), /LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8/);
  assert.match(spawned.args.at(-1), /'-u' 'attach-session'/);
  assert.equal(spawned.options.env.LANG, 'en_US.UTF-8');
  cleanup();
});
