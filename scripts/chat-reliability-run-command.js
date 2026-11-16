'use strict';

function resolveRunCommand(command, args, options = {}) {
  const platform = options.platform || process.platform;
  const env = options.env || process.env;
  if (platform !== 'win32' || command !== 'npm') return { command, args };
  return {
    command: env.ComSpec || 'cmd.exe',
    args: ['/d', '/c', env.PENTACLE_RELIABILITY_NPM || 'npm.cmd', ...args],
  };
}

const canonicalWindowsPowerShell = 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe';

function isTrustedWindowsPowerShell(value) {
  const normalized = String(value || '').replace(/\//g, '\\').toLowerCase();
  return normalized === canonicalWindowsPowerShell.toLowerCase()
    || normalized === '\\mnt\\c\\windows\\system32\\windowspowershell\\v1.0\\powershell.exe';
}

function resolveWindowsSanitizerCommand(options = {}) {
  const windowsPath = options.windowsPath || ((value) => value);
  // This is deliberately an argument seam rather than a process-environment
  // override. The real caller supplies only the canonical Windows executable
  // (or its WSL-mounted spelling) before the sanitized boundary exists.
  const powershell = options.powershellPath || canonicalWindowsPowerShell;
  if (!isTrustedWindowsPowerShell(powershell)) throw new Error('untrusted Windows PowerShell path');
  return {
    command: powershell,
    args: [
      '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
      windowsPath(options.sanitizer), '-Repository', windowsPath(options.repository),
      '-Wrapper', windowsPath(options.wrapper), '-Mode', 'Aggregate',
      '-ForwardArgsJson', JSON.stringify(options.forwardArgs || []),
    ],
  };
}

function exitCodeForSpawn(result) {
  return result && result.status === 0 ? 0 : (result && result.status ? result.status : 1);
}

module.exports = {
  canonicalWindowsPowerShell,
  isTrustedWindowsPowerShell,
  resolveRunCommand,
  resolveWindowsSanitizerCommand,
  exitCodeForSpawn,
};
