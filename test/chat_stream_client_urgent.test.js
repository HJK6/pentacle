'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createCcHandlers, createCollector } = require('../main/cc_handlers');

// Same real client/handler stack as the voice-answers wire test; only the socket is replaced.
function harness(t) {
  const clientPath = require.resolve('../main/chat_stream_client');
  delete require.cache[clientPath];
  const client = require(clientPath);
  const sent = [];
  client.connected = true;
  client._isAlive = true;
  client._sessions = [
    { stream_id: 'fixture:agent', session_kind: 'agent' },
    { stream_id: 'fixture:assistant', session_kind: 'assistant_composite' },
  ];
  client._ws = { readyState: 1, ping() {}, send(raw) {
    const payload = JSON.parse(raw);
    sent.push(payload);
    const pending = client._pending.get(payload.request_id);
    client._pending.delete(payload.request_id);
    pending.resolve({ type: 'send.result', request_id: payload.request_id, delivery: 'landed' });
  } };
  t.after(() => { client._ws = null; client.destroy(); delete require.cache[clientPath]; });
  const collector = createCollector();
  createCcHandlers({ CONFIG: { chatStream: {} }, chatStreamClient: client, harness: true }).register(collector);
  const send = (session, reply) => collector.table['chat-stream:send'].handler(
    {}, 'fixture', session, 'Ship it', 'request-1', 'optimistic-1', [], { stream_id: `fixture:${session}`, ...reply });
  return { sent, send };
}

test('an agent chat send carries urgent only when Steer now asks for it', async t => {
  const h = harness(t);
  await h.send('agent', {});
  await h.send('agent', { urgent: false });
  await h.send('agent', { urgent: 'yes' });
  await h.send('agent', { urgent: true });
  assert.deepEqual(h.sent.map(payload => 'urgent' in payload), [false, false, false, true]);
  assert.equal(h.sent[3].type, 'send');
  assert.equal(h.sent[3].text, 'Ship it');
});

test('the composite send branch never carries urgent', async t => {
  const h = harness(t);
  await h.send('assistant', { urgent: true });
  assert.equal(h.sent.length, 1);
  assert.equal('urgent' in h.sent[0], false);
  assert.equal(h.sent[0].message, 'Ship it');
});
