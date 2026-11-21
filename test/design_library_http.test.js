'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { main } = require('../server');
const L = require('../lib/design_library.cjs');
const { revision } = require('./fixtures/design_library.cjs');

async function start(t, mode = 'token', options = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gallery-http-')), library = path.join(root, 'library');
  const r = await revision(root, options); L.publishRevision({ dir: r.dir, library });
  const profile = path.join(root, 'profile.cjs'), token = crypto.randomBytes(24).toString('hex');
  fs.writeFileSync(profile, `module.exports={appName:'DesignTest',agents:{},chatStream:{hosts:['local']},dashboards:{designLibraryRoot:${JSON.stringify(library)}}}`);
  fs.writeFileSync(path.join(root, 'token'), token);
  const auth = mode === 'token' ? ['--token-file', path.join(root, 'token')] : ['--auth', 'tailscale', '--allow-login', 'operator@example.test', '--origin', 'https://app.example.ts.net'];
  const host = await main(['--profile', profile, '--port', '0', ...auth]);
  t.after(async () => { await host.close(); fs.rmSync(root, { recursive: true, force: true }); });
  const headers = mode === 'token' ? { cookie: `pentacle_web=${L.sha256(token)}` } : { 'Tailscale-User-Login': 'operator@example.test', 'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '100.64.1.2', 'X-Forwarded-Host': 'app.example.ts.net' };
  return { root, library, r, headers, url: `http://127.0.0.1:${host.port}` };
}
for (const mode of ['token', 'tailscale']) test(`${mode} authenticates catalog, item, prepared document and media without leaking host paths`, async t => {
  const h = await start(t, mode), id = h.r.manifest.revision_id;
  for (const route of ['/api/designs', `/api/designs/example/${id}/home`, `/design-media/example/${id}/screenshots/home.png`]) {
    const denied = await fetch(h.url + route, { redirect: 'manual' }); assert.ok([401, 302].includes(denied.status));
    assert.equal((await fetch(h.url + route, { headers: h.headers })).status, 200);
  }
  const data = await fetch(h.url + '/api/designs', { headers: h.headers }).then(r => r.json());
  assert.equal(data.projects.length, 1); assert.equal(data.authority, 'Claude Design'); assert.equal(JSON.stringify(data).includes(h.root), false);
  const config = await fetch(h.url + '/api/config', { headers: h.headers }).then(r => r.json()); assert.equal(config.dashboards.designLibraryRoot, undefined);
  const item = await fetch(h.url + `/api/designs/example/${id}/home`, { headers: h.headers }).then(r => r.json()); assert.match(item.document, /connect-src 'none'/);
  const history = await fetch(h.url + `/api/designs/example/${id}`, { headers: h.headers }).then(r => r.json()); assert.equal(history.revision_id, id);
});
test('media supports byte ranges and HEAD; raw sources, traversal, stale revisions and mutations refuse', async t => {
  const h = await start(t), id = h.r.manifest.revision_id, route = `/design-media/example/${id}/screenshots/home.png`;
  const head = await fetch(h.url + route, { method: 'HEAD', headers: h.headers }); assert.equal(head.status, 200); assert.equal((await head.arrayBuffer()).byteLength, 0);
  const part = await fetch(h.url + route, { headers: { ...h.headers, range: 'bytes=0-3' } }); assert.equal(part.status, 206); assert.match(part.headers.get('content-range'), /^bytes 0-3\//); assert.equal((await part.arrayBuffer()).byteLength, 4);
  for (const range of ['bytes=999999-', 'bytes=2-1', 'bytes=-0', 'bytes=0-1,3-4']) assert.equal((await fetch(h.url + route, { headers: { ...h.headers, range } })).status, 416);
  for (const bad of [`/design-media/example/${id}/source/index.html`, `/design-media/example/${id}/screenshots/%2e%2e%2fsource/index.html`, `/api/designs/example/${'a'.repeat(64)}/home`, `/api/designs/example/${id}/missing`]) assert.equal((await fetch(h.url + bad, { headers: h.headers })).status, 404);
  const png = path.join(h.library, 'projects/example/revisions', id, 'screenshots/home.png'); fs.writeFileSync(png, 'changed'); assert.equal((await fetch(h.url + route, { headers: h.headers })).status, 404);
});
test('large media HEAD and tiny ranges avoid whole-file buffering and repeated full verification', async t => {
  const h = await start(t, 'token', { mediaBytes: 8 * 1024 * 1024 }), id = h.r.manifest.revision_id;
  const original = fs.readFileSync, reads = [];
  fs.readFileSync = function(file, ...args) { if (String(file).endsWith('videos/large.mp4')) reads.push(String(file)); return original.call(this, file, ...args); };
  t.after(() => { fs.readFileSync = original; });
  // Catalog discovery verifies a new immutable revision once; no readFileSync
  // may allocate the complete media, including on that cold verification.
  await fetch(h.url + '/api/designs', { headers: h.headers });
  const originalOpen = fs.openSync, originalRead = fs.readSync, originalClose = fs.closeSync, mediaFds = new Set(); let verificationBytes = 0;
  fs.openSync = function(file, ...args) { const fd = originalOpen.call(this, file, ...args); if (String(file).endsWith('videos/large.mp4')) mediaFds.add(fd); return fd; };
  fs.readSync = function(fd, ...args) { const size = originalRead.call(this, fd, ...args); if (mediaFds.has(fd)) verificationBytes += size; return size; };
  fs.closeSync = function(fd) { mediaFds.delete(fd); return originalClose.call(this, fd); };
  t.after(() => { fs.openSync = originalOpen; fs.readSync = originalRead; fs.closeSync = originalClose; });
  const route = h.url + `/design-media/example/${id}/videos/large.mp4`;
  const head = await fetch(route, { method: 'HEAD', headers: h.headers }); assert.equal(head.status, 200); assert.equal(Number(head.headers.get('content-length')), 8 * 1024 * 1024);
  const range = await fetch(route, { headers: { ...h.headers, range: 'bytes=0-3' } }); assert.equal(range.status, 206); assert.deepEqual(Buffer.from(await range.arrayBuffer()), Buffer.alloc(4, 42));
  assert.deepEqual(reads, [], 'large media must use bounded hashing and ranged file streams, including cold discovery');
  assert.equal(verificationBytes, 0, 'unchanged, previously verified media must not be fully rehashed for HEAD or seeking');
});

test('catalog resolves boards, tracks selected lineage and revalidates with an authenticated ETag', async t => {
  function configure(color, sourceRevision) {
    return (descriptor, payload) => {
      const view = descriptor.views[0];
      fs.writeFileSync(path.join(payload, 'index.html'), '<link rel="stylesheet" href="color.css"><h1>A</h1>');
      fs.writeFileSync(path.join(payload, 'color.css'), `body{color:${color}}`);
      fs.writeFileSync(path.join(payload, 'b.html'), '<h1>B</h1>');
      descriptor.source_revision = sourceRevision;
      descriptor.files = L.inventory(payload);
      descriptor.views.push({ ...view, id: 'second', title: 'Second', entry: 'b.html' });
      descriptor.designs = [
        { id: 'design-a', title: 'A', nodes: [{ type: 'screen', view: 'home' }] },
        { id: 'design-b', title: 'B', nodes: [{ type: 'screen', view: 'second', x: 10, y: 20 }] },
      ];
    };
  }
  const dates = ['2026-01-01T10:00:00Z', '2026-01-02T10:00:00Z', '2026-01-03T10:00:00Z'];
  const h = await start(t, 'token', { configure: configure('red', 'a'), importedAt: dates[0], legacyPreparation: true });
  const url = h.url + '/api/designs';
  const get = tag => fetch(url, { headers: tag ? { ...h.headers, 'If-None-Match': tag } : h.headers });
  const initial = await get();
  const originalTag = initial.headers.get('etag');
  assert.match(originalTag, /^"[a-f0-9]{64}"$/);
  const original = (await initial.json()).projects[0];
  assert.deepEqual(original.designs.map(({ id, changed_at }) => [id, changed_at]), [['design-a', dates[0]], ['design-b', dates[0]]]);
  assert.deepEqual(original.designs[1].nodes[0], {
    type: 'screen', view: 'second', x: 10, y: 20, title: 'Second', w: 800, h: 600,
  });
  const unchanged = await get('W/' + originalTag);
  assert.equal(unchanged.status, 304);
  assert.equal(await unchanged.text(), '');
  assert.equal((await fetch(url, { headers: { 'If-None-Match': originalTag }, redirect: 'manual' })).status, 401);

  const b = await revision(path.join(h.root, 'b'), { configure: configure('green', 'b'), importedAt: dates[1] });
  L.publishRevision({ dir: b.dir, library: h.library });
  const afterPublish = await get(originalTag);
  assert.equal(afterPublish.status, 200);
  const nextTag = afterPublish.headers.get('etag');
  assert.notEqual(nextTag, originalTag);
  assert.deepEqual((await afterPublish.json()).projects[0].designs.map(design => design.changed_at), [dates[1], dates[0]]);

  const c = await revision(path.join(h.root, 'c'), { configure: configure('green', 'c'), importedAt: dates[2] });
  L.publishRevision({ dir: c.dir, library: h.library });
  const afterThird = await get(nextTag);
  assert.equal(afterThird.status, 200);
  assert.deepEqual((await afterThird.json()).projects[0].designs.map(design => design.changed_at), [dates[1], dates[0]]);
  L.rollback({ library: h.library, project: 'example', revision: h.r.manifest.revision_id });
  const afterRollback = await get(afterThird.headers.get('etag'));
  assert.equal(afterRollback.status, 200);
  const restored = (await afterRollback.json()).projects[0];
  assert.equal(restored.revision_id, original.revision_id);
  assert.deepEqual(restored.designs.map(design => design.changed_at), [dates[0], dates[0]]);
});
