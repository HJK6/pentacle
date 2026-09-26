'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const runner = path.join(repoRoot, 'scripts', 'run-tests.js');
const fixture = 'test/fixtures/runner_home_probe.js';

for (const failing of [false, true]) {
  test(`suite child home isolates marker and is cleaned after ${failing ? 'failure' : 'success'}`, () => {
    const parentHome = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-runner-parent-'));
    const parentMarker = path.join(parentHome, '.pentacle', 'desktop-runtime.json');
    const sentinel = Buffer.from('operator-sentinel\n');
    try {
      fs.mkdirSync(path.dirname(parentMarker));
      fs.writeFileSync(parentMarker, sentinel);
      const childEnv = {
        ...process.env,
        HOME: parentHome,
        USERPROFILE: parentHome,
        PENTACLE_MARKER_PROBE_FAIL: failing ? '1' : '0',
      };
      // Node marks a test-file process with this private recursion guard.
      // The nested runner is the subject of this test, not a nested test file.
      delete childEnv.NODE_TEST_CONTEXT;
      const run = spawnSync(process.execPath, [runner, fixture], {
        cwd: repoRoot,
        encoding: 'utf8',
        env: childEnv,
      });
      const output = `${run.stdout || ''}\n${run.stderr || ''}`;
      const found = output.match(/MARKER_PROBE_JSON=(\{[^\r\n]+\})/);
      assert.ok(found, `fixture did not report its home:\n${output}`);
      const child = JSON.parse(found[1]);
      assert.equal(run.status === 0, !failing, output);
      assert.notEqual(path.resolve(child.home), path.resolve(parentHome), 'test child must not use the caller home');
      assert.equal(path.resolve(child.HOME), path.resolve(child.home));
      assert.equal(path.resolve(child.USERPROFILE), path.resolve(child.home));
      assert.deepEqual(fs.readFileSync(parentMarker), sentinel, 'caller marker must remain byte-identical');
      assert.equal(fs.existsSync(child.home), false, 'owned child home must be removed after the test');
    } finally {
      fs.rmSync(parentHome, { recursive: true, force: true });
    }
  });
}
