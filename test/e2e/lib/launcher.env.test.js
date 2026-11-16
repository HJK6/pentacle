const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { buildChildEnv } = require('./launcher');

test('--config sets canonical PENTACLE_CONFIG to the resolved file path', () => {
  const configFile = 'tmp/pentacle-walk-config.js';
  const childEnv = buildChildEnv({ configFile, baseEnv: {} });

  assert.equal(childEnv.PENTACLE_CONFIG, path.resolve(configFile));
  assert.equal(childEnv.PENTACLE_CONFIG_FILE, undefined);
});

test('explicit env extras take precedence over --config', () => {
  const explicitConfig = '/tmp/explicit-pentacle.config.js';
  const childEnv = buildChildEnv({
    configFile: '/tmp/from-flag.config.js',
    env: { PENTACLE_CONFIG: explicitConfig },
    baseEnv: {},
  });

  assert.equal(childEnv.PENTACLE_CONFIG, explicitConfig);
  assert.equal(childEnv.PENTACLE_CONFIG_FILE, undefined);
});

test('no --config does not inject PENTACLE_CONFIG', () => {
  const childEnv = buildChildEnv({ baseEnv: {} });

  assert.equal(childEnv.PENTACLE_CONFIG, undefined);
  assert.equal(childEnv.PENTACLE_CONFIG_FILE, undefined);
});
