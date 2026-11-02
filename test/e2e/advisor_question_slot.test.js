'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('./lib/web_scenarios'), 'utf8');
const definition = source.match(/function advisorProbeSlot\(bindings, sessionName\) \{[\s\S]*?\n\}/);
assert.ok(definition, 'slot resolver must be present');
const advisorProbeSlot = vm.runInNewContext(`(${definition[0]})`);

test('probe slot resolves a composite session even when UI host alias differs from daemon host', () => {
  const bindings = [{ name: 'other', hostId: 'local' }, null,
    { name: 'assistant', hostId: 'local' }, null];
  // The actual daemon host is a distinct configured value; it is never
  // compared to the UI alias when locating the already-focused session.
  assert.equal(advisorProbeSlot(bindings, 'assistant'), 2);
  assert.equal(advisorProbeSlot(bindings, 'missing'), -1);
  assert.throws(() => advisorProbeSlot([...bindings, { name: 'assistant', hostId: 'remote' }], 'assistant'), /ambiguous/);
});

test('live profile uses configured daemon host independently of UI alias', () => {
  const { spawnSync } = require('node:child_process');
  const result = spawnSync(process.execPath, ['-e', `const p=require('./test/e2e/configs/advisor_question_live'); process.stdout.write(JSON.stringify(p.chatStream));`], {
    cwd: require('node:path').resolve(__dirname, '../..'),
    env: { ...process.env, PENTACLE_CARD_PROBE_WS_URL: 'ws://127.0.0.1:9999',
      PENTACLE_CARD_PROBE_TOKEN_PATH: '/tmp/probe-token', PENTACLE_CARD_PROBE_HOST: 'fixture-host' }, encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr);
  const profile = JSON.parse(result.stdout);
  assert.equal(profile.localHost, 'fixture-host');
  assert.deepEqual(profile.hosts, ['local']);
});
