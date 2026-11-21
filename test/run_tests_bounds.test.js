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
const blockingFixtures = (_root, files) => ({complete: true, files: Object.fromEntries(files.map(file => [file, {state: 'blocking'}]))});
const spawnClean = (command, args, config) => {
  const env = {...config.env};
  delete env.NODE_TEST_CONTEXT;
  return spawn(command, args, {...config, env});
};
const quarantine = (tests, ancestry = {}) => ({version: 1, nonblocking_tests: tests,
  history: Object.fromEntries(tests.map(id => [require('node:crypto').createHash('sha256').update(id).digest('hex'),
    {activation: 'test', file: require('node:crypto').createHash('sha256').update(id.split('::')[0]).digest('hex'), parents: ancestry[id] || []}]))});

test('quarantine inventory uses exact escaped names and rejects malformed data', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-quarantine-proof-'));
  try {
    fs.mkdirSync(path.join(dir, 'scripts'));
    const inventory = path.join(dir, 'scripts/quarantine.json');
    fs.writeFileSync(inventory, JSON.stringify(quarantine([
      'test/one.test.js::name with [literal] chars', 'tests/test_a.py::test_a',
    ])));
    const names = runner.quarantineByFile(dir).get('test/one.test.js');
    assert.deepEqual(names, [{name: 'name with [literal] chars', parents: [], activation: 'test'}]);
    fs.writeFileSync(inventory, '{invalid');
    assert.throws(() => runner.quarantineByFile(dir));
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('unreadable quarantine still runs blocking files and leaves the gate incomplete', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-quarantine-unreadable-'));
  try {
    fs.mkdirSync(path.join(dir, 'scripts'));
    fs.mkdirSync(path.join(dir, 'test'));
    fs.writeFileSync(path.join(dir, 'scripts/quarantine.json'), '{broken');
    fs.writeFileSync(path.join(dir, 'test/case.js'), "require('node:test')('blocking', () => console.log('BLOCKING EXECUTED'));\n");
    const spawnClean = (cmd, args, config) => { const env = {...config.env}; delete env.NODE_TEST_CONTEXT; return spawn(cmd, args, {...config, env}); };
    const result = await runner.run(dir, runner.parseArgs(['test/case.js']), {
      spawn: spawnClean, groupMembers: () => [], admissionQuery: () => ({complete: false}), stdout: quiet, stderr: quiet,
    });
    assert.equal(result.exit_code, 1);
    assert.match(result.files[0].execution.tail, /BLOCKING EXECUTED/);
    assert.ok(result.runner_errors.includes('quarantine_inventory_unavailable'));
  } finally { fs.rmSync(dir, {recursive: true, force: true}); }
});

test('candidate-only JavaScript quarantine stays blocking until base publication', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-quarantine-base-'));
  const git = (...args) => require('node:child_process').execFileSync('git', ['-C', dir, ...args], {stdio: 'ignore'});
  try {
    fs.mkdirSync(path.join(dir, 'scripts'));
    fs.mkdirSync(path.join(dir, 'test'));
    for (const name of ['admission_query.py', 'admission_fingerprint.py', 'admission_inputs.py', 'admission_policy.py'])
      fs.copyFileSync(path.join(root, 'scripts', name), path.join(dir, 'scripts', name));
    fs.writeFileSync(path.join(dir, 'test/cases.js'), "const t=require('node:test'); t('product failure', () => {throw Error('must block')});\n");
    const py = "import sys;sys.path.insert(0,sys.argv[1]+'/scripts');from admission_fingerprint import javascript_fingerprint;print(javascript_fingerprint(sys.argv[1],'test/cases.js'))";
    const fingerprint = require('node:child_process').execFileSync('python3', ['-c', py, dir], {encoding: 'utf8'}).trim();
    const key = require('node:crypto').createHash('sha256').update('javascript\0test/cases.js').digest('hex');
    fs.writeFileSync(path.join(dir, 'scripts/test-admission-inventory.json'), JSON.stringify({version: 1,
      base_sha: 'f3a76f479e14a390cd3d61234e1fd6e8a7d228d1', python: {}, javascript: {[key]: fingerprint}}));
    fs.writeFileSync(path.join(dir, 'scripts/test-admission-records.json'), JSON.stringify({version: 1, records: {}}));
    fs.writeFileSync(path.join(dir, 'scripts/quarantine.json'), JSON.stringify(quarantine([])));
    git('init', '-q'); git('config', 'user.name', 'fixture'); git('config', 'user.email', 'fixture@example.test');
    git('add', '.'); git('commit', '-qm', 'base'); git('update-ref', 'refs/remotes/origin/main', 'HEAD');
    fs.writeFileSync(path.join(dir, 'scripts/quarantine.json'), JSON.stringify(quarantine(['test/cases.js::product failure'])));
    const output = {text: '', write(s) { this.text += s; }};
    const spawnClean = (cmd, args, config) => { const env = {...config.env}; delete env.NODE_TEST_CONTEXT; return spawn(cmd, args, {...config, env}); };
    const result = await runner.run(dir, runner.parseArgs(['test/cases.js']), {spawn: spawnClean, groupMembers: () => [], stdout: quiet, stderr: output});
    assert.equal(result.exit_code, 1);
    assert.equal(result.files[0].status, 'fail');
    assert.match(result.files[0].execution.tail, /must block/);
    assert.match(output.text, /PENDING, NOT ON BASE/);
  } finally { fs.rmSync(dir, {recursive: true, force: true}); }
});

test('quarantined node case runs advisory while unrelated failure remains blocking', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-quarantine-run-'));
  try {
    fs.mkdirSync(path.join(dir, 'scripts'));
    fs.mkdirSync(path.join(dir, 'test'));
    for (const name of ['admission_query.py', 'admission_fingerprint.py', 'admission_inputs.py', 'admission_policy.py'])
      fs.copyFileSync(path.join(root, 'scripts', name), path.join(dir, 'scripts', name));
    fs.writeFileSync(path.join(dir, 'scripts/quarantine.json'), JSON.stringify(quarantine(['test/cases.js::intermittent [case]'])));
    const subject = path.join(dir, 'test/cases.js');
    const source = "const test = require('node:test'); const assert = require('node:assert/strict');\n" +
      "test('intermittent [case]', () => assert.fail('advisory failed'));\n";
    const options = runner.parseArgs(['test/cases.js']);
    const isolatedSpawn = (command, args, config) => {
      const env = {...config.env}; delete env.NODE_TEST_CONTEXT;
      return spawn(command, args, {...config, env});
    };
    const run = () => runner.run(dir, options, {spawn: isolatedSpawn, groupMembers: () => [],
      admissionQuery: () => ({complete: true, quarantine: quarantine(['test/cases.js::intermittent [case]']), files: {'test/cases.js': {state: 'blocking'}}}),
      stdout: quiet, stderr: quiet});
    fs.writeFileSync(subject, source + "test('product', () => assert.fail('product failed'));\n");
    let result = await run();
    assert.equal(result.exit_code, 1);
    assert.equal(result.files[0].advisory.exitCode, 1);
    assert.match(result.files[0].execution.tail, /cases\.js:3/);
    fs.writeFileSync(subject, source + "test('product', () => {});\n");
    result = await run();
    assert.equal(result.exit_code, 0);
    assert.equal(result.files[0].advisory.exitCode, 1);
    assert.match(result.files[0].advisory.tail, /cases\.js:2/);
    const held = await runner.run(dir, options, {spawn: isolatedSpawn, groupMembers: () => [],
      admissionQuery: () => ({complete: true, quarantine: quarantine(['test/cases.js::intermittent [case]']), files: {'test/cases.js': {state: 'advisory', reason: 'no review'}}}),
      stdout: quiet, stderr: quiet});
    assert.equal(held.exit_code, 0);
    assert.equal(held.files[0].status, 'advisory');
    assert.equal(held.files[0].execution.exitCode, 1);
    const unavailable = await runner.run(dir, options, {spawn: isolatedSpawn, groupMembers: () => [],
      admissionQuery: () => ({complete: false}), stdout: quiet, stderr: quiet});
    assert.equal(unavailable.exit_code, 1);
    assert.ok(unavailable.runner_errors.includes('admission_inventory_unavailable'));
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('nested quarantine runs the exact case advisory and keeps sibling blocking', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-quarantine-nested-'));
  try {
    fs.mkdirSync(path.join(dir, 'scripts'));
    fs.mkdirSync(path.join(dir, 'test'));
    for (const name of ['admission_query.py', 'admission_fingerprint.py', 'admission_inputs.py', 'admission_policy.py'])
      fs.copyFileSync(path.join(root, 'scripts', name), path.join(dir, 'scripts', name));
    const id = 'test/cases.js::parent child';
    fs.writeFileSync(path.join(dir, 'scripts/quarantine.json'), JSON.stringify(quarantine([id], {[id]: ['parent']})));
    fs.writeFileSync(path.join(dir, 'test/cases.js'), "const test=require('node:test'); const assert=require('node:assert/strict');\n" +
      "test('parent', async t => { await t.test('child', () => assert.fail('flaky')); await t.test('sibling', () => assert.fail('product')); });\n" +
      "test('other', async t => { await t.test('child', () => {}); });\n");
    const spawnClean = (cmd, args, config) => { const env = {...config.env}; delete env.NODE_TEST_CONTEXT; return spawn(cmd, args, {...config, env}); };
    const result = await runner.run(dir, runner.parseArgs(['--summary-path', path.join(dir, 'summary.json'), 'test/cases.js']), {spawn: spawnClean, groupMembers: () => [],
      admissionQuery: () => ({complete: true, quarantine: quarantine([id], {[id]: ['parent']}), files: {'test/cases.js': {state: 'blocking'}}}), stdout: quiet, stderr: quiet});
    assert.equal(result.exit_code, 1);
    assert.equal(result.files[0].execution.exitCode, 1);
    assert.equal(result.files[0].advisory.exitCode, 1);
    assert.notEqual(result.files[0].reason, 'advisory_incomplete');
    const receipt = spawnSync('python3', [path.join(dir, 'scripts/admission_query.py')], {encoding: 'utf8',
      input: JSON.stringify({operation: 'verify_junit', path: result.files[0].junit, expected: ['parent child']})});
    const blocking = JSON.parse(receipt.stdout);
    assert.equal(blocking.absent, true);
    assert.deepEqual(blocking.observed, ['other child', 'parent sibling']);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('the live JavaScript quarantine seed is unique and only it is removed from blocking JUnit', {
  skip: spawnSync('tmux', ['-V']).status !== 0 && 'tmux is not installed',
}, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-quarantine-seed-'));
  try {
    const file = 'test/web_server.test.js';
    const id = file + '::two concurrent connections keep their local tmux sessions isolated';
    // Keep this private socket live until the runner owns its teardown.
    // Otherwise tmux can leave a dead socket after the last web case exits.
    function spawnWithTmux(command, args, config) {
      const owned = spawnSync('tmux', ['has-session', '-t', '=quarantine-seed-owner'], {env: config.env});
      if (owned.status !== 0) {
        const started = spawnSync('tmux', ['new-session', '-d', '-s', 'quarantine-seed-owner'], {env: config.env});
        assert.equal(started.status, 0, String(started.stderr));
      }
      return spawnClean(command, args, config);
    }
    const result = await runner.run(root, runner.parseArgs(['--summary-path', path.join(dir, 'summary.json'), file]), {
      spawn: spawnWithTmux, stdout: quiet, stderr: quiet,
      admissionQuery: () => ({...blockingFixtures(root, [file]), quarantine: quarantine([id])}),
      quarantineAlert() { assert.fail('the live seed must have a unique selector'); },
    });
    assert.equal(result.exit_code, 0, JSON.stringify({reason: result.files[0].reason,
      runnerErrors: result.runner_errors, cleanupErrors: result.directory_cleanup_errors}));
    const row = result.files[0];
    const selected = id.split('::')[1];
    assert.match(fs.readFileSync(row.discovery.junit, 'utf8'), new RegExp(selected));
    assert.doesNotMatch(fs.readFileSync(row.junit, 'utf8'), new RegExp(selected));
    assert.match(fs.readFileSync(row.advisory.junit, 'utf8'), new RegExp(selected));
  } finally { fs.rmSync(dir, {recursive: true, force: true}); }
});

test('a top-level and nested name collision keeps both cases blocking', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-quarantine-collision-'));
  try {
    const file = 'test/cases.js';
    const id = file + '::collision';
    fs.mkdirSync(path.join(dir, 'scripts'));
    fs.mkdirSync(path.join(dir, 'test'));
    for (const name of ['admission_query.py', 'admission_fingerprint.py', 'admission_inputs.py', 'admission_policy.py'])
      fs.copyFileSync(path.join(root, 'scripts', name), path.join(dir, 'scripts', name));
    fs.writeFileSync(path.join(dir, file), `
      const test = require('node:test');
      test('collision', () => {});
      test('parent', async t => {
        await t.test('collision', () => { throw Error('collateral product failure'); });
        await t.test('sibling', () => {});
      });
    `);
    const alerts = [];
    const result = await runner.run(dir, runner.parseArgs(['--summary-path', path.join(dir, 'summary.json'), file]), {
      spawn: spawnClean, stdout: quiet, stderr: quiet,
      admissionQuery: () => ({...blockingFixtures(dir, [file]), quarantine: quarantine([id])}),
      quarantineAlert(testId, activation) { alerts.push([testId, activation]); return true; },
    });
    assert.deepEqual(alerts, [[id, 'test']]);
    assert.equal(result.exit_code, 1);
    const row = result.files[0];
    assert.equal(row.execution.exitCode, 1);
    assert.equal(row.advisory, undefined);
    const junitKey = require('node:crypto').createHash('sha256').update(file).digest('hex');
    assert.equal(path.basename(row.junit), junitKey + '.xml');
    const junit = fs.readFileSync(row.junit, 'utf8');
    assert.match(junit, /collateral product failure/);
    assert.equal((junit.match(/<testcase name="collision"/g) || []).length, 2);
  } finally { fs.rmSync(dir, {recursive: true, force: true}); }
});

test('blocking JUnit rejects a sibling that disappears or is skipped after discovery', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-quarantine-reconcile-'));
  try {
    const file = 'test/cases.js';
    const id = file + '::quarantined';
    fs.mkdirSync(path.join(dir, 'scripts'));
    fs.mkdirSync(path.join(dir, 'test'));
    for (const name of ['admission_query.py', 'admission_fingerprint.py', 'admission_inputs.py', 'admission_policy.py'])
      fs.copyFileSync(path.join(root, 'scripts', name), path.join(dir, 'scripts', name));
    for (const outcome of ['missing', 'skipped']) {
      fs.writeFileSync(path.join(dir, file), `
        const test = require('node:test');
        const filtered = process.execArgv.some(arg => arg.startsWith('--test-skip-pattern='));
        test('quarantined', () => {});
        if (!filtered || ${JSON.stringify(outcome)} === 'skipped')
          test('sibling', {skip: filtered}, () => {});
        test('always runs', () => {});
      `);
      const result = await runner.run(dir, runner.parseArgs([file]), {
        spawn: spawnClean, stdout: quiet, stderr: quiet,
        admissionQuery: () => ({...blockingFixtures(dir, [file]), quarantine: quarantine([id])}),
        quarantineAlert() { assert.fail('unfiltered discovery has unique cases'); },
      });
      assert.equal(result.files[0].execution.exitCode, 0, outcome);
      assert.equal(result.files[0].reason, 'blocking_quarantine_incomplete', outcome);
      assert.equal(result.exit_code, 1, outcome);
    }
  } finally { fs.rmSync(dir, {recursive: true, force: true}); }
});

test('Darwin group inspector rejects an incomplete row before claiming an empty group', () => {
  const vm = require('node:vm');
  const source = fs.readFileSync(path.join(root, 'scripts/lib/test-runner.js'), 'utf8');
  for (const output of ['', '1 1 Ss\ninvalid row\n', '1 not-a-pgid Ss\n', '1 1\n', '1 1 unknown\n', '9007199254740992 1 S\n']) {
    const loaded = { exports: {} };
    vm.runInNewContext(source, { module: loaded, process: { platform: 'darwin' },
      require: name => name === 'node:child_process' ? { execFileSync: () => output } : require(name) });
    assert.throws(() => loaded.exports.groupMembers(424242), /malformed|incomplete/);
  }
  const loaded = { exports: {} };
  vm.runInNewContext(source, { module: loaded, process: { platform: 'darwin' },
    require: name => name === 'node:child_process' ? { execFileSync: () => '1 1 Ss\n22 99 Z\n23 99 ?\n24 99 ?E\n' } : require(name) });
  assert.equal(loaded.exports.groupMembers(424242).length, 0, 'a foreign group is observation only');
  assert.equal(loaded.exports.groupMembers(99)[0].state, 'Z', 'zombies remain visible to the caller');
  assert.deepEqual(Array.from(loaded.exports.groupMembers(99), row => row.pid), [22, 23, 24], 'transitional states remain group members');
  const unavailable = { exports: {} };
  vm.runInNewContext(source, { module: unavailable, process: { platform: 'darwin' },
    require: name => name === 'node:child_process' ? { execFileSync: () => { throw Object.assign(new Error('scan timeout'), { code: 'ETIMEDOUT' }); } } : require(name) });
  assert.throws(() => unavailable.exports.groupMembers(424242), /inspection unavailable.*ETIMEDOUT/);
});

// Pass-expected fixtures get a generous per-file budget so a loaded host cannot turn them into false
// timeouts; cases that must time out pass a short budget explicitly (later flags win).
const SHORT = ['--file-timeout-ms', '1500'];
function invoke(files, flags = [], extraEnv = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-runner-proof-'));
  const summaryPath = path.join(dir, 'summary.json');
  const env = { ...process.env, ...extraEnv, PENTACLE_RUNNER_FIXTURES: '1' }; delete env.NODE_TEST_CONTEXT;
  try {
    const result = spawnSync(process.execPath, [path.join(root, 'scripts/run-tests.js'), '--file-timeout-ms', '5000', '--run-timeout-ms', '20000', '--summary-path', summaryPath, ...flags, ...files], { cwd: root, env, encoding: 'utf8', timeout: 30000, maxBuffer: 4 * 1024 * 1024 });
    return { ...result, summary: fs.existsSync(summaryPath) ? JSON.parse(fs.readFileSync(summaryPath, 'utf8')) : null, output: (result.stdout || '') + (result.stderr || '') };
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
}

test('pinned141-file discovery and the legacy algorithm remain equivalent', () => {
  const expected = JSON.parse(fs.readFileSync(path.join(root, fixture('base-discovered.json')), 'utf8'));
  const patterns = ['test/*.test.js', 'test/*.test.ts'];
  const synthetic = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-discovery-proof-'));
  try {
    for (const name of expected) { const target = path.join(synthetic, name); fs.mkdirSync(path.dirname(target), { recursive: true }); fs.writeFileSync(target, '// synthetic discovery entry'); }
    fs.mkdirSync(path.join(synthetic, 'test/directory.test.js'));
    assert.equal(expected.length, 141);
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
  const healthy = invoke([fixture('pass.js')]);
  assert.equal(healthy.status, 0, JSON.stringify(healthy.summary));
  const result = invoke([fixture('fail.js'), fixture('pass.js')]);
  assert.equal(result.status, 1);
  assert.deepEqual(result.summary.files.map(row => row.status), ['fail', 'pass'], JSON.stringify(result.summary));
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

test('a timed-out scan leaves its elapsed time and error in the cleanup receipt and stays incomplete', async () => {
  let now = 0, calls = 0;
  const groupMembers = () => {
    calls += 1;
    if (calls === 1) { now += 300; throw new Error('owned process inspection unavailable (ETIMEDOUT)'); }
    now += 10; return [];
  };
  const result = await runner.cleanupGroup(90001, { now: () => now, sleep: async ms => { now += ms; }, groupMembers, signalGroup() {} }, { termGraceMs: 10, killGraceMs: 10 });
  assert.equal(result.status, 'incomplete');
  assert.deepEqual(result.scans[0], { elapsed_ms: 300, members: null, error: 'owned process inspection unavailable (ETIMEDOUT)' });
  assert.deepEqual(result.scans.at(-1), { elapsed_ms: 10, members: 0, error: null });
  assert.equal(result.scans_dropped, 0);
});

test('scan receipts are bounded and keep the first and latest scans', async () => {
  let now = 0, calls = 0;
  const groupMembers = () => { calls += 1; now += calls; return [{ pid: 90002, state: 'S' }]; };
  const stderr = { text: '', write(chunk) { this.text += chunk; } };
  const result = await runner.cleanupGroup(90001, { now: () => now, sleep: async ms => { now += ms; }, groupMembers, signalGroup() {} }, { termGraceMs: 500, killGraceMs: 500 });
  assert.equal(result.status, 'incomplete');
  assert.equal(result.scans.length, 16);
  assert.equal(result.scans_dropped, calls - 16);
  assert.equal(result.scans[0].elapsed_ms, 1);
  assert.equal(result.scans.at(-1).elapsed_ms, calls);
  const fakeSpawn = () => {
    const child = new EventEmitter(); child.pid = 90001;
    child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
    child.stdout.destroy = child.stderr.destroy = child.unref = () => {};
    process.nextTick(() => child.emit('exit', 0, null));
    return child;
  };
  await runner.run(root, { patterns: [fixture('pass.js')], fileTimeoutMs: 5000, runTimeoutMs: 10000, termGraceMs: 10, killGraceMs: 10 }, {
    spawn: fakeSpawn, groupMembers: () => { throw new Error('owned process inspection unavailable (ETIMEDOUT)'); }, signalGroup() {},
    now: () => now, sleep: async ms => { now += ms; }, stdout: quiet, stderr,
  });
  assert.match(stderr.text, /CLEANUP INCOMPLETE .* scans=\[\{"elapsed_ms":0,"members":null,"error":"owned process inspection unavailable \(ETIMEDOUT\)"\}/);
});

test('whole-run deadline also bounds bundling and preserves later not_run files', async () => {
  const child = new EventEmitter(); child.pid = 90001; child.stdout = new EventEmitter(); child.stderr = new EventEmitter(); child.stdout.destroy = child.stderr.destroy = child.unref = () => {};
  const result = await runner.run(root, { patterns: [fixture('build_failed.ts'), fixture('pass.js')], fileTimeoutMs: 5000, runTimeoutMs: 40 }, { spawn: () => child, groupMembers: () => [], admissionQuery: blockingFixtures, sourceIdentity: () => null, signalGroup() {}, stdout: quiet, stderr: quiet });
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
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-missing-parent-'));
  try {
    const result = invoke([fixture('pass.js')], ['--summary-path', path.join(dir, 'absent', 'summary.json')]);
    assert.equal(result.status, 1);
    assert.match(result.output, /summary write failed/);
  } finally { fs.rmSync(dir, {recursive: true, force: true}); }
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
    spawn: fakeSpawn, groupMembers: () => [], admissionQuery: blockingFixtures, signalGroup() {}, abortSignal: controller.signal, stdout: quiet, stderr: quiet,
  });
  assert.equal(result.exit_code, 1);
  assert.deepEqual(result.files.map(row => row.status), ['fail', 'not_run']);
  assert.equal(result.files[1].reason, 'runner_interrupted');
});

test('interrupted unadmitted execution makes the gate incomplete', async () => {
  const controller = new AbortController();
  const fakeSpawn = () => {
    const child = new EventEmitter(); child.pid = 90001;
    child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
    child.stdout.destroy = child.stderr.destroy = child.unref = () => {};
    process.nextTick(() => controller.abort());
    return child;
  };
  const result = await runner.run(root, {patterns: [fixture('hang.js')], fileTimeoutMs: 5000, runTimeoutMs: 10000}, {
    spawn: fakeSpawn, groupMembers: () => [], signalGroup() {}, abortSignal: controller.signal,
    admissionQuery: (_root, files) => ({complete: true, files: Object.fromEntries(files.map(file => [file, {state: 'advisory'}]))}),
    stdout: quiet, stderr: quiet,
  });
  assert.equal(result.exit_code, 1);
  assert.equal(result.files[0].reason, 'advisory_incomplete');
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
      stdout: quiet, stderr: quiet, admissionQuery: blockingFixtures,
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
    admissionQuery: () => ({complete: true, files: {[fixture('pass.js')]: {state: 'blocking'}}}),
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
