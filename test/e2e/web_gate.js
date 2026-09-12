#!/usr/bin/env node
'use strict';

// ── Pentacle web-mode E2E gate (lane 3 of spec_pentacle__web_mode_2026_09) ───
//
//   node test/e2e/web_gate.js [--keep] [--cdp-port N] [--timeout MS]
//   node test/e2e/web_gate.js --profile <config.js>   # by-hand, external daemon
//
// The deterministic predeploy gate for web mode. With no --profile it is fully
// hermetic: it seeds a scratch chat-stream-v2 sessions DB (one visible session
// + a transcript), boots a loopback daemon against that DB, serves the web
// bundle from server/, drives it in real headless Chrome over CDP, and runs the
// named scenario functions from lib/web_scenarios.js. Every terminal action
// touches only a local `ptest-web-*` tmux session; nothing is spawned, closed,
// or sent on any shared/production daemon.
//
// Exit 0 = every scenario passed. Non-zero = a scenario failed or setup broke.

const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');
const { spawn, execFileSync } = require('child_process');

const cdp = require('./lib/cdp');
const scenarios = require('./lib/web_scenarios');
const { main: startHost } = require('../../server');

const ROOT = path.join(__dirname, '..', '..');
const CHROME_CANDIDATES = ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'];
const SEEDER = path.join(__dirname, 'lib', 'seed_web_gate.py');
const DAEMON = path.join(ROOT, 'services', 'chat-stream-v2', 'main.py');

// The seeded fixture — MUST match test/e2e/lib/seed_web_gate.py TRANSCRIPT.
const FIXTURE = {
  host: 'local',
  sessionName: 'web-gate-1',
  streamId: 'local:web-gate-1',
  transcript: [
    { kind: 'USER', text: 'hello from the web gate fixture' },
    { kind: 'ASSIST_TEXT', text: 'fixture assistant reply for the web gate' },
  ],
};

function parseArgs(argv) {
  const args = { profile: null, cdpPort: 0, keep: false, timeoutMs: 30000, python: process.env.PENTACLE_PYTHON || 'python3' };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--profile') args.profile = argv[++i];
    else if (a === '--cdp-port') args.cdpPort = Number(argv[++i]);
    else if (a === '--timeout') args.timeoutMs = Number(argv[++i]);
    else if (a === '--python') args.python = argv[++i];
    else if (a === '--keep') args.keep = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  return args;
}

function resolveChrome() {
  for (const bin of CHROME_CANDIDATES) {
    try { execFileSync('which', [bin], { stdio: 'ignore' }); return bin; } catch { /* next */ }
  }
  throw new Error(`no Chrome/Chromium on PATH (tried ${CHROME_CANDIDATES.join(', ')})`);
}

function tmux(args, opts = {}) {
  return execFileSync('tmux', args, { encoding: 'utf8', ...opts }).trim();
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function waitForListen(port, timeoutMs, isDead = () => false) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tryOnce = () => {
      if (isDead()) { reject(new Error(`daemon exited before listening on ${port}`)); return; }
      const sock = net.connect(port, '127.0.0.1');
      sock.once('connect', () => { sock.destroy(); resolve(); });
      sock.once('error', () => {
        sock.destroy();
        if (isDead()) reject(new Error(`daemon exited before listening on ${port}`));
        else if (Date.now() > deadline) reject(new Error(`daemon did not listen on ${port} within ${timeoutMs}ms`));
        else setTimeout(tryOnce, 150);
      });
    };
    tryOnce();
  });
}

function onceExit(proc) {
  return new Promise((resolve) => {
    if (!proc || proc.exitCode !== null || proc.signalCode !== null) { resolve(); return; }
    proc.once('exit', () => resolve());
    setTimeout(resolve, 3000); // don't hang teardown on a stuck child
  });
}

class Report {
  constructor(dir) { this.dir = dir; this.steps = []; fs.mkdirSync(dir, { recursive: true }); }
  ok(name, passed, detail = null) {
    this.steps.push({ name, ok: !!passed, detail });
    console.log(`   ${passed ? '✓' : '✗'} ${name}${passed ? '' : `  ${JSON.stringify(detail)}`}`);
    if (!passed) throw new Error(`assertion failed: ${name}`);
  }
  note(text) { console.log(`   · ${text}`); }
  write(extra = {}) {
    const failedScenarios = extra.failedScenarios || 0;
    // A setup/teardown error (the catch path) is a FAIL even though no scenario
    // step recorded a failure — the verdict artifact must match the non-zero exit.
    const status = (this.steps.every((s) => s.ok) && failedScenarios === 0 && !extra.error) ? 'PASS' : 'FAIL';
    const verdict = {
      scenario: 'web_gate', at: new Date().toISOString(),
      status,
      steps: this.steps, ...extra,
    };
    fs.writeFileSync(path.join(this.dir, 'verdict.json'), JSON.stringify(verdict, null, 2));
    return verdict;
  }
}

function writeProfile(scratch, daemonPort) {
  const config = {
    appName: 'Pentacle',
    features: { mic: false },
    tmux: 'tmux',
    hosts: { local: { kind: 'local' } },
    agents: {},
    chatStream: {
      url: `ws://127.0.0.1:${daemonPort}`,
      localHost: 'local',
      hosts: ['local'],
      // The loopback daemon trusts 127.0.0.1 and keeps no credential registry.
      tokenPath: path.join(scratch, 'no-such-operator-token'),
    },
  };
  const file = path.join(scratch, 'web_gate_profile.js');
  fs.writeFileSync(file, `'use strict';\nmodule.exports = ${JSON.stringify(config, null, 2)};\n`);
  return file;
}

async function startDaemon(args, scratch, runtime, fixtures = [FIXTURE]) {
  const db = path.join(scratch, 'sessions.db');
  // Seed BEFORE boot: the daemon rebuilds its inventory purely from this DB.
  const seed = fixtures.map(fixture => execFileSync(args.python, [SEEDER, '--db', db, '--host', fixture.host, '--session', fixture.sessionName],
    { encoding: 'utf8', cwd: ROOT })).join('');
  const daemonLog = fs.openSync(path.join(scratch, 'daemon.log'), 'a');

  // Retry on a lost port race (another process grabs the freePort() port before
  // the daemon binds it): a fresh port each attempt. The child is registered in
  // `runtime` BEFORE the readiness await so teardown can always kill it even if
  // startup fails mid-flight (no leaked daemon).
  let lastErr;
  for (let attempt = 0; attempt < 4; attempt++) {
    const port = await freePort();
    const proc = spawn(args.python, [DAEMON,
      '--host', '127.0.0.1', '--port', String(port), '--local-host', 'local',
      '--db', db,
      '--notifications-db', path.join(scratch, 'notifications.db'),
      '--assets-db', path.join(scratch, 'assets.db'),
      '--blob-root', path.join(scratch, 'blobs'),
      '--disable-hosts', '--disable-mirror', '--disable-nudges',
      '--disable-outbound-notices', '--disable-remote-presence',
    ], { cwd: ROOT, stdio: ['ignore', daemonLog, daemonLog] });
    runtime.daemonProc = proc;
    let dead = false;
    proc.once('exit', () => { dead = true; });
    try {
      await waitForListen(port, args.timeoutMs, () => dead);
      return { proc, port, seed: seed.trim() };
    } catch (e) {
      lastErr = e;
      try { proc.kill('SIGKILL'); } catch {}
      await onceExit(proc);
      runtime.daemonProc = null;
    }
  }
  try { fs.closeSync(daemonLog); } catch {}
  throw lastErr || new Error('daemon failed to start');
}

async function run(args) {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const report = new Report(path.join(__dirname, 'runs', stamp, 'web_gate'));
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-web-gate-'));
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-web-gate-chrome-'));
  const runtime = {};
  let daemon = null; let host = null; let chrome = null; let session = null;
  const fixture = args.profile ? null : FIXTURE;

  const cleanup = async () => {
    try { if (session) session.close(); } catch {}
    try { if (chrome && !args.keep) chrome.kill('SIGTERM'); } catch {}
    try { if (runtime.tmuxSession) tmux(['kill-session', '-t', `=${runtime.tmuxSession}`], { stdio: 'ignore' }); } catch {}
    try { if (host) await host.close(); } catch {}
    // Kill the daemon via the runtime handle (registered at spawn, so a
    // readiness-failure mid-startup is still cleaned up) and await its exit.
    const dproc = (daemon && daemon.proc) || runtime.daemonProc;
    try { if (dproc) dproc.kill('SIGTERM'); } catch {}
    await onceExit(dproc);
    try { if (!args.keep) fs.rmSync(scratch, { recursive: true, force: true }); } catch {}
    try { if (!args.keep) fs.rmSync(userDataDir, { recursive: true, force: true }); } catch {}
  };

  try {
    let profile = args.profile;
    if (!profile) {
      daemon = await startDaemon(args, scratch, runtime);
      profile = writeProfile(scratch, daemon.port);
      report.note(`seeded loopback daemon on 127.0.0.1:${daemon.port} (${daemon.seed})`);
    } else {
      report.note(`external daemon via profile ${profile} (observational: sidebar/transcript non-fatal)`);
    }

    host = await startHost(['--profile', profile, '--port', '0']);
    const url = `http://127.0.0.1:${host.port}/`;
    report.note(`web host ${url}`);

    const chromeBin = resolveChrome();
    const chromeLog = [];
    const cdpMatch = new RegExp(`127\\.0\\.0\\.1:${host.port}|Terminal Dashboard`);
    // Retry on a lost CDP port race (fresh port each attempt) unless a fixed
    // --cdp-port was requested.
    let cdpErr;
    for (let attempt = 0; attempt < 3; attempt++) {
      const cdpPort = args.cdpPort || await freePort();
      chrome = spawn(chromeBin, [
        '--headless=new', `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${userDataDir}`,
        '--no-first-run', '--no-default-browser-check', '--disable-gpu', '--window-size=1600,1000', url,
      ], { stdio: ['ignore', 'pipe', 'pipe'] });
      chrome.stdout.on('data', (d) => chromeLog.push(String(d)));
      chrome.stderr.on('data', (d) => chromeLog.push(String(d)));
      try {
        session = await cdp.connect(cdpPort, { match: cdpMatch });
        cdpErr = null;
        break;
      } catch (e) {
        cdpErr = e;
        try { chrome.kill('SIGKILL'); } catch {}
        await onceExit(chrome);
        chrome = null;
        if (args.cdpPort) break; // a fixed port was requested; don't rebind
      }
    }
    if (!session) throw cdpErr || new Error('could not attach Chrome over CDP');

    const ctx = { session, report, cdp, url, timeoutMs: args.timeoutMs, tmux, fixture, runtime };
    let failed = 0;
    for (const [name, fn] of scenarios.SCENARIOS) {
      console.log(`\n▸ ${name}`);
      try { await fn(ctx); } catch (e) { failed += 1; report.note(`scenario ${name} FAILED: ${e && e.message}`); }
    }

    fs.writeFileSync(path.join(report.dir, 'chrome.log'), chromeLog.join(''));
    fs.writeFileSync(path.join(report.dir, 'console.log'), (session.consoleLines || []).join('\n'));
    const verdict = report.write({ url, fixture, daemonPort: daemon && daemon.port, failedScenarios: failed });
    console.log(`\n◀ web_gate: ${verdict.status}\n  artifacts: ${report.dir}`);
    return verdict.status === 'PASS' && failed === 0 ? 0 : 1;
  } catch (e) {
    console.error(`\n◀ web_gate: FAIL — ${e && e.message}`);
    try { report.write({ error: String(e && e.stack ? e.stack : e) }); } catch {}
    console.error(`  artifacts: ${report.dir}`);
    return 1;
  } finally {
    await cleanup();
  }
}

if (require.main === module) {
  run(parseArgs(process.argv.slice(2))).then((code) => process.exit(code));
}

module.exports = { run, parseArgs, FIXTURE, startDaemon, writeProfile, freePort, onceExit };
