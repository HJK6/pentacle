const test = require('node:test');
const assert = require('node:assert/strict');
const { resolveBusinessSnapshot } = require('../main/business_envelope');
const { resolveForeclosureSnapshot } = require('../main/foreclosure_envelope');
const now = Date.parse('2026-01-01T00:01:00Z');
const envelope = data => ({ updated_at: '2026-01-01T00:00:00Z', server_received_at: '2026-01-01T00:00:00Z', freshness_ttl_sec: 10, data });
test('business aggregates all snapshots without losing queue or failed stage evidence', () => {
  const env = envelope({ default_batch: 'a', all_batches: ['a', 'b'], snapshots: {
    a: { scraped: 3, qualified: 2, by_area: { example: { scraped: 3 } }, pipeline_stages: [{ stage: 'scrape', state: 'complete' }], scraper_queue: { completed: 2 } },
    b: { scraped: 4, qualified: 1, by_area: { example: { scraped: 4 } }, pipeline_stages: [{ stage: 'scrape', state: 'failed', error: 'synthetic failure' }], scraper_queue: { failed: 1 } },
  } });
  const result = resolveBusinessSnapshot(env, false, now);
  assert.equal(result.scraped, 7); assert.equal(result.qualified, 3);
  assert.equal(result.by_area.example.scraped, 7); assert.equal(result.scraper_queue.failed, 1);
  assert.equal(result.pipeline_stages[0].state, 'failed'); assert.equal(result.pipeline_stages[0].error, 'synthetic failure');
  assert.equal(result._data_stale, true); assert.equal(result._transport_stale, true);
});
test('foreclosure selects requested/default batch and exposes missing selection', () => {
  const env = envelope({ default_batch: 'a', snapshots: { a: { batch: 'a', scraped: 3 }, b: { batch: 'b', scraped: 4 } } });
  assert.equal(resolveForeclosureSnapshot(env, 'b', true, now).batch, 'b');
  const missing = resolveForeclosureSnapshot(env, 'missing', true, now);
  assert.equal(missing.batch, 'a'); assert.equal(missing._missing_batch, 'missing');
});
test('legacy/no-data/empty snapshot paths preserve freshness and body', () => {
  for (const resolve of [env => resolveBusinessSnapshot(env, true, now), env => resolveForeclosureSnapshot(env, '', true, now)]) {
    assert.match(resolve(null).error, /no data/);
    assert.equal(resolve(envelope({ scraped: 6 })).scraped, 6);
    assert.equal(resolve(envelope({ snapshots: {}, scraped: 8 })).scraped, 8);
  }
});

test('configured hub client authenticates, receives snapshot and persists only owned temp cache', async () => {
  const fs = require('node:fs'); const os = require('node:os'); const path = require('node:path');
  const vm = require('node:vm'); const { createRequire } = require('node:module');
  const { EventEmitter } = require('node:events'); const { WebSocketServer } = require('ws');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'public-hub-test-'));
  const tokenPath = path.join(dir, 'synthetic-token'); fs.writeFileSync(tokenPath, 'synthetic-test-token');
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await new Promise(resolve => server.once('listening', resolve));
  let hello; let requestedPath;
  server.on('connection', (socket, request) => {
    requestedPath = request.url;
    socket.on('message', raw => { hello = JSON.parse(raw.toString()); });
    socket.send(JSON.stringify({ type: 'welcome', dashboards: [] }));
    socket.send(JSON.stringify({ type: 'snapshot', envelope: { dashboard_id: 'synthetic.board', data: { count: 7 } } }));
  });
  const file = path.join(__dirname, '../main/dashboard_hub_client.js');
  const module = { exports: {} }; const baseRequire = createRequire(file);
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), { module, require: name => name === 'os' ? { ...os, homedir: () => dir } : baseRequire(name),
    process, console: { log() {}, warn() {}, error() {} }, setTimeout, clearTimeout, setInterval, clearInterval });
  const client = module.exports; const app = new EventEmitter();
  try {
    client.init({ dashboardHub: { url: `ws://127.0.0.1:${server.address().port}`, readTokenPath: tokenPath } }, app);
    const deadline = Date.now() + 2000;
    while ((!hello || !client.get('synthetic.board')) && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 5));
    assert.ok(hello); assert.equal(hello.subscribe.all, true); assert.equal(client.connected, true);
    assert.equal(requestedPath, '/live?token=synthetic-test-token');
    assert.equal(client.get('synthetic.board').data.count, 7);
    app.emit('before-quit');
    const cached = JSON.parse(fs.readFileSync(path.join(dir, '.pentacle/dashboard-cache.json'), 'utf8'));
    assert.equal(cached['synthetic.board'].data.count, 7);
    assert.equal(JSON.stringify(cached).includes('synthetic-test-token'), false);
  } finally {
    app.emit('before-quit');
    if (client._pingInterval) clearInterval(client._pingInterval);
    for (const socket of server.clients) socket.terminate();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
