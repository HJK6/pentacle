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
  const runner = require('../scripts/lib/test-runner');
  const original = process.kill;
  const inspect = runner.groupMembers;
  const platform = Object.getOwnPropertyDescriptor(process, 'platform');
  const calls = [];
  process.kill = (pid, signal) => {
    calls.push([pid, signal]);
    if (pid === -424242) throw Object.assign(new Error('synthetic inspection denied'), { code: 'EPERM' });
    return original(pid, signal);
  };
  try {
    Object.defineProperty(process, 'platform', { ...platform, value: 'darwin' });
    for (const observation of [[{ pid: 424243, state: 'S' }], [{ pid: 424243, state: 'Z' }], new Error('scan unavailable'), Object.assign(new Error('scan timeout'), { code: 'ETIMEDOUT' })]) {
      runner.groupMembers = () => { if (observation instanceof Error) throw observation; return observation; };
      await assert.rejects(stopOwnedGroup({ pid: 424242, exitCode: 0, signalCode: null }), error => {
        assert.match(error.message, /cannot inspect.*synthetic inspection denied/);
        assert.equal(error.inspection[0].probe_error.code, 'EPERM');
        return true;
      });
    }
    assert.ok(calls.every(([pid, signal]) => pid === -424242 && signal === 0), 'no denied or foreign identity authorizes a signal');
  } finally { process.kill = original; runner.groupMembers = inspect; Object.defineProperty(process, 'platform', platform); }
});

test('owned Chrome EPERM retains a complete current empty-group witness without sending a signal', async () => {
  const { stopOwnedGroup } = require('./e2e/web_gate');
  const runner = require('../scripts/lib/test-runner');
  const original = process.kill, inspect = runner.groupMembers, calls = [];
  const platform = Object.getOwnPropertyDescriptor(process, 'platform');
  process.kill = (pid, signal) => { calls.push([pid, signal]); throw Object.assign(new Error('synthetic inspection denied'), { code: 'EPERM' }); };
  runner.groupMembers = pgid => { assert.equal(pgid, 424242); return []; };
  try {
    Object.defineProperty(process, 'platform', { ...platform, value: 'darwin' });
    const result = await stopOwnedGroup({ pid: 424242, exitCode: 0, signalCode: null });
    assert.equal(result.exited, true);
    assert.equal(result.inspection[0].probe_error.code, 'EPERM');
    assert.deepEqual(result.inspection[0].members, []);
    assert.ok(Date.parse(result.inspection[0].scan_finished_at));
    assert.deepEqual(calls, [[-424242, 0]]);
  } finally { process.kill = original; runner.groupMembers = inspect; Object.defineProperty(process, 'platform', platform); }
});

test('interrupt proof preserves its primary assertion and records secondary sentinel cleanup failure', async () => {
  const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
  const { EventEmitter } = require('node:events');
  const cases = [], secondary = [];
  const marker = { chromePid: 333333, daemonPid: 222222, userDataDir: '/synthetic/profile', scratch: '/synthetic/scratch', runtimeDir: '/synthetic/runtime' };
  let spawned = 0, primaryFailure = true;
  const fakeSpawn = () => {
    if (spawned++ % 2 === 0) return { pid: 424242 };
    const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
    child.exitCode = null; child.signalCode = null;
    process.nextTick(() => { child.stdout.emit('data', 'WEB_GATE_TEST_HOLD ' + JSON.stringify(marker) + '\nartifacts: /synthetic/report\n'); child.exitCode = primaryFailure ? 1 : 0; child.emit('exit', child.exitCode); });
    return child;
  };
  const modules = {
    'node:test': (name, options, body) => cases.push({ name, body }),
    'node:fs': { existsSync: () => true, mkdtempSync: () => '/synthetic/sentinel', rmSync() {}, readFileSync: () => JSON.stringify({ status: 'PASS', chrome_cleanup: { pid: marker.chromePid, exited: true } }) },
    'node:child_process': { spawn: fakeSpawn, execFileSync: () => '' },
    './web_gate': { resolveChrome: () => '/synthetic/chrome', freePort: async () => 1, stopOwnedGroup: async () => { throw new Error('secondary sentinel denied'); } },
    './lib/cdp': { connect: async () => ({ close() {}, send: async () => ({ product: 'Chrome/synthetic' }) }) },
    './lib/chrome_launch.cjs': { headlessChromeArgs: () => [] },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'e2e/web_gate_interrupt.test.js'), 'utf8'), {
    require: name => modules[name] || require(name), __dirname: path.join(__dirname, 'e2e'), setTimeout, clearTimeout,
    process: { execPath: '/synthetic/node', env: {}, kill: pid => { if (pid === marker.daemonPid) throw Object.assign(new Error('absent'), { code: 'ESRCH' }); return true; } },
    console: { log() {}, error: message => secondary.push(message) },
  });
  const selected = cases.find(row => row.name.includes('--keep success'));
  await assert.rejects(selected.body(), error => error.code === 'ERR_ASSERTION' && !error.message.includes('secondary sentinel denied'));
  assert.ok(secondary.some(message => message.includes('secondary sentinel denied')));
  primaryFailure = false;
  await assert.rejects(selected.body(), /secondary sentinel denied/);
  assert.ok(secondary.some(message => message.includes('"primary":null')));
});
