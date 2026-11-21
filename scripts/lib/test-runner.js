'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn, execFileSync } = require('node:child_process');
const { performance } = require('node:perf_hooks');

const DEFAULTS = Object.freeze({ fileTimeoutMs: 60000, runTimeoutMs: 900000, termGraceMs: 250, killGraceMs: 1000 });
const TAIL_BYTES = 65536;
const FORWARD_BYTES = 1048576;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function quarantineByFile(repoRoot, accepted) {
  const data = accepted || JSON.parse(fs.readFileSync(path.join(repoRoot, 'scripts/quarantine.json'), 'utf8'));
  const tests = data && data.nonblocking_tests;
  if (data?.version !== 1 || !Array.isArray(tests) || tests.some(t => typeof t !== 'string' || !t || !t.includes('::')) || new Set(tests).size !== tests.length) {
    throw new Error('invalid quarantine inventory');
  }
  const result = new Map();
  for (const id of tests) {
    const index = id.indexOf('::'), file = id.slice(0, index), name = id.slice(index + 2);
    if (!file.startsWith('test/') || !/\.(js|ts)$/.test(file)) continue;
    if (!name) throw new Error('invalid quarantined test identity');
    const history = data.history?.[crypto.createHash('sha256').update(id).digest('hex')];
    if (!history || !Array.isArray(history.parents) || history.parents.some(parent => typeof parent !== 'string' || !parent) ||
        (history.parents.length && !name.startsWith(history.parents.join(' ') + ' '))) throw new Error('invalid quarantined test ancestry');
    result.set(file, [...(result.get(file) || []), {name, parents: history.parents}]);
  }
  return result;
}

function stopOwnedTmux(home, env) {
  if (typeof process.getuid !== 'function') return;
  const socketDir = path.join(home, `tmux-${process.getuid()}`);
  if (!fs.existsSync(socketDir)) return;
  for (const name of fs.readdirSync(socketDir)) {
    const socket = path.join(socketDir, name);
    if (!fs.lstatSync(socket).isSocket()) throw new Error('unexpected private tmux entry');
    execFileSync('tmux', ['-S', socket, 'kill-server'], { env, stdio: 'ignore', timeout: 5000 });
  }
}

function admissionQuery(repoRoot, files, timeoutMs = 60000) {
  try {
    const output = execFileSync(process.env.PENTACLE_PYTHON || 'python3', [path.join(repoRoot, 'scripts/admission_query.py')], {
      input: JSON.stringify({root: repoRoot, files, platform: process.platform}), encoding: 'utf8', timeout: Math.max(1, Math.floor(timeoutMs)),
      maxBuffer: 8 * 1024 * 1024,
    });
    const value = JSON.parse(output);
    return value.complete === true && value.files && typeof value.files === 'object' && value.quarantine && typeof value.quarantine === 'object' ? value : {complete: false};
  } catch { return {complete: false}; }
}

function verifyAdvisory(repoRoot, junit, expected, timeoutMs) {
  try {
    const output = execFileSync(process.env.PENTACLE_PYTHON || 'python3', [path.join(repoRoot, 'scripts/admission_query.py')], {
      input: JSON.stringify({operation: 'verify_junit', path: junit, expected}), encoding: 'utf8', timeout: Math.max(1, Math.floor(timeoutMs)),
    });
    const result = JSON.parse(output);
    return result.complete === true && result.executed === true;
  } catch { return false; }
}

function verifyBlocking(repoRoot, junit, expected, timeoutMs) {
  try {
    const output = execFileSync(process.env.PENTACLE_PYTHON || 'python3', [path.join(repoRoot, 'scripts/admission_query.py')], {
      input: JSON.stringify({operation: 'verify_junit', path: junit, expected}), encoding: 'utf8', timeout: Math.max(1, Math.floor(timeoutMs)),
    });
    const result = JSON.parse(output);
    return result.complete === true && result.absent === true;
  } catch { return false; }
}

function sourceIdentity(repoRoot, timeoutMs) {
  try {
    timeoutMs = Math.max(1, Math.floor(timeoutMs));
    const sha = execFileSync('git', ['-C', repoRoot, 'rev-parse', 'HEAD'], {encoding: 'utf8', timeout: timeoutMs, stdio: ['ignore', 'pipe', 'ignore']}).trim();
    const dirty = !!execFileSync('git', ['-C', repoRoot, 'status', '--porcelain', '--untracked-files=all'], {encoding: 'utf8', timeout: timeoutMs, stdio: ['ignore', 'pipe', 'ignore']}).trim();
    return {sha, dirty};
  } catch { return null; }
}

function positive(value, name) {
  if (!/^[1-9][0-9]*$/.test(String(value)) || !Number.isSafeInteger(Number(value)) || Number(value) > 2147483647) {
    throw new Error(`${name} must be a positive integer no greater than 2147483647`);
  }
  return Number(value);
}

function parseArgs(argv, env = process.env) {
  const options = {
    fileTimeoutMs: positive(env.PENTACLE_TEST_FILE_TIMEOUT_MS ?? DEFAULTS.fileTimeoutMs, 'file timeout'),
    runTimeoutMs: positive(env.PENTACLE_TEST_RUN_TIMEOUT_MS ?? DEFAULTS.runTimeoutMs, 'run timeout'),
    summaryPath: env.PENTACLE_TEST_SUMMARY_PATH || null,
    patterns: [],
  };
  const fields = new Map([['--file-timeout-ms', 'fileTimeoutMs'], ['--run-timeout-ms', 'runTimeoutMs'], ['--summary-path', 'summaryPath']]);
  let literal = false;
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    if (arg === '--' && !literal) { literal = true; continue; }
    if (!literal && fields.has(arg.split('=')[0])) {
      const [flag, ...inline] = arg.split('=');
      const value = inline.length ? inline.join('=') : argv[++index];
      if (!value) throw new Error(`${flag} requires a value`);
      const field = fields.get(flag);
      options[field] = field === 'summaryPath' ? value : positive(value, flag);
    } else if (!literal && arg.startsWith('--')) throw new Error(`unknown runner flag: ${arg}`);
    else options.patterns.push(arg);
  }
  return options;
}

// Deliberately the original shallow, file-only, sorted discovery algorithm.
// In particular, duplicate patterns are not silently deduplicated.
function discover(repoRoot, patterns) {
  return patterns.flatMap(pattern => {
    if (!pattern.includes('*')) return fs.existsSync(path.join(repoRoot, pattern)) ? [pattern] : [];
    const directory = path.dirname(pattern);
    const [prefix, suffix] = path.basename(pattern).split('*');
    return fs.readdirSync(path.join(repoRoot, directory), { withFileTypes: true })
      .filter(entry => entry.isFile() && entry.name.startsWith(prefix) && entry.name.endsWith(suffix))
      .map(entry => path.join(directory, entry.name));
  }).sort();
}

function groupMembers(pgid) {
  if (process.platform === 'linux') {
    const members = [];
    for (const name of fs.readdirSync('/proc')) {
      if (!/^[0-9]+$/.test(name)) continue;
      let stat;
      try { stat = fs.readFileSync(`/proc/${name}/stat`, 'utf8'); }
      catch (error) { if (['ENOENT', 'ESRCH'].includes(error.code)) continue; throw error; }
      const fields = stat.slice(stat.lastIndexOf(')') + 2).trim().split(/\s+/);
      if (Number(fields[2]) === pgid) members.push({ pid: Number(name), state: fields[0] });
    }
    return members.sort((a, b) => a.pid - b.pid);
  }
  if (process.platform === 'darwin') {
    // Exact owned group filter. Bounded and injectable for unit tests; never a
    // process-name match or an SSH/daemon query.
    let output;
    try { output = execFileSync('ps', ['-A', '-o', 'pid=,pgid=,stat='], { encoding: 'utf8', timeout: 250, maxBuffer: 4 * 1024 * 1024 }); }
    catch (error) { throw new Error(`owned process inspection unavailable (${error.code || error.name})`); }
    const lines = output.trim().split('\n').filter(line => line.trim());
    if (!output.trim()) throw new Error('owned process inspection unavailable (incomplete ps output)');
    return lines.map(line => {
      const row = line.trim().split(/\s+/);
      if (row.length !== 3 || !row.slice(0, 2).every(value => /^\d+$/.test(value) && Number.isSafeInteger(Number(value)))
          || !/^[A-Z?][A-Za-z+<>?]*$/.test(row[2])) {
        throw new Error('owned process inspection unavailable (malformed ps row)');
      }
      return { pid: Number(row[0]), pgid: Number(row[1]), state: row[2][0] };
    }).filter(row => row.pgid === pgid).map(({ pid, state }) => ({ pid, state }));
  }
  throw new Error('requires: POSIX owned process-group inspection');
}

const SCAN_RECORD_LIMIT = 16;

async function cleanupGroup(pgid, deps, limits) {
  // scans: per-inspection elapsed/error receipt (diagnostic only; first and last rows kept when bounded).
  const result = { status: 'clean', pgid, survivor_pids: [], zombie_pids: [], signals: [], errors: [], scans: [], scans_dropped: 0 };
  if (!Number.isSafeInteger(pgid) || pgid <= 1 || pgid === process.pid) {
    return { ...result, status: 'incomplete', errors: ['invalid owned process group'] };
  }
  const record = row => {
    if (result.scans.length >= SCAN_RECORD_LIMIT) { result.scans.splice(SCAN_RECORD_LIMIT / 2, 1); result.scans_dropped += 1; }
    result.scans.push(row);
  };
  const inspect = () => {
    const start = deps.now();
    try {
      const members = deps.groupMembers(pgid);
      record({ elapsed_ms: Math.round((deps.now() - start) * 1000) / 1000, members: members.length, error: null });
      return members;
    } catch (error) {
      record({ elapsed_ms: Math.round((deps.now() - start) * 1000) / 1000, members: null, error: error.code || error.message });
      result.errors.push(`inspection: ${error.code || error.message}`);
      return null;
    }
  };
  const signal = name => {
    try { deps.signalGroup(pgid, name); result.signals.push(name); }
    catch (error) { if (error.code !== 'ESRCH') result.errors.push(`${name}: ${error.code || error.name}`); }
  };
  let members = inspect();
  // Inspection failure is not success; still signal only this owned group.
  if (members === null || members.some(row => row.state !== 'Z')) {
    signal('SIGTERM');
    const termEnd = deps.now() + limits.termGraceMs;
    while (deps.now() < termEnd) {
      await deps.sleep(Math.min(25, Math.max(1, termEnd - deps.now())));
      members = inspect();
      if (members && !members.some(row => row.state !== 'Z')) break;
    }
    if (members === null || members.some(row => row.state !== 'Z')) {
      signal('SIGKILL');
      const killEnd = deps.now() + limits.killGraceMs;
      while (deps.now() < killEnd) {
        await deps.sleep(Math.min(25, Math.max(1, killEnd - deps.now())));
        members = inspect();
        if (members && !members.some(row => row.state !== 'Z')) break;
      }
    }
  }
  members = inspect();
  if (members) {
    result.survivor_pids = members.filter(row => row.state !== 'Z').map(row => row.pid);
    result.zombie_pids = members.filter(row => row.state === 'Z').map(row => row.pid);
  }
  // Zombies cannot execute or hold descriptors and cannot be killed; report
  // them separately rather than misreport a dead PGID as a live survivor PID.
  if (members === null || result.survivor_pids.length || result.errors.length) result.status = 'incomplete';
  return result;
}

async function operation(command, args, { repoRoot, env, deadline, fileDeadline, label }, deps, limits) {
  const start = deps.now();
  if (deps.abortSignal?.aborted) return { aborted: true, timedOut: false, exitCode: null, signal: null, duration_ms: 0, tail: '', cleanup: { status: 'not_started', survivor_pids: [] } };
  const remaining = Math.min(deadline, fileDeadline) - start;
  if (remaining <= 0) return { timedOut: true, exitCode: null, signal: null, duration_ms: 0, tail: '', cleanup: { status: 'not_started', survivor_pids: [] } };
  let tail = Buffer.alloc(0), timeoutTail = null, forwarded = 0, child, timedOut = false, aborted = false, spawnError = null;
  const consume = (chunk, target) => {
    const data = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    tail = Buffer.concat([tail, data]).subarray(-TAIL_BYTES);
    if (forwarded < FORWARD_BYTES) target.write(data.subarray(0, FORWARD_BYTES - forwarded));
    forwarded += data.length;
  };
  let timer, onAbort;
  const outcome = await new Promise(resolve => {
    let settled = false;
    const settle = result => { if (settled) return; settled = true; clearTimeout(timer); if (onAbort) deps.abortSignal?.removeEventListener('abort', onAbort); resolve(result); };
    onAbort = () => { aborted = true; settle({ exitCode: null, signal: null }); };
    deps.abortSignal?.addEventListener('abort', onAbort, { once: true });
    try {
      child = deps.spawn(command, args, { cwd: repoRoot, env, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
      child.stdout.on('data', chunk => consume(chunk, deps.stdout));
      child.stderr.on('data', chunk => consume(chunk, deps.stderr));
      child.once('error', error => { spawnError = error.code || error.name; settle({ exitCode: null, signal: null }); });
      child.once('exit', (exitCode, signal) => {
        if (!timedOut && deps.now() >= Math.min(deadline, fileDeadline)) { timedOut = true; timeoutTail = Buffer.from(tail); }
        settle({ exitCode, signal });
      });
      const expire = () => {
        const left = Math.min(deadline, fileDeadline) - deps.now();
        if (left > 0) { timer = setTimeout(expire, Math.ceil(left)); return; }
        timedOut = true; timeoutTail = Buffer.from(tail); settle({ exitCode: null, signal: null });
      };
      timer = setTimeout(expire, Math.ceil(remaining));
      if (deps.abortSignal?.aborted) onAbort();
    } catch (error) { spawnError = error.code || error.name; settle({ exitCode: null, signal: null }); }
  });
  const cleanup = child?.pid ? await cleanupGroup(child.pid, deps, limits) : { status: spawnError ? 'not_started' : 'incomplete', survivor_pids: [] };
  // Leader exit does not imply pipe EOF: a descendant may have inherited it.
  child?.stdout?.destroy(); child?.stderr?.destroy(); child?.unref();
  const outputTail = (timeoutTail || tail).toString('utf8').split(/\r?\n/).slice(-20).join('\n');
  if (timedOut) deps.stderr.write(`\nTIMEOUT ${label}\n${outputTail}\n`);
  if (cleanup.status === 'incomplete') deps.stderr.write(`\nCLEANUP INCOMPLETE ${label} survivors=${JSON.stringify(cleanup.survivor_pids)} errors=${JSON.stringify(cleanup.errors)} scans=${JSON.stringify(cleanup.scans)}\n`);
  return { ...outcome, timedOut, aborted, spawnError, duration_ms: Math.round(deps.now() - start), tail: outputTail, output_truncated: forwarded > FORWARD_BYTES, cleanup };
}

async function run(repoRoot, options, injected = {}) {
  const deps = { spawn, groupMembers, admissionQuery, verifyAdvisory, verifyBlocking, sourceIdentity, makeTemporaryDirectory: prefix => fs.mkdtempSync(prefix), removeOwnedDirectory: directory => fs.rmSync(directory, { recursive: true, force: true }), signalGroup: (pgid, signal) => process.kill(-pgid, signal), now: () => performance.now(), sleep, stdout: process.stdout, stderr: process.stderr, ...injected };
  const controller = new AbortController();
  deps.abortSignal = deps.abortSignal || controller.signal;
  const interrupt = signal => controller.abort(signal);
  const onINT = () => interrupt('SIGINT'), onTERM = () => interrupt('SIGTERM');
  const limits = { ...DEFAULTS, ...options };
  const start = deps.now(), deadline = start + limits.runTimeoutMs;
  const entries = discover(repoRoot, options.patterns);
  const fixtureOnly = process.env.PENTACLE_RUNNER_FIXTURES === '1' && entries.length > 0 &&
    entries.every(file => file.startsWith('test/fixtures/') && !file.includes('..'));
  let admission = fixtureOnly ? {complete: true, files: Object.fromEntries(entries.map(file => [file, {state: 'blocking'}])), quarantine: {version: 1, nonblocking_tests: [], history: {}}} : deps.admissionQuery(repoRoot, entries, Math.min(60000, Math.max(1, deadline - deps.now())));
  let quarantinedTests = new Map(), quarantineError = !admission.complete;
  if (admission.complete) {
    try { quarantinedTests = quarantineByFile(repoRoot, admission.quarantine || {version: 1, nonblocking_tests: [], history: {}}); }
    catch { admission = {complete: false}; quarantineError = true; }
  }
  if (!entries.length) throw new Error('no tests matched');
  const summary = { version: 1, repo_root: path.resolve(repoRoot), source_before: deps.sourceIdentity(repoRoot, Math.min(1000, Math.max(1, deadline - deps.now()))), files: [], discovered: entries, file_timeout_ms: limits.fileTimeoutMs, run_timeout_ms: limits.runTimeoutMs, cleanup_allowance_ms: limits.termGraceMs + limits.killGraceMs, duration_ms: 0, exit_code: 1, runner_errors: [], directory_cleanup_errors: [] };
  if (!admission.complete) summary.runner_errors.push('admission_inventory_unavailable');
  if (quarantineError) summary.runner_errors.push('quarantine_inventory_unavailable');
  if (admission.complete && Array.isArray(admission.quarantine?.pending)) {
    for (const test of admission.quarantine.pending) deps.stderr.write(`QUARANTINE PENDING, NOT ON BASE: ${test}\n`);
  }
  const cacheRoot = path.join(repoRoot, 'node_modules', '.cache');
  let outDir, testHome, env;
  process.on('SIGINT', onINT); process.on('SIGTERM', onTERM);
  try {
    fs.mkdirSync(cacheRoot, { recursive: true });
    outDir = deps.makeTemporaryDirectory(path.join(cacheRoot, 'pentacle-tests-'));
    testHome = deps.makeTemporaryDirectory(path.join('/tmp', 'pentacle-test-home-'));
    const summaryParent = options.summaryPath && path.dirname(path.resolve(options.summaryPath));
    const junitDir = summaryParent && fs.existsSync(summaryParent) ? path.join(summaryParent, 'node-junit') : outDir;
    if (junitDir.startsWith(testHome + path.sep)) throw new Error('JUnit path must be outside owned home');
    fs.mkdirSync(junitDir, {recursive: true});
    env = { ...process.env, HOME: testHome, USERPROFILE: testHome, PENTACLE_RUNTIME_DIR: path.join(testHome, '.pentacle'), TMUX_TMPDIR: testHome };
    delete env.TMUX;
    for (let index = 0; index < entries.length; index++) {
      const file = entries[index], fileStart = deps.now();
      const row = { file, status: 'not_run', duration_ms: 0, cleanup: { status: 'not_started', survivor_pids: [] }, reason: null };
      summary.files.push(row);
      if (deps.abortSignal.aborted) { row.reason = 'runner_interrupted'; continue; }
      if (fileStart >= deadline) { row.reason = 'whole_run_ceiling'; continue; }
      if (process.platform === 'win32') { row.reason = 'requires: POSIX owned process-group cleanup'; continue; }
      const fileDeadline = fileStart + limits.fileTimeoutMs;
      let target = path.resolve(repoRoot, file);
      if (!target.startsWith(path.resolve(repoRoot) + path.sep) || !/\.(js|ts)$/.test(file)) { row.status = 'fail'; row.reason = 'unsupported_test_path'; continue; }
      const context = { repoRoot, env, deadline, fileDeadline, label: file };
      if (file.endsWith('.ts')) {
        const output = path.join(outDir, `${index}-${path.basename(file, '.ts')}.cjs`);
        const build = await operation(path.join(repoRoot, 'node_modules', '.bin', 'esbuild'), [target, '--bundle', '--platform=node', '--format=cjs', '--target=node18', '--external:node:*', '--external:jsdom', `--outfile=${output}`], context, deps, limits);
        row.build = build; row.cleanup = build.cleanup;
        if (build.aborted || build.timedOut || build.exitCode !== 0 || build.cleanup.status !== 'clean') {
          row.status = build.timedOut ? 'timeout' : 'build_failed'; row.reason = 'typescript_build'; row.duration_ms = Math.round(deps.now() - fileStart); continue;
        }
        target = output;
      }
      const key = crypto.createHash('sha256').update(file).digest('hex');
      const junit = path.join(junitDir, key + '.xml');
      try { fs.unlinkSync(junit); } catch (error) { if (error.code !== 'ENOENT') throw error; }
      const testArgs = ['--test', '--test-reporter=tap', '--test-reporter-destination=stdout',
                        '--test-reporter=junit', `--test-reporter-destination=${junit}`];
      row.junit = junit;
      const quarantined = quarantinedTests.get(path.relative(repoRoot, path.resolve(repoRoot, file)));
      const admissionRow = admission.complete ? admission.files[file] : null;
      if (admission.complete && (!admissionRow || !['blocking', 'advisory'].includes(admissionRow.state))) {
        row.status = 'fail'; row.reason = 'admission_result_missing'; continue;
      }
      if (admissionRow?.state === 'advisory') {
        deps.stderr.write(`ADMISSION ADVISORY ${file}: ${admissionRow.reason}\n`);
        const result = await operation(process.execPath, testArgs.concat(target), context, deps, limits);
        const complete = result.cleanup.status === 'clean' && !result.aborted && !result.timedOut &&
          !result.spawnError && (result.exitCode === 0 || result.exitCode === 1);
        Object.assign(row, {status: complete ? 'advisory' : 'fail', reason: complete ? 'unadmitted' : 'advisory_incomplete',
                            execution: result, cleanup: result.cleanup, duration_ms: Math.round(deps.now() - fileStart)});
        continue;
      }
      const escape = name => name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const quarantinePattern = quarantined && quarantined.map(item => escape(item.name)).join('|');
      if (quarantined) testArgs.push(`--test-skip-pattern=^(?:${quarantinePattern})$`);
      const result = await operation(process.execPath, [...testArgs, target], context, deps, limits);
      Object.assign(row, { status: result.timedOut ? 'timeout' : (!result.aborted && result.exitCode === 0 && result.cleanup.status === 'clean' ? 'pass' : 'fail'), execution: result, cleanup: result.cleanup, duration_ms: Math.round(deps.now() - fileStart) });
      if (quarantined) {
        const blockingVerifyMs = Math.floor(Math.min(deadline, fileDeadline) - deps.now());
        if (blockingVerifyMs <= 0 || !deps.verifyBlocking(repoRoot, junit, quarantined.map(item => item.name), blockingVerifyMs)) {
          row.status = 'fail'; row.reason = 'blocking_quarantine_incomplete';
        }
        for (const item of quarantined) deps.stderr.write(`ADVISORY ${file}::${item.name}\n`);
        const advisoryJunit = path.join(junitDir, key + '-advisory.xml');
        try { fs.unlinkSync(advisoryJunit); } catch (error) { if (error.code !== 'ENOENT') throw error; }
        const advisoryNames = new Set(quarantined.flatMap(item => [item.name, ...item.parents.map((_, i) => item.parents.slice(0, i + 1).join(' '))]));
        const advisoryPattern = [...advisoryNames].map(escape).join('|');
        row.advisory = await operation(process.execPath, ['--test', '--test-reporter=tap', '--test-reporter-destination=stdout',
          '--test-reporter=junit', `--test-reporter-destination=${advisoryJunit}`,
          `--test-name-pattern=^(?:${advisoryPattern})$`, target], context, deps, limits);
        row.advisory.junit = advisoryJunit;
        const verifyMs = Math.floor(Math.min(deadline, fileDeadline) - deps.now());
        if (row.advisory.cleanup.status !== 'clean') {
          row.status = 'fail'; row.reason = 'advisory_cleanup_incomplete';
          row.cleanup = row.advisory.cleanup;
        } else if (verifyMs <= 0 || !deps.verifyAdvisory(repoRoot, advisoryJunit, quarantined.map(item => item.name), verifyMs)) {
          row.status = 'fail'; row.reason = 'advisory_incomplete';
        }
      }
    }
  } catch (error) {
    summary.runner_errors.push(error.code || error.name);
    const current = summary.files.at(-1);
    if (current && current.status === 'not_run' && current.reason === null) { current.status = 'fail'; current.reason = 'runner_error'; }
    for (const file of entries.slice(summary.files.length)) summary.files.push({ file, status: 'not_run', duration_ms: 0, cleanup: { status: 'not_started', survivor_pids: [] }, reason: 'runner_error' });
  } finally {
    process.removeListener('SIGINT', onINT); process.removeListener('SIGTERM', onTERM);
    let tmuxClean = true;
    if (testHome && env) {
      try { stopOwnedTmux(testHome, env); }
      catch (error) { tmuxClean = false; summary.directory_cleanup_errors.push({ kind: 'tmux', error: error.code || error.name }); }
    }
    for (const [kind, directory] of [['test_home', testHome], ['bundle_output', outDir]]) {
      if (!directory) continue;
      if (kind === 'test_home' && !tmuxClean) continue;
      try { deps.removeOwnedDirectory(directory); }
      catch (error) { summary.directory_cleanup_errors.push({ kind, error: error.code || error.name }); }
    }
  }
  summary.duration_ms = Math.round(deps.now() - start);
  summary.generated_at = new Date().toISOString();
  summary.source_after = deps.sourceIdentity(repoRoot, Math.min(1000, Math.max(1, deadline - deps.now())));
  summary.exit_code = summary.files.every(row => ['pass', 'advisory'].includes(row.status)) && !summary.runner_errors.length && !summary.directory_cleanup_errors.length ? 0 : 1;
  deps.stderr.write('\nFILE\tSTATUS\tDURATION_MS\tCLEANUP\n');
  for (const row of summary.files) deps.stderr.write(`${row.file}\t${row.status}\t${row.duration_ms}\t${row.cleanup.status}${row.reason ? ` (${row.reason})` : ''}\n`);
  if (summary.runner_errors.length || summary.directory_cleanup_errors.length) deps.stderr.write(`RUNNER_ERRORS ${JSON.stringify({ runner: summary.runner_errors, cleanup: summary.directory_cleanup_errors })}\n`);
  if (options.summaryPath) {
    const destination = path.resolve(options.summaryPath);
    if (destination.startsWith(outDir + path.sep) || destination.startsWith(testHome + path.sep)) throw new Error('summary path must be outside owned cleanup directories');
    const temporary = destination + `.tmp-${process.pid}`;
    try { fs.writeFileSync(temporary, JSON.stringify(summary, null, 2) + '\n', { flag: 'wx' }); fs.renameSync(temporary, destination); }
    catch (error) { try { fs.unlinkSync(temporary); } catch {} throw new Error(`summary write failed (${error.code || error.name})`); }
  }
  return summary;
}

module.exports = { DEFAULTS, parseArgs, discover, groupMembers, cleanupGroup, operation, run, quarantineByFile, admissionQuery };
