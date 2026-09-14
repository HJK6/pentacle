'use strict';
const path = require('node:path');
const { execFile } = require('node:child_process');

// Host-owned configuration only. No command or argument is accepted from RPC.
function createMicStarter(config, deps = {}) {
  const execute = deps.execFile || execFile;
  let pending = null;
  return function startMic() {
    if (!config?.features?.mic || config.mic?.useStreamHost)
      return Promise.resolve({ ok: false, error: 'Local microphone recovery is unavailable for this profile.' });
    const command = config.mic?.startCommand;
    if (!command || typeof command.file !== 'string' || !path.isAbsolute(command.file)
      || !Array.isArray(command.args) || !command.args.every(a => typeof a === 'string'))
      return Promise.resolve({ ok: false, error: 'Microphone recovery is not configured on this host.' });
    if (pending) return pending;
    pending = new Promise(resolve => {
      execute(command.file, [...command.args], { shell: false, windowsHide: true, timeout: 75000, killSignal: 'SIGKILL', maxBuffer: 16384 }, (error, stdout) => {
        if (error) return resolve({ ok: false, error: error.killed
          ? 'Microphone startup timed out. Check the microphone connection and retry.'
          : 'Microphone could not start. Check the local service log and microphone connection.' });
        try {
          const proof = JSON.parse(String(stdout).trim());
          resolve(proof.ok === true && proof.ready === true ? { ok: true }
            : { ok: false, error: 'Microphone service did not confirm readiness.' });
        } catch { resolve({ ok: false, error: 'Microphone service did not confirm readiness.' }); }
      });
    }).catch(() => ({ ok: false, error: 'Microphone recovery could not run on this host.' }))
      .finally(() => { pending = null; });
    return pending;
  };
}
function micStartSameOrigin(req) {
  try {
    const origin = new URL(req.headers.origin);
    const expected = new URL(`${req.socket?.encrypted ? 'https' : 'http'}://${req.headers.host}`);
    return origin.origin === expected.origin && origin.pathname === '/' && !origin.search && !origin.hash && !origin.username && !origin.password;
  } catch { return false; }
}
module.exports = { createMicStarter, micStartSameOrigin };
