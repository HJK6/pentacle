'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

const WORKSPACE = '/next/design';
const EMBED = '/next/embed/design';
const TARGETS = [
  ['', null],
  ['/project-1/board', { project_id: 'project-1', design_id: 'board' }],
  ['/project-1/board/screen', { project_id: 'project-1', design_id: 'board', view_id: 'screen' }],
];
const INVALID_SUFFIXES = [
  '/', '/project-1', '//board', '/project-1/', '/project-1/board/',
  '/project-1/board/screen/extra', '/project-1/Board', '/project_1/board', '/-project/board',
  `/project-1/${'a'.repeat(81)}`, '/project-1/board/%73creen', '/project-1%2fboard',
  '?', '?view=screen', '/project-1/board/screen?view=other',
  '/project-1/../project-1/board', '/%2e%2e', '/..', '\\..', '/../../../login',
];
const IDENTITY = {
  'tailscale-user-login': 'tester@example.test',
  'x-forwarded-proto': 'https',
  'x-forwarded-for': 'fd7a:115c:a1e0::1234:5678',
};
const ASSETS = {
  '/next/xterm.css': '/* fixture xterm stylesheet */',
  '/next/style.css': '/* fixture Next stylesheet */',
  '/next/melody.css': '/* fixture Melody stylesheet */',
  '/next/melody_instrument.css': '/* fixture instrument stylesheet */',
  '/next/chat_core.bundle.js': '/* fixture chat core */',
  '/next/bundle.js': '/* fixture Next bundle */',
  '/next/design-embed.bundle.js': '/* fixture standalone bundle */',
};

async function startHost(t, mode) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'next-design-routes-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const profile = path.join(dir, 'profile.js');
  fs.writeFileSync(profile, "module.exports={appName:'DesignRouteFixture',dashboards:{designLibraryRoot:__dirname}};\n");
  const dist = path.join(dir, 'dist');
  fs.mkdirSync(path.join(dist, 'next'), { recursive: true });
  for (const page of ['index.html', 'design-embed.html']) {
    fs.copyFileSync(path.join(__dirname, '../renderer/next', page), path.join(dist, 'next', page));
  }
  for (const [asset, body] of Object.entries(ASSETS)) fs.writeFileSync(path.join(dist, asset), body);

  // Use the real startup snapshot with fixture files, without a browser build.
  const buildId = require('../server/web_build_id');
  const freeze = buildId.freezeWebDist;
  const mock = t.mock.method(buildId, 'freezeWebDist', () => freeze(dist));
  const serverPath = require.resolve('../server/index');
  delete require.cache[serverPath];
  const { main } = require(serverPath);
  mock.mock.restore();

  const token = path.join(dir, 'web.token');
  fs.writeFileSync(token, 'fixture-token\n');
  const authArgs = mode === 'identity'
    ? ['--auth', 'tailscale', '--allow-login', IDENTITY['tailscale-user-login'], '--origin', 'https://pentacle.example.ts.net']
    : mode === 'token' ? ['--token-file', token] : [];
  const host = await main(['--profile', profile, '--port', '0', ...authArgs]);
  t.after(() => host.close());
  return host.port;
}

// Fetch normalizes dot segments before transmission. Send the target verbatim.
function request(port, target, { headers = {}, method = 'GET', body } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: target, headers, method, agent: false }, res => {
      res.setEncoding('utf8');
      let text = '';
      res.on('data', chunk => { text += chunk; });
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: text }));
      res.on('error', reject);
    });
    req.on('error', reject);
    req.end(body);
  });
}

test('parser accepts only canonical Design addresses and returns the target identity', () => {
  const { parseDesignRoute } = require('../lib/design_routes.cjs');
  for (const [prefix, mode] of [[WORKSPACE, 'workspace'], [EMBED, 'embed']]) {
    for (const [suffix, target] of TARGETS) assert.deepEqual(parseDesignRoute(prefix + suffix), { mode, target });
    const longId = 'a'.repeat(80);
    assert.deepEqual(parseDesignRoute(`${prefix}/0/${longId}`), {
      mode, target: { project_id: '0', design_id: longId },
    });
    for (const suffix of [...INVALID_SUFFIXES, '#', '/project-1/board#legacy', '/project-1/board\n']) {
      assert.equal(parseDesignRoute(prefix + suffix), null, prefix + suffix);
    }
  }
  for (const target of [undefined, null, '/next/', '/next/designing', '/next/design-embed.html', 'https://example.test/next/design']) {
    assert.equal(parseDesignRoute(target), null, String(target));
  }
});

test('identity mode serves the exact workspace and standalone shells with their assets', async t => {
  const port = await startHost(t, 'identity');
  const headers = IDENTITY;
  const next = await request(port, '/next/', { headers });
  assert.equal(next.status, 200);
  assert.match(next.body, /window\.__PENTACLE_CONFIG__=.*"appName":"DesignRouteFixture"/);

  for (const [suffix] of TARGETS) {
    const workspace = await request(port, WORKSPACE + suffix, { headers });
    assert.equal(workspace.status, 200, WORKSPACE + suffix);
    assert.equal(workspace.body, next.body);

    const standalone = await request(port, EMBED + suffix, { headers });
    assert.equal(standalone.status, 200, EMBED + suffix);
    assert.match(standalone.headers['content-type'], /^text\/html/);
    assert.equal(standalone.body, fs.readFileSync(path.join(__dirname, '../renderer/next/design-embed.html'), 'utf8'));
    assert.match(standalone.body, /<meta name="viewport" content="width=device-width, initial-scale=1">/);
    assert.match(standalone.body, /<main id="app"><\/main>/);
    assert.doesNotMatch(standalone.body, /PENTACLE_CONFIG|<form|chat_core/);

    for (const [address, page, paths] of [
      [WORKSPACE + suffix, workspace, Object.keys(ASSETS).filter(asset => asset !== '/next/design-embed.bundle.js')],
      [EMBED + suffix, standalone, ['/next/style.css', '/next/design-embed.bundle.js']],
    ]) {
      const assets = [...page.body.matchAll(/(?:href|src)="([^"]+)"/g)].map(([, asset]) => asset);
      assert.deepEqual(assets, paths, address);
      for (const asset of assets) {
        const url = new URL(asset, `http://127.0.0.1:${port}${address}`);
        const response = await request(port, url.pathname, { headers });
        assert.equal(response.status, 200, `${address} → ${asset}`);
        assert.equal(response.body, ASSETS[asset]);
      }
    }
  }
});

test('identity HTTP rejects invalid raw paths, direct entry access and non-GET standalone requests', async t => {
  const port = await startHost(t, 'identity');
  const headers = IDENTITY;
  const invalid = ['/next/design-embed.html', '/next/./design-embed.html', '/next/%64esign', '/next/embed/%64esign'];
  for (const prefix of [WORKSPACE, EMBED]) {
    invalid.push(...INVALID_SUFFIXES.map(suffix => prefix + suffix));
    invalid.push(`/ignored/..${prefix}`, `http://example.test${prefix}`, `/${prefix}`, '/' + prefix.slice(1).replaceAll('/', '\\'));
  }
  for (const target of invalid) assert.equal((await request(port, target, { headers })).status, 404, target);

  for (const method of ['HEAD', 'POST']) {
    const response = await request(port, EMBED, { headers, method });
    assert.equal(response.status, 405, method);
    assert.equal(response.headers.allow, 'GET');
  }
  for (const target of [WORKSPACE, EMBED, EMBED + '/project-1/board/screen', EMBED + '/']) {
    const response = await request(port, target);
    assert.equal(response.status, 401, target);
    assert.deepEqual(JSON.parse(response.body), { error: 'tailnet identity required' });
  }
});

test('token login opens ordinary Next and workspace Design while standalone returns 404', async t => {
  const port = await startHost(t, 'token');
  const login = await request(port, '/login');
  assert.equal(login.status, 200);
  assert.match(login.body, /<form method="POST" action="\/login">/);
  const signedIn = await request(port, '/login', { method: 'POST', body: 'token=fixture-token' });
  assert.equal(signedIn.status, 302);
  assert.equal(signedIn.headers.location, '/');
  const cookie = signedIn.headers['set-cookie'][0].split(';', 1)[0];

  for (const [suffix] of TARGETS) {
    const workspace = await request(port, WORKSPACE + suffix);
    assert.equal(workspace.status, 302, WORKSPACE + suffix);
    assert.equal(workspace.headers.location, '/login');
    assert.equal((await request(port, WORKSPACE + suffix, { headers: { cookie } })).status, 200);
    assert.equal((await request(port, EMBED + suffix, { headers: { cookie } })).status, 404);
    assert.equal((await request(port, EMBED + suffix)).status, 404);
  }
  assert.equal((await request(port, '/next/', { headers: { cookie } })).status, 200);
  assert.equal((await request(port, WORKSPACE + '/../../../login', { headers: { cookie } })).status, 404);
  assert.equal((await request(port, '/next/design-embed.html', { headers: { cookie } })).status, 404);
});

test('local loopback mode keeps workspace Design available and refuses standalone', async t => {
  const port = await startHost(t, 'loopback');
  assert.equal((await request(port, WORKSPACE)).status, 200);
  assert.equal((await request(port, EMBED)).status, 404);
  assert.equal((await request(port, '/next/design-embed.html')).status, 404);
});
