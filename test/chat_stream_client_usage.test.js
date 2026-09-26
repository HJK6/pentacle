const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const WebSocket = require('ws');
const providerFailure = require('./fixtures/limits_provider_failure.json');

test('real collector failure retains limits and health at startup and live refresh', (t) => {
  const { client, ws } = openClient();
  t.after(() => client.destroy());
  sendFrame(ws, { type: 'snapshot', events: [], sessions: [], ...providerFailure.failed });
  assert.deepEqual(client.snapshot().limits, providerFailure.failed.limits);
  assert.deepEqual(client.snapshot().limits_health, providerFailure.failed.limits_health);
  sendFrame(ws, { type: 'limits.update', ...providerFailure.healthy });
  assert.deepEqual(client.snapshot().limits_health, providerFailure.healthy.limits_health);
  sendFrame(ws, { type: 'limits.update', ...providerFailure.failed });
  assert.deepEqual(client.snapshot().limits, providerFailure.failed.limits);
  assert.deepEqual(client.snapshot().limits_health, providerFailure.failed.limits_health);
});

test('buffered handshake accepts real collector failure with retained values', (t) => {
  const { client } = openClient();
  t.after(() => client.destroy());
  client._queueHandshakeLimitsUpdate({ type: 'limits.update', ...providerFailure.failed });
  client._flushHandshakeLimitsUpdate();
  assert.deepEqual(client.snapshot().limits, providerFailure.failed.limits);
  assert.deepEqual(client.snapshot().limits_health, providerFailure.failed.limits_health);
});

function loadClientWithFakeWebSocket(FakeWebSocket) {
  const clientPath = require.resolve('../main/chat_stream_client');
  const wsPath = require.resolve('ws');
  const originalClientCache = require.cache[clientPath];
  const originalWsCache = require.cache[wsPath];
  delete require.cache[clientPath];
  require.cache[wsPath] = {
    id: wsPath,
    filename: wsPath,
    loaded: true,
    exports: FakeWebSocket,
  };
  const loaded = require('../main/chat_stream_client');
  delete require.cache[clientPath];
  if (originalClientCache) {
    require.cache[clientPath] = originalClientCache;
  }
  if (originalWsCache) {
    require.cache[wsPath] = originalWsCache;
  } else {
    delete require.cache[wsPath];
  }
  return loaded;
}

function makeFakeWebSocketClass() {
  class FakeWebSocket extends EventEmitter {
    constructor(url) {
      super();
      this.url = url;
      this.readyState = WebSocket.OPEN;
      this.sent = [];
      this.terminateCalls = 0;
      FakeWebSocket.instances.push(this);
    }

    send(raw) {
      this.sent.push(JSON.parse(raw));
    }

    ping() {}

    terminate() {
      this.terminateCalls += 1;
    }
  }
  FakeWebSocket.instances = [];
  FakeWebSocket.CONNECTING = WebSocket.CONNECTING;
  FakeWebSocket.OPEN = WebSocket.OPEN;
  FakeWebSocket.CLOSING = WebSocket.CLOSING;
  FakeWebSocket.CLOSED = WebSocket.CLOSED;
  return FakeWebSocket;
}

function openClient() {
  const frames = [];
  const FakeWebSocket = makeFakeWebSocketClass();
  const client = loadClientWithFakeWebSocket(FakeWebSocket);
  client._readToken = () => '';
  client.init(
    { chatStream: { heartbeatMs: 30000 } },
    (frame) => frames.push(frame),
  );
  const ws = FakeWebSocket.instances[0];
  ws.emit('open');
  sendFrame(ws, { type: 'welcome' });
  return { client, emitted: frames, frames, ws, FakeWebSocket };
}

function sendFrame(ws, frame) {
  ws.emit('message', Buffer.from(JSON.stringify(frame)));
}

test('renderer state pull does not expose the shared partial event cache as a complete transcript', (t) => {
  const { createCcHandlers, createCollector } = require('../main/cc_handlers');
  const { client } = openClient();
  t.after(() => client.destroy());
  client._events = [
    { stream_id: 'chat-a', daemon_seq: 1, kind: 'USER', text: 'kept in another browser' },
    { stream_id: 'chat-b', daemon_seq: 2, kind: 'ASSIST_TEXT', text: 'shared cache tail' },
  ];
  const collector = createCollector();
  createCcHandlers({ CONFIG: { chatStream: {} }, chatStreamClient: client, harness: true }).register(collector);
  const state = collector.table['chat-stream:get-state'].handler();
  assert.deepEqual(state.events, [], 'a partial global ring must not replace fetched per-stream history');
  assert.equal(client.snapshot().events.length, 2, 'internal bounded cache is retained for its own consumers');
});

test('image upload frames obey the daemon one MiB decoded chunk limit', async (t) => {
  const { client, ws } = openClient();
  t.after(() => client.destroy());
  sendFrame(ws, { type: 'snapshot', events: [], sessions: [] });
  const bytes = Buffer.alloc(1024 * 1024 + 1, 0x5a);
  const pending = client.uploadBlob({ data: bytes, requestId: 'upload-boundary' });
  sendFrame(ws, { type: 'upload_blob.init.ok', request_id: 'upload-boundary' });
  await new Promise((resolve) => setImmediate(resolve));
  const chunks = ws.sent.filter((frame) => frame.type === 'upload_blob_chunk');
  sendFrame(ws, { type: 'upload_blob.ok', request_id: 'upload-boundary', blob_sha: 'fixture' });
  await pending;
  assert.ok(chunks.length >= 2, 'a one MiB plus one byte image needs multiple chunks');
  assert.ok(chunks.every((frame) => Buffer.from(frame.data_b64, 'base64').length <= 1024 * 1024));
  assert.equal(chunks.at(-1).final, true);
  assert.ok(chunks.slice(0, -1).every((frame) => frame.final === false));
  assert.deepEqual(Buffer.concat(chunks.map((frame) => Buffer.from(frame.data_b64, 'base64'))), bytes);
});

test('image upload boundaries preserve exact bytes through the final frame', async (t) => {
  const { client, ws } = openClient();
  t.after(() => client.destroy());
  sendFrame(ws, { type: 'snapshot', events: [], sessions: [] });
  for (const length of [1024 * 1024 - 1, 1024 * 1024, 25 * 1024 * 1024]) {
    ws.sent.length = 0;
    const bytes = Buffer.alloc(length, length % 251);
    const id = `upload-${length}`;
    const pending = client.uploadBlob({ data: bytes, requestId: id });
    sendFrame(ws, { type: 'upload_blob.init.ok', request_id: id });
    await new Promise((resolve) => setImmediate(resolve));
    const chunks = ws.sent.filter((frame) => frame.type === 'upload_blob_chunk');
    assert.equal(chunks.length, Math.ceil(length / (1024 * 1024)));
    assert.ok(chunks.every((frame) => Buffer.from(frame.data_b64, 'base64').length <= 1024 * 1024));
    assert.ok(chunks.slice(0, -1).every((frame) => frame.final === false));
    assert.equal(chunks.at(-1).final, true);
    assert.deepEqual(Buffer.concat(chunks.map((frame) => Buffer.from(frame.data_b64, 'base64'))), bytes);
    sendFrame(ws, { type: 'upload_blob.ok', request_id: id, blob_sha: 'fixture' });
    await pending;
  }
});

test('lost final image upload response times out without sending a chat message', async (t) => {
  const { client, ws } = openClient();
  t.after(() => client.destroy());
  sendFrame(ws, { type: 'snapshot', events: [], sessions: [] });
  const pending = client.uploadBlob({ data: Buffer.from('fixture'), requestId: 'upload-timeout', timeoutMs: 15 });
  const settled = pending.then(() => null, (error) => error);
  sendFrame(ws, { type: 'upload_blob.init.ok', request_id: 'upload-timeout' });
  await new Promise((resolve) => setTimeout(resolve, 35));
  assert.equal((await settled)?.error, 'timed_out');
  assert.equal(ws.sent.filter((frame) => frame.type === 'send').length, 0);
});

test('fetched image chunks decode and re-encode into one valid blob', async (t) => {
  const { client, ws } = openClient();
  t.after(() => client.destroy());
  sendFrame(ws, { type: 'snapshot', events: [], sessions: [] });
  const pending = client.fetchBlob({ blobSha: 'fixture', requestId: 'fetch-image' });
  sendFrame(ws, { type: 'fetch_blob.chunk', request_id: 'fetch-image', blob_sha: 'fixture', size_bytes: 4, content_b64: Buffer.from('ab').toString('base64'), final: false });
  sendFrame(ws, { type: 'fetch_blob.chunk', request_id: 'fetch-image', blob_sha: 'fixture', size_bytes: 4, content_b64: Buffer.from('cd').toString('base64'), final: false });
  sendFrame(ws, { type: 'fetch_blob.ok', request_id: 'fetch-image', blob_sha: 'fixture', size_bytes: 4, final: true });
  const result = await pending;
  assert.deepEqual(Buffer.from(result.content_b64, 'base64'), Buffer.from('abcd'));
});

test('image upload bridge passes the renderer base64 bytes to the blob client', async () => {
  const { createCcHandlers, createCollector } = require('../main/cc_handlers');
  const collector = createCollector();
  let received;
  createCcHandlers({ CONFIG: { chatStream: {} }, chatStreamClient: {
    uploadBlob: async (payload) => { received = payload; return { blob_sha: 'fixture' }; },
  }, harness: true }).register(collector);
  const bytes = Buffer.from('image-bytes');
  const reply = await collector.table['chat-stream:upload-blob'].handler({}, {
    dataBase64: bytes.toString('base64'), sizeHintBytes: bytes.length,
  });
  assert.equal(reply.ok, true);
  assert.deepEqual(received.data, bytes);
});

const NULL_LIMITS = Object.freeze([
  Object.freeze({ id: 'claude', label: 'Claude', pct: null, resets_at_iso: null, resets_text: null, upstream_reported_at: null, probed_at: null }),
  Object.freeze({ id: 'fable', label: 'Fable', pct: null, resets_at_iso: null, resets_text: null, upstream_reported_at: null, probed_at: null }),
  Object.freeze({ id: 'codex', label: 'Codex', pct: null, resets_at_iso: null, resets_text: null, upstream_reported_at: null, probed_at: null }),
]);

const LIMITS_A = Object.freeze([
  Object.freeze({ id: 'claude', label: 'Claude', pct: 21, resets_at_iso: '2026-08-24T00:00:00Z', resets_text: 'tomorrow 7 pm', upstream_reported_at: null, probed_at: null }),
  Object.freeze({ id: 'fable', label: 'Fable', pct: 43, resets_at_iso: null, resets_text: '6d', upstream_reported_at: null, probed_at: null }),
  Object.freeze({ id: 'codex', label: 'Codex', pct: 65, resets_at_iso: '2026-08-30T00:00:00Z', resets_text: '7d', upstream_reported_at: '2026-08-26T09:30:00Z', probed_at: '2026-08-26T09:30:01Z' }),
]);

const LIMITS_B = Object.freeze([
  Object.freeze({ id: 'claude', label: 'Claude', pct: 87, resets_at_iso: null, resets_text: '8h', upstream_reported_at: null, probed_at: null }),
  Object.freeze({ id: 'fable', label: 'Fable', pct: 9, resets_at_iso: null, resets_text: null, upstream_reported_at: null, probed_at: null }),
  Object.freeze({ id: 'codex', label: 'Codex', pct: 11, resets_at_iso: '2026-09-01T00:00:00Z', resets_text: null, upstream_reported_at: '2026-08-26T09:35:00Z', probed_at: '2026-08-26T09:35:01Z' }),
]);

const ITEM5_LIMITS = Object.freeze([
  Object.freeze({ ...LIMITS_A[0], upstream_reported_at: null, probed_at: null }),
  Object.freeze({ ...LIMITS_A[1], upstream_reported_at: null, probed_at: null }),
  Object.freeze({
    ...LIMITS_A[2],
    upstream_reported_at: '2026-08-26T09:30:00Z',
    probed_at: '2026-08-26T09:30:01Z',
  }),
]);

const LIMITS_HEALTH_OK = Object.freeze({
  schema_version: 1,
  claude: Object.freeze({
    attempted_at: '2026-08-28T07:00:00Z',
    outcome: 'ok',
    error: null,
    upstream_reported_at: '2026-08-28T07:00:01Z',
    probed_at: '2026-08-28T07:00:02Z',
    stale_after_seconds: 600,
  }),
});

function reconnectLimits(step) {
  const second = String(step).padStart(2, '0');
  return LIMITS_B.map((row, index) => index === 2 ? {
    ...row,
    pct: 11 + step,
    upstream_reported_at: `2026-08-27T21:40:${second}Z`,
    probed_at: `2026-08-27T21:41:${second}Z`,
  } : row);
}

test('ChatStreamClient starts with a complete fixed-order valid-null limits list', (t) => {
  const { client } = openClient();
  t.after(() => client.destroy());

  assert.deepEqual(client.snapshot().limits, NULL_LIMITS);
  assert.equal(client.snapshot().limits_health, null);
});

test('ChatStreamClient installs limits and health atomically and clears health for old-daemon frames', (t) => {
  const { client, emitted, ws } = openClient();
  t.after(() => client.destroy());

  sendFrame(ws, { type: 'snapshot', events: [], drafts: {}, sessions: [], limits: LIMITS_A, limits_health: LIMITS_HEALTH_OK });
  assert.deepEqual(client.snapshot().limits, LIMITS_A);
  assert.deepEqual(client.snapshot().limits_health, LIMITS_HEALTH_OK);

  const malformed = { ...LIMITS_HEALTH_OK, claude: { ...LIMITS_HEALTH_OK.claude, outcome: 'not-a-real-outcome' } };
  sendFrame(ws, { type: 'limits.update', limits: LIMITS_B, limits_health: malformed });
  assert.deepEqual(client.snapshot().limits, LIMITS_A, 'malformed health retains the prior limits pair');
  assert.deepEqual(client.snapshot().limits_health, LIMITS_HEALTH_OK, 'malformed health retains the prior health pair');

  sendFrame(ws, { type: 'limits.update', limits: LIMITS_B });
  assert.deepEqual(client.snapshot().limits, LIMITS_B);
  assert.equal(client.snapshot().limits_health, null, 'old-daemon update clears unknown health');
  assert.equal(Object.hasOwn(emitted.at(-1), 'limits_health'), false);
});

test('ChatStreamClient atomically stores a complete limits list from a snapshot', (t) => {
  const { client, emitted, ws } = openClient();
  t.after(() => client.destroy());

  sendFrame(ws, { type: 'snapshot', events: [], drafts: {}, sessions: [], limits: LIMITS_A });

  assert.deepEqual(client._limits, LIMITS_A);
  assert.deepEqual(client.snapshot().limits, LIMITS_A);
  assert.deepEqual(emitted.at(-1).limits, LIMITS_A);
});

test('snapshot without limits stays canonical while the client retains cached limits', (t) => {
  const { client, emitted, frames, ws } = openClient();
  t.after(() => client.destroy());

  sendFrame(ws, { type: 'snapshot', events: [], drafts: {}, sessions: [], limits: LIMITS_A });
  emitted.length = 0;
  frames.length = 0;
  sendFrame(ws, {
    type: 'snapshot',
    events: [{ kind: 'TEXT', text: 'new event' }],
    drafts: {},
    sessions: [],
  });

  assert.equal(Object.prototype.hasOwnProperty.call(frames[0], 'limits'), false);
  assert.deepEqual(client._limits, LIMITS_A);
  assert.deepEqual(client.snapshot().limits, LIMITS_A);
  assert.equal(Object.prototype.hasOwnProperty.call(emitted[0], 'limits'), false);
});

test('ChatStreamClient completely replaces limits after an incremental limits.update', (t) => {
  const { client, emitted, frames, ws } = openClient();
  t.after(() => client.destroy());

  sendFrame(ws, { type: 'snapshot', events: [], drafts: {}, sessions: [], limits: LIMITS_A });
  emitted.length = 0;
  sendFrame(ws, { type: 'limits.update', limits: LIMITS_B });

  assert.deepEqual(client._limits, LIMITS_B);
  assert.deepEqual(client.snapshot().limits, LIMITS_B);
  assert.equal(emitted.length, 1);
  assert.deepEqual(emitted[0].limits, LIMITS_B);
  assert.deepEqual(frames.at(-1), { type: 'limits.update', limits: LIMITS_B, state_version: 1 });
});

test('daemon restart reconnect replays a limits update received during the snapshot handshake', (t) => {
  const { client, emitted, frames, ws: first, FakeWebSocket } = openClient();
  t.after(() => client.destroy());

  sendFrame(first, { type: 'snapshot', events: [], drafts: {}, sessions: [], limits: LIMITS_A });
  let current = first;
  let expected = LIMITS_A;

  for (let step = 1; step <= 5; step += 1) {
    expected = reconnectLimits(step);
    current.emit('close');
    client._connect();
    current = FakeWebSocket.instances.at(-1);
    current.emit('open');
    sendFrame(current, { type: 'welcome' });

    // A restarted daemon can finish a newer probe after it captures the hello
    // snapshot but before that snapshot reaches the desktop. The update is the
    // authoritative later frame and must survive handshake completion.
    sendFrame(current, { type: 'limits.update', limits: expected });
    sendFrame(current, { type: 'snapshot', events: [], drafts: {}, sessions: [], limits: LIMITS_A });

    assert.deepEqual(client.snapshot().limits, expected);
    assert.equal(current.sent.length, 1, 'one hello subscription per socket generation');
    assert.equal(current.sent[0].type, 'hello');
  }

  assert.equal(
    frames.filter((frame) => frame.type === 'limits.update').length,
    5,
    'each accepted probe update is forwarded exactly once',
  );

  emitted.length = 0;
  frames.length = 0;
  const direct = reconnectLimits(6);
  sendFrame(current, { type: 'limits.update', limits: direct });
  assert.deepEqual(client.snapshot().limits, direct, 'ready-state event receipt remains authoritative');
  assert.equal(emitted.filter((frame) => frame.type === 'snapshot').length, 0);
  assert.deepEqual(frames, [{
    type: 'limits.update',
    limits: direct,
    state_version: client.snapshot().state_version,
  }]);
});

test('reconnect backfill interleaving preserves event order with one canonical delivery', async (t) => {
  const { client, frames, ws: first, FakeWebSocket } = openClient();
  t.after(() => client.destroy());
  const event = (daemon_seq) => ({ daemon_seq, stream_id: 'stream-a' });
  sendFrame(first, { type: 'snapshot', events: [event(10)] });
  first.emit('close');
  client._connect();
  const second = FakeWebSocket.instances.at(-1);
  second.emit('open');
  sendFrame(second, { type: 'welcome' });
  sendFrame(second, { type: 'snapshot', events: [event(10)] });
  frames.length = 0;
  const backfill = client.requestStreamEvents({ streamId: 'stream-a' });
  const requestId = second.sent.at(-1).request_id;
  sendFrame(second, { type: 'chat.event', event: event(13) });
  sendFrame(second, {
    type: 'request_stream_events.chunk', request_id: requestId, stream_id: 'stream-a',
    events: [event(11)],
  });
  sendFrame(second, {
    type: 'request_stream_events.ok', request_id: requestId, stream_id: 'stream-a',
    events: [event(12)],
  });
  assert.equal((await backfill).ok, true);
  assert.deepEqual(client.snapshot().events.map((event) => event.daemon_seq), [10, 11, 12, 13]);
  assert.deepEqual(frames.map((frame) => frame.type), ['chat.event', 'stream_events']);
});

test('ChatStreamClient accepts an explicit complete valid-null limits update', (t) => {
  const { client, emitted, ws } = openClient();
  t.after(() => client.destroy());

  sendFrame(ws, { type: 'snapshot', events: [], drafts: {}, sessions: [], limits: LIMITS_A });
  emitted.length = 0;
  sendFrame(ws, { type: 'limits.update', limits: NULL_LIMITS });

  assert.deepEqual(client._limits, NULL_LIMITS);
  assert.equal(emitted.length, 1);
  assert.deepEqual(emitted[0].limits, NULL_LIMITS);
});

test('ChatStreamClient rejects malformed limits lists atomically', (t) => {
  const { client, emitted, ws } = openClient();
  t.after(() => client.destroy());

  sendFrame(ws, { type: 'snapshot', events: [], drafts: {}, sessions: [], limits: LIMITS_A });
  emitted.length = 0;
  const invalid = [
    LIMITS_B.slice(0, 2),
    [LIMITS_B[1], LIMITS_B[0], LIMITS_B[2]],
    [{ ...LIMITS_B[0], label: 'Claude Weekly' }, LIMITS_B[1], LIMITS_B[2]],
    [{ ...LIMITS_B[0], extra: true }, LIMITS_B[1], LIMITS_B[2]],
    [{ ...LIMITS_B[0], pct: true }, LIMITS_B[1], LIMITS_B[2]],
    [{ ...LIMITS_B[0], resets_at_iso: 123 }, LIMITS_B[1], LIMITS_B[2]],
    null,
  ];
  for (const limits of invalid) sendFrame(ws, { type: 'limits.update', limits });
  sendFrame(ws, { type: 'snapshot', events: [], drafts: {}, sessions: [], limits: LIMITS_B.slice(0, 2) });

  assert.deepEqual(client.snapshot().limits, LIMITS_A);
  assert.equal(emitted.length, invalid.length + 1, 'each invalid canonical frame is forwarded once');
  assert.deepEqual(emitted.at(-1).limits, LIMITS_B.slice(0, 2));
});

test('item5 ChatStreamClient validates and preserves Codex freshness stamps atomically', (t) => {
  const { client, emitted, ws } = openClient();
  t.after(() => client.destroy());

  sendFrame(ws, { type: 'snapshot', events: [], drafts: {}, sessions: [], limits: ITEM5_LIMITS });
  assert.deepEqual(client.snapshot().limits, ITEM5_LIMITS);
  emitted.length = 0;

  const invalid = [
    ITEM5_LIMITS.map((row, index) => index === 2 ? { ...row, probed_at: null } : row),
    ITEM5_LIMITS.map((row, index) => index === 2 ? { ...row, upstream_reported_at: 'not-a-time' } : row),
    ITEM5_LIMITS.map((row, index) => index === 2 ? {
      ...row,
      upstream_reported_at: '2026-02-30T09:30:00Z',
      probed_at: '2026-02-30T09:30:01Z',
    } : row),
    ITEM5_LIMITS.map((row, index) => index === 2 ? {
      ...row,
      upstream_reported_at: '2026-08-26T09:30:02Z',
      probed_at: '2026-08-26T09:30:01Z',
    } : row),
    ITEM5_LIMITS.map((row, index) => index === 0 ? {
      ...row,
      upstream_reported_at: '2026-08-26T09:30:00Z',
      probed_at: '2026-08-26T09:30:01Z',
    } : row),
  ];
  for (const limits of invalid) sendFrame(ws, { type: 'limits.update', limits });

  assert.deepEqual(client.snapshot().limits, ITEM5_LIMITS);
  assert.equal(emitted.length, invalid.length);

  const validLeapFractionOffset = ITEM5_LIMITS.map((row, index) => index === 2 ? {
    ...row,
    upstream_reported_at: '2024-02-29T09:30:00.123456Z',
    probed_at: '2024-02-29T09:30:00.654321+00:00',
  } : row);
  sendFrame(ws, { type: 'limits.update', limits: validLeapFractionOffset });
  assert.deepEqual(client.snapshot().limits, validLeapFractionOffset);
  assert.equal(emitted.length, invalid.length + 1);
});

test('ChatStreamClient orders Codex freshness stamps at whole-second granularity', (t) => {
  // The live collector stamps upstream_reported_at with microseconds while a
  // whole-second probed_at can land in the same second; usage_state.py accepts
  // that pair, so the desktop must not drop the whole limits set for it.
  const { client, emitted, ws } = openClient();
  t.after(() => client.destroy());
  sendFrame(ws, { type: 'snapshot', events: [], drafts: {}, sessions: [], limits: ITEM5_LIMITS });
  emitted.length = 0;

  const sameSecondSubMillisSkew = ITEM5_LIMITS.map((row, index) => index === 2 ? {
    ...row,
    upstream_reported_at: '2026-09-04T04:50:00.089123Z',
    probed_at: '2026-09-04T04:50:00Z',
  } : row);
  sendFrame(ws, { type: 'limits.update', limits: sameSecondSubMillisSkew });
  assert.deepEqual(client.snapshot().limits, sameSecondSubMillisSkew);
  assert.equal(emitted.length, 1);

  const nextSecondSkew = ITEM5_LIMITS.map((row, index) => index === 2 ? {
    ...row,
    upstream_reported_at: '2026-09-04T04:50:01.000001Z',
    probed_at: '2026-09-04T04:50:00.999Z',
  } : row);
  sendFrame(ws, { type: 'limits.update', limits: nextSecondSkew });
  assert.deepEqual(client.snapshot().limits, sameSecondSubMillisSkew);
  assert.equal(emitted.length, 2);
});

test('ChatStreamClient stores daemon-owned hosts.stats frames', (t) => {
  const { client, emitted, ws } = openClient();
  t.after(() => client.destroy());

  sendFrame(ws, { type: 'snapshot', events: [], drafts: {}, sessions: [] });

  emitted.length = 0;
  sendFrame(ws, {
    type: 'hosts.stats',
    hosts: {
      'hostb': { host: 'hostb', cpu_load_1m: 0.4, sampled_at: '2026-09-04T23:00:00Z' },
      'hosta': { host: 'hosta', cpu_load_1m: 1.2, sampled_at: '2026-09-04T23:00:00Z' },
    },
  });

  assert.deepEqual(client.snapshot().hosts_stats, {
    'hostb': { host: 'hostb', cpu_load_1m: 0.4, sampled_at: '2026-09-04T23:00:00Z' },
    'hosta': { host: 'hosta', cpu_load_1m: 1.2, sampled_at: '2026-09-04T23:00:00Z' },
  });
  assert.equal(emitted.length, 1);
  assert.deepEqual(emitted[0], {
    type: 'hosts.stats',
    hosts: {
      'hostb': { host: 'hostb', cpu_load_1m: 0.4, sampled_at: '2026-09-04T23:00:00Z' },
      'hosta': { host: 'hosta', cpu_load_1m: 1.2, sampled_at: '2026-09-04T23:00:00Z' },
    },
    state_version: 1,
  });
});
