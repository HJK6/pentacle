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
module.exports = { withRuntimeDirectory };
