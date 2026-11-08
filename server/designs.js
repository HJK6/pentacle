'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { createLibraryReader, contained, fileState, ID, HASH } = require('../lib/design_library.cjs');

function publicItem(item, kind) {
  const out = { id: item.id, title: item.title, kind, entry: item.entry, surface: item.surface || item.screen || '', variant: item.variant || '', group: item.group || '', viewport: item.viewport, approval_ref: item.approval_ref || null, compatibility: item.compatibility, generated: item.generated || null, screenshots: item.screenshots || [], video: item.video || null, prototype: !!item.prepared };
  if (kind === 'flow') out.steps = item.steps.map(s => ({ name: s.name, action: s.action }));
  return out;
}
function publicManifest(m) {
  return { project_id: m.project_id, title: m.title, revision_id: m.revision_id, source_revision: m.source_revision, source_kind: m.provenance.source_kind, source_url: m.provenance.source_url || null, imported_at: m.imported_at, complete: m.complete, missing_paths: m.missing_paths, authority: 'Claude Design', status: 'Mirror / local candidates', views: m.views.map(v => publicItem(v, 'view')), flows: m.flows.map(f => publicItem(f, 'flow')) };
}
function createDesignRoutes({ libraryRoot } = {}) {
  const reader = createLibraryReader(libraryRoot);
  const json = (res, status, value, head = false) => {
    const body = Buffer.from(JSON.stringify(value));
    res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': body.length, 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' }).end(head ? undefined : body);
  };
  function handle(req, res, urlPath) {
    if (urlPath !== '/api/designs' && !urlPath.startsWith('/api/designs/') && !urlPath.startsWith('/design-media/')) return false;
    const head = req.method === 'HEAD';
    if (!['GET', 'HEAD'].includes(req.method)) { json(res, 405, { error: 'method not allowed' }, head); return true; }
    try {
      if (urlPath === '/api/designs') {
        const catalog = reader.catalog();
        json(res, 200, { ...catalog, projects: catalog.projects.map(p => ({ ...p, manifest: p.manifest ? publicManifest(p.manifest) : null })) }, head); return true;
      }
      const isMedia = urlPath.startsWith('/design-media/');
      const rest = urlPath.slice(isMedia ? '/design-media/'.length : '/api/designs/'.length);
      const [project, revision, ...parts] = rest.split('/');
      if (!ID.test(project) || !HASH.test(revision)) throw new Error('invalid route');
      const loaded = reader.revision(project, revision), manifest = loaded.manifest;
      if (!isMedia) {
        if (!parts.length) { json(res, 200, publicManifest(manifest), head); return true; }
        const id = parts[0]; if (parts.length !== 1 || !ID.test(id)) throw new Error('invalid item');
        const item = [...manifest.views, ...manifest.flows].find(v => v.id === id);
        if (!item) throw new Error('unknown item');
        const value = { item: publicItem(item, manifest.views.includes(item) ? 'view' : 'flow') };
        const prepared = item.prepared || manifest.views.find(v => v.entry === item.entry)?.prepared;
        if (prepared) value.document = fs.readFileSync(contained(loaded.dir, prepared), 'utf8');
        json(res, 200, value, head); return true;
      }
      // URL strings are never filesystem parameters until the exact manifest
      // allowlist has admitted them. Percent encodings are deliberately refused.
      const asset = parts.join('/');
      const allow = new Set([...manifest.views, ...manifest.flows].flatMap(i => [...(i.screenshots || []).map(s => s.path), ...(i.video ? [i.video] : [])]));
      if (!allow.has(asset)) throw new Error('unknown media');
      const ext = path.extname(asset).toLowerCase();
      const type = { '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.mp4': 'video/mp4' }[ext];
      if (!type) throw new Error('unsupported media');
      const file = contained(loaded.dir, asset), size = manifest.files.find(f => f.path === asset)?.size;
      const headers = { 'content-type': type, 'cache-control': 'private, no-store', 'x-content-type-options': 'nosniff', 'accept-ranges': 'bytes' };
      const range = req.headers.range;
      let start = 0, end = size - 1;
      if (range) {
        const match = /^bytes=(\d*)-(\d*)$/.exec(range);
        if (!match || (!match[1] && !match[2])) { res.writeHead(416, { ...headers, 'content-range': `bytes */${size}` }).end(); return true; }
        if (match[1]) { start = Number(match[1]); end = match[2] ? Math.min(Number(match[2]), end) : end; }
        else start = Math.max(0, size - Number(match[2]));
        if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start > end || start >= size || Number(match[2]) === 0 && !match[1]) { res.writeHead(416, { ...headers, 'content-range': `bytes */${size}` }).end(); return true; }
        headers['content-range'] = `bytes ${start}-${end}/${size}`;
      }
      headers['content-length'] = end - start + 1;
      const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
      if (fileState(fs.fstatSync(fd, { bigint: true })) !== loaded.fileStates.get(asset)) { fs.closeSync(fd); throw new Error('media changed'); }
      res.writeHead(range ? 206 : 200, headers);
      if (head || size === 0) { fs.closeSync(fd); res.end(); }
      else {
        const stream = fs.createReadStream(file, { fd, autoClose: true, start, end, highWaterMark: 64 * 1024 });
        stream.on('error', () => res.destroy()); res.on('close', () => stream.destroy()); stream.pipe(res);
      }
    } catch { json(res, 404, { error: 'design unavailable or still syncing' }, head); }
    return true;
  }
  return { handle, reader };
}
module.exports = { createDesignRoutes, publicItem, publicManifest };
