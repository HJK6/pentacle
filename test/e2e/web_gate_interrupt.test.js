'use strict';

// Interrupt teardown for test/e2e/web_gate.js: SIGINT, SIGTERM and SIGHUP sent to
// the gate process (only) must exit 130/143/129 and leave no Chrome, daemon or
// directory of that invocation behind. Run explicitly (it needs Chrome and the
// web build, and boots the gate three times, so it is outside the default
// `npm test` glob):
//
//   npm run build:web && node --test test/e2e/web_gate_interrupt.test.js

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn, execFileSync } = require('node:child_process');

const { resolveChrome, freePort, stopOwnedGroup } = require('./web_gate');
const cdp = require('./lib/cdp');
const os = require('node:os');
const { headlessChromeArgs } = require('./lib/chrome_launch.cjs');

const ROOT = path.join(__dirname, '..', '..');
const GATE = path.join(__dirname, 'web_gate.js');
const MARKER = 'WEB_GATE_TEST_HOLD ';
const BOOT_TIMEOUT_MS = 90000;
const EXIT_TIMEOUT_MS = 20000;
const CASES = [['SIGINT', 130], ['SIGTERM', 143], ['SIGHUP', 129]];

function skipReason() {
  if (!fs.existsSync(path.join(ROOT, 'renderer', 'dist', 'web', 'web.html'))) return 'web build missing (run npm run build:web)';
  try { resolveChrome(); } catch (error) { return `Chrome unavailable: ${error.message}`; }
  return false;
}

function processTable() {
  const out = execFileSync('ps', ['-ww', '-Ao', 'pid=,ppid=,pgid=,command='], { encoding: 'utf8' });
  return out.split('\n').map((line) => line.match(/^\s*(\d+)\s+(\d+)\s+(\d+)\s+(.*)$/))
    .filter(Boolean).map((m) => ({ pid: Number(m[1]), ppid: Number(m[2]), pgid: Number(m[3]), command: m[4] }));
}

function audit(label) {
  const table = processTable();
  const roots = new Set(table.filter(p => p.command.startsWith(resolveChrome() + ' ')).map(p => p.pid));
  const pids = table.filter(p => roots.has(p.pid) || roots.has(p.pgid) || roots.has(p.ppid))
    .map(({ pid, ppid, pgid }) => ({ pid, ppid, pgid }));
  console.log(`WEB_GATE_BROWSER_AUDIT ${JSON.stringify({ label, pids })}`);
}

function alive(pid) {
  try { process.kill(pid, 0); return true; } catch (error) { return error.code === 'EPERM'; }
}

function waitForExit(child, timeoutMs) {
  return new Promise((resolve) => {
    if (child.exitCode !== null || child.signalCode !== null) { resolve(true); return; }
    const timer = setTimeout(() => resolve(false), timeoutMs);
    child.once('exit', () => { clearTimeout(timer); resolve(true); });
  });
}

function waitForMarker(child, getOutput, timeoutMs) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeoutMs;
    const poll = () => {
      const line = getOutput().split('\n').find((l) => l.startsWith(MARKER));
      if (line) { resolve(JSON.parse(line.slice(MARKER.length))); return; }
      if (child.exitCode !== null || child.signalCode !== null) { reject(new Error(`gate exited before the hold marker:\n${getOutput().slice(-2000)}`)); return; }
      if (Date.now() > deadline) { reject(new Error(`no hold marker within ${timeoutMs}ms:\n${getOutput().slice(-2000)}`)); return; }
      setTimeout(poll, 200);
    };
    poll();
  });
}

// Stops what the marker names, by recorded PID, group or exact user-data-dir only, so a
// failing run never leaves processes behind. The test runner's own group is never signalled.
function tearDownMarked(marker) {
  if (!marker) return;
  const kill = (target) => { try { process.kill(target, 'SIGKILL'); } catch { /* already gone */ } };
  const table = processTable();
  const chrome = table.find((p) => p.pid === marker.chromePid);
  if (chrome && chrome.pgid === marker.chromePid) kill(-marker.chromePid);
  for (const p of table) {
    if (p.command.includes(`--user-data-dir=${marker.userDataDir}`)) kill(p.pid);
  }
  const daemon = table.find((p) => p.pid === marker.daemonPid);
  if (daemon && daemon.command.includes(marker.scratch)) kill(daemon.pid);
  for (const dir of [marker.userDataDir, marker.scratch, marker.runtimeDir]) {
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* best effort */ }
  }
}

for (const [signal, expectedCode] of CASES) {
  test(`web gate ${signal} exits ${expectedCode} and leaves nothing running`, { skip: skipReason(), timeout: 180000 }, async () => {
    audit(`before ${signal}`);
    let output = '';
    const gate = spawn(process.execPath, [GATE], {
      cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, PENTACLE_WEB_GATE_TEST_HOLD_AFTER_ATTACH: '1' },
    });
    gate.stdout.on('data', (d) => { output += String(d); });
    gate.stderr.on('data', (d) => { output += String(d); });
    let marker = null;
    const problems = [];
    try {
      marker = await waitForMarker(gate, () => output, BOOT_TIMEOUT_MS);
      assert.ok(marker.chromePid > 0 && marker.daemonPid > 0, 'marker names a Chrome and a daemon PID');
      gate.kill(signal);
      const exited = await waitForExit(gate, EXIT_TIMEOUT_MS);
      if (!exited) problems.push(`gate still running ${EXIT_TIMEOUT_MS}ms after ${signal}`);
      else if (gate.exitCode !== expectedCode) problems.push(`exit ${gate.exitCode}/${gate.signalCode}, expected ${expectedCode}`);
      const survivors = processTable().filter((p) => p.command.includes(marker.userDataDir));
      if (survivors.length) problems.push(`${survivors.length} process(es) still reference ${marker.userDataDir}: ${survivors.map((p) => p.pid).join(',')}`);
      if (alive(marker.daemonPid)) problems.push(`daemon ${marker.daemonPid} still running`);
      for (const dir of [marker.userDataDir, marker.scratch, marker.runtimeDir]) {
        if (fs.existsSync(dir)) problems.push(`directory still exists: ${dir}`);
      }
    } finally {
      tearDownMarked(marker);
      if (gate.exitCode === null && gate.signalCode === null) {
        gate.kill('SIGKILL');
        await waitForExit(gate, 5000);
      }
    }
    audit(`after ${signal}`);
    assert.deepEqual(problems, []);
  });
}

// --keep retains debug files, while the same owner still releases every browser.
// One unrelated, test-owned Chrome stays alive as an ownership sentinel.
for (const failPage of [false, true]) {
  test(`web gate --keep ${failPage ? 'failure' : 'success'} retains files and releases only its browser`, { skip: skipReason(), timeout: 180000 }, async () => {
    audit(`before --keep ${failPage ? 'failure' : 'success'}`);
    const sentinelDir = fs.mkdtempSync(path.join(os.tmpdir(), 'web-gate-sentinel-'));
    const sentinelPort = await freePort();
    const sentinel = spawn(resolveChrome(), [...headlessChromeArgs(), `--remote-debugging-port=${sentinelPort}`,
      `--user-data-dir=${sentinelDir}`, 'about:blank'], { stdio: 'ignore', detached: true });
    let sentinelPage, page, marker, gate;
    let output = '';
    try {
      sentinelPage = await cdp.connect(sentinelPort, { match: /about:blank/ });
      gate = spawn(process.execPath, [GATE, '--keep', '--scenario', 'transport-and-config', '--timeout', '1500'], {
        cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'],
        env: { ...process.env, PENTACLE_WEB_GATE_TEST_HOLD_AFTER_ATTACH: '1' },
      });
      gate.stdout.on('data', d => { output += String(d); });
      gate.stderr.on('data', d => { output += String(d); });
      marker = await waitForMarker(gate, () => output, BOOT_TIMEOUT_MS);
      if (failPage) {
        const row = processTable().find(p => p.pid === marker.chromePid);
        const port = Number(row.command.match(/--remote-debugging-port=(\d+)/)[1]);
        page = await cdp.connect(port);
        await page.send('Page.navigate', { url: 'about:blank' });
      }
      assert.ok(await waitForExit(gate, EXIT_TIMEOUT_MS), output);
      assert.equal(gate.exitCode, failPage ? 1 : 0, output);
      const reportDir = output.match(/artifacts: (.+)/)?.[1].trim();
      assert.ok(reportDir, output);
      const verdict = JSON.parse(fs.readFileSync(path.join(reportDir, 'verdict.json')));
      assert.equal(verdict.status, failPage ? 'FAIL' : 'PASS');
      const survivors = processTable().filter(p => p.command.includes(marker.userDataDir));
      assert.deepEqual(survivors, [], 'run-owned Chrome and descendants must be absent even with --keep');
      assert.equal(verdict.chrome_cleanup.exited, true);
      assert.equal(verdict.chrome_cleanup.pid, marker.chromePid);
      for (const dir of [marker.userDataDir, marker.scratch]) assert.ok(fs.existsSync(dir), `debug files retained: ${dir}`);
      assert.equal(alive(marker.daemonPid), false);
      assert.ok(alive(sentinel.pid), 'foreign Chrome sentinel remains alive');
      assert.equal((await sentinelPage.send('Browser.getVersion')).product.startsWith('Chrome/'), true);
    } finally {
      page?.close(); sentinelPage?.close();
      tearDownMarked(marker);
      if (gate && gate.exitCode === null && gate.signalCode === null) {
        gate.kill('SIGKILL'); await waitForExit(gate, 5000);
      }
      await stopOwnedGroup(sentinel);
      assert.deepEqual(processTable().filter(p => p.command.includes(`--user-data-dir=${sentinelDir}`)), [], 'sentinel cleanup complete');
      fs.rmSync(sentinelDir, { recursive: true, force: true });
      audit(`after --keep ${failPage ? 'failure' : 'success'}`);
    }
  });
}
