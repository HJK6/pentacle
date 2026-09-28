const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

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
  { type: 'assistant.lifecycle.ok', code: 'consent_pending', challenge: { challenge_id: 'designation-approval' } },
], async (sent) => {
  const reply = await chatStreamClient.lifecycleAuthority({ action: 'designate', targetStreamId: 'node-a:v2-a', reason: 'why' });
  assert.equal(reply.code, 'consent_pending');
  assert.equal(reply.challenge.challenge_id, 'designation-approval');
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
  { type: 'assistant.lifecycle.ok', code: 'consent_pending', challenge: { challenge_id: 'revocation-approval' } },
  { type: 'assistant.lifecycle.ok', grant: { revision: 5 } },
], async (sent) => {
  const reply = await chatStreamClient.lifecycleAuthority({ action: 'revoke', reason: 'stop' });
  assert.equal(reply.code, 'consent_pending');
  assert.equal(reply.challenge.challenge_id, 'revocation-approval');
  assert.deepEqual(sent[1].payload, { type: 'assistant.lifecycle', action: 'revoke', reason: 'stop', expected_revision: 5 });
  await chatStreamClient.lifecycleAuthority({ action: 'inspect' });
  assert.equal(sent.length, 3);
}));

test('consent status reads the requested challenge without a lifecycle mutation', () => withFakeDaemon([
  { type: 'consent.status.ok', challenge: { challenge_id: 'approval', state: 'approved' } },
], async (sent) => {
  const reply = await chatStreamClient.lifecycleAuthority({ action: 'consent-status', challengeId: 'approval' });
  assert.equal(reply.challenge.state, 'approved');
  assert.deepEqual(sent[0].payload, { type: 'consent.status', challenge_id: 'approval' });
  assert.equal(sent.length, 1);
}));

// Exercise the actual renderer function without booting its unrelated UI.
const rendererSource = fs.readFileSync(path.join(__dirname, '../renderer/app.js'), 'utf8');
const rendererStart = rendererSource.indexOf('async function changeLifecycleAuthority(');
const rendererEnd = rendererSource.indexOf('// ── Actions', rendererStart);
assert.ok(rendererStart >= 0 && rendererEnd > rendererStart);
async function rendererApproval({ action = 'designate', statuses = [], result, confirmed = true } = {}) {
  const calls = [], toasts = [], confirmations = [];
  let now = 1000;
  const pending = { ok: true, code: 'consent_pending', challenge: { challenge_id: 'approval', expires_at: 10 } };
  const context = vm.createContext({
    IS_CLIENT: true,
    chatSessionStateForNameHost: () => ({ stream_id: 'node-a:manager' }),
    showToast: (message, options) => toasts.push({ message, options }),
    Date: { now: () => now },
    setTimeout: (fn, delay) => { now += delay; fn(); },
    window: {
      confirmDialog: async (message, options) => { confirmations.push({ message, options }); return confirmed; },
      cc: { chatLifecycleAuthority: async (payload) => {
        calls.push(JSON.parse(JSON.stringify(payload)));
        if (payload.action === 'inspect') return { ok: true, grant: { stream_id: 'node-a:prior' }, target: { eligible: true, session_generation: 'G1' } };
        if (payload.action === 'consent-status') {
          assert.equal(toasts.some(t => t.message === 'Phone approval applied.'), false);
          return statuses.shift() || { ok: true, challenge: { state: 'pending' } };
        }
        return result || pending;
      } },
    },
  });
  vm.runInContext(rendererSource.slice(rendererStart, rendererEnd), context);
  await context.changeLifecycleAuthority(action, 'manager', 'node-a');
  return { calls, toasts, confirmations };
}

for (const action of ['designate', 'revoke']) {
  for (const state of ['approved', 'denied', 'expired']) {
    test(`renderer pending approval polls the real challenge status: ${action}/${state}`, async () => {
      const flow = await rendererApproval({ action, statuses: [
        { ok: true, challenge: { state: 'pending' } }, { ok: true, challenge: { state } },
      ] });
      assert.equal(flow.confirmations[0].options.confirmLabel, 'Request approval on phone');
      assert.equal(flow.calls[1].action, action);
      assert.deepEqual(flow.calls.slice(2), [
        { action: 'consent-status', challengeId: 'approval' },
        { action: 'consent-status', challengeId: 'approval' },
      ]);
      assert.match(flow.toasts[0].message, /^Pending approval on phone/);
      assert.equal(flow.toasts.at(-1).message, state === 'approved' ? 'Phone approval applied.' : `Phone approval ${state}.`);
    });
  }
}

test('renderer fails closed on unavailable or expired approval', async () => {
  const unavailable = await rendererApproval({ statuses: [{ ok: false, error: 'disconnected' }] });
  assert.equal(unavailable.toasts.at(-1).message, 'Approval status unavailable. Check the phone.');
  assert.equal(unavailable.toasts.at(-1).options.type, 'error');
  assert.equal(unavailable.calls.filter(c => c.action === 'consent-status').length, 1);
  const expired = await rendererApproval();
  assert.equal(expired.toasts.at(-1).message, 'Phone approval expired. Request a new approval.');
  assert.equal(expired.toasts.some(t => t.message === 'Phone approval applied.'), false);
});

test('renderer cancellation and an immediate legacy receipt cannot report approval', async () => {
  const canceled = await rendererApproval({ confirmed: false });
  assert.equal(canceled.calls.length, 1);
  assert.equal(canceled.toasts.length, 0);
  const legacy = await rendererApproval({ result: { ok: true, receipt: { revision: 1 } } });
  assert.equal(legacy.calls.length, 2);
  assert.equal(legacy.toasts.at(-1).message, 'Phone approval is required; no authority was changed.');
});

test('transfer is not an operator web action', () => withFakeDaemon([
  { type: 'assistant.lifecycle.ok', grant: { revision: 1 } },
], async (sent) => {
  await assert.rejects(chatStreamClient.lifecycleAuthority({ action: 'transfer', targetStreamId: 'node-a:v2-b', reason: 'x' }));
  assert.equal(sent.length, 1);
}));
