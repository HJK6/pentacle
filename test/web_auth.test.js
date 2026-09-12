'use strict';

// ── Web-host authentication and the bind guard ───────────────────────────────
// A loopback bind is single-user and open, exactly as lane 1 shipped. A
// routable bind refuses to start without a token file, and once auth is on the
// token is presented once at /login for an HttpOnly cookie that every page, api
// call and websocket upgrade is gated on. Auth is exercised on a 127.0.0.1 bind
// by supplying a token file (loopback + token opts into auth), so the test never
// needs a routable address.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const WebSocket = require('ws');

const { main, isLoopbackBind } = require('../server/index.js');

function scratch() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-web-auth-'));
  const home = path.join(dir, 'home');
  fs.mkdirSync(home);
  const file = path.join(dir, 'profile.js');
  fs.writeFileSync(file, `'use strict';
module.exports = {
  appName: 'PentacleAuthTest',
  agents: { codex: { command: 'codex' } },
  workingDirectory: '${dir.replace(/\\/g, '\\\\')}',
  chatStream: { url: 'ws://127.0.0.1:1', hosts: ['local'], token: 'never-ship-me', tokenPath: '${path.join(dir, 'no-token').replace(/\\/g, '\\\\')}' },
};
`);
  return { dir, home, profile: file };
}

// A host started with HOME redirected so no state escapes into the real profile.
async function startHost(t, argv, home) {
  const realHome = process.env.HOME;
  process.env.HOME = home;
  const host = await main(argv);
  t.after(async () => {
    try { await host.close(); } catch {}
    if (realHome === undefined) delete process.env.HOME; else process.env.HOME = realHome;
  });
  return host;
}

// Attempt a /cc upgrade; resolves 'open' if it becomes a socket, 'rejected'
// otherwise. Closes any socket it opens.
function tryWs(port, headers) {
  return new Promise((resolve) => {
    const ws = new WebSocket(`ws://127.0.0.1:${port}/cc`, headers ? { headers } : undefined);
    ws.once('open', () => { ws.close(); resolve('open'); });
    ws.once('unexpected-response', () => { try { ws.terminate(); } catch {} resolve('rejected'); });
    ws.once('error', () => resolve('rejected'));
  });
}

function cookieFrom(setCookie) {
  return String(setCookie || '').split(';')[0];  // "pentacle_web=<hash>"
}

test('isLoopbackBind classifies loopback and routable addresses', () => {
  for (const b of ['127.0.0.1', 'localhost', '::1', '127.0.0.5', undefined]) {
    assert.equal(isLoopbackBind(b), true, `${b} is loopback`);
  }
  for (const b of ['100.80.28.24', '0.0.0.0', '192.168.1.5', '::']) {
    assert.equal(isLoopbackBind(b), false, `${b} is routable`);
  }
});

test('a routable bind refuses to start without a token file', async (t) => {
  const { profile, home } = scratch();
  const realHome = process.env.HOME;
  process.env.HOME = home;
  t.after(() => { if (realHome === undefined) delete process.env.HOME; else process.env.HOME = realHome; });
  await assert.rejects(
    main(['--profile', profile, '--bind', '100.80.28.24', '--port', '0']),
    /token-file/,
    'binding a routable address without a token must refuse to start',
  );
});

test('a routable bind refuses to start with an empty or missing token file', async (t) => {
  const { dir, profile, home } = scratch();
  const realHome = process.env.HOME;
  process.env.HOME = home;
  t.after(() => { if (realHome === undefined) delete process.env.HOME; else process.env.HOME = realHome; });

  const empty = path.join(dir, 'empty.token');
  fs.writeFileSync(empty, '   \n');
  await assert.rejects(main(['--profile', profile, '--bind', '0.0.0.0', '--port', '0', '--token-file', empty]), /empty/);

  const missing = path.join(dir, 'nope.token');
  await assert.rejects(main(['--profile', profile, '--bind', '0.0.0.0', '--port', '0', '--token-file', missing]), /cannot read/);
});

test('a loopback bind needs no token: it starts and accepts an unauthenticated socket', async (t) => {
  const { profile, home } = scratch();
  const host = await startHost(t, ['--profile', profile, '--port', '0'], home);
  assert.equal(await tryWs(host.port), 'open', 'loopback accepts a cookieless upgrade');
  const cfg = await fetch(`http://127.0.0.1:${host.port}/api/config`).then((r) => r.json());
  assert.equal(cfg.appName, 'PentacleAuthTest');
});

test('with a token, unauthenticated access is rejected and /login mints a working cookie', async (t) => {
  const { dir, profile, home } = scratch();
  const tokenPath = path.join(dir, 'web.token');
  fs.writeFileSync(tokenPath, 'super-secret-token\n');
  const host = await startHost(t, ['--profile', profile, '--bind', '127.0.0.1', '--port', '0', '--token-file', tokenPath], home);
  const base = `http://127.0.0.1:${host.port}`;

  // ── unauthenticated ──
  assert.equal(await tryWs(host.port), 'rejected', 'a cookieless upgrade is rejected');
  const apiNoCookie = await fetch(`${base}/api/config`);
  assert.equal(apiNoCookie.status, 401, 'api without a cookie is 401');
  const pageNoCookie = await fetch(`${base}/`, { redirect: 'manual' });
  assert.equal(pageNoCookie.status, 302, 'a page navigation redirects to /login');
  assert.equal(pageNoCookie.headers.get('location'), '/login');
  const loginGet = await fetch(`${base}/login`);
  assert.equal(loginGet.status, 200, '/login itself is reachable');

  // ── wrong token ──
  const bad = await fetch(`${base}/login`, {
    method: 'POST', redirect: 'manual',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ token: 'wrong' }).toString(),
  });
  assert.equal(bad.status, 401, 'the wrong token is refused');
  assert.equal(bad.headers.get('set-cookie'), null, 'no cookie is set for a bad token');

  // ── right token ──
  const good = await fetch(`${base}/login`, {
    method: 'POST', redirect: 'manual',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ token: 'super-secret-token' }).toString(),
  });
  assert.equal(good.status, 302, 'the right token redirects in');
  const setCookie = good.headers.get('set-cookie');
  assert.match(setCookie, /HttpOnly/i);
  assert.match(setCookie, /SameSite=Strict/i);
  const cookie = cookieFrom(setCookie);

  // ── authenticated ──
  assert.equal(await tryWs(host.port, { Cookie: cookie }), 'open', 'the cookie authenticates the upgrade');
  const apiWithCookie = await fetch(`${base}/api/config`, { headers: { cookie } });
  assert.equal(apiWithCookie.status, 200, 'api with the cookie is served');
  assert.equal((await apiWithCookie.json()).appName, 'PentacleAuthTest');

  // A tampered cookie is not accepted.
  assert.equal(await tryWs(host.port, { Cookie: 'pentacle_web=deadbeef' }), 'rejected', 'a bogus cookie is rejected');
});
