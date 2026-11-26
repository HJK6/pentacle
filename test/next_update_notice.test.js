'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { createUpdateCheck } = require('../renderer/next/update_notice');

test('a newer version is announced only when both builds are known and differ', async () => {
  let served = { buildId: 'one' };
  const changes = [];
  const updates = createUpdateCheck({ ownId: 'one', getBuild: async () => served, onChange: value => changes.push(value) });
  assert.equal(await updates.check(), false, 'a page on the served build has nothing to load');
  served = null;
  assert.equal(await updates.check(), false, 'an unknown served build is not an update');
  served = { buildId: 'two' };
  assert.equal(await updates.check(), true);
  assert.equal(await updates.check(), true);
  assert.deepEqual(changes, [true], 'the same answer is announced once');
  served = { buildId: 'one' };
  assert.equal(await updates.check(), false, 'a host rolled back to this page\'s build withdraws it');
  assert.deepEqual(changes, [true, false]);
});

test('a failed check keeps the last answer, and a page with no build of its own never asks', async () => {
  let fail = false, asked = 0;
  const updates = createUpdateCheck({ ownId: 'one', onChange() {},
    getBuild: async () => { asked++; if (fail) throw new Error('offline'); return { buildId: 'two' }; } });
  assert.equal(await updates.check(), true);
  fail = true;
  assert.equal(await updates.check(), true);
  assert.equal(updates.available(), true);
  const unknown = createUpdateCheck({ ownId: '', getBuild: async () => { asked = -1; return { buildId: 'two' }; }, onChange() {} });
  assert.equal(await unknown.check(), false);
  assert.equal(asked, 2);
});
