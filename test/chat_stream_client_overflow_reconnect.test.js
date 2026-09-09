'use strict';

// This deliberately has no third-party test dependency.  It loads the actual
// desktop ChatStreamClient with only its websocket transport replaced, then
// drives the production websocket handler and _mergeStreamEvents reducer.
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const Module = require('node:module');

function makeFakeWebSocket() {
  class FakeWebSocket extends EventEmitter {
    constructor(url) {
      super();
      this.url = url;
      this.readyState = FakeWebSocket.OPEN;
      this.sent = [];
      FakeWebSocket.instances.push(this);
    }

    send(raw) { this.sent.push(JSON.parse(raw)); }
    ping() {}
    terminate() { this.terminated = true; }
  }
  FakeWebSocket.instances = [];
  FakeWebSocket.CONNECTING = 0;
  FakeWebSocket.OPEN = 1;
  FakeWebSocket.CLOSING = 2;
  FakeWebSocket.CLOSED = 3;
  return FakeWebSocket;
}

function loadProductionClient(FakeWebSocket) {
  const clientPath = require.resolve('../main/chat_stream_client');
  const originalLoad = Module._load;
  delete require.cache[clientPath];
  Module._load = function load(request, parent, isMain) {
    if (request === 'ws') return FakeWebSocket;
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    return require('../main/chat_stream_client');
  } finally {
    Module._load = originalLoad;
  }
}

function helloAndSnapshot(socket, snapshot = { type: 'snapshot', events: [], sessions: [] }) {
  socket.emit('open');
  socket.emit('message', JSON.stringify({ type: 'welcome' }));
  socket.emit('message', JSON.stringify(snapshot));
}

test('production reducer renders dropped FINAL exactly once after 1011 reconnect and history replay', async () => {
  const FakeWebSocket = makeFakeWebSocket();
  const client = loadProductionClient(FakeWebSocket);
  const emitted = [];
  const finalEvent = {
    daemon_seq: 9001,
    stream_id: 'harness:overflow-final',
    kind: 'ASSIST_TEXT',
    text: 'FINAL dropped on the overload socket',
  };

  try {
    client._readToken = () => '';
    client.init({ chatStream: { heartbeatMs: 30000 } }, (frame) => emitted.push(frame));
    const overflowSocket = FakeWebSocket.instances[0];
    helloAndSnapshot(overflowSocket);

    // Candidate daemon policy signals irrecoverable queue loss with 1011.  The
    // final event is intentionally absent: it was dropped with this socket.
    overflowSocket.emit('close', 1011, Buffer.from('slow_consumer'));
    client._connect();
    const reconnectSocket = FakeWebSocket.instances[1];
    helloAndSnapshot(reconnectSocket);

    const recovery = client.requestStreamEvents({ streamId: finalEvent.stream_id });
    const request = reconnectSocket.sent.find((frame) => frame.type === 'request_stream_events');
    assert.ok(request, 'reconnect uses the existing history RPC for recovery');
    reconnectSocket.emit('message', JSON.stringify({
      type: 'request_stream_events.ok',
      request_id: request.request_id,
      stream_id: finalEvent.stream_id,
      // Replay overlap is normal; the production reducer must de-duplicate.
      events: [finalEvent, { ...finalEvent }],
    }));
    await recovery;

    const rendered = client.snapshot().events.filter((event) => event.daemon_seq === finalEvent.daemon_seq);
    assert.equal(rendered.length, 1, 'dropped FINAL is rendered exactly once');
    assert.equal(rendered[0].text, finalEvent.text, 'final rendered state is the replayed FINAL');
    assert.ok(emitted.some((frame) => frame.connected === false), 'production client observed disconnect');
    assert.equal(emitted.filter((frame) => frame.type === '__reconnect').length, 2, 'one reconnect generation follows 1011');
  } finally {
    client.destroy();
  }
});
