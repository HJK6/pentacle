const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const {
  buildCc, buildHost, createTransport, browserClipboard, chatPopoutContextFromSearch,
  showWebToast, browserDownloadImage, webContextMenuItems, renderWebContextMenu,
} = require('../renderer/web_cc');

// ── a minimal DOM, enough for the toast / download / context-menu shims ──────
function makeEl(tag) {
  return {
    tag, style: {}, children: [], _listeners: {}, id: '', textContent: '', href: '', download: '', parent: null,
    setAttribute(k, v) { this['_' + k] = v; },
    appendChild(c) { c.parent = this; this.children.push(c); return c; },
    remove() { const p = this.parent; if (!p) return; const i = p.children.indexOf(this); if (i >= 0) p.children.splice(i, 1); this.parent = null; },
    addEventListener(n, fn) { (this._listeners[n] || (this._listeners[n] = [])).push(fn); },
    removeEventListener() {},
    click() { for (const fn of this._listeners.click || []) fn(); },
    contains(node) { return node === this || this.children.some((c) => c.contains && c.contains(node)); },
  };
}
function fakeDocument() {
  const doc = {
    body: makeEl('body'),
    created: [],
    createElement(tag) { const el = makeEl(tag); this.created.push(el); return el; },
    getElementById(id) {
      const walk = (el) => { if (el.id === id) return el; for (const c of el.children) { const r = walk(c); if (r) return r; } return null; };
      return walk(this.body);
    },
    addEventListener() {}, removeEventListener() {},
  };
  return doc;
}
function fakeTransportWithEmit() {
  const t = fakeTransport();
  const listeners = new Map();
  t.emit = (event, ...args) => { const h = listeners.get(event); if (h) h(...args); };
  t.on = (event, handler) => { listeners.set(event, handler); };
  t.listeners = listeners;
  return t;
}

function preloadCcKeys() {
  const ipcRenderer = { invoke: () => Promise.resolve(), send() {}, on() {}, removeAllListeners() {} };
  const context = {
    require(name) {
      if (name === 'electron') return { clipboard: { writeText() {}, readText: () => '' }, ipcRenderer };
      if (name === './config-loader') return { loadConfig: () => ({ config: {} }) };
      return require(name);
    },
    window: {},
    process,
  };
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8'),
    context,
    { filename: 'preload.js' },
  );
  return Object.keys(context.window.cc).sort();
}

function fakeTransport() {
  const calls = [];
  const fires = [];
  const listeners = new Map();
  return {
    calls,
    fires,
    listeners,
    call(method, ...args) { calls.push({ method, args }); return Promise.resolve({ method }); },
    fire(method, ...args) { fires.push({ method, args }); return undefined; },
    on(event, handler) { listeners.set(event, handler); },
  };
}

test('the web shim exposes exactly the window.cc surface preload does', () => {
  const cc = buildCc(fakeTransport(), { clipboard: { writeText() {}, readText: () => '' }, chatPopoutContext: null, reload() {} });
  assert.deepEqual(Object.keys(cc).sort(), preloadCcKeys(),
    'renderer/web_cc.js must stay method-for-method with preload.js');
});

test('send-mode methods return undefined and invoke-mode methods return promises', () => {
  const transport = fakeTransport();
  const cc = buildCc(transport, { clipboard: { writeText() {}, readText: () => '' }, chatPopoutContext: null, reload() {} });

  assert.equal(cc.writePty(0, 'x'), undefined);
  assert.equal(cc.resizePty(0, 80, 24), undefined);
  assert.equal(cc.tmuxSend(0, '-H', '1B'), undefined);
  assert.equal(cc.scrollTmux(0, 'up'), undefined);
  assert.equal(cc.exitCopyMode(0), undefined);
  assert.equal(cc.reloadApp(), undefined, 'reloadApp is local and answers nothing');
  assert.ok(cc.createPty(0, 's', 'local', 80, 24) instanceof Promise);
  assert.ok(cc.killPty(0) instanceof Promise);
  assert.ok(cc.pastePty(0, 'text') instanceof Promise);
  assert.ok(cc.getConfig() instanceof Promise);

  assert.deepEqual(transport.fires.map((f) => f.method), [
    'pty:write', 'pty:resize', 'pty:tmux-send', 'pty:scroll', 'pty:exit-copy-mode',
  ]);
  assert.deepEqual(transport.calls.map((c) => c.method), ['pty:create', 'pty:kill', 'pty:paste', 'get-config']);
});

test('hostId defaults match preload for every defaulted argument', () => {
  const transport = fakeTransport();
  const cc = buildCc(transport, { clipboard: { writeText() {}, readText: () => '' }, chatPopoutContext: null, reload() {} });

  cc.createPty(0, 'sess');
  cc.checkSession('sess');
  cc.newSession('codex');
  cc.chatSend(undefined, 'sess', 'hi');
  cc.scrollTmux(0, 'up');

  assert.deepEqual(transport.calls[0].args, [0, 'sess', 'local', undefined, undefined]);
  assert.deepEqual(transport.calls[1].args, ['sess', 'local']);
  assert.deepEqual(transport.calls[2].args, ['codex', 'local']);
  assert.deepEqual(transport.calls[3].args, ['local', 'sess', 'hi']);
  assert.deepEqual(transport.fires[0].args, [0, 'up', 1], 'scroll defaults to one line, as preload does');
});

test('push subscriptions replace the previous handler, as removeAllListeners does', () => {
  const transport = fakeTransport();
  const cc = buildCc(transport, { clipboard: { writeText() {}, readText: () => '' }, chatPopoutContext: null, reload() {} });
  const seen = [];

  cc.onPtyData(() => seen.push('first'));
  cc.onPtyData((slot, data) => seen.push(`second:${slot}:${data}`));
  transport.listeners.get('pty:data')(2, 'hello');

  assert.deepEqual(seen, ['second:2:hello']);
});

test('openExternal opens a tab for http(s) and refuses anything else', async () => {
  const opened = [];
  const realOpen = global.window;
  global.window = { open: (...args) => opened.push(args) };
  try {
    const cc = buildCc(fakeTransport(), { clipboard: { writeText() {}, readText: () => '' }, chatPopoutContext: null, reload() {} });
    // Mirrors main.js openPublicUrl: the same shape for the same inputs.
    assert.deepEqual(await cc.openExternal('https://example.com/x'), { ok: true });
    assert.deepEqual(await cc.openExternal('file:///etc/passwd'), { ok: false, error: 'unsupported URL' });
    assert.deepEqual(await cc.openExternal('not a url'), { ok: false, error: 'unsupported URL' });
    assert.equal(opened.length, 1);
    assert.equal(opened[0][1], '_blank');
  } finally {
    global.window = realOpen;
  }
});

test('clipboard methods delegate to the injected clipboard and never hit the wire', () => {
  const written = [];
  const transport = fakeTransport();
  const clipboard = { writeText: (t) => { written.push(t); }, readText: async () => 'from-clipboard' };
  const cc = buildCc(transport, { clipboard, chatPopoutContext: null, reload() {} });

  cc.writeClipboard(null);
  cc.writeClipboard('copied');

  assert.deepEqual(written, ['', 'copied']);
  assert.deepEqual(transport.calls, [], 'the clipboard must never be routed to the host');
  assert.deepEqual(transport.fires, []);
  return cc.readClipboard().then((v) => assert.equal(v, 'from-clipboard'));
});

test('the browser clipboard falls back to an empty read rather than throwing', async () => {
  const previous = global.navigator;
  global.navigator = { clipboard: { readText: () => Promise.reject(new Error('denied')) } };
  try {
    assert.equal(await browserClipboard().readText(), '');
  } finally {
    global.navigator = previous;
  }
});

test('window.HOST mirrors preload’s synchronous host metadata', () => {
  assert.deepEqual(
    buildHost({ hostname: 'amaterasu', platform: 'linux', isClient: false, dashboardHub: { url: 'http://h' } }),
    {
      hostname: 'amaterasu',
      platform: 'linux',
      isClient: false,
      hasRemote: false,
      hasDashboardHub: true,
      dashboardHubConfig: { url: 'http://h' },
    },
  );
  assert.equal(buildHost({ remote: { host: 'x' } }).isClient, true, 'a remote block means client mode');
  assert.equal(buildHost({}).hasDashboardHub, false);
  assert.equal(buildHost({ dashboardHub: {} }).dashboardHubConfig, null, 'a hub without a url is not configured');
});

test('the chat popout context comes from the query string', () => {
  const ctx = { stream_id: 'amaterasu:v2-1', host: 'amaterasu', session_name: 'sess', title: 'T' };
  const search = `?pentacle-chat-popout=${encodeURIComponent(JSON.stringify(ctx))}`;

  assert.deepEqual(chatPopoutContextFromSearch(search), {
    stream_id: 'amaterasu:v2-1',
    host: 'amaterasu',
    desktop_host: 'amaterasu',
    session_name: 'sess',
    title: 'T',
  });
  // Regression: URLSearchParams already decodes, so a second decodeURIComponent
  // ate a literal % in a title and could turn a valid context into null.
  const percent = { stream_id: 'amaterasu:v2-1', host: 'amaterasu', session_name: 'sess', title: '100% done' };
  assert.equal(
    chatPopoutContextFromSearch(`?pentacle-chat-popout=${encodeURIComponent(JSON.stringify(percent))}`).title,
    '100% done',
  );
  assert.equal(chatPopoutContextFromSearch(''), null);
  assert.equal(chatPopoutContextFromSearch('?pentacle-chat-popout=not-json'), null);
  assert.equal(chatPopoutContextFromSearch('?pentacle-chat-popout=%7B%22host%22%3A%22x%22%7D'), null,
    'an incomplete context is rejected, matching preload');
});

// ── transport behaviour, with a stand-in WebSocket ───────────────────────────

function installFakeWebSocket() {
  const instances = [];
  class FakeWebSocket {
    static OPEN = 1;
    static CLOSED = 3;
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.sent = [];
      this.handlers = {};
      instances.push(this);
    }
    addEventListener(name, fn) { (this.handlers[name] ||= []).push(fn); }
    send(text) { this.sent.push(JSON.parse(text)); }
    close() { this.readyState = FakeWebSocket.CLOSED; this.emit('close', {}); }
    emit(name, event) { for (const fn of this.handlers[name] || []) fn(event); }
    open() { this.readyState = FakeWebSocket.OPEN; this.emit('open', {}); }
    deliver(payload) { this.emit('message', { data: JSON.stringify(payload) }); }
  }
  const previous = global.WebSocket;
  global.WebSocket = FakeWebSocket;
  return { instances, restore() { global.WebSocket = previous; } };
}

test('calls made before the socket opens are queued and flushed on connect', (t) => {
  const fake = installFakeWebSocket();
  const transport = createTransport({ url: 'ws://host/cc', logger: { warn() {} } });
  // Close the transport before restoring the real WebSocket, so a pending
  // reconnect timer cannot fire against the restored global.
  t.after(() => { transport.close(); fake.restore(); });
  const socket = fake.instances[0];

  const pending = transport.call('get-config');
  transport.fire('pty:write', 0, 'queued');
  assert.deepEqual(socket.sent, [], 'nothing goes out before the socket is open');

  socket.open();
  assert.deepEqual(socket.sent.map((f) => f.method), ['get-config', 'pty:write']);

  socket.deliver({ id: socket.sent[0].id, ok: true, result: { appName: 'P' } });
  return pending.then((result) => assert.deepEqual(result, { appName: 'P' }));
});

test('a dropped socket rejects every in-flight request instead of hanging', async (t) => {
  const fake = installFakeWebSocket();
  const transport = createTransport({ url: 'ws://host/cc', logger: { warn() {} } });
  // Close the transport before restoring the real WebSocket, so a pending
  // reconnect timer cannot fire against the restored global.
  t.after(() => { transport.close(); fake.restore(); });
  const socket = fake.instances[0];
  socket.open();

  const pending = transport.call('chat-stream:get-state');
  socket.close();

  await assert.rejects(pending, /connection lost/);
});

test('an error response rejects with the code the host sent', async (t) => {
  const fake = installFakeWebSocket();
  const transport = createTransport({ url: 'ws://host/cc', logger: { warn() {} } });
  // Close the transport before restoring the real WebSocket, so a pending
  // reconnect timer cannot fire against the restored global.
  t.after(() => { transport.close(); fake.restore(); });
  const socket = fake.instances[0];
  socket.open();

  const pending = transport.call('meeting:open');
  socket.deliver({ id: socket.sent[0].id, ok: false, error: { code: 'web_unsupported', message: 'native BrowserWindow' } });

  await assert.rejects(pending, (e) => e.code === 'web_unsupported' && /native BrowserWindow/.test(e.message));
});

test('push frames reach the registered listener and unknown ones are ignored', (t) => {
  const fake = installFakeWebSocket();
  const transport = createTransport({ url: 'ws://host/cc', logger: { warn() {} } });
  // Close the transport before restoring the real WebSocket, so a pending
  // reconnect timer cannot fire against the restored global.
  t.after(() => { transport.close(); fake.restore(); });
  const socket = fake.instances[0];
  socket.open();
  const frames = [];
  transport.on('chat-stream:frame', (frame) => frames.push(frame));

  socket.deliver({ event: 'chat-stream:frame', args: [{ type: 'session.update' }] });
  socket.deliver({ event: 'nobody-listening', args: [1] });
  socket.deliver('not-an-object');

  assert.deepEqual(frames, [{ type: 'session.update' }]);
});

test('the transport reconnects after a drop', (t) => {
  const fake = installFakeWebSocket();
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const transport = createTransport({ url: 'ws://host/cc', logger: { warn() {} } });
  t.after(() => { transport.close(); fake.restore(); });

  fake.instances[0].open();
  fake.instances[0].close();
  t.mock.timers.tick(5000);

  assert.equal(fake.instances.length, 2, 'a dropped socket is replaced');
});

// ── native-method browser shims (lane 2) ─────────────────────────────────────

test('showWebToast renders into the DOM and is a no-op without one', () => {
  const doc = fakeDocument();
  assert.equal(showWebToast('hi there', { document: doc }), true);
  assert.equal(doc.body.children.length, 1);
  assert.equal(doc.body.children[0].textContent, 'hi there');
  assert.equal(showWebToast('x', { document: null }), false, 'no DOM → no throw, no toast');
});

test('saveImage hands the paste to the viewer as a download, never over the wire', async () => {
  const doc = fakeDocument();
  const res = browserDownloadImage('QUJD', { document: doc });
  assert.deepEqual(res, { ok: true });
  const anchor = doc.created.find((el) => el.tag === 'a');
  assert.equal(anchor.href, 'data:image/png;base64,QUJD');
  assert.match(anchor.download, /^pentacle-paste-\d+\.png$/);
  assert.equal(browserDownloadImage('QUJD', { document: null }).ok, false, 'no DOM → a clean failure, not a throw');

  // Through buildCc: it returns a promise (invoke shape) and never touches the wire.
  const realDoc = global.document;
  global.document = fakeDocument();
  try {
    const transport = fakeTransport();
    const cc = buildCc(transport, { clipboard: { writeText() {}, readText: () => '' }, chatPopoutContext: null, reload() {} });
    assert.ok(cc.saveImage('QUJD') instanceof Promise);
    assert.deepEqual(await cc.saveImage('QUJD'), { ok: true });
    assert.deepEqual(transport.calls, [], 'saveImage must not round-trip to the host');
    assert.deepEqual(transport.fires, []);
  } finally { global.document = realDoc; }
});

test('meeting and mic surface a toast only when the mic feature is on, and never hit the wire', async () => {
  const realDoc = global.document;
  global.document = fakeDocument();
  try {
    const transport = fakeTransport();
    const withMic = buildCc(transport, { clipboard: { writeText() {}, readText: () => '' }, chatPopoutContext: null, reload() {}, config: { features: { mic: true } } });
    assert.equal(withMic.openMeeting(), undefined, 'openMeeting keeps its send-mode shape');
    assert.equal(withMic.closeMeeting(), undefined, 'closeMeeting keeps its send-mode shape');
    // startMicServer must resolve FALSY, or app.js's `if (ok)` proceeds as if the
    // mic had started (a truthy object was the bug QA caught).
    assert.equal(await withMic.startMicServer(), null, 'startMicServer resolves falsy so the mic reads as unavailable');
    assert.deepEqual(transport.fires, [], 'meeting/mic never reach the host');
    assert.deepEqual(transport.calls, []);
    // open + close + mic each toast when the mic feature is on.
    assert.equal(global.document.body.children.length, 3, 'open, close and mic each surface a toast');

    // Feature off: silent, no toast, and mic still resolves falsy.
    global.document = fakeDocument();
    const noMic = buildCc(fakeTransport(), { clipboard: { writeText() {}, readText: () => '' }, chatPopoutContext: null, reload() {}, config: { features: { mic: false } } });
    noMic.openMeeting();
    noMic.closeMeeting();
    assert.equal(await noMic.startMicServer(), null, 'mic is still unavailable with the feature off');
    assert.equal(global.document.body.children.length, 0, 'no toast when the feature is off');
  } finally { global.document = realDoc; }
});

test('the web context menu fires the same assign-slot / action events as the desktop', () => {
  const fired = [];
  const emit = (event, ...args) => fired.push([event, ...args]);
  const items = webContextMenuItems('sessX', 'Display X', 'amaterasu', emit);

  // Four slots, then rename + delete.
  const slots = items.filter((i) => /^Open in slot/.test(i.label || ''));
  assert.equal(slots.length, 4, 'one entry per renderer slot');
  slots[2].onSelect();
  items.find((i) => i.label === 'Rename').onSelect();
  items.find((i) => i.label === 'Delete').onSelect();

  assert.deepEqual(fired, [
    ['assign-slot', 2, 'sessX', 'amaterasu'],
    ['action', 'rename', 'sessX', { displayName: 'Display X', hostId: 'amaterasu' }],
    ['action', 'trash', 'sessX', 'amaterasu'],
  ]);
});

test('showContextMenu renders a menu whose clicks drive the registered listeners', () => {
  const realDoc = global.document;
  const doc = fakeDocument();
  global.document = doc;
  try {
    const transport = fakeTransportWithEmit();
    const cc = buildCc(transport, { clipboard: { writeText() {}, readText: () => '' }, chatPopoutContext: null, reload() {} });
    // app.js registers these; the menu must reach them.
    const assigned = [];
    const actions = [];
    cc.onAssignSlot((slot, session, host) => assigned.push([slot, session, host]));
    cc.onAction((action, session, extra) => actions.push([action, session, extra]));

    assert.equal(cc.showContextMenu('sessX', 'Display X', 'local'), undefined, 'send-mode shape');
    assert.deepEqual(transport.fires, [], 'the menu is local — nothing goes to the host');

    const menu = doc.getElementById('web-context-menu');
    assert.ok(menu, 'a menu was rendered');
    const row = (label) => menu.children.find((c) => c.textContent === label);
    row('Open in slot 1').click();
    row('Rename').click();
    assert.deepEqual(assigned, [[0, 'sessX', 'local']]);
    assert.deepEqual(actions, [['rename', 'sessX', { displayName: 'Display X', hostId: 'local' }]]);
  } finally { global.document = realDoc; }
});

test('renderWebContextMenu keeps only one menu open at a time', () => {
  const doc = fakeDocument();
  const noop = () => {};
  renderWebContextMenu([{ label: 'A', onSelect: noop }], { document: doc });
  renderWebContextMenu([{ label: 'B', onSelect: noop }], { document: doc });
  const menus = doc.body.children.filter((c) => c.id === 'web-context-menu');
  assert.equal(menus.length, 1, 'a second open replaces the first');
});
