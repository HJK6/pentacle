const { test } = require('node:test');
const assert = require('node:assert/strict');

const chatStreamClient = require('../main/chat_stream_client');

function withFakeDaemon(replies, fn) {
  const original = chatStreamClient.sendCommand;
  const sent = [];
  chatStreamClient.sendCommand = async (payload, prefix, options) => {
    sent.push({ payload, prefix, options });
    return replies.shift();
  };
  return Promise.resolve(fn(sent)).finally(() => { chatStreamClient.sendCommand = original; });
}

test('designate binds the freshly read target generation and grant revision', () => withFakeDaemon([
  { type: 'assistant.lifecycle.ok', grant: { revision: 3 }, target: { session_generation: 'g-7', eligible: true } },
  { type: 'assistant.lifecycle.ok', receipt: { revision: 4 } },
], async (sent) => {
  const reply = await chatStreamClient.lifecycleAuthority({ action: 'designate', targetStreamId: 'node-a:v2-a', reason: 'why' });
  assert.equal(reply.receipt.revision, 4);
  assert.deepEqual(sent[0].payload, { type: 'assistant.lifecycle', action: 'inspect', target_stream_id: 'node-a:v2-a' });
  assert.deepEqual(sent[1].payload, {
    type: 'assistant.lifecycle', action: 'designate', reason: 'why', expected_revision: 3,
    target_stream_id: 'node-a:v2-a', target_generation: 'g-7',
  });
  assert.equal(sent[1].prefix, 'assistant.lifecycle');
  assert.match(sent[1].options.requestId, /^[0-9a-f-]{36}$/);
  // No actor, role or credential claim rides the wire; the daemon derives them.
  for (const key of ['actor_kind', 'operator_principal', 'role', 'stream_token']) assert.equal(Object.hasOwn(sent[1].payload, key), false);
}));

test('revoke sends no target and inspect-only stops after the read', () => withFakeDaemon([
  { type: 'assistant.lifecycle.ok', grant: { revision: 5, stream_id: 'node-a:v2-a' } },
  { type: 'assistant.lifecycle.ok', receipt: { revision: 6 } },
  { type: 'assistant.lifecycle.ok', grant: { revision: 6 } },
], async (sent) => {
  await chatStreamClient.lifecycleAuthority({ action: 'revoke', reason: 'stop' });
  assert.deepEqual(sent[1].payload, { type: 'assistant.lifecycle', action: 'revoke', reason: 'stop', expected_revision: 5 });
  await chatStreamClient.lifecycleAuthority({ action: 'inspect' });
  assert.equal(sent.length, 3);
}));

test('transfer is not an operator web action', () => withFakeDaemon([
  { type: 'assistant.lifecycle.ok', grant: { revision: 1 } },
], async (sent) => {
  await assert.rejects(chatStreamClient.lifecycleAuthority({ action: 'transfer', targetStreamId: 'node-a:v2-b', reason: 'x' }));
  assert.equal(sent.length, 1);
}));
