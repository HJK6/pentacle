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
  assert.deepEqual(calls[1].args, ['attach-session', '-t', '=second']);
  cleanup(); assert.equal(processes[1].killed, true);
});
