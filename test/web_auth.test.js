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

const { main, isLoopbackBind, parseArgs, createLoginLimiter } = require('../server/index.js');

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

test('--token-path (daemon credential) and --token-file (web login) are distinct flags', () => {
  const a = parseArgs(['--profile', 'x', '--token-path', '/home/example/.config/pentacle-stream/token', '--token-file', '/tmp/web.token']);
  assert.equal(a.tokenPath, '/home/example/.config/pentacle-stream/token', 'the daemon credential path is captured');
  assert.equal(a.tokenFile, '/tmp/web.token', 'the web login token file is separate');
  assert.equal(parseArgs([]).tokenPath, null, 'no daemon credential path by default');
});

test('isLoopbackBind classifies loopback addresses and FAILS CLOSED on empty/malformed', () => {
  for (const b of ['127.0.0.1', 'localhost', '::1', '127.0.0.5', '  127.0.0.1  ']) {
    assert.equal(isLoopbackBind(b), true, `${b} is loopback`);
  }
  // Routable AND empty/whitespace/undefined (a wildcard listener) must be non-loopback,
  // so auth is required rather than silently disabled.
  for (const b of ['198.51.100.24', '0.0.0.0', '192.168.1.5', '::', '', '   ', undefined, null]) {
    assert.equal(isLoopbackBind(b), false, `${JSON.stringify(b)} must not be treated as loopback`);
  }
});

test('an empty --bind is rejected outright (no accidental wildcard listener)', async (t) => {
  const { profile, home } = scratch();
  const realHome = process.env.HOME;
  process.env.HOME = home;
  t.after(() => { if (realHome === undefined) delete process.env.HOME; else process.env.HOME = realHome; });
  assert.throws(() => parseArgs(['--profile', profile, '--bind', '']), /invalid --bind/);
  assert.throws(() => parseArgs(['--profile', profile, '--bind', '   ']), /invalid --bind/);
  await assert.rejects(main(['--profile', profile, '--bind', '', '--port', '0']), /invalid --bind/);
});

test('a routable bind refuses to start without a token file', async (t) => {
  const { profile, home } = scratch();
  const realHome = process.env.HOME;
  process.env.HOME = home;
  t.after(() => { if (realHome === undefined) delete process.env.HOME; else process.env.HOME = realHome; });
  await assert.rejects(
    main(['--profile', profile, '--bind', '198.51.100.24', '--port', '0']),
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

test('with a token, an authenticated upgrade must come from the host\'s own origin or carry none', async (t) => {
  const { dir, profile, home } = scratch();
  const tokenPath = path.join(dir, 'web.token');
  fs.writeFileSync(tokenPath, 'super-secret-token\n');
  const host = await startHost(t, ['--profile', profile, '--bind', '127.0.0.1', '--port', '0', '--token-file', tokenPath], home);
  const login = await fetch(`http://127.0.0.1:${host.port}/login`, {
    method: 'POST', redirect: 'manual',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ token: 'super-secret-token' }).toString(),
  });
  const cookie = cookieFrom(login.headers.get('set-cookie'));
  const authority = `127.0.0.1:${host.port}`;

  assert.equal(await tryWs(host.port, { Cookie: cookie }), 'open', 'a CLI client with the cookie and no Origin connects');
  assert.equal(await tryWs(host.port, { Cookie: cookie, Origin: `http://${authority}` }), 'open', 'the page served by this host connects');
  assert.equal(await tryWs(host.port, { Cookie: cookie, Origin: `http://127.0.0.1:${host.port + 1}` }), 'rejected', 'another port on the same host is refused');
  assert.equal(await tryWs(host.port, { Cookie: cookie, Origin: `http://app.${authority}` }), 'rejected', 'a subdomain is refused');
  assert.equal(await tryWs(host.port, { Cookie: cookie, Origin: 'https://example.com' }), 'rejected', 'a foreign site is refused');
  assert.equal(await tryWs(host.port, { Cookie: cookie, Origin: 'null' }), 'rejected', 'an opaque origin is refused');
});

test('login failures back off per peer and a success clears the peer', () => {
  let now = 1000;
  const limiter = createLoginLimiter({ now: () => now });
  const peer = '203.0.113.7';
  for (let i = 0; i < 3; i += 1) {
    assert.equal(limiter.retryAfterMs(peer), 0, `attempt ${i + 1} is allowed`);
    limiter.failed(peer);
  }
  assert.equal(limiter.retryAfterMs(peer), 0, 'the first three failures cost nothing');
  limiter.failed(peer);
  assert.equal(limiter.retryAfterMs(peer), 1000, 'the fourth failure waits one second');
  assert.equal(limiter.retryAfterMs('203.0.113.8'), 0, 'another peer is unaffected');
  now += 1000;
  assert.equal(limiter.retryAfterMs(peer), 0, 'the wait elapses');
  limiter.failed(peer);
  assert.equal(limiter.retryAfterMs(peer), 2000, 'each further failure doubles the wait');
  for (let i = 0; i < 30; i += 1) { now += 600000; limiter.failed(peer); }
  assert.ok(limiter.retryAfterMs(peer) <= 300000, 'the wait is capped at five minutes');
  limiter.succeeded(peer);
  assert.equal(limiter.retryAfterMs(peer), 0, 'a success clears the peer');
});

test('a locked peer gets 429 with Retry-After even for the right token', async (t) => {
  const { dir, profile, home } = scratch();
  const tokenPath = path.join(dir, 'web.token');
  fs.writeFileSync(tokenPath, 'super-secret-token\n');
  const host = await startHost(t, ['--profile', profile, '--bind', '127.0.0.1', '--port', '0', '--token-file', tokenPath], home);
  const login = token => fetch(`http://127.0.0.1:${host.port}/login`, {
    method: 'POST', redirect: 'manual',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ token }).toString(),
  });
  for (let i = 0; i < 4; i += 1) assert.equal((await login('wrong')).status, 401, `failure ${i + 1}`);
  const locked = await login('super-secret-token');
  assert.equal(locked.status, 429, 'the right token is refused while the peer is locked');
  assert.ok(Number(locked.headers.get('retry-after')) >= 1, 'Retry-After tells the client when to retry');
  assert.equal(locked.headers.get('set-cookie'), null, 'no cookie while locked');
});

test('hosted auth mode is derived from the effective server auth object only', () => {
  const { createAuth, hostedDashboardAuthMode } = require('../server/index.js');
  assert.equal(createAuth('synthetic-token').mode, 'token');
  assert.equal(hostedDashboardAuthMode({ mode: 'tailscale' }), 'identity');
  assert.equal(hostedDashboardAuthMode({ mode: 'token' }), 'token');
  for (const auth of [null, undefined, {}, { mode: 'identity' }, { mode: 'other' }]) {
    assert.equal(hostedDashboardAuthMode(auth), 'unknown');
  }
});

test('unauthenticated loopback admits local tools and same-origin pages, not foreign sites or DNS rebinding', async (t) => {
  const { profile, home } = scratch();
  const host = await startHost(t, ['--profile', profile, '--port', '0'], home);
  const authority = `127.0.0.1:${host.port}`;
  const origin = `http://${authority}`;
  // Raw requests let duplicate Host fields reach the SERVER. Node's HTTP
  // client rejects a Host array before sending, which is not security evidence.
  const request = (headers, upgrade = false) => new Promise((resolve, reject) => {
    const socket = require('node:net').connect(host.port, '127.0.0.1');
    const timer = setTimeout(() => { socket.destroy(); reject(Error('raw HTTP timeout')); }, 3000);
    socket.once('close', () => clearTimeout(timer));
    socket.once('error', reject);
    let response = '';
    socket.on('data', chunk => {
      response += chunk;
      if (!response.includes('\r\n')) return;
      const status = Number(response.match(/^HTTP\/1\.1 (\d{3})/)?.[1]);
      socket.destroy(); resolve(status);
    });
    socket.once('connect', () => {
      const fields = { Host: authority, ...headers };
      const lines = [`GET ${upgrade ? '/cc' : '/api/config'} HTTP/1.1`];
      for (const [key, value] of Object.entries(fields)) {
        for (const item of Array.isArray(value) ? value : [value]) lines.push(`${key}: ${item}`);
      }
      lines.push(...(upgrade ? ['Connection: Upgrade', 'Upgrade: websocket', 'Sec-WebSocket-Version: 13', 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=='] : ['Connection: close']));
      socket.write(lines.join('\r\n') + '\r\n\r\n');
    });
  });
  for (const headers of [
    {}, { Origin: origin },
    { Host: `localhost:${host.port}`, Origin: `http://localhost:${host.port}` },
    { Host: `[::1]:${host.port}`, Origin: `http://[::1]:${host.port}` },
    { Host: `[::ffff:7f00:1]:${host.port}`, Origin: `http://[::ffff:7f00:1]:${host.port}` },
  ]) {
    assert.equal(await request(headers), 200, JSON.stringify(headers));
    assert.equal(await tryWs(host.port, headers), 'open', JSON.stringify(headers));
  }
  // Fetch metadata: the app's own requests, typed/bookmarked navigations and a
  // top-level link from another site load; the initiator cannot read those.
  for (const headers of [
    { 'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty', Origin: origin },
    { 'Sec-Fetch-Site': 'none', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document' },
    { 'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document' },
  ]) {
    assert.equal(await request(headers), 200, JSON.stringify(headers));
  }
  assert.equal(await tryWs(host.port, { 'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'websocket', Origin: origin }), 'open');
  for (const headers of [
    { Origin: 'https://untrusted.example' }, { Origin: 'null' }, { Origin: `${origin}/` },
    { Origin: `http://127.0.0.1:${host.port + 1}` },
    { Origin: `https://${authority}`, 'X-Forwarded-Proto': 'https' },
    { Host: `rebind.example:${host.port}`, Origin: `http://rebind.example:${host.port}` },
    { Host: `rebind.example:${host.port}` }, { Host: `127.0.0.1:${host.port + 1}` },
    { Host: `[::ffff:c0a8:1]:${host.port}`, Origin: `http://[::ffff:c0a8:1]:${host.port}` },
    { Host: `127.1:${host.port}`, Origin: origin },
    { Host: `localhost@${authority}`, Origin: origin }, { Host: `${authority}/path`, Origin: origin },
    { Origin: [origin, origin] }, { Host: [authority, authority], Origin: origin },
    // A no-cors GET carries no Origin; its fetch metadata still names the site.
    { 'Sec-Fetch-Site': 'same-site', 'Sec-Fetch-Mode': 'no-cors', 'Sec-Fetch-Dest': 'empty' },
    { 'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'no-cors', 'Sec-Fetch-Dest': 'script' },
    { 'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'iframe' },
    { 'Sec-Fetch-Site': ['same-origin', 'same-origin'] }, { 'Sec-Fetch-Site': 'unexpected' },
  ]) {
    assert.equal(await request(headers), 403, JSON.stringify(headers));
    assert.equal(await request(headers, true), 403, `upgrade ${JSON.stringify(headers)}`);
  }
});
