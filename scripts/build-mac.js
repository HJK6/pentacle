#!/usr/bin/env node
// macOS build + install to /Applications.
// Equivalent to the old `npm run build` mac-only command.
// Usage: node scripts/build-mac.js  (or: npm run build:mac)
'use strict';
const { execSync } = require('child_process');
const path = require('path');
const fs = require('fs');

if (process.platform !== 'darwin') {
  process.stderr.write('build-mac.js: must be run on macOS\n');
  process.exit(1);
}

const root = path.join(__dirname, '..');
let appName = 'Pentacle';
try {
  appName = require(path.join(root, 'config-loader')).loadConfig(root).config.appName || 'Pentacle';
} catch {}

console.log(`Building ${appName}...`);
execSync('electron-builder --mac', { cwd: root, stdio: 'inherit' });

const distApp = path.join(root, 'dist', 'mac-arm64', `${appName}.app`);
const fallbackApp = path.join(root, 'dist', 'mac', `${appName}.app`);
const src = fs.existsSync(distApp) ? distApp : fallbackApp;

if (!fs.existsSync(src)) {
  process.stderr.write(`build-mac.js: built app not found at ${src}\n`);
  process.exit(1);
}

const dest = `/Applications/${appName}.app`;
console.log(`Installing to ${dest}...`);
execSync(`rm -rf ${JSON.stringify(dest)}`, { stdio: 'inherit' });
execSync(`cp -R ${JSON.stringify(src)} ${JSON.stringify(dest)}`, { stdio: 'inherit' });
console.log('Done.');
