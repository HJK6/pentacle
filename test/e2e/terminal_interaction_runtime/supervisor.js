'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

// The parent outlives Electron's storage workers. It only removes the root it
// created, after child close, with a receipt bound to this exact invocation.
async function runOwnedElectron({ executable, args, output, env = process.env, onChild = () => {} }) {
  fs.mkdirSync(output, { recursive: true });
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-terminal-fixture-'));
  const result = { root, childClosed: false, directoryRemoved: false };
  let child;
  try { child = spawn(executable, args, {
    env: { ...env, PENTACLE_RUNTIME_FIXTURE_ROOT: root, PENTACLE_RUNTIME_OUTPUT: output },
    stdio: ['ignore', 'inherit', 'inherit'],
  }); } catch(error) {
    result.spawnError=error.message;result.childClosed=true;
    fs.rmSync(root,{recursive:true,force:true});result.directoryRemoved=!fs.existsSync(root);result.exitCode=2;
    fs.writeFileSync(path.join(output,'post-exit-cleanup.json'),JSON.stringify(result,null,2));return result;
  }
  onChild(child);
  await new Promise(resolve => {
    child.once('error', error => { result.spawnError = error.message; });
    child.once('close', (code, signal) => {
      result.childClosed = true;
      result.childCode = code;
      result.childSignal = signal;
      resolve();
    });
  });
  let safeToRemove = !!result.spawnError && !child.pid;
  try {
    const receipt = JSON.parse(fs.readFileSync(path.join(output, 'receipt.json'), 'utf8'));
    result.receiptMatchesRoot = receipt.fixtureRoot === root;
    safeToRemove ||= result.receiptMatchesRoot && receipt.cleanup?.serverCleaned === true;
  } catch { result.receiptMatchesRoot = false; }
  if (safeToRemove) {
    try { fs.rmSync(root, { recursive: true, force: true }); result.directoryRemoved = !fs.existsSync(root); }
    catch (error) { result.cleanupError = error.message; }
  } else result.preservedForRecovery = true;
  result.exitCode = result.spawnError || result.childSignal || !result.directoryRemoved ? 2 : result.childCode ?? 2;
  fs.writeFileSync(path.join(output, 'post-exit-cleanup.json'), JSON.stringify(result, null, 2));
  return result;
}
module.exports = { runOwnedElectron };
