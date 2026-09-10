'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { runOwnedElectron } = require('./supervisor');

function outputDirectory() { return fs.mkdtempSync(path.join(os.tmpdir(), 'terminal-supervisor-test-')); }
test('removes the exact owned profile after child shutdown recreates it', async () => {
  const output = outputDirectory();
  const neighbor = path.join(output, 'unrelated');
  fs.mkdirSync(neighbor);
  try {
    const script = `const fs=require('fs'),path=require('path');const root=process.env.PENTACLE_RUNTIME_FIXTURE_ROOT;
      fs.rmSync(root,{recursive:true});fs.mkdirSync(path.join(root,'profile','Session Storage'),{recursive:true});
      fs.writeFileSync(path.join(root,'profile','Session Storage','late-write'),'recreated at shutdown');
      fs.writeFileSync(path.join(process.env.PENTACLE_RUNTIME_OUTPUT,'receipt.json'),JSON.stringify({fixtureRoot:root,cleanup:{serverCleaned:true}}));`;
    const result = await runOwnedElectron({ executable: process.execPath, args: ['-e', script], output });
    assert.equal(result.childClosed, true);
    assert.equal(result.directoryRemoved, true);
    assert.equal(fs.existsSync(result.root), false);
    assert.equal(fs.existsSync(neighbor), true);
    assert.equal(result.exitCode, 0);
  } finally { fs.rmSync(output, { recursive: true, force: true }); }
});

test('spawn failure removes only the never-used owned root', async () => {
  const output = outputDirectory();
  try {
    const result = await runOwnedElectron({ executable: path.join(output, 'missing-executable'), args: [], output });
    assert.equal(result.childClosed, true);
    assert.equal(result.directoryRemoved, true);
    assert.equal(result.exitCode, 2);
  } finally { fs.rmSync(output, { recursive: true, force: true }); }
});

test('signaled child with uncertain server preserves its root despite a stale successful receipt', async () => {
  const output = outputDirectory();
  let result;
  try {
    fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify({ fixtureRoot: '/unrelated-prior-root', cleanup: { serverCleaned: true } }));
    result = await runOwnedElectron({ executable: process.execPath, args: ['-e', "process.kill(process.pid,'SIGTERM')"], output });
    assert.equal(result.childSignal, 'SIGTERM');
    assert.equal(result.receiptMatchesRoot, false);
    assert.equal(result.preservedForRecovery, true);
    assert.equal(fs.existsSync(result.root), true);
    assert.equal(result.exitCode, 2);
  } finally {
    if (result) fs.rmSync(result.root, { recursive: true, force: true });
    fs.rmSync(output, { recursive: true, force: true });
  }
});
