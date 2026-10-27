'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { parseArgs } = require('./e2e/web_gate');

test('web gate keeps the full default and rejects unknown named scopes before startup', () => {
  assert.deepEqual(parseArgs([]).scenarios, []);
  assert.deepEqual(parseArgs(['--scenario', 'installed-client-seam']).scenarios, ['installed-client-seam']);
  assert.throws(() => parseArgs(['--scenario', 'installed-client-sema']), /unknown scenario/);
  assert.throws(() => parseArgs(['--scenario']), /unknown scenario/);
});
