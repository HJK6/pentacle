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
    for (const observation of [[{ pid: 424243, state: 'S' }], [{ pid: 424243, state: 'Z' }], [{ pid: 424243, state: '?' }], new Error('scan unavailable'), Object.assign(new Error('scan timeout'), { code: 'ETIMEDOUT' })]) {
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

test('owned Chrome stop requires child exit and group absence within each existing phase budget', async t => {
  const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
  const { EventEmitter } = require('node:events');
  const source = fs.readFileSync(path.join(__dirname, 'e2e/web_gate.js'), 'utf8');
  const seam = source.slice(source.indexOf('function onceExit('), source.indexOf('// server/index.js main()'));
  async function run(configure) {
    let now = 0, sequence = 0, settled = false, result, error;
    const timers = new Map(), trace = [];
    const child = Object.assign(new EventEmitter(), { pid: 424242, exitCode: null, signalCode: null });
    const later = (fn, ms, kind = 'counterpart') => { const id = ++sequence; timers.set(id, { at: now + ms, fn, kind }); return id; };
    const state = { child, later, trace, now: () => now,
      exit() { child.exitCode = 0; trace.push({ event: 'exit', at: now }); child.emit('exit', 0); } };
    const behavior = configure(state);
    class Clock extends Date {
      constructor(...args) { super(...(args.length ? args : [1700000000000 + now])); }
      static now() { return 1700000000000 + now; }
    }
    const api = vm.runInNewContext(seam + '\n({ stopOwnedGroup });', {
      Date: Clock, setTimeout: (fn, ms) => later(fn, ms, 'owned-wait'), clearTimeout: id => timers.delete(id),
      process: { platform: 'darwin', kill(pgid, signal) {
        assert.equal(pgid, -424242, 'only the exact owned group may be probed or signalled');
        trace.push({ event: 'kill', signal, at: now, childExited: child.exitCode !== null });
        return behavior.kill(signal);
      } },
      require(name) { assert.equal(name, '../../scripts/lib/test-runner'); return { groupMembers: () => behavior.members || [] }; },
    });
    const done = api.stopOwnedGroup(child, 100).then(value => { result = value; settled = true; }, value => { error = value; settled = true; });
    for (let step = 0; step < 100 && !settled; step++) {
      for (let micro = 0; micro < 8; micro++) await Promise.resolve();
      if (settled) break;
      const next = [...timers].sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
      assert.ok(next, 'the bounded owned-child operation must settle or have a scheduled event');
      now = next[1].at; timers.delete(next[0]); next[1].fn();
    }
    assert.equal(settled, true, 'the stop operation remains finite'); await done;
    assert.equal(child.listenerCount('exit'), 0, 'exit listener removed on success or failure');
    assert.equal([...timers.values()].filter(timer => timer.kind === 'owned-wait').length, 0, 'no child-wait or group-wait timer remains');
    return { result, error, trace, elapsed: now };
  }
  const absent = () => { throw Object.assign(new Error('synthetic absent'), { code: 'ESRCH' }); };
  await t.test('post-TERM verification waits for the direct child before an early denied probe', async () => {
    const out = await run(({ child, later, exit }) => ({ members: [{ pid: 424242, state: '?' }], kill(signal) {
      if (signal === 'SIGTERM') { later(exit, 20); return true; }
      if (child.exitCode !== null) return absent();
      if (signal === 0 && this.termSent) throw Object.assign(new Error('synthetic pre-exit denial'), { code: 'EPERM' });
      this.termSent = true; return true;
    } }));
    assert.equal(out.error, undefined); assert.equal(out.result.exited, true); assert.equal(out.elapsed, 20);
    const term = out.trace.findIndex(row => row.signal === 'SIGTERM');
    assert.ok(out.trace.slice(term + 1).filter(row => row.event === 'kill').every(row => row.childExited));
    assert.deepEqual(out.trace.filter(row => typeof row.signal === 'string').map(row => row.signal), ['SIGTERM']);
  });
  await t.test('an observed leader exit never overrides EPERM with a retained helper', async () => {
    const out = await run(({ child, later, exit }) => ({ members: [{ pid: 424243, state: '?' }], kill(signal) {
      if (signal === 'SIGTERM') { later(exit, 20); return true; }
      if (signal === 0 && child.exitCode !== null) throw Object.assign(new Error('synthetic post-exit denial'), { code: 'EPERM' });
      return true;
    } }));
    assert.match(out.error?.message || '', /synthetic post-exit denial/); assert.equal(out.result, undefined);
    assert.equal(out.error.inspection[0].members[0].state, '?');
    assert.ok(out.trace.some(row => row.signal === 0 && row.childExited));
    assert.deepEqual(out.trace.filter(row => typeof row.signal === 'string').map(row => row.signal), ['SIGTERM']);
  });
  await t.test('an absent group without observed child exit fails and is never signalled again', async () => {
    const out = await run(() => ({ kill: absent }));
    assert.match(out.error?.message || '', /child.*exit.*not observed/i);
    assert.equal(out.result, undefined); assert.equal(out.elapsed, 100);
    assert.ok(out.trace.every(row => row.signal === 0));
  });
  await t.test('child-exit timeout after TERM never escalates against a verified absent group', async () => {
    const out = await run(() => ({ termSent: false, kill(signal) {
      if (signal === 'SIGTERM') { this.termSent = true; return true; }
      if (signal === 0 && this.termSent) return absent();
      return true;
    } }));
    assert.match(out.error?.message || '', /child.*exit.*not observed/i); assert.equal(out.elapsed, 100);
    assert.deepEqual(out.trace.filter(row => typeof row.signal === 'string').map(row => row.signal), ['SIGTERM']);
  });
  await t.test('TERM-resistant owned child reaches KILL within the original TERM deadline', async () => {
    const out = await run(({ child, later, exit }) => ({ kill(signal) {
      if (signal === 'SIGKILL') later(exit, 20);
      if (signal === 0 && child.exitCode !== null) return absent();
      return true;
    } }));
    assert.equal(out.error, undefined); assert.equal(out.result.forced, true);
    assert.equal(out.trace.find(row => row.signal === 'SIGKILL').at, 100);
    assert.equal(out.elapsed, 120);
  });
  await t.test('leader exit still requires a separate whole-group absence observation', async () => {
    const out = await run(({ now, later, exit }) => ({ kill(signal) {
      if (signal === 'SIGTERM') later(exit, 20);
      if (signal === 0 && now() >= 60) return absent();
      return true;
    } }));
    assert.equal(out.error, undefined); assert.equal(out.result.exited, true);
    const exitedAt = out.trace.find(row => row.event === 'exit').at;
    assert.ok(out.trace.some(row => row.signal === 0 && row.childExited && row.at > exitedAt));
    assert.ok(out.elapsed >= 60 && out.elapsed <= 100);
  });
  await t.test('signal errors and a surviving child exhaust only the two existing budgets', async () => {
    const out = await run(() => ({ kill(signal) {
      if (signal !== 0) throw Object.assign(new Error('synthetic signal denied'), { code: 'EPERM' });
      return true;
    } }));
    assert.match(out.error?.message || '', /survived SIGKILL/); assert.equal(out.result, undefined);
    assert.equal(out.elapsed, 200);
    assert.deepEqual(out.trace.filter(row => typeof row.signal === 'string').map(row => [row.signal, row.at]), [['SIGTERM', 0], ['SIGKILL', 100]]);
  });
});
