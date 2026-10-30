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


test('owned Chrome inspection failure cannot be reported as successful cleanup', async () => {
  const { stopOwnedGroup } = require('./e2e/web_gate');
  const original = process.kill;
  process.kill = (pid, signal) => {
    if (pid === -424242) throw Object.assign(new Error('synthetic inspection denied'), { code: 'EPERM' });
    return original(pid, signal);
  };
  try {
    await assert.rejects(stopOwnedGroup({ pid: 424242, exitCode: 0, signalCode: null }), /cannot inspect.*synthetic inspection denied/);
  } finally { process.kill = original; }
});
