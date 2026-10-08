'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createSeed, readCosmoVocabulary, createFakeCosmo, startFakeCosmo } = require('./e2e/lib/fake_cosmo_server');
const vocabulary = { assistant: 'fixture-assistant', app: 'app', partnerWho: 'fixture-partner', bothWho: 'both' };
const today = '2099-06-15';
function fixture() { return createFakeCosmo({ today, vocabulary }); }
function request(f, method, url, body = null) { return f.dispatch({ method, url, body, authorization: 'Bearer synthetic-test-only' }); }
test('fixture vocabulary comes from daemon constants without duplicating private identities', () => {
  const values = readCosmoVocabulary();
  const source = fs.readFileSync(path.join(__dirname, '../services/chat-stream-v2/household.py'), 'utf8');
  for (const [key, name] of Object.entries({ assistant: 'COSMO_ASSISTANT', app: 'COSMO_APP', partnerWho: 'COSMO_PARTNER_WHO', bothWho: 'COSMO_BOTH_WHO' })) {
    assert.ok(values[key] === JSON.parse(source.match(new RegExp(`^${name} = ("[^"\\n]+")`, 'm'))[1]), `matches ${name}`);
  }
});
test('synthetic seed covers every list and attribution, due and event distinction', () => {
  const seed = createSeed(today, vocabulary);
  assert.deepEqual(Object.keys(seed.lists), ['tasks', 'grocery', 'meals', 'chores', 'study']);
  for (const rows of Object.values(seed.lists)) assert.equal(rows.length, 2);
  const items = Object.values(seed.lists).flat();
  assert.ok(items.some(x => x.due_date < today));
  assert.ok(items.some(x => x.due_date === '2099-06-16'));
  assert.ok(items.some(x => x.created_by === vocabulary.assistant));
  assert.ok(items.some(x => x.scope === 'shared' && x.priority === 'hi' && x.created_by === 'partner_assistant'));
  assert.ok(seed.events.some(x => x.date === today && x.time === null));
  assert.ok(seed.events.some(x => x.date === today && x.time === '14:30'));
  assert.ok(seed.events.some(x => x.date === '2099-06-16' && x.who === vocabulary.partnerWho && x.scope === 'operator'));
  assert.ok(seed.events.some(x => x.who === vocabulary.bothWho && x.scope === 'shared'));
  assert.ok(seed.events.some(x => x.date !== today && x.date.startsWith(today.slice(0, 7)) && x.created_by === vocabulary.assistant));
});
test('fake speaks exact Cosmo routes for all six daemon verbs with auth-presence-only log', async () => {
  const f = fixture();
  for (const list of Object.keys(f.seed.lists)) assert.equal((await request(f, 'GET', `/lists/${list}/items`)).body.items.length, 2);
  assert.equal((await request(f, 'GET', '/events?from=2099-06-15&to=2099-06-15')).body.items.length, 2);
  const added = await request(f, 'POST', '/lists/grocery/items', { label: 'Synthetic addition' });
  assert.equal(added.status, 201); assert.equal(added.body.list, 'grocery');
  assert.equal(added.body.scope, 'operator'); assert.equal(added.body.created_by, 'app');
  assert.equal((await request(f, 'PATCH', `/lists/items/${added.body.id}/done`)).status, 200);
  assert.equal((await request(f, 'DELETE', `/lists/items/${added.body.id}`)).status, 204);
  const event = await request(f, 'POST', '/events', { date: today, time: null, title: 'Synthetic event', who: vocabulary.partnerWho });
  assert.equal(event.status, 201); assert.equal(event.body.who, vocabulary.partnerWho);
  assert.equal((await request(f, 'DELETE', `/events/${event.body.id}`)).status, 204);
  const log = (await request(f, 'GET', '/__fixture/calls')).body.calls;
  assert.equal(log.length, 11); assert.ok(log.every(x => x.authPresent === true));
  assert.ok(log.every(x => !('authorization' in x)));
  assert.ok(!JSON.stringify(log).includes('synthetic-test-only'));
});
test('fixture rejects wrong fields, missing bearer and unsupported routes', async () => {
  const f = fixture();
  assert.equal((await f.dispatch({ method: 'GET', url: '/events', body: null })).status, 401);
  assert.equal((await request(f, 'POST', '/lists/grocery/items', { label: 'Synthetic', scope: 'shared' })).status, 400);
  assert.equal((await request(f, 'POST', '/events', { date: today, title: 'Synthetic', who: 'self' })).status, 400);
  assert.equal((await request(f, 'PATCH', '/lists/items/1/done', {})).status, 400);
  assert.equal((await request(f, 'GET', '/events?from=2099-06-01&to=2099-06-30&scope=shared')).status, 400);
  assert.equal((await request(f, 'GET', '/elsewhere')).status, 404);
});
test('one-shot delay commits once before its eventual reply and never delays readback', async () => {
  const waits = [];
  const f = createFakeCosmo({ today, vocabulary, sleep: ms => new Promise(resolve => waits.push({ ms, resolve })) });
  assert.equal((await request(f, 'POST', '/__fixture/delay', { method: 'POST', path: '/events', ms: 6000 })).status, 200);
  const body = { date: today, time: null, title: 'Synthetic delayed', who: vocabulary.partnerWho };
  const pending = request(f, 'POST', '/events', body);
  await Promise.resolve(); assert.equal(waits[0].ms, 6000);
  assert.equal((await request(f, 'GET', `/events?from=${today}&to=${today}`)).body.items.length, 2);
  waits[0].resolve(); assert.equal((await pending).status, 201);
  assert.equal((await request(f, 'GET', `/events?from=${today}&to=${today}`)).body.items.length, 3);
  assert.equal((await request(f, 'POST', '/events', { ...body, title: 'Synthetic immediate' })).status, 201);
  assert.equal(waits.length, 1);
  assert.equal(f.calls.filter(x => x.method === 'POST' && x.path === '/events').length, 2);
});
test('fixture start is hard-pinned to loopback and closes its owned server', async t => {
  const f = await startFakeCosmo({ today, vocabulary });
  let closed = false;
  t.after(() => { if (!closed) return f.close(); });
  assert.equal(new URL(f.url).hostname, '127.0.0.1');
  const response = await fetch(`${f.url}/lists/tasks/items`, { headers: { authorization: 'Bearer synthetic-test-only' } });
  assert.equal(response.status, 200); assert.equal((await response.json()).items.length, 2);
  await f.close(); closed = true;
  await assert.rejects(fetch(`${f.url}/lists/tasks/items`), /fetch failed/);
});
