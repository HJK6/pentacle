'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync, spawn } = require('node:child_process');
const { EventEmitter } = require('node:events');
const runner = require('../scripts/lib/test-runner');
const root = path.resolve(__dirname, '..');
const fixture = name => `test/fixtures/root_runner/${name}`;
const quiet = { write() {} };

// Pass-expected fixtures get a generous per-file budget so a loaded host cannot turn them into false
// timeouts; cases that must time out pass a short budget explicitly (later flags win).
const SHORT = ['--file-timeout-ms', '1500'];
function invoke(files, flags = [], extraEnv = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-runner-proof-'));
  const summaryPath = path.join(dir, 'summary.json');
  const env = { ...process.env, ...extraEnv }; delete env.NODE_TEST_CONTEXT;
  try {
    const result = spawnSync(process.execPath, [path.join(root, 'scripts/run-tests.js'), '--file-timeout-ms', '5000', '--run-timeout-ms', '20000', '--summary-path', summaryPath, ...flags, ...files], { cwd: root, env, encoding: 'utf8', timeout: 30000, maxBuffer: 4 * 1024 * 1024 });
    return { ...result, summary: fs.existsSync(summaryPath) ? JSON.parse(fs.readFileSync(summaryPath, 'utf8')) : null, output: (result.stdout || '') + (result.stderr || '') };
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
}

test('pinned144-file discovery and the legacy algorithm remain equivalent', () => {
  const expected = JSON.parse(fs.readFileSync(path.join(root, fixture('base-discovered.json')), 'utf8'));
  const patterns = ['test/*.test.js', 'test/*.test.ts', 'test/e2e/terminal_interaction_runtime/*.test.js'];
  const synthetic = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-discovery-proof-'));
  try {
    for (const name of expected) { const target = path.join(synthetic, name); fs.mkdirSync(path.dirname(target), { recursive: true }); fs.writeFileSync(target, '// synthetic discovery entry'); }
    fs.mkdirSync(path.join(synthetic, 'test/directory.test.js'));
    assert.equal(expected.length, 144);
    assert.deepEqual(runner.discover(synthetic, patterns), expected);
  } finally { fs.rmSync(synthetic, { recursive: true, force: true }); }
  // Compare on today's tree too, without freezing future additions out of it.
  const legacy = patterns.flatMap(pattern => {
    const directory = path.dirname(pattern), [prefix, suffix] = path.basename(pattern).split('*');
    return fs.readdirSync(path.join(root, directory), { withFileTypes: true })
      .filter(entry => entry.isFile() && entry.name.startsWith(prefix) && entry.name.endsWith(suffix))
      .map(entry => path.join(directory, entry.name));
  }).sort();
  assert.deepEqual(runner.discover(root, patterns), legacy);
  assert.deepEqual(runner.discover(root, [fixture('pass.js'), fixture('pass.js')]), [fixture('pass.js'), fixture('pass.js')]);
});

test('hang is named, bounded, retains tail and does not block a subsequent pass', () => {
  const result = invoke([fixture('hang.js'), fixture('pass.js')], ['--file-timeout-ms', '3000']);
  assert.equal(result.status, 1, result.output);
  assert.deepEqual(result.summary.files.map(row => row.status), ['timeout', 'pass']);
  assert.equal(result.summary.files[0].file, fixture('hang.js'));
  assert.match(result.summary.files[0].execution.tail, /SYNTHETIC_HANG_STARTED/);
  assert.match(result.stderr, /TIMEOUT test\/fixtures\/root_runner\/hang.js/);
  assert.ok(result.summary.duration_ms < 8000);
});

test('ordinary pass and assertion failure retain exit-code semantics', () => {
  assert.equal(invoke([fixture('pass.js')]).status, 0);
  const result = invoke([fixture('fail.js'), fixture('pass.js')]);
  assert.equal(result.status, 1);
  assert.deepEqual(result.summary.files.map(row => row.status), ['fail', 'pass']);
});

test('TypeScript syntax failure remains build_failed, never skipped or hidden', () => {
  const result = invoke([fixture('build_failed.ts'), fixture('pass.js')]);
  assert.equal(result.status, 1);
  assert.deepEqual(result.summary.files.map(row => row.status), ['build_failed', 'pass']);
  assert.equal(result.summary.files[0].reason, 'typescript_build');
});

test('absolute ceiling lists every remaining source as not_run and fails', () => {
  const result = invoke([fixture('hang.js'), fixture('pass.js'), fixture('term_zero.js')], ['--run-timeout-ms', '250', '--file-timeout-ms', '5000']);
  assert.equal(result.status, 1);
  assert.deepEqual(result.summary.files.map(row => row.status), ['timeout', 'not_run', 'not_run']);
  assert.equal(result.summary.files[1].reason, 'whole_run_ceiling');
});

test('a timeout cannot turn green when its child handles TERM with exit zero', () => {
  const result = invoke([fixture('term_zero.js')], SHORT);
  assert.equal(result.status, 1);
  assert.equal(result.summary.files[0].status, 'timeout');
});

test('owned descendant cleanup never kills an unrelated sibling', async t => {
  const sibling = spawn(process.execPath, ['-e', 'setInterval(()=>{},1000)'], { stdio: 'ignore' });
  t.after(() => sibling.kill('SIGTERM'));
  const result = invoke([fixture('child_left.js')], SHORT);
  assert.equal(result.status, 1, result.output);
  assert.equal(result.summary.files[0].status, 'timeout');
  const child = Number(result.output.match(/SYNTHETIC_CHILD_PID=(\d+)/)?.[1]);
  assert.ok(child > 1);
  assert.doesNotThrow(() => process.kill(sibling.pid, 0));
  const cleanup = result.summary.files[0].cleanup;
  assert.ok(!cleanup.survivor_pids.includes(sibling.pid));
  const remaining = runner.groupMembers(cleanup.pgid).filter(row => row.state !== 'Z');
  if (remaining.length) {
    assert.equal(cleanup.status, 'incomplete');
    assert.deepEqual(cleanup.survivor_pids, remaining.map(row => row.pid));
  } else assert.equal(cleanup.status, 'clean');
});

test('incomplete cleanup reports real member PIDs and only targets its owned group', async () => {
  let now = 0; const signals = [];
  const result = await runner.cleanupGroup(90001, { now: () => now, sleep: async ms => { now += ms; }, groupMembers: () => [{ pid: 90002, state: 'S' }], signalGroup: (pgid, signal) => signals.push([pgid, signal]) }, { termGraceMs: 10, killGraceMs: 10 });
  assert.equal(result.status, 'incomplete');
  assert.deepEqual(result.survivor_pids, [90002]);
  assert.deepEqual(signals, [[90001, 'SIGTERM'], [90001, 'SIGKILL']]);
});

test('denied inspection is an incomplete result, not clean success', async () => {
  let now = 0;
  const result = await runner.cleanupGroup(90001, { now: () => now, sleep: async ms => { now += ms; }, groupMembers: () => { throw Object.assign(new Error('denied'), { code: 'EACCES' }); }, signalGroup() {} }, { termGraceMs: 10, killGraceMs: 10 });
  assert.equal(result.status, 'incomplete');
  assert.ok(result.errors.some(error => error.includes('EACCES')));
});

test('whole-run deadline also bounds bundling and preserves later not_run files', async () => {
  const child = new EventEmitter(); child.pid = 90001; child.stdout = new EventEmitter(); child.stderr = new EventEmitter(); child.stdout.destroy = child.stderr.destroy = child.unref = () => {};
  const result = await runner.run(root, { patterns: [fixture('build_failed.ts'), fixture('pass.js')], fileTimeoutMs: 5000, runTimeoutMs: 40 }, { spawn: () => child, groupMembers: () => [], signalGroup() {}, stdout: quiet, stderr: quiet });
  assert.equal(result.exit_code, 1);
  assert.deepEqual(result.files.map(row => row.status), ['timeout', 'not_run']);
  assert.equal(result.files[0].reason, 'typescript_build');
});

test('CLI flags override environment; invalid bounds never disable a gate', () => {
  assert.equal(runner.parseArgs(['--file-timeout-ms', '75'], { PENTACLE_TEST_FILE_TIMEOUT_MS: '50' }).fileTimeoutMs, 75);
  for (const value of ['0', '-1', 'NaN', 'Infinity', '1.5', '2147483648']) assert.throws(() => runner.parseArgs(['--run-timeout-ms', value]), /positive integer/);
  assert.throws(() => runner.parseArgs(['--summary-path']), /requires a value/);
});

test('summary-write failure is nonzero despite a passing test', () => {
  const result = invoke([fixture('pass.js')], ['--summary-path', path.join(os.tmpdir(), 'pentacle-no-such-parent', 'summary.json')]);
  assert.equal(result.status, 1);
  assert.match(result.output, /summary write failed/);
});

test('microphone fixture owns its WebSocket implementation even without a global', () => {
  const preload = path.join(root, fixture('remove_global_websocket.cjs'));
  const result = invoke(['test/web_mic_adapter.test.js'], ['--file-timeout-ms', '5000'], { NODE_OPTIONS: `--require=${JSON.stringify(preload)}` });
  assert.equal(result.status, 0, result.output);
  assert.equal(result.summary.files[0].status, 'pass');
});

test('a reported cleanup survivor makes the whole run fail despite exit zero', async () => {
  let now = 0;
  const fakeSpawn = () => {
    const child = new EventEmitter(); child.pid = 90001;
    child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
    child.stdout.destroy = child.stderr.destroy = child.unref = () => {};
    process.nextTick(() => child.emit('exit', 0, null));
    return child;
  };
  const result = await runner.run(root, { patterns: [fixture('pass.js')], fileTimeoutMs: 5000, runTimeoutMs: 10000, termGraceMs: 10, killGraceMs: 10 }, {
    spawn: fakeSpawn, groupMembers: () => [{ pid: 90002, state: 'S' }], signalGroup() {},
    now: () => now, sleep: async ms => { now += ms; }, stdout: quiet, stderr: quiet,
  });
  assert.equal(result.exit_code, 1);
  assert.equal(result.files[0].status, 'fail');
  assert.deepEqual(result.files[0].cleanup.survivor_pids, [90002]);
});

test('interruption stops scheduling and cleans the active owned group', async () => {
  const controller = new AbortController();
  const fakeSpawn = () => {
    const child = new EventEmitter(); child.pid = 90001;
    child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
    child.stdout.destroy = child.stderr.destroy = child.unref = () => {};
    process.nextTick(() => controller.abort());
    return child;
  };
  const result = await runner.run(root, { patterns: [fixture('hang.js'), fixture('pass.js')], fileTimeoutMs: 5000, runTimeoutMs: 10000 }, {
    spawn: fakeSpawn, groupMembers: () => [], signalGroup() {}, abortSignal: controller.signal, stdout: quiet, stderr: quiet,
  });
  assert.equal(result.exit_code, 1);
  assert.deepEqual(result.files.map(row => row.status), ['fail', 'not_run']);
  assert.equal(result.files[1].reason, 'runner_interrupted');
});

test('late exit delivered before the timer callback still counts as timeout', async () => {
  let now = 0;
  const spawnLate = () => {
    const child = new EventEmitter(); child.pid = 90001; child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
    child.stdout.destroy = child.stderr.destroy = child.unref = () => {};
    process.nextTick(() => { now = 20; child.emit('exit', 0, null); });
    return child;
  };
  const result = await runner.operation('synthetic', [], { repoRoot: root, env: {}, deadline: 10, fileDeadline: 10, label: 'late.js' }, {
    spawn: spawnLate, now: () => now, groupMembers: () => [], signalGroup() {}, sleep: async () => {}, stdout: quiet, stderr: quiet,
  }, runner.DEFAULTS);
  assert.equal(result.timedOut, true);
  assert.equal(result.exitCode, 0);
});

test('abort during bundle cleanup prevents the execution spawn', async () => {
  const controller = new AbortController(); let spawns = 0;
  const fakeSpawn = () => {
    spawns++;
    const child = new EventEmitter(); child.pid = 90001; child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
    child.stdout.destroy = child.stderr.destroy = child.unref = () => {};
    process.nextTick(() => child.emit('exit', 0, null)); return child;
  };
  const result = await runner.run(root, { patterns: [fixture('build_failed.ts'), fixture('pass.js')], fileTimeoutMs: 5000, runTimeoutMs: 10000 }, {
    spawn: fakeSpawn, groupMembers: () => { controller.abort(); return []; }, signalGroup() {}, abortSignal: controller.signal, stdout: quiet, stderr: quiet,
  });
  assert.equal(spawns, 1);
  assert.equal(result.exit_code, 1);
  assert.equal(result.files[1].status, 'not_run');
});

test('owned-directory cleanup failure preserves JSON attribution and fails', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-cleanup-proof-'));
  const summaryPath = path.join(dir, 'summary.json');
  try {
    const result = await runner.run(root, { patterns: [fixture('pass.js')], fileTimeoutMs: 5000, runTimeoutMs: 10000, summaryPath }, {
      stdout: quiet, stderr: quiet,
      removeOwnedDirectory(directory) { fs.rmSync(directory, { recursive: true, force: true }); throw Object.assign(new Error('synthetic cleanup failure'), { code: 'EACCES' }); },
    });
    assert.equal(result.exit_code, 1);
    const recorded = JSON.parse(fs.readFileSync(summaryPath, 'utf8'));
    assert.equal(recorded.files[0].status, 'pass');
    assert.equal(recorded.directory_cleanup_errors.length, 2);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('partial temporary-directory setup is cleaned and every file is attributed', async () => {
  let first, calls = 0;
  const result = await runner.run(root, { patterns: [fixture('pass.js')], fileTimeoutMs: 5000, runTimeoutMs: 10000 }, {
    stdout: quiet, stderr: quiet,
    makeTemporaryDirectory(prefix) { if (++calls === 2) throw Object.assign(new Error('synthetic setup failure'), { code: 'EACCES' }); first = fs.mkdtempSync(prefix); return first; },
  });
  assert.equal(result.exit_code, 1);
  assert.equal(result.files[0].status, 'not_run');
  assert.equal(fs.existsSync(first), false);
  assert.deepEqual(result.runner_errors, ['EACCES']);
});

test('large output keeps a bounded final tail and bounded forwarded bytes', () => {
  const result = invoke([fixture('large_output.js')], SHORT);
  assert.equal(result.status, 1);
  const execution = result.summary.files[0].execution;
  assert.equal(execution.output_truncated, true);
  assert.ok(Buffer.byteLength(execution.tail) <= 65536);
  assert.match(execution.tail, /SYNTHETIC_FINAL_TAIL_MARKER/);
  assert.ok(Buffer.byteLength(result.stdout) <= 1048576);
});

test('runner fixture manifest pins every synthetic fixture byte', () => {
  const crypto = require('node:crypto');
  const folder = path.join(root, 'test/fixtures/root_runner');
  const manifest = JSON.parse(fs.readFileSync(path.join(folder, 'MANIFEST.json'), 'utf8'));
  assert.deepEqual(manifest.files.map(row => path.basename(row.path)).sort(), fs.readdirSync(folder).filter(name => name !== 'MANIFEST.json').sort());
  for (const row of manifest.files) {
    const bytes = fs.readFileSync(path.join(root, row.path));
    assert.equal(row.synthetic, true);
    assert.equal(row.contains_real_traffic, false);
    assert.equal(bytes.length, row.bytes);
    assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'), row.sha256);
  }
});
