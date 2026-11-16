const test = require('node:test');
const assert = require('node:assert/strict');
const {
  resolveRunCommand, resolveWindowsSanitizerCommand, exitCodeForSpawn,
} = require('../../../scripts/chat-reliability-run-command');

test('Windows unit scripts run npm.cmd through cmd.exe', () => {
  const resolved = resolveRunCommand('npm', ['run', 'test:chat-store'], {
    platform: 'win32',
    env: { ComSpec: 'C:\\Windows\\System32\\cmd.exe', PENTACLE_RELIABILITY_NPM: 'C:\\nvm4w\\nodejs\\npm.cmd' },
  });
  assert.deepEqual(resolved, {
    command: 'C:\\Windows\\System32\\cmd.exe',
    args: ['/d', '/c', 'C:\\nvm4w\\nodejs\\npm.cmd', 'run', 'test:chat-store'],
  });
});

test('non-Windows commands stay direct', () => {
  assert.deepEqual(
    resolveRunCommand('npm', ['run', 'test:e2e-unit'], { platform: 'linux', env: {} }),
    { command: 'npm', args: ['run', 'test:e2e-unit'] },
  );
});

test('aggregate starts with the absolute no-profile sanitizer and preserves failure exits', () => {
  const resolved = resolveWindowsSanitizerCommand({
    powershellPath: 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe',
    sanitizer: '/mnt/c/repo/scripts/run-walk-win-sanitized.ps1',
    repository: '/mnt/c/repo',
    wrapper: 'C:\\repo\\scripts\\run-walk-win.cmd',
    forwardArgs: ['--host', 'hostb'],
    windowsPath: (value) => String(value).replace('/mnt/c/', 'C:\\').replaceAll('/', '\\'),
  });
  assert.equal(resolved.command, 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe');
  assert.deepEqual(resolved.args.slice(0, 6), ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File']);
  assert.equal(resolved.args.includes('-Mode'), true);
  assert.equal(resolved.args.includes('Aggregate'), true);
  assert.equal(exitCodeForSpawn({ status: 0 }), 0);
  assert.equal(exitCodeForSpawn({ status: 7 }), 7);
  assert.equal(exitCodeForSpawn({ status: null }), 1);
  assert.equal(exitCodeForSpawn({ error: new Error('spawn') }), 1);
});

test('aggregate refuses environment-selected PowerShell executables', () => {
  assert.throws(() => resolveWindowsSanitizerCommand({
    powershellPath: 'C:\\temp\\powershell.exe', sanitizer: 'C:\\repo\\script.ps1', repository: 'C:\\repo', wrapper: 'C:\\repo\\run.cmd',
  }), /untrusted Windows PowerShell path/);
});
