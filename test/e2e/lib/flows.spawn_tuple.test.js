'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { spawnThrowawayChat } = require('./flows');

function context({ catalogOk = true, tuple = ['provider_a-fixture-model', 'high'], actual = 'provider_a' } = {}) {
  const calls = { spawn: [], tracked: [] };
  const window = {
    cc: { chatSpawnCatalog: async () => ({ ok: catalogOk, catalog: { profiles: { desktop_manual: { provider_a: tuple } } } }) },
    PentacleHarnessActions: {
      spawnThrowaway: async (options) => {
        calls.spawn.push(options);
        const provider = options.provider && options.model && options.effort ? actual : 'provider_c';
        return { sessionName: 'test-only', streamId: 'hosta:test-only', hostId: 'local', slot: 0,
          spawnAck: { requested: { provider }, resolved: { provider }, actualLaunch: { provider } } };
      },
    },
  };
  return { calls, ctx: {
    eval: code => vm.runInNewContext(code, { window }),
    assert: (label, value) => assert.ok(value, label),
    trackSpawned: (...args) => calls.tracked.push(args),
    waitFor: async () => true,
    log: () => {},
  } };
}

test('requested Provider A walk overrides an existing Provider C modal preference with explicit catalog tuple', async () => {
  const { ctx, calls } = context();
  await spawnThrowawayChat(ctx, 'provider_a');
  assert.equal(calls.spawn[0].provider, 'provider_a');
  assert.equal(calls.spawn[0].model, 'provider_a-fixture-model');
  assert.equal(calls.spawn[0].effort, 'high');
  assert.equal(calls.tracked.length, 1);
});

for (const options of [{ catalogOk: false }, { tuple: null }, { tuple: ['model-only'] }]) {
  test(`invalid catalog cannot spawn: ${JSON.stringify(options)}`, async () => {
    const { ctx, calls } = context(options);
    await assert.rejects(spawnThrowawayChat(ctx, 'provider_a'));
    assert.equal(calls.spawn.length, 0);
  });
}

test('wrong returned provider fails but the owned throwaway is still tracked for cleanup', async () => {
  const { ctx, calls } = context({ actual: 'provider_c' });
  await assert.rejects(spawnThrowawayChat(ctx, 'provider_a'));
  assert.equal(calls.tracked.length, 1);
});
