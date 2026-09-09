const test = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');

test('submodules are initialized and checked out exactly at their pinned commits', () => {
  const output = execFileSync('git', ['submodule', 'status'], {
    cwd: process.cwd(),
    encoding: 'utf8',
  });
  const dirty = output
    .split('\n')
    .map((line) => line.trimEnd())
    .filter(Boolean)
    .filter((line) => /^[+\-U]/.test(line));

  assert.deepEqual(dirty, [], `submodule status contains drifted entries:\n${dirty.join('\n')}`);
});
