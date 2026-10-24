const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const Module = require('node:module');

function fixture(t) {
  class Socket extends EventEmitter {
    static OPEN = 1;
    static instances = [];
    constructor() { super(); this.readyState = 1; this.sent = []; Socket.instances.push(this); }
    send(raw) { this.sent.push(JSON.parse(raw)); }
    ping() {}
    terminate() { this.readyState = 3; }
  }
  const file = require.resolve('../main/chat_stream_client');
  const original = Module._load;
  delete require.cache[file];
  Module._load = function (name, parent, isMain) {
    return name === 'ws' ? Socket : original.call(this, name, parent, isMain);
  };
  let client;
  try { client = require(file); } finally { Module._load = original; }
  const frames = [];
  client._readToken = () => '';
  client.init({ chatStream: { url: 'ws://fixture' } }, frame => frames.push({ ...frame, transportReady: client.connected }));
  t.after(() => client.destroy());
  function open(socket = Socket.instances.at(-1)) {
    socket.emit('open');
    socket.emit('message', JSON.stringify({ type: 'welcome' }));
    return socket;
  }
  function ready(socket) {
    socket.emit('message', JSON.stringify({ type: 'snapshot', events: [], sessions: [] }));
  }
  const socket = open(); ready(socket);
  function drop(code = 1001) { socket.readyState = 3; socket.emit('close', code, Buffer.from('going away')); }
  return { client, frames, socket, Socket, open, ready, drop };
}

test('normal daemon shutdown preserves the current view through a brief authenticated recovery', t => {
  const f = fixture(t);
  f.drop();
  assert.equal(f.client.connected, false, 'the transport is really unavailable');
  assert.equal(f.client.snapshot().connected, true, 'the open conversation remains usable while recovery is pending');
  assert.equal(f.frames.some(frame => frame.connected === false), false, 'no reconnect banner for the brief restart');
  f.client._connect();
  const next = f.open(); f.ready(next);
  assert.equal(f.client.connected, true);
  assert.equal(f.frames.some(frame => frame.connected === false), false);
});

test('same-key replay is signaled only after authentication, never at socket-open', async t => {
  const f = fixture(t);
  const pending = f.client.sendMessage({ host: 'fixture', sessionName: 'owned', text: 'accepted before the lost reply', requestId: 'stable-request', optimisticId: 'stable-message' });
  f.drop();
  await assert.rejects(pending, error => error.error === 'Stream disconnected');
  const count = f.frames.filter(frame => frame.type === '__reconnect').length;
  f.client._connect();
  const next = f.open();
  assert.equal(f.frames.filter(frame => frame.type === '__reconnect').length, count, 'opening transport cannot trigger replay');
  f.ready(next);
  const recovered = f.frames.filter(frame => frame.type === '__reconnect');
  assert.equal(recovered.length, count + 1);
  assert.equal(recovered.at(-1).transportReady, true, 'replay can now reach the daemon receipt dedupe');
  const replay = f.client.sendMessage({ host: 'fixture', sessionName: 'owned', text: 'accepted before the lost reply', requestId: 'stable-request', optimisticId: 'stable-message' });
  assert.equal(next.sent.at(-1).request_id, 'stable-request');
  next.emit('message', JSON.stringify({ type: 'send.result', request_id: 'stable-request', delivery: 'landed', submission_confirmed: true }));
  assert.equal((await replay).submission_confirmed, true);
});

test('send during normal restart stays recoverable under its original identity', async t => {
  const f = fixture(t); f.drop();
  await assert.rejects(f.client.sendMessage({ host: 'fixture', sessionName: 'owned', text: 'queued while reconnecting', requestId: 'new-request', optimisticId: 'new-message' }),
    error => error.error === 'Stream disconnected', 'the existing optimistic reducer treats the offline attempt as transient');
  assert.equal(f.socket.sent.filter(frame => frame.type === 'send').length, 0, 'unaccepted input is never reported delivered');
});

test('a new send cannot terminate the replacement socket while its handshake is pending', async t => {
  const f = fixture(t); f.drop(); f.client._connect();
  const next = f.Socket.instances.at(-1); next.emit('open');
  await assert.rejects(f.client.sendMessage({ host: 'fixture', sessionName: 'owned', text: 'during authentication', requestId: 'pending-ready', optimisticId: 'pending-message' }),
    error => error.error === 'Stream disconnected');
  assert.equal(next.readyState, f.Socket.OPEN, 'the focused liveness probe must leave authentication alone');
  next.emit('message', JSON.stringify({ type: 'welcome' })); f.ready(next);
  assert.equal(f.client.connected, true);
});

test('persistent outage becomes visible after the bounded recovery window', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture(t); f.drop();
  t.mock.timers.tick(4999);
  assert.equal(f.client.snapshot().connected, true);
  t.mock.timers.tick(1);
  assert.equal(f.client.snapshot().connected, false);
  assert.equal(f.frames.filter(frame => frame.connected === false).length, 1);
});

test('failed transport reconnects cannot extend the outage deadline', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture(t); f.drop(); t.mock.timers.tick(4000);
  f.client._connect(); const failed = f.Socket.instances.at(-1);
  failed.readyState = 3; failed.emit('close', 1006);
  t.mock.timers.tick(1000);
  assert.equal(f.client.snapshot().connected, false, 'the original five-second deadline still applies');
});

test('authentication failure interrupts the grace immediately and does not consume a replay generation', t => {
  const f = fixture(t); const generation = f.frames.find(frame => frame.type === '__reconnect').next_generation;
  f.drop(); f.client._connect(); const refused = f.open();
  refused.emit('message', JSON.stringify({ type: 'auth.error', error: 'operator_auth_v2_invalid' }));
  assert.equal(f.client.snapshot().connected, false);
  assert.equal(f.client.snapshot().error, 'operator_auth_v2_invalid');
  f.client._connect(); const next = f.open(); f.ready(next);
  assert.equal(f.frames.filter(frame => frame.type === '__reconnect').at(-1).generation, generation);
});

test('cold start and non-shutdown socket failure are not hidden', t => {
  const f = fixture(t); f.drop(1011);
  assert.equal(f.client.snapshot().connected, false);
  assert.equal(f.frames.some(frame => frame.connected === false), true);
});
