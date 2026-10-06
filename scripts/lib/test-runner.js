'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn, execFileSync } = require('node:child_process');
const { performance } = require('node:perf_hooks');

const DEFAULTS = Object.freeze({ fileTimeoutMs: 60000, runTimeoutMs: 900000, termGraceMs: 250, killGraceMs: 1000 });
const TAIL_BYTES = 65536;
const FORWARD_BYTES = 1048576;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

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
    return output.trim().split('\n').filter(Boolean).map(line => line.trim().split(/\s+/))
      .filter(row => Number(row[1]) === pgid).map(row => ({ pid: Number(row[0]), state: row[2][0] }));
  }
  throw new Error('requires: POSIX owned process-group inspection');
}

async function cleanupGroup(pgid, deps, limits) {
  const result = { status: 'clean', pgid, survivor_pids: [], zombie_pids: [], signals: [], errors: [] };
  if (!Number.isSafeInteger(pgid) || pgid <= 1 || pgid === process.pid) {
    return { ...result, status: 'incomplete', errors: ['invalid owned process group'] };
  }
  const inspect = () => {
    try { return deps.groupMembers(pgid); }
    catch (error) { result.errors.push(`inspection: ${error.code || error.message}`); return null; }
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
  if (cleanup.status === 'incomplete') deps.stderr.write(`\nCLEANUP INCOMPLETE ${label} survivors=${JSON.stringify(cleanup.survivor_pids)} errors=${JSON.stringify(cleanup.errors)}\n`);
  return { ...outcome, timedOut, aborted, spawnError, duration_ms: Math.round(deps.now() - start), tail: outputTail, output_truncated: forwarded > FORWARD_BYTES, cleanup };
}

async function run(repoRoot, options, injected = {}) {
  const deps = { spawn, groupMembers, makeTemporaryDirectory: prefix => fs.mkdtempSync(prefix), removeOwnedDirectory: directory => fs.rmSync(directory, { recursive: true, force: true }), signalGroup: (pgid, signal) => process.kill(-pgid, signal), now: () => performance.now(), sleep, stdout: process.stdout, stderr: process.stderr, ...injected };
  const controller = new AbortController();
  deps.abortSignal = deps.abortSignal || controller.signal;
  const interrupt = signal => controller.abort(signal);
  const onINT = () => interrupt('SIGINT'), onTERM = () => interrupt('SIGTERM');
  const limits = { ...DEFAULTS, ...options };
  const entries = discover(repoRoot, options.patterns);
  if (!entries.length) throw new Error('no tests matched');
  const start = deps.now(), deadline = start + limits.runTimeoutMs;
  const summary = { version: 1, files: [], discovered: entries, file_timeout_ms: limits.fileTimeoutMs, run_timeout_ms: limits.runTimeoutMs, cleanup_allowance_ms: limits.termGraceMs + limits.killGraceMs, duration_ms: 0, exit_code: 1, runner_errors: [], directory_cleanup_errors: [] };
  const cacheRoot = path.join(repoRoot, 'node_modules', '.cache');
  let outDir, testHome;
  process.on('SIGINT', onINT); process.on('SIGTERM', onTERM);
  try {
    fs.mkdirSync(cacheRoot, { recursive: true });
    outDir = deps.makeTemporaryDirectory(path.join(cacheRoot, 'pentacle-tests-'));
    testHome = deps.makeTemporaryDirectory(path.join(os.tmpdir(), 'pentacle-test-home-'));
    const env = { ...process.env, HOME: testHome, USERPROFILE: testHome };
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
      const result = await operation(process.execPath, ['--test', target], context, deps, limits);
      Object.assign(row, { status: result.timedOut ? 'timeout' : (!result.aborted && result.exitCode === 0 && result.cleanup.status === 'clean' ? 'pass' : 'fail'), execution: result, cleanup: result.cleanup, duration_ms: Math.round(deps.now() - fileStart) });
    }
  } catch (error) {
    summary.runner_errors.push(error.code || error.name);
    const current = summary.files.at(-1);
    if (current && current.status === 'not_run' && current.reason === null) { current.status = 'fail'; current.reason = 'runner_error'; }
    for (const file of entries.slice(summary.files.length)) summary.files.push({ file, status: 'not_run', duration_ms: 0, cleanup: { status: 'not_started', survivor_pids: [] }, reason: 'runner_error' });
  } finally {
    process.removeListener('SIGINT', onINT); process.removeListener('SIGTERM', onTERM);
    for (const [kind, directory] of [['test_home', testHome], ['bundle_output', outDir]]) {
      if (!directory) continue;
      try { deps.removeOwnedDirectory(directory); }
      catch (error) { summary.directory_cleanup_errors.push({ kind, error: error.code || error.name }); }
    }
  }
  summary.duration_ms = Math.round(deps.now() - start);
  summary.exit_code = summary.files.every(row => row.status === 'pass') && !summary.runner_errors.length && !summary.directory_cleanup_errors.length ? 0 : 1;
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

module.exports = { DEFAULTS, parseArgs, discover, groupMembers, cleanupGroup, operation, run };
