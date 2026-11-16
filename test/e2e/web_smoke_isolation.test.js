'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');

const profilePath = require.resolve('./configs/web_mode_local_smoke');
const { allocateReportDir, stopOwnedProcess } = require('./web_gate');

function loadProfile(rawPort) {
  const previous = process.env.PENTACLE_SMOKE_PORT;
  try {
    if (rawPort === undefined) delete process.env.PENTACLE_SMOKE_PORT;
    else process.env.PENTACLE_SMOKE_PORT = rawPort;
    delete require.cache[profilePath];
    return require(profilePath);
  } finally {
    if (previous === undefined) delete process.env.PENTACLE_SMOKE_PORT;
    else process.env.PENTACLE_SMOKE_PORT = previous;
    delete require.cache[profilePath];
  }
}

test('external smoke profile uses only an explicit valid loopback port', () => {
  for (const port of ['1', '49001', '65535']) {
    assert.equal(loadProfile(port).chatStream.url, `ws://127.0.0.1:${Number(port)}`);
  }
  for (const port of [undefined, '', '0', '-1', '+1', '1.5', '0x1234', '65536', 'abc']) {
    assert.throws(() => loadProfile(port), /PENTACLE_SMOKE_PORT.*1\.\.65535/);
  }
});

test('two report allocations with the same millisecond cannot share a verdict path', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-smoke-report-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const fixedNow = () => new Date('2026-09-26T10:00:00.123Z');
  const first = allocateReportDir(fixedNow, root);
  const second = allocateReportDir(fixedNow, root);
  assert.notEqual(first, second);
  for (const [dir, verdict] of [[first, 'first'], [second, 'second']]) {
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'verdict.json'), verdict);
  }
  assert.equal(fs.readFileSync(path.join(first, 'verdict.json'), 'utf8'), 'first');
  assert.equal(fs.readFileSync(path.join(second, 'verdict.json'), 'utf8'), 'second');
});

test('owned daemon ignoring TERM is killed and reported', async () => {
  const proc = new EventEmitter();
  proc.pid = 12345; proc.exitCode = null; proc.signalCode = null;
  const signals = [];
  proc.kill = (signal) => {
    signals.push(signal);
    if (signal === 'SIGKILL') { proc.signalCode = signal; proc.emit('exit', null, signal); }
    return true;
  };
  const receipt = await stopOwnedProcess(proc, 5);
  assert.deepEqual(signals, ['SIGTERM', 'SIGKILL']);
  assert.deepEqual(receipt, { pid: 12345, exited: true, forced: true, exit_code: null, signal: 'SIGKILL' });
});

test('owned daemon surviving KILL makes cleanup fail', async () => {
  const proc = new EventEmitter();
  proc.pid = 12346; proc.exitCode = null; proc.signalCode = null;
  const signals = [];
  proc.kill = (signal) => { signals.push(signal); return true; };
  await assert.rejects(stopOwnedProcess(proc, 5), /CLEANUP_FAIL.*survived SIGKILL/);
  assert.deepEqual(signals, ['SIGTERM', 'SIGKILL']);
});
