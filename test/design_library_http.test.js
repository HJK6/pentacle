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

async function start(t, mode = 'token') {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gallery-http-')), library = path.join(root, 'library');
  const r = await revision(root); L.publishRevision({ dir: r.dir, library });
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
