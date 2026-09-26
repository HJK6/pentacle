'use strict';
const { resolveMicUrl } = require('./mic-url');

const GET_PATHS = new Set(['/status', '/calibration', '/logs', '/transcripts', '/transcript', '/wake/last-claim']);
const POST_PATHS = new Set(['/mode/on', '/mode/off', '/mode/clipboard', '/mode/meeting', '/copy/start', '/copy/stop', '/wake/claim', '/actions/outcome', '/calibrate/start', '/calibrate/stop', '/audio/keep']);

// The authenticated web host controls its configured microphone. Browser input
// selects an existing operation, never a destination or an arbitrary URL.
function createMicRequest(config, fetchStatus = fetch) {
  return async function micRequest(method, path, body) {
    if (!config?.features?.mic) return { ok: false, status: 503, error: 'Microphone is disabled.' };
    const allowed = method === 'GET'
      ? GET_PATHS.has(path) || /^\/(transcript|clipboard)\/since\/\d+$/.test(path)
      : method === 'POST' && POST_PATHS.has(path);
    if (typeof path !== 'string' || !allowed) return { ok: false, status: 400, error: 'Unsupported microphone operation.' };
    try {
      const options = { method, signal: AbortSignal.timeout(path === '/copy/stop' ? 50000 : 10000) };
      if (body !== undefined) {
        if (method !== 'POST' || !body || typeof body !== 'object' || Array.isArray(body))
          return { ok: false, status: 400, error: 'Invalid microphone request.' };
        options.body = JSON.stringify(body);
        if (Buffer.byteLength(options.body) > 8192) return { ok: false, status: 413, error: 'Microphone request is too large.' };
        options.headers = { 'Content-Type': 'application/json' };
      }
      const response = await fetchStatus(resolveMicUrl(config) + path, options);
      const data = await response.json();
      return response.ok ? data : { ...data, ok: false, status: response.status };
    } catch {
      return null; // Existing renderer offline/recovery path.
    }
  };
}
module.exports = { createMicRequest };
