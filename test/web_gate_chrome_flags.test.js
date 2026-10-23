'use strict';

// Contract: every e2e headless Chrome launcher must not depend on the caller's HOME keychain.
// On macOS a HOME without a login keychain (isolated gate environments) left the page
// target at about:blank with CDP Runtime.enable / Page.navigate timing out; --use-mock-keychain
// fixes it (Chrome 155 canary: empty HOME fails, empty HOME + flag loads and runs JS).

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const E2E = path.join(__dirname, 'e2e');
const LAUNCHER = /["']--headless=new["']/g;

function launchers() {
  const found = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) { if (entry.name !== 'runs' && entry.name !== 'node_modules') walk(full); continue; }
      if (!/\.(js|cjs|py)$/.test(entry.name)) continue;
      const source = fs.readFileSync(full, 'utf8');
      const launches = (source.match(LAUNCHER) || []).length;
      if (launches) found.push({ file: path.relative(E2E, full), launches, flags: (source.match(/["']--use-mock-keychain["']/g) || []).length });
    }
  };
  walk(E2E);
  return found;
}

test('every e2e headless Chrome launch carries --use-mock-keychain', () => {
  const all = launchers();
  assert.ok(all.some((l) => l.file === 'web_gate.js'), 'found the web gate launcher');
  assert.ok(all.length >= 10, `expected the known launchers, saw ${all.length}`);
  for (const l of all) assert.ok(l.flags >= l.launches, `${l.file}: ${l.launches} headless launch(es) but ${l.flags} --use-mock-keychain`);
});
