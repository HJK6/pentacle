'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const store = require('../../main/review_store');
const { main } = require('../../server');
const forbiddenItems = new Set(['ack', 'acked', 'acked_at', 'history', 'op_id', 'seq', 'receipts', 'accepted_at', 'seq_seen', 'result']);
const forbiddenAcks = new Set(['kind', 'title', 'source', 'origin_note', 'link', 'created_at', 'first_raised_at', 'resolved_at', 'resolution_reason', 'project']);
function visit(value, forbidden) {
  if (!value || typeof value !== 'object') return;
  for (const [key, child] of Object.entries(value)) { assert.equal(forbidden.has(key), false, `forbidden ${key}`); visit(child, forbidden); }
}
function temp() { return fs.mkdtempSync(path.join(os.tmpdir(), 'review-nonoverlap-')); }

test('CLI-owned item mutations and host-owned ack mutations never cross files', async () => {
  const root = temp(), dir = path.join(root, 'inbox');
  const actionItems = path.join(root, 'action-items.json'); fs.writeFileSync(actionItems, '{"tv":"unchanged"}\n');
  const tvBefore = fs.readFileSync(actionItems);
  const id = store.addItem(dir, { kind: 'review', title: 'Review item ownership', project: 'Pentacle' }).item.id;
  const config = path.join(root, 'config.js');
  fs.writeFileSync(config, `module.exports={appName:'Pentacle',hosts:{local:{kind:'local'}},agents:{},features:{mic:false},reviewInbox:{dir:${JSON.stringify(dir)}}};\n`);
  const web = await main(['--profile', config, '--port', '0']);
  const send = async (route, body) => {
    const response = await fetch(`${web.url}${route}`, { method: 'POST', headers: { Origin: web.url, 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const text = await response.text();
    assert.equal(response.status, 200, text);
    return JSON.parse(text);
  };
  try {
    const itemsBefore = fs.readFileSync(path.join(dir, 'items.json'));
    const ackResponse = await send('/api/review-inbox/ack', { id, op: 'ack', op_id: crypto.randomUUID() });
    visit(ackResponse, forbiddenAcks);
    await send('/api/review-inbox/ack', { id, op: 'unack', op_id: crypto.randomUUID() });
    assert.deepEqual(fs.readFileSync(path.join(dir, 'items.json')), itemsBefore);
    const acksBefore = fs.readFileSync(path.join(dir, 'acks.json'));
    store.addItem(dir, { kind: 'input', title: 'Choose a review window' });
    store.resolveItem(dir, id, 'Closed');
    assert.deepEqual(fs.readFileSync(path.join(dir, 'acks.json')), acksBefore);
    const run = crypto.randomBytes(16).toString('hex');
    const smoke = store.addItem(dir, { kind: 'review', title: `Review inbox smoke ${run}`, smoke_run: run }).item.id;
    await send('/api/review-inbox/smoke-cleanup', { id: smoke, run_id: run, op_id: crypto.randomUUID() });
    const itemBytes = fs.readFileSync(path.join(dir, 'items.json'));
    assert.ok(JSON.parse(itemBytes).items.some(x => x.id === smoke));
    const ackBytes = fs.readFileSync(path.join(dir, 'acks.json'));
    store.smokeRemove(dir, smoke, run);
    assert.deepEqual(fs.readFileSync(path.join(dir, 'acks.json')), ackBytes);
    visit(store.readItems(dir).value, forbiddenItems);
    assert.equal(store.readItems(dir).value.items.find(item => item.id === id).project, 'Pentacle');
    visit(store.readAcks(dir).value, forbiddenAcks);
    assert.deepEqual(fs.readFileSync(actionItems), tvBefore);
  } finally { await web.close(); }
});

test('seed changes only items.json when an ack file already exists', () => {
  const root = temp(), dir = path.join(root, 'inbox');
  store.addItem(dir, { kind: 'answer', title: 'Temporary setup item' });
  const id = store.readItems(dir).value.items[0].id;
  store.ackItem(dir, { id, op: 'ack', op_id: crypto.randomUUID() });
  const acksBefore = fs.readFileSync(path.join(dir, 'acks.json'));
  fs.unlinkSync(path.join(dir, 'items.json'));
  const seed = fs.readFileSync(path.join(__dirname, '../fixtures/review_seed.json'));
  store.seedItems(dir, seed);
  assert.deepEqual(fs.readFileSync(path.join(dir, 'acks.json')), acksBefore);
  visit(store.readItems(dir).value, forbiddenItems);
});
