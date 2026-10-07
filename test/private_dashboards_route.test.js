'use strict';

// ── /dashboards/private/<catalog_version>/<path> ────────────────────────────
// Auth before any filesystem access, per-version allowlist from that version's
// catalog.json, realpath containment, sha256 match, versions served side by
// side and picked up without a host restart, and `catalogRoot` never sent to
// the browser. Synthetic catalogs only (fixture F-G shape).

const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { main } = require('../server/index.js');
const { createPrivateDashboardRoute } = require('../server/private_dashboards.js');

const N = '0.2.0+aaaaaaa';
const N1 = '0.2.1+bbbbbbb';
const sha = (text) => crypto.createHash('sha256').update(text).digest('hex');

function installVersion(root, version, files) {
  const dir = path.join(root, version);
  fs.mkdirSync(path.join(dir, 'web'), { recursive: true });
  for (const [rel, text] of Object.entries(files)) fs.writeFileSync(path.join(dir, rel), text);
  const catalog = {
    schema_version: 1, catalog_version: version,
    package: { repo: 'example/dashboards', commit: '1'.repeat(40) }, requires: { host_api: 1 },
    libs: [{ path: 'web/example-lib.js', sha256: sha(files['web/example-lib.js']) }],
    boards: [{ id: 'example-board', name: 'Example', kind: 'web-adapter',
      web: { script: 'web/example-board.js', sha256: sha(files['web/example-board.js']),
        css: 'web/example-board.css', css_sha256: sha(files['web/example-board.css']) } }],
  };
  fs.writeFileSync(path.join(dir, 'catalog.json'), JSON.stringify(catalog));
  return dir;
}

function versionFiles(tag) {
  return {
    'web/example-lib.js': `window.exampleLib='${tag}';\n`,
    'web/example-board.js': `window.DASHBOARDS.push({id:'example-board',tag:'${tag}'});\n`,
    'web/example-board.css': `.example-board{--tag:'${tag}'}\n`,
  };
}

function setup() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-dashroute-'));
  const home = path.join(dir, 'home');
  const root = path.join(dir, 'catalogs');
  fs.mkdirSync(home);
  fs.mkdirSync(root);
  fs.writeFileSync(path.join(dir, 'outside.js'), 'window.escaped=true;\n');
  fs.writeFileSync(path.join(dir, 'token'), 'fixture-web-token\n');
  const profile = path.join(dir, 'profile.js');
  fs.writeFileSync(profile, `'use strict';
module.exports = {
  appName: 'PentacleRouteTest',
  agents: {},
  chatStream: { url: 'ws://127.0.0.1:1', hosts: ['local'] },
  dashboards: { catalogSpecId: 'example__dashboard_catalog', catalogRoot: ${JSON.stringify(root)} },
};
`);
  return { dir, home, root, profile };
}

async function startHost(t, env) {
  const realHome = process.env.HOME;
  process.env.HOME = env.home;
  const host = await main(['--profile', env.profile, '--port', '0', '--token-file', path.join(env.dir, 'token')]);
  t.after(async () => {
    try { await host.close(); } catch {}
    if (realHome === undefined) delete process.env.HOME; else process.env.HOME = realHome;
    fs.rmSync(env.dir, { recursive: true, force: true });
  });
  const login = await fetch(`${host.url}/login`, { method: 'POST', redirect: 'manual',
    headers: { 'content-type': 'application/x-www-form-urlencoded' }, body: 'token=fixture-web-token' });
  const cookie = login.headers.get('set-cookie').split(';')[0];
  const get = (p, authed = true) => fetch(`${host.url}${p}`, { redirect: 'manual', headers: authed ? { cookie } : {} });
  return { host, get };
}

test('unauthenticated requests are refused before any catalog filesystem access', async (t) => {
  const env = setup();
  installVersion(env.root, N, versionFiles('n'));
  const { get } = await startHost(t, env);
  const touched = [];
  const spies = ['realpathSync', 'readFileSync', 'existsSync', 'statSync'].map((name) => {
    const original = fs[name];
    fs[name] = function spy(p, ...rest) {
      if (String(p).startsWith(env.root)) touched.push(`${name}:${p}`);
      return original.call(this, p, ...rest);
    };
    return () => { fs[name] = original; };
  });
  try {
    const res = await get(`/dashboards/private/${N}/web/example-board.js`, false);
    assert.equal(res.status, 302);
    assert.equal(res.headers.get('location'), '/login');
    const api = await get(`/dashboards/private/${N}/web/example-board.js`, false);
    assert.notEqual(api.status, 200);
    assert.deepEqual(touched, []);
    // The spy is live: an authenticated request does read the catalog root.
    assert.equal((await get(`/dashboards/private/${N}/web/example-board.js`)).status, 200);
    assert.ok(touched.length > 0);
  } finally {
    spies.forEach((restore) => restore());
  }
});

test('versions N and N+1 serve libs, css and scripts side by side without a restart', async (t) => {
  const env = setup();
  const filesN = versionFiles('n');
  installVersion(env.root, N, filesN);
  const { get } = await startHost(t, env);
  for (const rel of Object.keys(filesN)) {
    const res = await get(`/dashboards/private/${N}/${rel}`);
    assert.equal(res.status, 200, rel);
    assert.equal(await res.text(), filesN[rel]);
    assert.equal(res.headers.get('cache-control'), 'no-store');
    assert.match(res.headers.get('content-type'), rel.endsWith('.css') ? /text\/css/ : /text\/javascript/);
  }
  // Not installed yet → 404; installed afterwards → served by the same process.
  assert.equal((await get(`/dashboards/private/${N1}/web/example-board.js`)).status, 404);
  const filesN1 = versionFiles('n1');
  installVersion(env.root, N1, filesN1);
  for (const rel of Object.keys(filesN1)) {
    const res = await get(`/dashboards/private/${N1}/${rel}`);
    assert.equal(res.status, 200, rel);
    assert.equal(await res.text(), filesN1[rel]);
  }
  assert.equal(await (await get(`/dashboards/private/${N}/web/example-board.js`)).text(), filesN['web/example-board.js']);
});

test('unlisted, traversal, other-extension and malformed paths are 404', async (t) => {
  const env = setup();
  installVersion(env.root, N, versionFiles('n'));
  fs.writeFileSync(path.join(env.root, N, 'web', 'unlisted.js'), 'x\n');
  const { get } = await startHost(t, env);
  for (const p of [
    `/dashboards/private/${N}/web/unlisted.js`,
    `/dashboards/private/${N}/catalog.json`,
    `/dashboards/private/${N}/web/../catalog.json`,
    `/dashboards/private/${N}/web/%2e%2e/catalog.json`,
    `/dashboards/private/${N}/web/`,
    `/dashboards/private/${N}/`,
    `/dashboards/private/..%2f${N}/web/example-board.js`,
    '/dashboards/private/../web/example-board.js',
    `/dashboards/private/${N}/web/example-board.js.map`,
    `/dashboards/private/${N}/WEB/example-board.js`,
    '/dashboards/private/',
  ]) {
    const res = await get(p);
    assert.equal(res.status, 404, p);
  }
});

test('hash mismatch and symlink escapes are 404', async (t) => {
  const env = setup();
  installVersion(env.root, N, versionFiles('n'));
  installVersion(env.root, N1, versionFiles('n1'));
  // N+1 bytes under N's listing (the F-G cross-version case).
  fs.copyFileSync(path.join(env.root, N1, 'web', 'example-board.js'), path.join(env.root, N, 'web', 'example-board.js'));
  // A listed file that is a symlink out of the version directory.
  const lib = path.join(env.root, N1, 'web', 'example-lib.js');
  const outside = path.join(env.dir, 'outside.js');
  fs.writeFileSync(outside, fs.readFileSync(lib));
  fs.rmSync(lib);
  fs.symlinkSync(outside, lib);
  // A version directory that is a symlink out of the catalog root.
  const elsewhere = path.join(env.dir, 'elsewhere');
  fs.mkdirSync(elsewhere);
  installVersion(elsewhere, '0.3.0', versionFiles('x'));
  fs.symlinkSync(path.join(elsewhere, '0.3.0'), path.join(env.root, '0.3.0'));
  const { get } = await startHost(t, env);
  assert.equal((await get(`/dashboards/private/${N}/web/example-board.js`)).status, 404);
  assert.equal((await get(`/dashboards/private/${N}/web/example-lib.js`)).status, 200);
  assert.equal((await get(`/dashboards/private/${N1}/web/example-lib.js`)).status, 404);
  assert.equal((await get(`/dashboards/private/${N1}/web/example-board.js`)).status, 200);
  assert.equal((await get('/dashboards/private/0.3.0/web/example-board.js')).status, 404);
});

test('a catalog.json naming another version serves nothing for that directory', async (t) => {
  const env = setup();
  const dir = installVersion(env.root, N, versionFiles('n'));
  fs.renameSync(dir, path.join(env.root, '0.9.9'));
  const { get } = await startHost(t, env);
  assert.equal((await get('/dashboards/private/0.9.9/web/example-board.js')).status, 404);
});

test('publicConfig carries catalogSpecId but never catalogRoot', async (t) => {
  const env = setup();
  const { get } = await startHost(t, env);
  const config = await (await get('/api/config')).json();
  assert.equal(config.dashboards.catalogSpecId, 'example__dashboard_catalog');
  assert.equal('catalogRoot' in config.dashboards, false);
  assert.equal(JSON.stringify(config).includes(env.root), false);
});

test('without a catalogRoot the route answers 404 and touches nothing', () => {
  const route = createPrivateDashboardRoute({});
  let status = 0;
  const res = { writeHead(code) { status = code; return this; }, end() {} };
  assert.equal(route.handle({ method: 'GET' }, res, `/dashboards/private/${N}/web/example-board.js`), true);
  assert.equal(status, 404);
  assert.equal(route.handle({ method: 'GET' }, res, '/other'), false);
});
