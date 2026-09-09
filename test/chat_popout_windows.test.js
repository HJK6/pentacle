const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');

const { createChatPopoutManager } = require('../main/chat_popout_windows');

class FakeWebContents extends EventEmitter {
  constructor() { super(); this.sent = []; }
  send(channel, payload) { this.sent.push({ channel, payload }); }
}

class FakeWindow extends EventEmitter {
  static instances = [];
  static nextId = 1;
  constructor(options) {
    super();
    this.id = FakeWindow.nextId++;
    this.options = options;
    this.webContents = new FakeWebContents();
    this.destroyed = false;
    this.focusCalls = 0;
    FakeWindow.instances.push(this);
  }
  isDestroyed() { return this.destroyed; }
  loadFile(file) { this.loaded = file; }
  focus() { this.focusCalls += 1; }
  show() {}
  close() { this.destroyed = true; this.emit('closed'); }
}

function reset() { FakeWindow.instances = []; FakeWindow.nextId = 1; }
function args(stream = 'hosta:provider_c-1') {
  return { stream_id: stream, host: 'hosta', session_name: stream.split(':')[1], title: 'A chat' };
}

test('chat registry reuses a stream and supplies synchronous child context', () => {
  reset();
  const manager = createChatPopoutManager({ BrowserWindow: FakeWindow, appRoot: '/tmp/public-desktop' });
  assert.equal(manager.open(args()).reused, false);
  assert.equal(manager.open(args()).reused, true);
  assert.equal(FakeWindow.instances.length, 1);
  assert.equal(FakeWindow.instances[0].focusCalls, 1);
  const contextArg = FakeWindow.instances[0].options.webPreferences.additionalArguments[0];
  assert.match(contextArg, /^--pentacle-chat-popout=/);
  assert.match(decodeURIComponent(contextArg), /"stream_id":"hosta:provider_c-1"/);
});

test('chat registry broadcasts and docks only the matching child', () => {
  reset();
  const mainWindow = { webContents: new FakeWebContents(), isDestroyed: () => false };
  const manager = createChatPopoutManager({ BrowserWindow: FakeWindow, appRoot: '/tmp/public-desktop', getMainWindow: () => mainWindow });
  manager.open(args('hosta:provider_c-1'));
  manager.open(args('hosta:provider_c-2'));
  manager.broadcast('chat-stream:frame', { type: 'chat.event' });
  assert.equal(FakeWindow.instances.every((window) => window.webContents.sent.length === 1), true);
  assert.deepEqual(manager.dock({ stream_id: 'hosta:provider_c-2' }), { ok: true, docked: true });
  assert.equal(FakeWindow.instances[0].destroyed, false);
  assert.equal(FakeWindow.instances[1].destroyed, true);
  assert.equal(mainWindow.webContents.sent[0].channel, 'chat:popout-dock');
});

test('chat registry closes a vanished session from the raw frame channel', () => {
  reset();
  const manager = createChatPopoutManager({ BrowserWindow: FakeWindow, appRoot: '/tmp/public-desktop' });
  manager.open(args());
  manager.broadcast('chat-stream:frame', {
    type: 'session.inventory',
    sessions: [{ host: 'hosta', session_name: 'other' }],
  });
  assert.equal(manager.size(), 0);
});

test('chat raw frame inventory ignores malformed payloads and preserves a matching session', () => {
  reset();
  const manager = createChatPopoutManager({ BrowserWindow: FakeWindow, appRoot: '/tmp/public-desktop' });
  manager.open(args());
  for (const sessions of [undefined, null, { session_name: 'provider_c-1' }]) {
    manager.broadcast('chat-stream:frame', { type: 'session.inventory', sessions });
    assert.equal(manager.size(), 1);
  }
  manager.broadcast('chat-stream:frame', {
    type: 'session.inventory',
    sessions: [{ host: 'hosta', session_name: 'provider_c-1' }],
  });
  assert.equal(manager.size(), 1);
});
