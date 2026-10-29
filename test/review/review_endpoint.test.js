'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const WebSocket = require('ws');
const { main } = require('../../server');
const store = require('../../main/review_store');
const { loadConfig } = require('../../config-loader');
function tmp() { return fs.mkdtempSync(path.join(os.tmpdir(), 'review-endpoint-')); }
function config(root, dir) {
  const file = path.join(root, 'config.js');
  fs.writeFileSync(file, `module.exports={appName:'Pentacle',hosts:{local:{kind:'local'}},agents:{},features:{mic:false},reviewInbox:{dir:${JSON.stringify(dir)}}};\n`);
  return file;
}
async function post(web, route, value, options = {}) {
  const origin = options.origin === undefined ? web.url : options.origin;
  const headers = { ...(origin === null ? {} : { Origin: origin }), 'Content-Type': options.type || 'application/json', ...(options.headers || {}) };
  const response = await fetch(`${web.url}${route}`, { method: options.method || 'POST', headers,
    body: options.raw === undefined ? JSON.stringify(value) : options.raw });
  const text = await response.text();
  return { status: response.status, body: response.headers.get('content-type')?.includes('application/json') ? JSON.parse(text) : text };
}
async function ccRead(web) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${web.url.replace('http:', 'ws:')}/cc`, { origin: web.url });
    ws.once('open', () => ws.send(JSON.stringify({ id: 1, method: 'review-inbox:read', args: [] })));
    ws.once('message', raw => { ws.close(); resolve(JSON.parse(raw)); });
    ws.once('error', reject);
  });
}

test('local POST routes enforce Origin, shape, method and known IDs, and cc read is bounded', async () => {
  const root = tmp(), dir = path.join(root, 'inbox'), file = config(root, dir);
  const id = store.addItem(dir, { kind: 'review', title: 'Review endpoint behavior' }).item.id;
  const web = await main(['--profile', file, '--port', '0']);
  try {
    const publicConfig = await (await fetch(`${web.url}/api/config`)).json();
    assert.deepEqual(publicConfig.reviewInbox, { enabled: true });
    assert.equal(JSON.stringify(publicConfig).includes(dir), false);
    const route = '/api/review-inbox/ack';
    assert.equal((await post(web, route, { id, op: 'ack', op_id: crypto.randomUUID() }, { origin: null })).status, 403);
    assert.equal((await post(web, route, { id, op: 'ack', op_id: crypto.randomUUID() }, { origin: 'https://forged.example' })).status, 403);
    assert.equal((await post(web, route, { id, op: 'ack', op_id: crypto.randomUUID() }, { type: 'text/plain' })).status, 415);
    assert.equal((await post(web, route, {}, { raw: '{"id":"a","id":"b"}' })).status, 400);
    assert.equal((await post(web, route, {}, { raw: 'x'.repeat(513) })).status, 400);
    assert.equal((await post(web, route, { id, op: 'ack', op_id: crypto.randomUUID(), title: 'extra' })).status, 422);
    assert.equal((await post(web, route, { id: `ri_${'0'.repeat(32)}`, op: 'ack', op_id: crypto.randomUUID() })).status, 404);
    const get = await fetch(`${web.url}${route}`);
    assert.equal(get.status, 405);
    const noGet = await fetch(`${web.url}/api/review-inbox/items`);
    assert.equal(noGet.status, 404);
    const op1 = crypto.randomUUID(), op2 = crypto.randomUUID(), op3 = crypto.randomUUID();
    const first = await post(web, route, { id, op: 'ack', op_id: op1 }); assert.equal(first.status, 200);
    const noOp = await post(web, route, { id, op: 'ack', op_id: op2 }); assert.equal(noOp.body.result.changed, false);
    const opposite = await post(web, route, { id, op: 'unack', op_id: op3 }); assert.equal(opposite.status, 200);
    const acksBefore = fs.readFileSync(path.join(dir, 'acks.json'));
    const replay = await post(web, route, { id, op: 'ack', op_id: op2 });
    assert.deepEqual(replay.body.result, noOp.body.result);
    assert.equal(replay.body.current.state, 'pending');
    assert.deepEqual(fs.readFileSync(path.join(dir, 'acks.json')), acksBefore);
    assert.equal((await post(web, route, { id, op: 'unack', op_id: op2 })).status, 409);
    const unackNoOpId = crypto.randomUUID();
    const unackNoOp = await post(web, route, { id, op: 'unack', op_id: unackNoOpId });
    assert.equal(unackNoOp.body.result.changed, false);
    await post(web, route, { id, op: 'ack', op_id: crypto.randomUUID() });
    const ackFile = fs.readFileSync(path.join(dir, 'acks.json'));
    const unackReplay = await post(web, route, { id, op: 'unack', op_id: unackNoOpId });
    assert.deepEqual(unackReplay.body.result, unackNoOp.body.result);
    assert.equal(unackReplay.body.current.state, 'done');
    assert.deepEqual(fs.readFileSync(path.join(dir, 'acks.json')), ackFile);
    store.resolveItem(dir, id, 'Closed');
    assert.equal((await post(web, route, { id, op: 'ack', op_id: crypto.randomUUID() })).status, 409);
    assert.equal((await post(web, route, { id, op: 'ack', op_id: op2 })).body.current.state, 'resolved');
    const good = await ccRead(web); assert.equal(good.result.state, 'ok');
    const itemsBeforeCorruption = fs.readFileSync(path.join(dir, 'items.json'));
    fs.writeFileSync(path.join(dir, 'items.json'), Buffer.from([0xff]));
    const bad = await ccRead(web); assert.equal(bad.result.state, 'invalid');
    assert.equal(JSON.stringify(bad).includes(dir), false);
    assert.equal(JSON.stringify(bad).includes('EACCES'), false);
    fs.writeFileSync(path.join(dir, 'items.json'), itemsBeforeCorruption);
    fs.chmodSync(path.join(dir, 'items.json'), 0o000);
    try {
      const unreadable = await ccRead(web);
      assert.equal(unreadable.result.state, 'unavailable');
      assert.equal(unreadable.result.error_code, 'store_unreadable');
      assert.equal(JSON.stringify(unreadable).includes(dir), false);
    } finally { fs.chmodSync(path.join(dir, 'items.json'), 0o600); }
  } finally { await web.close(); }
});

test('effective overlay config differs only by the private reviewInbox key', () => {
  const root = tmp(), dir = path.join(root, 'inbox'), overlay = config(root, dir);
  const base = path.join(root, 'base.js');
  fs.writeFileSync(base, "module.exports={appName:'Pentacle',hosts:{local:{kind:'local'}},agents:{},features:{mic:false}};\n");
  const loadedBase = loadConfig(path.join(__dirname, '../..'), { PENTACLE_CONFIG: base }).config;
  const loadedOverlay = loadConfig(path.join(__dirname, '../..'), { PENTACLE_CONFIG: overlay }).config;
  const { reviewInbox, ...rest } = loadedOverlay;
  assert.deepEqual(rest, loadedBase);
  assert.deepEqual(reviewInbox, { dir });
});

test('Tailscale identity and exact Origin protect both writes', async () => {
  const root = tmp(), dir = path.join(root, 'inbox'), file = config(root, dir);
  const runId = crypto.randomBytes(16).toString('hex');
  const id = store.addItem(dir, { kind: 'review', title: `Review inbox smoke ${runId}`, smoke_run: runId }).item.id;
  const web = await main(['--profile', file, '--port', '0', '--auth', 'tailscale', '--allow-login', 'reviewer@example.test', '--origin', 'https://review.tail.test']);
  try {
    const identity = { 'tailscale-user-login': 'reviewer@example.test', 'x-forwarded-proto': 'https', 'x-forwarded-for': '100.64.1.1' };
    const ack = '/api/review-inbox/ack', clean = '/api/review-inbox/smoke-cleanup';
    const request = { id, op: 'ack', op_id: crypto.randomUUID() };
    assert.equal((await post(web, ack, request, { origin: 'https://review.tail.test' })).status, 401);
    assert.equal((await post(web, ack, request, { headers: identity, origin: null })).status, 403);
    assert.equal((await post(web, clean, { id, run_id: store.readItems(dir).value.items[0].smoke.run_id, op_id: crypto.randomUUID() }, { headers: identity, origin: null })).status, 403);
    assert.equal((await post(web, ack, request, { headers: identity, origin: 'https://forged.example' })).status, 403);
    assert.equal((await post(web, ack, request, { headers: identity, origin: 'https://review.tail.test' })).status, 200);
  } finally { await web.close(); }
});
