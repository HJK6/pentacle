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

for (const remote of [false, true]) {
  test(`scroll enters copy-mode and routes both commands to the window's pane (${remote ? 'SSH' : 'local'})`, async () => {
    const handlers = new Map(), listeners = new Map(), calls = [];
    const makeEvent = (id) => {
      const sender = new EventEmitter(); sender.id = id; sender.isDestroyed = () => false;
      return { sender };
    };
    const first = makeEvent(20), second = makeEvent(21);
    let nextPane = 40;
    const cleanup = registerTerminalIpc({ handle: (n, fn) => handlers.set(n, fn), on: (n, fn) => listeners.set(n, fn) },
      { tmux: 'fixture-tmux', hosts: { peer: { host: 'peer.example', user: 'operator', tmux: '/usr/bin/tmux' } } }, {},
      { execute: async (file, args) => { calls.push({ file, args }); return { stdout: `%${nextPane++}\n` }; },
        pty: { spawn: () => ({ onData() {}, onExit() {}, kill() {} }) } });
    try {
      await handlers.get('pty:create')(first, 0, 'first', remote ? 'peer' : 'local');
      await handlers.get('pty:create')(second, 0, 'second', remote ? 'peer' : 'local');
      calls.length = 0;
      listeners.get('pty:scroll')(first, 0, 'up', 15);
      listeners.get('pty:scroll')(second, 0, 'down', 250);
      assert.equal(calls.length, 2);
      for (const [index, pane, count, direction] of [[0, '%40', '15', 'scroll-up'], [1, '%41', '100', 'scroll-down']]) {
        if (remote) {
          assert.equal(calls[index].file, process.platform === 'win32' ? 'ssh.exe' : 'ssh');
          assert.deepEqual(calls[index].args.slice(0, -1), ['-tt', '-p', '22', '--', 'operator@peer.example']);
          assert.equal(calls[index].args.at(-1), `LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 '/usr/bin/tmux' 'copy-mode' '-t' '${pane}' '-e' ';' 'send-keys' '-t' '${pane}' '-X' '-N' '${count}' '${direction}'`);
        } else {
          assert.equal(calls[index].file, 'fixture-tmux');
          assert.deepEqual(calls[index].args, ['copy-mode', '-t', pane, '-e', ';', 'send-keys', '-t', pane, '-X', '-N', count, direction]);
        }
      }
      await handlers.get('pty:kill')(first, 0);
      listeners.get('pty:scroll')(first, 0, 'up', 15);
      assert.equal(calls.length, 2, 'a detached slot cannot scroll another window');
      listeners.get('pty:exit-copy-mode')(second, 0);
      assert.equal(calls.length, 3);
      if (remote) assert.match(calls[2].args.at(-1), /'send-keys' '-t' '%41' '-X' 'cancel'$/);
      else assert.deepEqual(calls[2].args, ['send-keys', '-t', '%41', '-X', 'cancel']);
    } finally { cleanup(); }
  });
}
