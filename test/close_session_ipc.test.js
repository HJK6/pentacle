const { test } = require('node:test');
const assert = require('node:assert/strict');

const chatStreamClient = require('../main/chat_stream_client');

test('closeSession serializes operator_confirm for both option spellings only when enabled', async () => {
  const originalSendCommand = chatStreamClient.sendCommand;
  const payloads = [];
  chatStreamClient.sendCommand = async (payload) => {
    payloads.push(payload);
    return { ok: true };
  };

  try {
    await chatStreamClient.closeSession({
      host: 'host-a',
      sessionName: 'session-a',
      operatorConfirm: true,
      force: true,
    });
    await chatStreamClient.closeSession({
      host: 'host-b',
      sessionName: 'session-b',
      operator_confirm: true,
    });
    await chatStreamClient.closeSession({
      host: 'host-c',
      sessionName: 'session-c',
      force: true,
    });
  } finally {
    chatStreamClient.sendCommand = originalSendCommand;
  }

  assert.equal(payloads.length, 3);
  assert.equal(payloads[0].operator_confirm, true);
  assert.equal(payloads[1].operator_confirm, true);
  assert.equal(Object.hasOwn(payloads[2], 'operator_confirm'), false);
});
