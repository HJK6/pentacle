const test = require('node:test');
const assert = require('node:assert/strict');

const { createWsBridge } = require('../server/ws_bridge');

function fakeSocket(name) {
  return { name, frames: [], send(text) { this.frames.push(JSON.parse(text)); } };
}

function makeBridge(overrides = {}) {
  const seen = { writes: [], creates: [], destroyed: [] };
  const table = {
    'get-config': { mode: 'invoke', handler: async () => ({ appName: 'Pentacle' }) },
    // Mirrors terminal_adapter: keys by event.sender.id and pushes through it.
    'pty:create': {
      mode: 'invoke',
      handler: async (event, slot) => {
        seen.creates.push([event.sender.id, slot]);
        event.sender.once('destroyed', () => seen.destroyed.push([event.sender.id, slot]));
        return `%${slot}`;
      },
    },
    'pty:write': { mode: 'send', handler: (event, slot, data) => seen.writes.push([event.sender.id, slot, data]) },
    'echo:data': { mode: 'invoke', handler: (event, slot, data) => { event.sender.send('pty:data', slot, data); return true; } },
    boom: { mode: 'invoke', handler: async () => { throw new Error('handler exploded'); } },
    'boom:send': { mode: 'send', handler: () => { throw new Error('silent explosion'); } },
    ...(overrides.table || {}),
  };
  return { bridge: createWsBridge({ table, logger: { warn() {} } }), seen, table };
}

test('a request gets a correlated ok response', async () => {
  const { bridge } = makeBridge();
  const s = fakeSocket('a');
  bridge.addSocket(s);

  await bridge.handleMessage(s, JSON.stringify({ id: 7, method: 'get-config', args: [] }));

  assert.deepEqual(s.frames, [{ id: 7, ok: true, result: { appName: 'Pentacle' } }]);
});

test('a handler throw becomes a correlated error, not a dropped request', async () => {
  const { bridge } = makeBridge();
  const s = fakeSocket('a');
  bridge.addSocket(s);

  await bridge.handleMessage(s, JSON.stringify({ id: 'x1', method: 'boom' }));

  assert.equal(s.frames.length, 1);
  assert.equal(s.frames[0].ok, false);
  assert.equal(s.frames[0].error.code, 'handler_error');
  assert.match(s.frames[0].error.message, /handler exploded/);
});

test('send-mode methods are fire-and-forget in both directions', async () => {
  const { bridge, seen } = makeBridge();
  const s = fakeSocket('a');
  bridge.addSocket(s);

  await bridge.handleMessage(s, JSON.stringify({ id: 1, method: 'pty:write', args: [2, 'hi'] }));
  await bridge.handleMessage(s, JSON.stringify({ id: 2, method: 'boom:send', args: [] }));

  assert.equal(seen.writes.length, 1);
  assert.deepEqual(s.frames, [], 'a send channel must never answer, even when it throws');
});

test('unknown and invoke-mode refusals answer with distinct codes', async () => {
  const { bridge } = makeBridge();
  const s = fakeSocket('a');
  bridge.addSocket(s);

  await bridge.handleMessage(s, JSON.stringify({ id: 1, method: 'nope' }));
  await bridge.handleMessage(s, JSON.stringify({ id: 2, method: 'clipboard:read-text' }));
  await bridge.handleMessage(s, JSON.stringify({ id: 3, method: 'open-external', args: ['https://x'] }));

  assert.equal(s.frames[0].error.code, 'unknown_method');
  // Serving this would hand back the HOST's clipboard.
  assert.equal(s.frames[1].error.code, 'web_local');
  assert.match(s.frames[1].error.message, /navigator\.clipboard/);
  assert.equal(s.frames[2].error.code, 'web_local');
});

test('a refused send-mode channel stays silent, like every other send channel', async () => {
  const { bridge } = makeBridge();
  const s = fakeSocket('a');
  bridge.addSocket(s);

  // preload fires these with ipcRenderer.send and no id; an error frame would
  // arrive unsolicited at a caller that is not listening for a reply.
  await bridge.handleMessage(s, JSON.stringify({ method: 'meeting:open', args: [] }));
  await bridge.handleMessage(s, JSON.stringify({ method: 'meeting:close', args: [] }));
  await bridge.handleMessage(s, JSON.stringify({ method: 'context-menu', args: ['sess', 'Sess', 'local'] }));
  await bridge.handleMessage(s, JSON.stringify({ method: 'app:reload', args: [] }));

  assert.deepEqual(s.frames, []);
});

test('malformed frames are rejected without throwing', async () => {
  const { bridge } = makeBridge();
  const s = fakeSocket('a');
  bridge.addSocket(s);

  await bridge.handleMessage(s, 'not json at all');
  await bridge.handleMessage(s, JSON.stringify({ id: 4 }));

  assert.equal(s.frames[0].error.code, 'bad_request');
  assert.equal(s.frames[1].error.code, 'bad_request');
  assert.equal(s.frames[1].id, 4);
});

test('each connection gets its own sender id, so slots cannot collide', async () => {
  const { bridge, seen } = makeBridge();
  const a = fakeSocket('a');
  const b = fakeSocket('b');
  bridge.addSocket(a);
  bridge.addSocket(b);

  await bridge.handleMessage(a, JSON.stringify({ id: 1, method: 'pty:create', args: [0] }));
  await bridge.handleMessage(b, JSON.stringify({ id: 1, method: 'pty:create', args: [0] }));

  const [idA, idB] = seen.creates.map(([id]) => id);
  assert.notEqual(idA, idB, 'two tabs attaching slot 0 must not share a key');
});

test('pty output reaches only the connection that produced it', async () => {
  const { bridge } = makeBridge();
  const a = fakeSocket('a');
  const b = fakeSocket('b');
  bridge.addSocket(a);
  bridge.addSocket(b);

  await bridge.handleMessage(a, JSON.stringify({ id: 1, method: 'echo:data', args: [0, 'for-a'] }));
  await bridge.handleMessage(b, JSON.stringify({ id: 1, method: 'echo:data', args: [1, 'for-b'] }));

  assert.deepEqual(a.frames.filter((f) => f.event), [{ event: 'pty:data', args: [0, 'for-a'] }]);
  assert.deepEqual(b.frames.filter((f) => f.event), [{ event: 'pty:data', args: [1, 'for-b'] }]);
});

test('closing a socket destroys its sender so the adapter tears attachments down', async () => {
  const { bridge, seen } = makeBridge();
  const a = fakeSocket('a');
  bridge.addSocket(a);
  await bridge.handleMessage(a, JSON.stringify({ id: 1, method: 'pty:create', args: [0] }));

  bridge.removeSocket(a);

  assert.equal(seen.destroyed.length, 1, 'the adapter’s destroyed hook must fire');
  assert.equal(bridge.connections.size, 0);
});

test('a destroyed sender stops pushing and reports itself destroyed', async () => {
  const { bridge, table } = makeBridge();
  const a = fakeSocket('a');
  const sender = bridge.addSocket(a);
  assert.equal(sender.isDestroyed(), false);

  bridge.removeSocket(a);
  sender.send('pty:data', 0, 'after-close');

  assert.equal(sender.isDestroyed(), true);
  assert.deepEqual(a.frames, []);
  assert.equal(typeof table['pty:create'].handler, 'function');
});

test('a frame from an unknown socket is ignored rather than throwing', async () => {
  const { bridge } = makeBridge();
  const ghost = fakeSocket('ghost');

  await bridge.handleMessage(ghost, JSON.stringify({ id: 1, method: 'get-config' }));

  assert.deepEqual(ghost.frames, []);
});

test('non-pty events are broadcast to every connection', () => {
  const { bridge } = makeBridge();
  const a = fakeSocket('a');
  const b = fakeSocket('b');
  bridge.addSocket(a);
  bridge.addSocket(b);

  bridge.broadcast('chat-stream:frame', { type: 'session.update' });

  const expected = [{ event: 'chat-stream:frame', args: [{ type: 'session.update' }] }];
  assert.deepEqual(a.frames, expected);
  assert.deepEqual(b.frames, expected);
});

test('a socket that throws on send does not break the broadcast', () => {
  const { bridge } = makeBridge();
  const bad = { send() { throw new Error('socket gone'); } };
  const good = fakeSocket('good');
  bridge.addSocket(bad);
  bridge.addSocket(good);

  bridge.broadcast('action', 'focus', 'sess');

  assert.deepEqual(good.frames, [{ event: 'action', args: ['focus', 'sess'] }]);
});

test('closeAll destroys every connection', async () => {
  const { bridge, seen } = makeBridge();
  const a = fakeSocket('a');
  const b = fakeSocket('b');
  bridge.addSocket(a);
  bridge.addSocket(b);
  await bridge.handleMessage(a, JSON.stringify({ id: 1, method: 'pty:create', args: [0] }));
  await bridge.handleMessage(b, JSON.stringify({ id: 1, method: 'pty:create', args: [1] }));

  bridge.closeAll();

  assert.equal(bridge.connections.size, 0);
  assert.equal(seen.destroyed.length, 2);
});

test('a handler returning undefined answers with an explicit null', async () => {
  const { bridge } = makeBridge({ table: { void: { mode: 'invoke', handler: async () => undefined } } });
  const a = fakeSocket('a');
  bridge.addSocket(a);

  await bridge.handleMessage(a, JSON.stringify({ id: 9, method: 'void' }));

  assert.deepEqual(a.frames, [{ id: 9, ok: true, result: null }]);
});
