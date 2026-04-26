#!/usr/bin/env node
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync, spawn } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const STATE_DIR = path.join(ROOT, '.pentacle-sync');
const STATE_FILE = path.join(STATE_DIR, 'state.json');

function parseArgs(argv) {
  const args = { branch: 'main', dryRun: false, skipRestart: false };
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i];
    if (key === '--dry-run') args.dryRun = true;
    else if (key === '--skip-restart') args.skipRestart = true;
    else if (key.startsWith('--')) args[key.slice(2)] = argv[++i];
  }
  return args;
}

function detectMachine() {
  if (process.env.TRIFORCE_MACHINE) return process.env.TRIFORCE_MACHINE.toLowerCase();
  const host = os.hostname().toLowerCase();
  if (host.includes('amaterasu')) return 'amaterasu';
  if (host.includes('merlin') || host.includes('macbook')) return 'merlin';
  if (host.includes('bartimaeus') || host.includes('mac-mini')) return 'bartimaeus';
  const home = os.homedir();
  if (home === '/Users/vgujju') return 'merlin';
  if (home === '/Users/bartimaeus') return 'bartimaeus';
  if (home === '/home/vamsh' || home === '/mnt/c/Users/vamsh') return 'amaterasu';
  return host.replace(/[^a-z0-9_-]+/g, '-').replace(/^-|-$/g, '') || 'unknown';
}

function run(cmd, args, opts = {}) {
  const pathEntries = [
    path.dirname(process.execPath || ''),
    '/opt/homebrew/bin',
    '/usr/local/bin',
    '/usr/bin',
    '/bin',
    '/usr/sbin',
    '/sbin',
  ].filter(Boolean);
  const res = spawnSync(cmd, args, {
    cwd: opts.cwd || ROOT,
    shell: !!opts.shell,
    text: true,
    encoding: 'utf8',
    env: {
      ...process.env,
      PATH: `${pathEntries.join(path.delimiter)}${path.delimiter}${process.env.PATH || ''}`,
      ...(opts.env || {}),
    },
    maxBuffer: 20 * 1024 * 1024,
  });
  return {
    ok: res.status === 0,
    status: res.status,
    stdout: res.stdout || '',
    stderr: res.stderr || '',
    command: [cmd, ...args].join(' '),
  };
}

function npmCommand() {
  if (process.env.PENTACLE_NPM_PATH) return process.env.PENTACLE_NPM_PATH;
  const nodeDir = path.dirname(process.execPath || '');
  for (const candidate of [
    path.join(nodeDir, process.platform === 'win32' ? 'npm.cmd' : 'npm'),
    '/opt/homebrew/bin/npm',
    '/usr/local/bin/npm',
    '/usr/bin/npm',
    '/mnt/c/nvm4w/nodejs/npm.cmd',
    '/mnt/c/Program Files/nodejs/npm.cmd',
  ]) {
    if (candidate && fs.existsSync(candidate)) return candidate;
  }
  return 'npm';
}

function must(res) {
  if (!res.ok) {
    const err = new Error(`${res.command} failed`);
    err.result = res;
    throw err;
  }
  return res.stdout.trim();
}

function readMessage(args) {
  if (args['message-json']) return JSON.parse(args['message-json']);
  if (args['message-file']) return JSON.parse(fs.readFileSync(args['message-file'], 'utf8'));
  if (!process.stdin.isTTY) {
    const raw = fs.readFileSync(0, 'utf8').trim();
    if (raw) return JSON.parse(raw);
  }
  return {
    source: 'pentacle_desktop_sync',
    repo: 'pentacle',
    branch: args.branch || 'main',
    commit: args.commit,
    sender_machine: args.sender,
    target_machine: args.target,
    summary: args.summary || '',
  };
}

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(value, null, 2) + '\n');
}

function readState() {
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
  } catch {
    return {};
  }
}

function pendingFile(message, reason, details) {
  const commit = String(message.commit || message.commit_sha || 'unknown');
  const short = commit.replace(/[^a-fA-F0-9]/g, '').slice(0, 12) || 'unknown';
  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\..+/, 'Z');
  const file = path.join(STATE_DIR, 'pending', `${stamp}_${short}_${detectMachine()}.json`);
  writeJson(file, {
    status: 'pending',
    reason,
    details,
    message,
    created_at: new Date().toISOString(),
  });
  return file;
}

function isAncestor(commit, ref) {
  if (!commit) return false;
  return run('git', ['merge-base', '--is-ancestor', commit, ref]).ok;
}

function changedBetween(before, after, file) {
  if (!before || before === after) return false;
  const res = run('git', ['diff', '--name-only', before, after, '--', file]);
  return res.ok && res.stdout.trim().length > 0;
}

function installIfNeeded(before, after, dryRun) {
  const needsInstall =
    !fs.existsSync(path.join(ROOT, 'node_modules')) ||
    changedBetween(before, after, 'package.json') ||
    changedBetween(before, after, 'package-lock.json');
  if (!needsInstall) return 'dependencies unchanged';
  if (dryRun) return 'would install dependencies';
  must(run(npmCommand(), ['install']));
  return 'dependencies installed';
}

function verify(dryRun) {
  if (dryRun) return 'would run syntax checks and chat UI tests';
  must(run(process.execPath, ['--check', 'main.js']));
  must(run(process.execPath, ['--check', 'renderer/app.js']));
  const test = run(npmCommand(), ['run', 'test:chat-ui']);
  if (!test.ok && !/Missing script/.test(test.stderr + test.stdout)) must(test);
  return 'verified';
}

function restartForMachine(machine, dryRun, skipRestart, restartCommand) {
  if (skipRestart) return 'restart skipped';
  if (restartCommand) {
    if (dryRun) return `would run custom restart: ${restartCommand}`;
    must(run(restartCommand, [], { shell: true }));
    return 'custom restart completed';
  }
  if (dryRun) return `would restart Pentacle on ${machine}`;

  if (machine === 'amaterasu') {
    const cmd = [
      'taskkill /F /T /IM electron.exe 2>NUL',
      'cd /D C:\\Users\\vamsh\\repos\\pentacle',
      'start "Pentacle" C:\\nvm4w\\nodejs\\npm.cmd start',
    ].join(' & ');
    const res = run('cmd.exe', ['/C', cmd]);
    if (!res.ok) must(res);
    return 'windows npm start relaunched';
  }

  if (process.platform === 'darwin') {
    const deploy = run(npmCommand(), ['run', 'deploy']);
    if (deploy.ok) return 'mac app rebuilt and relaunched';

    const appName = 'Pentacle';
    run('killall', [appName]);
    const child = spawn(npmCommand(), ['start'], {
      cwd: ROOT,
      detached: true,
      stdio: 'ignore',
      env: {
        ...process.env,
        PATH: `${path.dirname(process.execPath || '')}${path.delimiter}/opt/homebrew/bin${path.delimiter}/usr/local/bin${path.delimiter}${process.env.PATH || ''}`,
      },
    });
    child.unref();
    return `mac packaged deploy failed; source app relaunched: ${(deploy.stderr || deploy.stdout || '').trim().split('\n').slice(-1)[0] || 'deploy failed'}`;
  }

  try {
    run('pkill', ['-f', 'electron .']);
  } catch {}
  const child = spawn('npm', ['start'], {
    cwd: ROOT,
    detached: true,
    stdio: 'ignore',
    env: process.env,
  });
  child.unref();
  return 'npm start relaunched';
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const machine = (args.machine || detectMachine()).toLowerCase();
  const message = readMessage(args);
  const repo = message.repo || 'pentacle';
  const source = message.source || 'pentacle_desktop_sync';
  const target = String(message.target_machine || message.target || '').toLowerCase();
  const sender = String(message.sender_machine || message.sender || '').toLowerCase();
  const branch = message.branch || args.branch || 'main';
  const commit = String(message.commit || message.commit_sha || '').trim();
  const dryRun = !!args.dryRun;

  if (repo !== 'pentacle' && source !== 'pentacle_desktop_sync') {
    console.log(JSON.stringify({ ok: true, status: 'ignored', summary: `ignored repo ${repo}` }));
    return;
  }
  if (target && target !== machine) {
    console.log(JSON.stringify({ ok: true, status: 'ignored', summary: `ignored target ${target}` }));
    return;
  }
  if (sender && sender === machine) {
    console.log(JSON.stringify({ ok: true, status: 'ignored', summary: 'ignored self-sent update' }));
    return;
  }
  if (!commit) throw new Error('missing commit');

  const state = readState();
  const before = must(run('git', ['rev-parse', 'HEAD']));
  if (!dryRun) must(run('git', ['fetch', 'origin', branch]));

  const dirty = run('git', ['status', '--short']).stdout.trim();
  if (dirty) {
    if (dryRun) {
      console.log(JSON.stringify({ ok: true, status: 'pending', summary: 'would stop for dirty worktree' }));
      return;
    }
    const file = pendingFile(message, 'dirty worktree', dirty);
    console.log(JSON.stringify({ ok: true, status: 'pending', summary: `dirty worktree; wrote ${file}` }));
    return;
  }

  if (!dryRun && !isAncestor(commit, 'HEAD')) {
    must(run('git', ['checkout', branch]));
    const pull = run('git', ['pull', '--ff-only', 'origin', branch]);
    if (!pull.ok) {
      const file = pendingFile(message, 'fast-forward failed', pull.stderr || pull.stdout);
      console.log(JSON.stringify({ ok: true, status: 'pending', summary: `fast-forward failed; wrote ${file}` }));
      return;
    }
  }

  const after = dryRun ? before : must(run('git', ['rev-parse', 'HEAD']));
  if (!dryRun && !isAncestor(commit, 'HEAD')) {
    throw new Error(`updated to ${after.slice(0, 12)} but target ${commit.slice(0, 12)} is not present`);
  }

  const dependencyStatus = installIfNeeded(before, after, dryRun);
  const verificationStatus = verify(dryRun);
  const shouldRestart = state.last_applied_commit !== commit || state.last_restart_commit !== commit || dryRun;
  const restartStatus = shouldRestart
    ? restartForMachine(machine, dryRun, args.skipRestart, args['restart-command'] || process.env.PENTACLE_SYNC_RESTART_COMMAND)
    : 'already restarted for commit';

  if (!dryRun) {
    writeJson(STATE_FILE, {
      last_applied_commit: commit,
      last_restart_commit: commit,
      branch,
      machine,
      updated_at: new Date().toISOString(),
      dependencyStatus,
      verificationStatus,
      restartStatus,
    });
  }

  console.log(JSON.stringify({
    ok: true,
    status: 'synced',
    summary: `Pentacle synced on ${machine}: ${commit.slice(0, 12)}`,
    commit,
    dependencyStatus,
    verificationStatus,
    restartStatus,
  }));
}

try {
  main();
} catch (e) {
  const details = e.result ? `${e.result.stderr || e.result.stdout}`.trim() : (e.stack || e.message);
  try {
    const args = parseArgs(process.argv.slice(2));
    const message = readMessage(args);
    const file = pendingFile(message, e.message || 'sync failed', details);
    console.error(JSON.stringify({ ok: false, status: 'failed', summary: e.message, pending: file, details }));
  } catch {
    console.error(JSON.stringify({ ok: false, status: 'failed', summary: e.message, details }));
  }
  process.exit(1);
}
