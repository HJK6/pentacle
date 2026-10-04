'use strict';

// Preserve the daemon's stable refusal code through both Electron and web IPC.
// Do not expose free-form server/transport exception text in this file boundary.
async function fetchBlobReply(client, blobSha) {
  try { return { ok: true, ...await client.fetchBlob({ blobSha }) }; }
  catch (error) {
    const code = error?.error_code || error?.code;
    const error_code = ['blob_unknown', 'blob_forbidden', 'authentication_required', 'scope_denied'].includes(code) ? code : 'fetch_failed';
    return { ok: false, error_code, error: 'File fetch failed' };
  }
}
module.exports = { fetchBlobReply };
