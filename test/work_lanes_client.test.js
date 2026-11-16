'use strict';

// Desktop chat-stream client wiring for first-class work lanes
// (spec_pentacle__first_class_work_lanes_2026_10, M4): capability negotiation,
// frame passthrough, and the generation-scoped closed-history read.
const test = require('node:test');
const assert = require('node:assert/strict');
const { WebSocketServer } = require('ws');
const clientSingleton = require('../main/chat_stream_client');
const fixture = require('../pentacle-chat-core/tests/fixtures/work-lanes-inventory.json');

test('hello advertises work_lanes_v1 alongside composite support', () => {
  const client = new clientSingleton.constructor();
  client._cfg = { features: { chatUi: true } };
  const caps = client._helloPayload({}, { kind: 'v1' }).capabilities;
  assert.equal(caps.work_lanes_v1, true);
  assert.equal(caps.assistant_composite_v1, true);
});

test('closed-history read sends the lane generation to the daemon', async () => {
  const client = new clientSingleton.constructor();
  const sent = [];
  client.sendCommand = async (payload) => { sent.push(payload); return { events: [] }; };
  await client.requestStreamEvents({ streamId: 'fixture-host:v2-lead0003', generation: 'gen-lead-0003', limit: 50 });
  assert.equal(sent[0].type, 'request_stream_events');
  assert.equal(sent[0].generation, 'gen-lead-0003');
  assert.equal(sent[0].stream_id, 'fixture-host:v2-lead0003');
  await client.requestStreamEvents({ streamId: 'fixture-host:v2-lead0001' });
  assert.equal('generation' in sent[1], false);
});

test('work_lanes.inventory pushes and the snapshot work_lanes field reach the renderer unchanged', async (t) => {
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await new Promise((resolve) => server.once('listening', resolve));
  const client = new clientSingleton.constructor();
  t.after(async () => {
    client.destroy();
    for (const socket of server.clients) socket.terminate();
    await new Promise((resolve) => server.close(resolve));
  });
  server.on('connection', (socket) => {
    socket.send(JSON.stringify({ type: 'welcome' }));
    socket.on('message', (data) => {
      if (JSON.parse(data.toString()).type !== 'hello') return;
      socket.send(JSON.stringify({ type: 'snapshot', sessions: [], events: [], ...fixture.hello_field }));
      socket.send(JSON.stringify(fixture.inventory_frame));
    });
  });
  const frames = [];
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('fixture handshake timed out')), 3000);
    client.init({ features: { chatUi: true }, chatStream: {
      url: `ws://127.0.0.1:${server.address().port}`, token: 'test-only',
    } }, (frame) => {
      frames.push(frame);
      if (frame.type === 'work_lanes.inventory') { clearTimeout(timer); resolve(); }
    });
  });
  const snapshot = frames.find((frame) => frame.type === 'snapshot');
  assert.deepEqual(snapshot.work_lanes, fixture.hello_field.work_lanes);
  const { state_version: _version, ...pushed } = frames.find((frame) => frame.type === 'work_lanes.inventory');
  assert.deepEqual(pushed, fixture.inventory_frame);
});

test('cached snapshot carries the latest lane inventory so a reloaded renderer shows lanes at once', async (t) => {
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await new Promise((resolve) => server.once('listening', resolve));
  const client = new clientSingleton.constructor();
  t.after(async () => {
    client.destroy();
    for (const socket of server.clients) socket.terminate();
    await new Promise((resolve) => server.close(resolve));
  });
  const newer = { ...fixture.inventory_frame, lanes: fixture.inventory_frame.lanes.slice(0, 1),
    counts: { open: 1, active: 0, paused: 0, blocked: 1 }, generated_at: '2026-10-07T19:05:00.000Z' };
  server.on('connection', (socket) => {
    socket.send(JSON.stringify({ type: 'welcome' }));
    socket.on('message', (data) => {
      if (JSON.parse(data.toString()).type !== 'hello') return;
      socket.send(JSON.stringify({ type: 'snapshot', sessions: [], events: [], ...fixture.hello_field }));
      socket.send(JSON.stringify(newer));
    });
  });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('fixture handshake timed out')), 3000);
    client.init({ features: { chatUi: true }, chatStream: {
      url: `ws://127.0.0.1:${server.address().port}`, token: 'test-only',
    } }, (frame) => { if (frame.type === 'work_lanes.inventory') { clearTimeout(timer); resolve(); } });
  });
  assert.equal(client.snapshot().work_lanes.counts.open, 1);
  assert.equal(client.snapshot().work_lanes.generated_at, '2026-10-07T19:05:00.000Z');
});

test('a daemon without lanes leaves snapshot.work_lanes absent', () => {
  const client = new clientSingleton.constructor();
  assert.equal('work_lanes' in client.snapshot(), false);
});
