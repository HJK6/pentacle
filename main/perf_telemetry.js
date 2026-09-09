'use strict';

// Cold-open performance telemetry harness.
//
// Gated on PENTACLE_PERF_TELEMETRY=1 — otherwise every call is a no-op.
// When enabled, each `record(event, details)` appends one JSON line to
// `<userData>/perf-telemetry/<startup_id>.jsonl`. The first call also
// emits a `harness:init` line carrying app/version/platform metadata.
//
// Use `scripts/analyze-cold-open.js` to summarize a captured run.

const fs = require('fs');
const path = require('path');
const os = require('os');

let _enabled = null;
let _stream = null;
let _logPath = null;
let _startedAtNs = null;
let _startupId = null;

function _isEnabled() {
  if (_enabled !== null) return _enabled;
  _enabled = process.env.PENTACLE_PERF_TELEMETRY === '1';
  return _enabled;
}

function _resolveUserDataDir() {
  // Try Electron's `app.getPath` if loaded inside a main process; fall back
  // to a platform-appropriate dir so this module is usable from scripts.
  try {
    const { app } = require('electron');
    if (app && typeof app.getPath === 'function') return app.getPath('userData');
  } catch (_) {
    // Not running inside Electron (e.g. a unit test). Fall through.
  }
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', 'Pentacle');
  }
  if (process.platform === 'win32') {
    return path.join(process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), 'Pentacle');
  }
  return path.join(os.homedir(), '.config', 'Pentacle');
}

function _ensureStream() {
  if (_stream) return _stream;
  try {
    const baseDir = path.join(_resolveUserDataDir(), 'perf-telemetry');
    fs.mkdirSync(baseDir, { recursive: true });
    _startupId = `${Date.now()}-${process.pid}`;
    _logPath = path.join(baseDir, `${_startupId}.jsonl`);
    _startedAtNs = process.hrtime.bigint();
    _stream = fs.createWriteStream(_logPath, { flags: 'a' });
    _writeRaw({
      ts_ms: 0,
      wall_ms: Date.now(),
      event: 'harness:init',
      details: {
        startup_id: _startupId,
        log_path: _logPath,
        platform: process.platform,
        node: process.versions.node,
        electron: process.versions.electron || null,
        pid: process.pid,
      },
    });
  } catch (err) {
    _enabled = false;
    if (process.env.DEBUG) {
      // Surface only when explicitly debugging; otherwise stay quiet so the
      // harness never affects app behavior.
      console.warn('[perf_telemetry] disabling — failed to open log:', err?.message || err);
    }
    return null;
  }
  return _stream;
}

function _writeRaw(record) {
  try {
    _stream.write(JSON.stringify(record) + '\n');
  } catch (err) {
    // Best-effort: swallow write errors so telemetry never breaks the app.
    if (process.env.DEBUG) console.warn('[perf_telemetry] write failed:', err?.message || err);
  }
}

function record(event, details) {
  if (!_isEnabled()) return;
  if (!_ensureStream()) return;
  const elapsedMs = Number(process.hrtime.bigint() - _startedAtNs) / 1e6;
  _writeRaw({
    ts_ms: Math.round(elapsedMs * 1000) / 1000,
    wall_ms: Date.now(),
    event: String(event || ''),
    details: details || null,
  });
}

function logPath() {
  if (!_isEnabled()) return null;
  if (!_ensureStream()) return null;
  return _logPath;
}

function isEnabled() {
  return _isEnabled();
}

module.exports = { record, logPath, isEnabled };
