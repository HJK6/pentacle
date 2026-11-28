'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { fixtureDaemonEnv } = require('./web_gate');

test('fixture daemon environment drops live settings and keeps fixture overrides', (t) => {
  const originalEnv = process.env;
  t.after(() => { process.env = originalEnv; });
  process.env = {
    PENTACLE_DAEMON_PROGRESS_PATH: '/synthetic/live-progress.json',
    PENTACLE_DAEMON_INSTANCE_ID: 'live-instance',
    PENTACLE_DAEMON_STDOUT_PATH: '/synthetic/live-out.log',
    PENTACLE_DAEMON_STDERR_PATH: '/synthetic/live-err.log',
    PENTACLE_DOT_TLS_PORT: '8443',
    PENTACLE_DOT_TLS_BINDS: '127.0.0.1',
    PENTACLE_DOT_TLS_CERT: '/synthetic/live-cert.pem',
    PENTACLE_DOT_TLS_KEY: '/synthetic/live-key.pem',
    PENTACLE_WEB_GATE_COSMO_URL: 'parent-url',
    WEB_GATE_UNRELATED: 'inherited',
  };
  const parentEnv = { ...process.env };

  assert.deepEqual(fixtureDaemonEnv({
    WEB_GATE_FIXTURE_POLICY: 'fixture-only',
    PENTACLE_WEB_GATE_COSMO_URL: 'http://127.0.0.1:1234',
  }), {
    WEB_GATE_UNRELATED: 'inherited',
    WEB_GATE_FIXTURE_POLICY: 'fixture-only',
    PENTACLE_WEB_GATE_COSMO_URL: 'http://127.0.0.1:1234',
  });
  assert.deepEqual(process.env, parentEnv);
});

test('both fixture daemon spawn sites use the scrubbed environment', () => {
  const source = fs.readFileSync(require.resolve('./web_gate'), 'utf8');
  const spawns = source.match(/spawn\(args\.python, \[DAEMON,[\s\S]*?\}\);/g) || [];
  assert.equal(spawns.length, 2);
  for (const spawn of spawns) {
    assert.match(spawn, /\benv:\s*fixtureDaemonEnv\(/);
  }
});
