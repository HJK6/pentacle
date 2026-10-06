'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

// Separate from --keep evidence: the live runtime marker never escapes this scope.
async function withRuntimeDirectory(run, { env = process.env, filesystem = fs, tempRoot = os.tmpdir() } = {}) {
  const directory = filesystem.mkdtempSync(path.join(tempRoot, 'pentacle-web-runtime-'));
  const prior = env.PENTACLE_RUNTIME_DIR;
  const hadPrior = Object.prototype.hasOwnProperty.call(env, 'PENTACLE_RUNTIME_DIR');
  env.PENTACLE_RUNTIME_DIR = directory;
  let cleanupAttempted = false;
  const cleanup = () => {
    if (cleanupAttempted) return;
    cleanupAttempted = true;
    filesystem.rmSync(directory, { recursive: true, force: true });
  };
  try {
    return await run(directory, cleanup);
  } finally {
    if (hadPrior) env.PENTACLE_RUNTIME_DIR = prior;
    else delete env.PENTACLE_RUNTIME_DIR;
    cleanup();
  }
}
// A child gate that starts its own in-process host gets its own owned runtime directory, so its
// handshake cannot write the marker to the caller's (or the real default) location.
function execWithRuntimeDirectory(exec, command, args, options = {}, scope = {}) {
  return withRuntimeDirectory(async directory => exec(command, args,
    { ...options, env: { ...(options.env || process.env), PENTACLE_RUNTIME_DIR: directory } }), scope);
}
module.exports = { withRuntimeDirectory, execWithRuntimeDirectory };
