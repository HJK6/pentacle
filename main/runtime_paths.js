'use strict';

const os = require('node:os');
const path = require('node:path');

// Resolve at use time: in-process test hosts may install their override after import.
// An unset/empty override preserves the historic ~/.pentacle directory.
function runtimeDirectory(env = process.env, home = os.homedir()) {
  return env.PENTACLE_RUNTIME_DIR || path.join(home, '.pentacle');
}

function desktopRuntimePath(env = process.env, home = os.homedir()) {
  return path.join(runtimeDirectory(env, home), 'desktop-runtime.json');
}

module.exports = { runtimeDirectory, desktopRuntimePath };
