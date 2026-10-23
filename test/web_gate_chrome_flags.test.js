'use strict';

// Contract: the web gate's headless Chrome must not depend on the caller's HOME keychain.
// On macOS a HOME without a login keychain (isolated gate environments) left the page
// target at about:blank with CDP Runtime.enable timing out; --use-mock-keychain fixes it
// (proved with a Chrome 155 canary: empty HOME fails, empty HOME + flag loads and runs JS).

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

test('web_gate launches Chrome with --use-mock-keychain', () => {
  const source = fs.readFileSync(path.join(__dirname, 'e2e', 'web_gate.js'), 'utf8');
  const launch = source.slice(source.indexOf("spawn(chromeBin, ["), source.indexOf("], { stdio: ['ignore', 'pipe', 'pipe'], detached: true })"));
  assert.ok(launch.includes("'--headless=new'"), 'found the Chrome launch');
  assert.ok(launch.includes("'--use-mock-keychain'"), 'Chrome must not use the caller keychain');
});
