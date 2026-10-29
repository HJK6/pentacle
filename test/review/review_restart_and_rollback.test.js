'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn, spawnSync } = require('node:child_process');
const store = require('../../main/review_store');
const ROOT = path.join(__dirname, '../..');
const BASE = '9905e02d7daf865185b6778032350aeaafc0c435';
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'review-restart-'));
function config(root, dir) {
  const file = path.join(root, 'config.js');
  fs.writeFileSync(file, `module.exports={appName:'Pentacle',hosts:{local:{kind:'local'}},agents:{},features:{mic:false},reviewInbox:{dir:${JSON.stringify(dir)}}};\n`);
  return file;
}
function startHost(root, file) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [path.join(root, 'server/index.js'), '--profile', file, '--port', '0'],
      { cwd: root, env: { ...process.env, NODE_PATH: path.join(ROOT, 'node_modules') }, stdio: ['ignore', 'pipe', 'pipe'] });
    let output = '', errors = '', ready = false;
    child.stdout.on('data', chunk => {
      output += chunk;
      const match = /listening on http:\/\/127\.0\.0\.1:(\d+)/.exec(output);
      if (!ready && match) { ready = true; resolve({ child, url: `http://127.0.0.1:${match[1]}`, errors: () => errors }); }
    });
    child.stderr.on('data', chunk => { errors += chunk; });
    child.once('exit', code => { if (!ready) reject(new Error(`host exited ${code}: ${errors.slice(-300)}`)); });
    setTimeout(() => { if (!ready) { child.kill('SIGTERM'); reject(new Error(`host start timeout: ${errors.slice(-300)}`)); } }, 15000).unref();
  });
}
async function stopHost(host) {
  if (!host || host.child.exitCode !== null || host.child.signalCode !== null) return;
  await new Promise(resolve => { host.child.once('exit', resolve); host.child.kill('SIGTERM'); });
}
async function post(host, id, op, op_id) {
  const res = await fetch(`${host.url}/api/review-inbox/ack`, { method: 'POST', headers: { Origin: host.url, 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, op, op_id }) });
  return { status: res.status, body: await res.json() };
}

test('real host restart replays both lost no-op responses without reversing a later tab', async () => {
  const root = tmp(), dir = path.join(root, 'inbox'), file = config(root, dir);
  const id = store.addItem(dir, { kind: 'answer', title: 'Restart reply receipt', project: 'Agenteach' }).item.id;
  let host = await startHost(ROOT, file);
  try {
    await post(host, id, 'ack', crypto.randomUUID());
    const ackNoOpId = crypto.randomUUID();
    const acceptedAckNoOp = await post(host, id, 'ack', ackNoOpId);
    assert.equal(acceptedAckNoOp.body.result.changed, false);
    await stopHost(host); host = await startHost(ROOT, file);
    const tabB = await post(host, id, 'unack', crypto.randomUUID());
    const before = fs.readFileSync(path.join(dir, 'acks.json'));
    const retry = await post(host, id, 'ack', ackNoOpId);
    assert.deepEqual(retry.body.result, acceptedAckNoOp.body.result);
    assert.equal(retry.body.current.state, 'pending');
    assert.equal(retry.body.current.seq, tabB.body.current.seq);
    assert.deepEqual(fs.readFileSync(path.join(dir, 'acks.json')), before);
    const unackNoOpId = crypto.randomUUID();
    const acceptedUnackNoOp = await post(host, id, 'unack', unackNoOpId);
    assert.equal(acceptedUnackNoOp.body.result.changed, false);
    await stopHost(host); host = await startHost(ROOT, file);
    const tabBAck = await post(host, id, 'ack', crypto.randomUUID());
    const beforeRetry = fs.readFileSync(path.join(dir, 'acks.json'));
    const retryUnack = await post(host, id, 'unack', unackNoOpId);
    assert.deepEqual(retryUnack.body.result, acceptedUnackNoOp.body.result);
    assert.equal(retryUnack.body.current.state, 'done');
    assert.equal(retryUnack.body.current.seq, tabBAck.body.current.seq);
    assert.deepEqual(fs.readFileSync(path.join(dir, 'acks.json')), beforeRetry);
    const race = await Promise.all([post(host, id, 'unack', crypto.randomUUID()), post(host, id, 'ack', crypto.randomUUID())]);
    assert.ok(race.every(x => x.status === 200));
    const final = store.projection(dir).items.find(x => x.id === id);
    const latest = race.reduce((a, b) => a.body.current.seq > b.body.current.seq ? a : b);
    assert.equal(final.seq, latest.body.current.seq);
    assert.equal(final.state, latest.body.current.state);
    assert.equal(final.project, 'Agenteach');
  } finally { await stopHost(host); }
});

test('base web host ignores persisted v1 items and v2 receipts during rollback, then candidate reads them', async () => {
  const root = tmp(), dir = path.join(root, 'inbox'), file = config(root, dir), oldRoot = path.join(root, 'old');
  const id = store.addItem(dir, { kind: 'review', title: 'Rollback data survival' }).item.id;
  const projectId = store.addItem(dir, { kind: 'input', title: 'Project data survival', project: 'DUMBO' }).item.id;
  let candidate = await startHost(ROOT, file), old;
  try {
    await post(candidate, id, 'ack', crypto.randomUUID());
    const noOp = crypto.randomUUID(); await post(candidate, id, 'ack', noOp);
    await stopHost(candidate); candidate = null;
    fs.mkdirSync(oldRoot);
    const archive = spawnSync('git', ['archive', '--format=tar', BASE], { cwd: ROOT, maxBuffer: 64 * 1024 * 1024 });
    assert.equal(archive.status, 0);
    const untar = spawnSync('tar', ['-xf', '-', '-C', oldRoot], { input: archive.stdout });
    assert.equal(untar.status, 0);
    fs.mkdirSync(path.join(oldRoot, 'renderer/dist'), { recursive: true });
    fs.cpSync(path.join(ROOT, 'renderer/dist/web'), path.join(oldRoot, 'renderer/dist/web'), { recursive: true });
    const beforeItems = fs.readFileSync(path.join(dir, 'items.json'));
    const beforeAcks = fs.readFileSync(path.join(dir, 'acks.json'));
    old = await startHost(oldRoot, file);
    const oldRead = await fetch(`${old.url}/api/review-inbox/ack`, { method: 'GET' });
    assert.equal(oldRead.status, 404);
    await stopHost(old); old = null;
    assert.deepEqual(fs.readFileSync(path.join(dir, 'items.json')), beforeItems);
    assert.deepEqual(fs.readFileSync(path.join(dir, 'acks.json')), beforeAcks);
    candidate = await startHost(ROOT, file);
    const replay = await post(candidate, id, 'ack', noOp);
    assert.equal(replay.body.replayed, true);
    assert.equal(replay.body.current.state, 'done');
    assert.equal(store.readAcks(dir).value.entries[id].history.length, 1);
    assert.equal(store.projection(dir).items.find(item => item.id === id).project, 'Other');
    assert.equal(store.projection(dir).items.find(item => item.id === projectId).project, 'DUMBO');
  } finally { await stopHost(candidate); await stopHost(old); }
});

test('seed rerun and corrupt/stale data remain explicit after restart', async () => {
  const root = tmp(), dir = path.join(root, 'inbox');
  const seed = fs.readFileSync(path.join(__dirname, '../fixtures/review_seed.json'));
  store.seedItems(dir, seed);
  const before = fs.readFileSync(path.join(dir, 'items.json'));
  assert.equal(store.seedItems(dir, seed).changed, false);
  assert.deepEqual(fs.readFileSync(path.join(dir, 'items.json')), before);
  const items = store.readItems(dir).value;
  items.updated_at = new Date(Date.now() - 26 * 3600 * 1000).toISOString();
  fs.writeFileSync(path.join(dir, 'items.json'), JSON.stringify(items));
  assert.equal(store.projection(dir).stale, true);
  items.schema = 'lews-review-items/unknown';
  fs.writeFileSync(path.join(dir, 'items.json'), JSON.stringify(items));
  assert.equal(store.projection(dir).state, 'invalid');
});
