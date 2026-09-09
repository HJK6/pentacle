'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  createSpawnCatalogLoader,
  isUsableSpawnCatalogResponse,
} = require('../renderer/spawn_catalog_loader');

const catalog = {
  catalog_version: 'spawn-catalog-v2',
  profiles: { desktop_manual: { claude: ['claude-opus-4-8', 'high'], codex: ['gpt-5.6-sol', 'high'] } },
  models: { claude: { 'claude-fable-5': { efforts: ['high'] } } },
};

test('spawn catalog loader coalesces a cold request and serves the warmed catalog without another IPC call', async () => {
  let calls = 0;
  let resolveFetch;
  const loader = createSpawnCatalogLoader(() => {
    calls += 1;
    return new Promise((resolve) => { resolveFetch = resolve; });
  });

  const warm = loader.load();
  const concurrent = loader.load();
  assert.equal(calls, 0, 'fetch starts in the next microtask');
  await Promise.resolve();
  assert.equal(calls, 1);
  resolveFetch({ ok: true, catalog });
  assert.deepEqual(await warm, { ok: true, catalog });
  assert.deepEqual(await concurrent, { ok: true, catalog });

  const interactive = await loader.load();
  assert.deepEqual(interactive, { ok: true, catalog });
  assert.equal(calls, 1, 'opening New Chat after warmup performs no catalog IPC');
  assert.equal(loader.peek(), catalog);
});

test('failed or malformed catalog replies are not cached and remain retryable', async () => {
  const replies = [
    { ok: false, error: 'disconnected' },
    { ok: true, catalog: { profiles: {} } },
    { ok: true, catalog },
  ];
  let calls = 0;
  const loader = createSpawnCatalogLoader(async () => replies[calls++]);

  assert.equal(isUsableSpawnCatalogResponse(await loader.load()), false);
  assert.equal(loader.peek(), null);
  assert.equal(isUsableSpawnCatalogResponse(await loader.load()), false);
  assert.equal(loader.peek(), null);
  assert.equal(isUsableSpawnCatalogResponse(await loader.load()), true);
  assert.equal(loader.peek(), catalog);
  assert.equal(calls, 3);
});

test('a rejected cold request clears the in-flight slot for the next attempt', async () => {
  let calls = 0;
  const loader = createSpawnCatalogLoader(async () => {
    calls += 1;
    if (calls === 1) throw new Error('socket unavailable');
    return { ok: true, catalog };
  });

  await assert.rejects(loader.load(), /socket unavailable/);
  assert.equal(loader.peek(), null);
  assert.deepEqual(await loader.load(), { ok: true, catalog });
  assert.equal(calls, 2);
});
