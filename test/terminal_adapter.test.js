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
  assert.deepEqual(calls, ['ssh.exe', 'ssh.exe', 'ssh.exe']);
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
      { execute: async (file, args) => { calls.push({ file, args }); return { stdout: args.includes('display-message') || args.at(-1).includes("'display-message'") ? `%${nextPane++}\n` : '' }; },
        pty: { spawn: () => ({ onData() {}, onExit() {}, kill() {} }) } });
    try {
      await handlers.get('pty:create')(first, 0, 'first', remote ? 'peer' : 'local');
      await handlers.get('pty:create')(second, 0, 'second', remote ? 'peer' : 'local');
      calls.length = 0;
      listeners.get('pty:scroll')(first, 0, 'up', 15);
      listeners.get('pty:scroll')(second, 0, 'down', 250);
      await new Promise(resolve => setImmediate(resolve));
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
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(calls.length, 3);
      if (remote) assert.match(calls[2].args.at(-1), /'copy-mode' '-t' '%41' '-q'$/);
      else assert.deepEqual(calls[2].args, ['copy-mode', '-t', '%41', '-q']);
    } finally { cleanup(); }
  });
}

test('paste waits for history exit, keeps order, and discards an attachment replaced during SSH', async () => {
  const handlers = new Map(), pending = [], writes = [], commands = [];
  const sender = new EventEmitter(); sender.id = 30; sender.isDestroyed = () => false;
  let pane = 50;
  const cleanup = registerTerminalIpc({ handle: (n, fn) => handlers.set(n, fn), on() {} },
    { hosts: { peer: { host: 'peer.example' } } }, {}, {
      execute: async (file, args) => {
        if (args.at(-1).includes('display-message')) return { stdout: `%${pane++}` };
        if (args.at(-1).includes('set-option')) return { stdout: '' };
        commands.push({ file, args }); return new Promise((resolve, reject) => pending.push({ resolve, reject }));
      },
      pty: { spawn: () => ({ onData() {}, onExit() {}, kill() {}, write: text => writes.push(text) }) },
    });
  const event = { sender }, flush = () => new Promise(resolve => setImmediate(resolve));
  try {
    await handlers.get('pty:create')(event, 0, 'fixture', 'peer');
    const first = handlers.get('pty:paste')(event, 0, 'one');
    const second = handlers.get('pty:paste')(event, 0, 'two');
    await flush(); assert.deepEqual(writes, []); assert.equal(commands.length, 1);
    assert.match(commands[0].args.at(-1), /'copy-mode' '-t' '%50' '-q'$/);
    pending.shift().resolve({ stdout: '' }); assert.equal(await first, true);
    await flush(); assert.deepEqual(writes, ['one']); assert.equal(commands.length, 2);
    pending.shift().resolve({ stdout: '' }); assert.equal(await second, true);
    assert.deepEqual(writes, ['one', 'two']);
    const stale = handlers.get('pty:paste')(event, 0, 'stale'); await flush();
    await handlers.get('pty:create')(event, 0, 'replacement', 'peer');
    pending.shift().resolve({ stdout: '' }); assert.equal(await stale, false);
    assert.deepEqual(writes, ['one', 'two']);
    const failed = handlers.get('pty:paste')(event, 0, 'failed');
    const rejects = assert.rejects(failed, /SSH unavailable/); await flush();
    pending.shift().reject(Error('SSH unavailable')); await rejects;
    assert.deepEqual(writes, ['one', 'two']);
    const recovered = handlers.get('pty:paste')(event, 0, 'recovered'); await flush();
    pending.shift().resolve({ stdout: '' }); assert.equal(await recovered, true);
    assert.deepEqual(writes, ['one', 'two', 'recovered']);
  } finally { cleanup(); }
});

for (const remote of [false, true]) {
  test(`attach awaits session-scoped mouse setup before spawning (${remote ? 'SSH' : 'local'})`, async () => {
    const handlers = new Map(), calls = [], spawns = [];
    const sender = new EventEmitter(); sender.id = 40; sender.isDestroyed = () => false;
    let finishSetup;
    const cleanup = registerTerminalIpc({ handle: (n, fn) => handlers.set(n, fn), on() {} },
      { hosts: { peer: { host: 'peer.example' } } }, {}, {
        execute: async (file, args) => {
          calls.push({ file, args });
          if (args.includes('display-message') || args.at(-1).includes('display-message')) return { stdout: '%60' };
          return new Promise(resolve => { finishSetup = resolve; });
        },
        pty: { spawn: (file, args) => { spawns.push({ file, args }); return { onData() {}, onExit() {}, kill() {} }; } },
      });
    try {
      const attached = handlers.get('pty:create')({ sender }, 0, 'fixture', remote ? 'peer' : 'local');
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(spawns.length, 0);
      if (remote) assert.equal(calls[1].args.at(-1), "LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 'tmux' 'set-option' '-t' '=fixture:' 'mouse' 'off' ';' 'set-option' '-w' '-t' '=fixture:' 'window-size' 'latest'");
      else assert.deepEqual(calls[1].args, ['set-option', '-t', '=fixture:', 'mouse', 'off', ';', 'set-option', '-w', '-t', '=fixture:', 'window-size', 'latest']);
      finishSetup({ stdout: '' }); assert.equal(await attached, '%60'); assert.equal(spawns.length, 1);
      await handlers.get('pty:kill')({ sender }, 0);
      assert.equal(calls.length, 2, 'detach never re-enables tmux mouse');
    } finally { cleanup(); }
  });
}

for (const failure of ['replace', 'destroy', 'setup-error']) {
  test(`pending attach cannot spawn after ${failure}`, async () => {
    const handlers = new Map(), spawns = [];
    const sender = new EventEmitter(); sender.id = 41; let destroyed = false; sender.isDestroyed = () => destroyed;
    let release, reject;
    const cleanup = registerTerminalIpc({ handle: (n, fn) => handlers.set(n, fn), on() {} }, {}, {}, {
      execute: async (_file, args) => args[0] === 'display-message' ? { stdout: '%70' } : new Promise((res, rej) => { release = res; reject = rej; }),
      pty: { spawn: () => { spawns.push(1); return { onData() {}, onExit() {}, kill() {} }; } },
    });
    try {
      const attached = handlers.get('pty:create')({ sender }, 0, 'fixture');
      const rejected = assert.rejects(attached, failure === 'setup-error' ? /setup failed/ : /superseded/);
      await new Promise(resolve => setImmediate(resolve));
      if (failure === 'replace') await handlers.get('pty:kill')({ sender }, 0);
      if (failure === 'destroy') { destroyed = true; sender.emit('destroyed'); }
      if (failure === 'setup-error') reject(Error('setup failed')); else release({ stdout: '' });
      await rejected; assert.equal(spawns.length, 0);
      assert.equal(await handlers.get('pty:paste')({ sender }, 0, 'stale'), false);
    } finally { cleanup(); }
  });
}

test('history exit precedes first-key, interrupt and Ctrl+Enter; subsequent typing adds no tmux calls', async () => {
  const handlers = new Map(), listeners = new Map(), pending = [], calls = [], writes = [];
  const sender = new EventEmitter(); sender.id = 45; sender.isDestroyed = () => false;
  const cleanup = registerTerminalIpc({ handle: (n, f) => handlers.set(n, f), on: (n, f) => listeners.set(n, f) }, {}, {}, {
    execute: async (_file, args) => {
      if (args[0] === 'display-message') return { stdout: '%80' };
      if (args[0] === 'set-option') return { stdout: '' };
      calls.push(args); return new Promise(resolve => pending.push(resolve));
    },
    pty: { spawn: () => ({ onData() {}, onExit() {}, kill() {}, write: data => writes.push(data) }) },
  });
  const event = { sender }, flush = () => new Promise(resolve => setImmediate(resolve));
  try {
    await handlers.get('pty:create')(event, 0, 'fixture');
    listeners.get('pty:scroll')(event, 0, 'up', 15);
    listeners.get('pty:exit-copy-mode')(event, 0);
    listeners.get('pty:write')(event, 0, 'x');
    listeners.get('pty:write')(event, 0, '\x03');
    listeners.get('pty:tmux-send')(event, 0, '-H', '1B', '5B', '31', '33', '3B', '35', '75');
    await flush(); assert.equal(calls.length, 1); assert.deepEqual(writes, []);
    pending.shift()({ stdout: '' }); await flush();
    assert.deepEqual(calls[1], ['copy-mode', '-t', '%80', '-q']); assert.deepEqual(writes, []);
    pending.shift()({ stdout: '' }); await flush();
    assert.deepEqual(writes, ['x', '\x03']);
    assert.deepEqual(calls[2], ['send-keys', '-t', '%80', '-H', '1B', '5B', '31', '33', '3B', '35', '75']);
    pending.shift()({ stdout: '' }); await flush();
    for (const ch of 'typing') { listeners.get('pty:exit-copy-mode')(event, 0); listeners.get('pty:write')(event, 0, ch); }
    await flush(); assert.equal(calls.length, 3); assert.equal(writes.slice(2).join(''), 'typing');
  } finally { cleanup(); }
});

test('wheel bursts coalesce without moving history changes across queued typing', async () => {
  const handlers = new Map(), listeners = new Map(), calls = [], pending = [], writes = [];
  const sender = new EventEmitter(); sender.id = 46; sender.isDestroyed = () => false;
  const cleanup = registerTerminalIpc({ handle: (n, f) => handlers.set(n, f), on: (n, f) => listeners.set(n, f) }, {}, {}, {
    execute: async (_file, args) => {
      if (args[0] === 'display-message') return { stdout: '%90' };
      if (args[0] === 'set-option') return { stdout: '' };
      calls.push(args); return new Promise(resolve => pending.push(resolve));
    }, pty: { spawn: () => ({ onData() {}, onExit() {}, kill() {}, write: data => writes.push(data) }) },
  });
  const event = { sender }, flush = () => new Promise(resolve => setImmediate(resolve));
  try {
    await handlers.get('pty:create')(event, 0, 'fixture');
    for (let i = 0; i < 5; i++) listeners.get('pty:scroll')(event, 0, 'up', 3);
    listeners.get('pty:write')(event, 0, 'barrier');
    for (let i = 0; i < 4; i++) listeners.get('pty:scroll')(event, 0, 'up', 2);
    await flush(); assert.equal(calls.length, 1); assert.deepEqual(calls[0].slice(-2), ['15', 'scroll-up']);
    pending.shift()({ stdout: '' }); await flush(); assert.deepEqual(writes, []);
    pending.shift()({ stdout: '' }); await flush();
    assert.deepEqual(writes, ['barrier']); assert.deepEqual(calls[2].slice(-2), ['8', 'scroll-up']);
    pending.shift()({ stdout: '' }); await flush(); assert.equal(calls.length, 3);
  } finally { cleanup(); }
});
