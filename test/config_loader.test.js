const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const { candidateConfigPaths, machineKey, withThemeDefaults } = require('../config-loader');

// Regression guard (public_e2e_harness): a config missing dark/terminal crashed
// the app at launch (main.js `backgroundColor: CONFIG.dark.bg` → "Cannot read
// properties of undefined (reading 'bg')", no usable window). The packaged app
// is the trap (its resolved config can differ from dev). Pentacle is dark-only;
// there is no light theme block.
test('withThemeDefaults backfills missing theme blocks (no .bg crash possible)', () => {
  const c = withThemeDefaults({ features: { chatUi: true } }, process.cwd() + '/');
  assert.ok(c.dark && typeof c.dark.bg === 'string', 'dark.bg present');
  assert.ok(c.terminal && typeof c.terminal === 'object', 'terminal block present');
  assert.equal(c.light, undefined, 'no light theme is backfilled (dark-only)');
});

test('withThemeDefaults leaves a config that already has all theme blocks unchanged', () => {
  const orig = { dark: { bg: '#111' }, terminal: { background: '#111' }, features: {} };
  const c = withThemeDefaults(orig, process.cwd() + '/');
  assert.equal(c.dark.bg, '#111');
  assert.equal(c.terminal.background, '#111');
});

test('withThemeDefaults fills the absent terminal block (partial config)', () => {
  const c = withThemeDefaults({ dark: { bg: '#abc' } }, process.cwd() + '/');
  assert.equal(c.dark.bg, '#abc', 'present dark kept');
  assert.ok(c.terminal && typeof c.terminal === 'object', 'absent terminal filled');
});

test('machineKey normalizes a public hostname deterministically', () => {
  assert.equal(machineKey('example.local'), 'example-local');
  assert.equal(machineKey('Example Local'), 'example-local');
});

test('a normalized hostname selects local config candidates first', () => {
  const paths = candidateConfigPaths('/repo/public-desktop', {}, 'example.local');
  const base = path.resolve('/repo/public-desktop');
  const key = machineKey('example.local');

  assert.equal(paths[0], path.join(base, 'configs', `${key}.local.js`));
  assert.equal(paths[1], path.join(base, 'configs', `${key}.js`));
});
