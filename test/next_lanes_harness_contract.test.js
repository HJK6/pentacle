'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { runInNewContext } = require('node:vm');

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;

  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.listeners = new Map();
    this.sent = [];
    FakeWebSocket.latest = this;
    queueMicrotask(() => {
      this.readyState = FakeWebSocket.OPEN;
      this.emit('open');
    });
  }

  on(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  emit(name, value) {
    for (const listener of this.listeners.get(name) || []) listener(value);
  }

  send(raw) {
    const frame = JSON.parse(String(raw));
    this.sent.push(frame);
    if (frame.type === 'hello') {
      queueMicrotask(() => this.emit('message', JSON.stringify({ type: 'snapshot' })));
      return;
    }
    const publication = { source: 'wire-contract', request_id: frame.request_id };
    queueMicrotask(() => {
      this.emit('message', JSON.stringify({ type: 'chat.event', event: publication }));
      this.emit('message', JSON.stringify({ type: `${frame.type}.ok`, request_id: frame.request_id }));
    });
  }

  close() { this.readyState = 3; }
  terminate() { this.readyState = 3; }
}

function loadFixtureRequestWithPublication() {
  const filename = path.join(__dirname, 'e2e/lib/next_lanes_harness.js');
  const source = fs.readFileSync(filename, 'utf8');
  const start = source.indexOf('async function fixtureRequestWithPublication(');
  const end = source.indexOf('\nasync function notificationFor(', start);
  assert.ok(start >= 0 && end > start, 'publication helper source is present');
  return runInNewContext(`(${source.slice(start, end).trim()})`, {
    WebSocket: FakeWebSocket,
    questionFixtureToken: (runtime, producerStreamId) => runtime.fixtureTokens?.[producerStreamId] || null,
    setTimeout,
    clearTimeout,
  });
}

test('publication helper authenticates hello and sends closed-schema request unchanged', async () => {
  const helper = loadFixtureRequestWithPublication();
  const producerStreamId = 'fixture:wire-contract-producer';
  const token = 'synthetic-fixture-token';
  const runtime = { fixtureDaemonPort: 4321, fixtureTokens: { [producerStreamId]: token } };
  const messages = [
    {
      type: 'assistant.operation',
      request_id: 'wire-contract-operation',
      composite_stream_id: 'fixture:composite',
      dispatch_id: 'none',
      operation: 'work_lane.adopt',
      payload: { adoption_key: 'fixture:adoption', title: 'Synthetic lane' },
    },
    {
      type: 'assistant.publish',
      request_id: 'wire-contract-publication',
      composite_stream_id: 'fixture:composite',
      dispatch_id: 'none',
      publish_kind: 'prose',
      message: 'Synthetic fixture prose.',
    },
  ];

  for (const message of messages) {
    FakeWebSocket.latest = null;
    const result = await helper(runtime, producerStreamId, message,
      event => event?.source === 'wire-contract' && event?.request_id === message.request_id);

    assert.deepEqual(FakeWebSocket.latest.sent[0], {
      type: 'hello',
      client: 'test',
      from_stream_id: producerStreamId,
      stream_token: token,
      subscribe: { all: true },
    });
    assert.deepEqual(FakeWebSocket.latest.sent[1], message);
    assert.equal(Object.hasOwn(FakeWebSocket.latest.sent[1], 'from_stream_id'), false);
    assert.equal(Object.hasOwn(FakeWebSocket.latest.sent[1], 'stream_token'), false);
    assert.deepEqual(JSON.parse(JSON.stringify(result)), {
      reply: { type: `${message.type}.ok`, request_id: message.request_id },
      publication: { source: 'wire-contract', request_id: message.request_id },
    });
  }
});
