const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');

const { createAssetPopoutManager } = require('../main/asset_popout_windows');

class FakeWebContents extends EventEmitter {
  constructor() {
    super();
    this.sent = [];
  }

  send(channel, payload) {
    this.sent.push({ channel, payload });
  }
}

class FakeWindow extends EventEmitter {
  static nextId = 1;
  static instances = [];

  constructor(options) {
    super();
    this.id = FakeWindow.nextId++;
    this.options = options;
    this.webContents = new FakeWebContents();
    this.destroyed = false;
    this.focusCalls = 0;
    this.showCalls = 0;
    this.loaded = null;
    FakeWindow.instances.push(this);
  }

  isDestroyed() {
    return this.destroyed;
  }

  loadFile(file) {
    this.loaded = file;
    this.webContents.emit('did-finish-load');
  }

  focus() {
    this.focusCalls += 1;
  }

  show() {
    this.showCalls += 1;
  }

  close() {
    this.destroyed = true;
    this.emit('closed');
  }
}

test('asset pop-out registry prevents double-pop and broadcasts updates', () => {
  FakeWindow.instances = [];
  FakeWindow.nextId = 1;
  const manager = createAssetPopoutManager({
    BrowserWindow: FakeWindow,
    appRoot: '/tmp/public-desktop',
    getMainWindow: () => null,
  });

  const first = manager.open({
    stream_id: 'hostb:provider_c-1',
    asset_id: 'asset-1',
    asset: { session_key: { host: 'hostb', session_name: 'provider_c-1', stream_id: 'hostb:provider_c-1' } },
  });
  const second = manager.open({ stream_id: 'hostb:provider_c-1', asset_id: 'asset-1' });
  const third = manager.open({
    stream_id: 'hostb:provider_c-2',
    asset_id: 'asset-1',
    asset: { session_key: { host: 'hostb', session_name: 'provider_c-2', stream_id: 'hostb:provider_c-2' } },
  });

  assert.equal(first.reused, false);
  assert.equal(second.reused, true);
  assert.equal(third.reused, false);
  assert.equal(FakeWindow.instances.length, 2);
  assert.equal(FakeWindow.instances[0].focusCalls, 1);

  manager.broadcast('chat-stream:frame', { type: 'asset.update', asset_id: 'asset-1' });
  assert.equal(FakeWindow.instances[0].webContents.sent.at(-1).channel, 'chat-stream:frame');
  assert.equal(FakeWindow.instances[1].webContents.sent.at(-1).channel, 'chat-stream:frame');
});

test('asset pop-out registry docks and tears down by session', () => {
  FakeWindow.instances = [];
  FakeWindow.nextId = 1;
  const mainWindow = {
    webContents: new FakeWebContents(),
    isDestroyed: () => false,
  };
  const manager = createAssetPopoutManager({
    BrowserWindow: FakeWindow,
    appRoot: '/tmp/public-desktop',
    getMainWindow: () => mainWindow,
  });

  manager.open({
    stream_id: 'hostb:provider_c-1',
    asset_id: 'asset-1',
    asset: { session_key: { host: 'hostb', session_name: 'provider_c-1', stream_id: 'hostb:provider_c-1' } },
  });
  manager.dock({ asset_id: 'asset-1' });
  assert.equal(mainWindow.webContents.sent[0].channel, 'asset:dock');
  assert.equal(manager.size(), 0);

  manager.open({
    stream_id: 'hostb:provider_c-2',
    asset_id: 'asset-2',
    asset: { session_key: { host: 'hostb', session_name: 'provider_c-2', stream_id: 'hostb:provider_c-2' } },
  });
  manager.closeForSession({ host: 'hostb', session_name: 'provider_c-2' });
  assert.equal(manager.size(), 0);

  manager.open({
    stream_id: 'hostb:provider_c-3',
    asset_id: 'asset-3',
    asset: { session_key: { host: 'hostb', session_name: 'provider_c-3', stream_id: 'hostb:provider_c-3' } },
  });
  manager.broadcast('chat-stream:frame', {
    type: 'session.inventory',
    sessions: [{ host: 'hostb', session_name: 'provider_c-other', stream_id: 'hostb:provider_c-other' }],
  });
  assert.equal(manager.size(), 0);
});

test('asset pop-out raw frame inventory ignores malformed payloads and preserves a matching session', () => {
  FakeWindow.instances = [];
  FakeWindow.nextId = 1;
  const manager = createAssetPopoutManager({
    BrowserWindow: FakeWindow,
    appRoot: '/tmp/public-desktop',
    getMainWindow: () => null,
  });
  const open = () => manager.open({
    stream_id: 'hosta:provider_c-1',
    asset_id: 'asset-1',
    asset: { session_key: { host: 'hosta', session_name: 'provider_c-1', stream_id: 'hosta:provider_c-1' } },
  });

  open();
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

test('asset pop-out registry docks only the matching scoped asset id', () => {
  FakeWindow.instances = [];
  FakeWindow.nextId = 1;
  const mainWindow = {
    webContents: new FakeWebContents(),
    isDestroyed: () => false,
  };
  const manager = createAssetPopoutManager({
    BrowserWindow: FakeWindow,
    appRoot: '/tmp/public-desktop',
    getMainWindow: () => mainWindow,
  });

  manager.open({
    stream_id: 'hostb:provider_c-1',
    asset_id: 'shared-id',
    asset: { session_key: { host: 'hostb', session_name: 'provider_c-1', stream_id: 'hostb:provider_c-1' } },
  });
  manager.open({
    stream_id: 'hostb:provider_c-2',
    asset_id: 'shared-id',
    asset: { session_key: { host: 'hostb', session_name: 'provider_c-2', stream_id: 'hostb:provider_c-2' } },
  });

  manager.dock({ stream_id: 'hostb:provider_c-2', asset_id: 'shared-id' });

  assert.equal(manager.size(), 1);
  assert.equal(FakeWindow.instances[0].destroyed, false);
  assert.equal(FakeWindow.instances[1].destroyed, true);
  assert.equal(mainWindow.webContents.sent[0].payload.stream_id, 'hostb:provider_c-2');
});
