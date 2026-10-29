'use strict';
const path = require('node:path');
const store = require('../main/review_store');
const TYPE = 'application/json; charset=utf-8';
function reply(res, status, value) {
  res.writeHead(status, { 'content-type': TYPE, 'cache-control': 'no-store' }).end(JSON.stringify(value));
}
function hasDuplicateKeys(text) {
  const seen = new Set();
  for (let i = 0; i < text.length; i++) {
    if (text[i] !== '"') continue;
    const start = i++;
    while (i < text.length) {
      if (text[i] === '\\') { i += 2; continue; }
      if (text[i] === '"') break;
      i++;
    }
    if (i >= text.length) return true;
    let after = i + 1;
    while (/\s/.test(text[after] || '')) after++;
    if (text[after] !== ':') continue;
    let key;
    try { key = JSON.parse(text.slice(start, i + 1)); } catch { return true; }
    if (seen.has(key)) return true;
    seen.add(key);
  }
  return false;
}
function body(req, limit) {
  return new Promise((resolve, reject) => {
    const chunks = []; let size = 0; let exceeded = false;
    req.on('data', chunk => {
      size += chunk.length;
      if (size > limit) exceeded = true;
      else chunks.push(chunk);
    });
    req.on('error', () => reject(new store.ReviewError('malformed_body', 'malformed_body', 400, 2)));
    req.on('end', () => {
      if (exceeded) { reject(new store.ReviewError('body_too_large', 'body_too_large', 400, 2)); return; }
      let text, parsed;
      try {
        text = new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks));
        if (hasDuplicateKeys(text)) throw new Error('duplicate');
        parsed = JSON.parse(text);
      } catch { reject(new store.ReviewError('malformed_body', 'malformed_body', 400, 2)); return; }
      resolve(parsed);
    });
  });
}
function localOriginAllowed(req, singleHeader, headerValues) {
  const origins = headerValues(req, 'origin');
  const host = singleHeader(req, 'host');
  if (origins.length !== 1 || !host) return false;
  let target;
  try { target = new URL(`http://${host}`); } catch { return false; }
  if (target.username || target.password || target.pathname !== '/' || target.search || target.hash) return false;
  if (Number(target.port || 80) !== req.socket?.localPort) return false;
  return origins[0] === target.origin;
}
function makeReviewRoutes({ config, auth, singleHeader, headerValues, broadcast }) {
  return async function reviewRoute(req, res, urlPath) {
    const ack = urlPath === '/api/review-inbox/ack';
    const cleanup = urlPath === '/api/review-inbox/smoke-cleanup';
    if (!ack && !cleanup) return false;
    if (req.method !== 'POST') { reply(res, 405, { ok: false, code: 'method_not_allowed' }); return true; }
    if (!(auth?.mode === 'tailscale' ? auth.originAllowed(req) : localOriginAllowed(req, singleHeader, headerValues))) {
      reply(res, 403, { ok: false, code: 'origin_refused' }); return true;
    }
    if (singleHeader(req, 'content-type') !== 'application/json') {
      reply(res, 415, { ok: false, code: 'unsupported_type' }); return true;
    }
    if (!path.isAbsolute(config.reviewInbox?.dir || '')) {
      reply(res, 503, { ok: false, code: 'store_unreadable' }); return true;
    }
    try {
      const request = await body(req, ack ? 512 : 256);
      const result = ack ? store.ackItem(config.reviewInbox.dir, request) : store.smokeCleanup(config.reviewInbox.dir, request);
      if (ack && result.result.changed && !result.replayed) broadcast?.('review-inbox:changed', { id: request.id, seq: result.current.seq });
      if (cleanup) broadcast?.('review-inbox:changed', { id: request.id, cleaned: true });
      reply(res, 200, result);
    } catch (e) {
      reply(res, e instanceof store.ReviewError ? e.status : 503,
        { ok: false, code: e instanceof store.ReviewError ? e.code : 'store_unreadable',
          ...(e?.code === 'capacity' ? { warning: 'Inbox nearing capacity' } : {}) });
    }
    return true;
  };
}
module.exports = { makeReviewRoutes, localOriginAllowed, hasDuplicateKeys };
