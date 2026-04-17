#!/usr/bin/env node
// Rebuild native modules (node-pty) against Electron's Node ABI.
// Runs on darwin + linux (required for HOST mode).
// Best-effort on win32 — skips silently if rebuild fails.
'use strict';
const { execSync } = require('child_process');

const platform = process.platform;

if (platform === 'darwin' || platform === 'linux') {
  try {
    execSync('electron-rebuild', { stdio: 'inherit' });
  } catch (e) {
    process.stderr.write('[postinstall] electron-rebuild failed: ' + (e.message || e) + '\n');
    if (platform === 'darwin') {
      process.exit(1);
    }
    // On Linux, warn but don't crash install — CLIENT-mode users don't need node-pty locally.
    process.stderr.write('[postinstall] node-pty may not work until you fix the build toolchain.\n');
    process.stderr.write('[postinstall] On Ubuntu: sudo apt install build-essential python3-dev\n');
    process.exit(0);
  }
} else if (platform === 'win32') {
  // Windows CLIENT mode typically doesn't use node-pty locally (SSH to WSL).
  // Rebuild anyway so HOST mode works if someone runs Pentacle inside WSL.
  try {
    execSync('electron-rebuild', { stdio: 'inherit' });
  } catch {
    process.stderr.write('[postinstall] electron-rebuild failed on win32 (best-effort, continuing)\n');
  }
}
