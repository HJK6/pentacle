'use strict';

// ── Private dashboard files ─────────────────────────────────────────────────
// GET /dashboards/private/<catalog_version>/<path>
//
// `dashboards.catalogRoot` (host profile, never sent to the browser) names a
// parent directory of immutable version directories, each holding the built
// catalog of one private dashboard release:
//
//   <catalogRoot>/<catalog_version>/catalog.json
//   <catalogRoot>/<catalog_version>/web/<file>.js|css
//
// The caller authenticates first (server/index.js gates every path). A file is
// served only when it is listed in that version's catalog.json (libs[].path,
// web.script, web.css), its realpath is beneath the realpath of the version
// directory, and its bytes match the listed sha256. Everything else is a 404:
// no directory listing, no other extension, no general filesystem route.
// Version directories are read lazily, so installing a new version needs no
// host restart, and old and new versions are served side by side.

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const ROUTE_PREFIX = '/dashboards/private/';
const VERSION_RE = /^[0-9A-Za-z.+-]{1,64}$/;
const FILE_RE = /^web\/[a-z0-9][a-z0-9._-]{0,80}\.(js|css)$/;
const SHA256_RE = /^[0-9a-f]{64}$/;
const MAX_CATALOG_BYTES = 1024 * 1024;
const FILE_TYPES = { '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };

function sha256(buffer) {
  return crypto.createHash('sha256').update(buffer).digest('hex');
}

// path → sha256 for every file a catalog version lists; null if unusable.
function catalogAllowlist(catalog, version) {
  if (!catalog || typeof catalog !== 'object' || catalog.schema_version !== 1) return null;
  if (catalog.catalog_version !== version) return null;
  const allow = new Map();
  const add = (file, hash) => {
    if (typeof file === 'string' && FILE_RE.test(file) && typeof hash === 'string' && SHA256_RE.test(hash)) {
      allow.set(file, hash);
    }
  };
  for (const lib of Array.isArray(catalog.libs) ? catalog.libs : []) add(lib && lib.path, lib && lib.sha256);
  for (const board of Array.isArray(catalog.boards) ? catalog.boards : []) {
    const web = board && board.kind === 'web-adapter' ? board.web : null;
    if (!web) continue;
    add(web.script, web.sha256);
    if (web.css) add(web.css, web.css_sha256);
  }
  return allow;
}

function createPrivateDashboardRoute({ catalogRoot, log = () => {} } = {}) {
  const root = catalogRoot ? path.resolve(String(catalogRoot)) : null;
  // Positive results only: a version directory installed after a miss is
  // picked up on the next request without a restart.
  const versions = new Map();

  function loadVersion(version) {
    if (versions.has(version)) return versions.get(version);
    let rootReal;
    let dirReal;
    try {
      rootReal = fs.realpathSync(root);
      dirReal = fs.realpathSync(path.join(root, version));
    } catch {
      return null;
    }
    if (path.dirname(dirReal) !== rootReal) return null;  // a symlinked version dir may not escape
    let catalog;
    try {
      const raw = fs.readFileSync(path.join(dirReal, 'catalog.json'));
      if (raw.length > MAX_CATALOG_BYTES) return null;
      catalog = JSON.parse(raw.toString('utf8'));
    } catch {
      return null;
    }
    const allow = catalogAllowlist(catalog, version);
    if (!allow) return null;
    const entry = { dirReal, allow };
    versions.set(version, entry);
    return entry;
  }

  function notFound(res) {
    res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' }).end('not found');
  }

  // Returns true when the request belonged to this route (answered either way).
  function handle(req, res, urlPath) {
    if (!urlPath.startsWith(ROUTE_PREFIX)) return false;
    if (!root || (req.method !== 'GET' && req.method !== 'HEAD')) { notFound(res); return true; }
    const rest = urlPath.slice(ROUTE_PREFIX.length);
    const slash = rest.indexOf('/');
    const version = slash > 0 ? rest.slice(0, slash) : '';
    const file = slash > 0 ? rest.slice(slash + 1) : '';
    if (!VERSION_RE.test(version) || /^\.+$/.test(version) || !FILE_RE.test(file)) { notFound(res); return true; }
    const entry = loadVersion(version);
    const expected = entry && entry.allow.get(file);
    if (!expected) { notFound(res); return true; }
    let body;
    try {
      const fileReal = fs.realpathSync(path.join(entry.dirReal, file));
      if (!fileReal.startsWith(entry.dirReal + path.sep)) { notFound(res); return true; }
      body = fs.readFileSync(fileReal);
    } catch {
      notFound(res);
      return true;
    }
    if (sha256(body) !== expected) {
      log(`[web] private dashboard file hash mismatch: ${version}/${file}`);
      notFound(res);
      return true;
    }
    res.writeHead(200, {
      'content-type': FILE_TYPES[path.extname(file)],
      'cache-control': 'no-store',
      'x-content-type-options': 'nosniff',
    });
    res.end(req.method === 'HEAD' ? undefined : body);
    return true;
  }

  return { handle, enabled: Boolean(root) };
}

module.exports = { createPrivateDashboardRoute, catalogAllowlist, ROUTE_PREFIX };
