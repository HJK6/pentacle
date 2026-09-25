'use strict';

// ── Web-host Tailscale identity auth (`--auth tailscale`) ────────────────────
// Behind `tailscale serve` the host binds loopback and trusts the proxy's
// identity headers instead of a token. Every case starts from a complete valid
// fixture (loopback peer, allow-listed login, forwarded https, one tailnet
// X-Forwarded-For, canonical Origin on the websocket) and varies exactly one
// field, for HTTP and for the /cc upgrade separately.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const WebSocket = require('ws');

const { main, parseArgs, createTailscaleAuth, isTailnetAddress } = require('../server/index.js');
const { micStartSameOrigin } = require('../server/mic_starter');

const LOGIN = 'operator@example.test';
const ORIGIN = 'https://pentacle-host.example-tailnet.ts.net';
const HOST = 'pentacle-host.example-tailnet.ts.net';

const VALID_V4 = {
  'tailscale-user-login': LOGIN,
  'x-forwarded-proto': 'https',
  'x-forwarded-for': '100.104.128.92',
};
const VALID_V6 = { ...VALID_V4, 'x-forwarded-for': 'fd7a:115c:a1e0::1234:5678' };

// One field varied per case. `undefined` removes the header; an array sends it
// as repeated header lines.
const IDENTITY_CASES = [
  ['login missing', { 'tailscale-user-login': undefined }],
  ['login other', { 'tailscale-user-login': 'someone-else@example.test' }],
  ['login case variant', { 'tailscale-user-login': 'Operator@Example.test' }],
  ['login duplicated', { 'tailscale-user-login': [LOGIN, LOGIN] }],
  ['proto http', { 'x-forwarded-proto': 'http' }],
  ['proto missing', { 'x-forwarded-proto': undefined }],
  ['xff missing', { 'x-forwarded-for': undefined }],
  ['xff non-tailnet', { 'x-forwarded-for': '10.0.0.5' }],
  ['xff list of two', { 'x-forwarded-for': '100.104.128.92, 100.70.128.35' }],
  ['xff duplicate header', { 'x-forwarded-for': ['100.104.128.92', '100.104.128.92'] }],
];

function vary(base, change) {
  const out = { ...base };
  for (const [k, v] of Object.entries(change)) {
    if (v === undefined) delete out[k]; else out[k] = v;
  }
  return out;
}

function scratch() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-web-ts-auth-'));
  const home = path.join(dir, 'home');
  fs.mkdirSync(home);
  const profile = path.join(dir, 'profile.js');
  fs.writeFileSync(profile, "module.exports={appName:'PentacleTsAuthTest',chatStream:{localHost:'fixture',hosts:['local']},features:{mic:false}};\n");
  return { dir, home, profile };
}

async function startHost(t, extra = []) {
  const { dir, home, profile } = scratch();
  const realHome = process.env.HOME;
  process.env.HOME = home;
  const host = await main(['--profile', profile, '--port', '0', ...extra]);
  t.after(async () => {
    try { await host.close(); } catch {}
    if (realHome === undefined) delete process.env.HOME; else process.env.HOME = realHome;
    fs.rmSync(dir, { recursive: true, force: true });
  });
  return host;
}

const TS_ARGS = ['--bind', '127.0.0.1', '--auth', 'tailscale', '--allow-login', LOGIN, '--origin', ORIGIN];

function get(port, urlPath, headers = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: urlPath, method: 'GET', headers }, (res) => {
      let body = '';
      res.on('data', (c) => { body += c; });
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body }));
    });
    req.on('error', reject);
    req.end();
  });
}

// Resolves { outcome: 'open', ws } or { outcome: 'rejected', status }.
function tryWs(port, headers) {
  return new Promise((resolve) => {
    const ws = new WebSocket(`ws://127.0.0.1:${port}/cc`, { headers });
    ws.once('open', () => resolve({ outcome: 'open', ws }));
    ws.once('unexpected-response', (_req, res) => { try { ws.terminate(); } catch {} resolve({ outcome: 'rejected', status: res.statusCode }); });
    ws.once('error', () => resolve({ outcome: 'rejected', status: null }));
  });
}

function request(ws, method, args = []) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('fixture response timeout')), 2000);
    ws.once('message', (raw) => { clearTimeout(timer); resolve(JSON.parse(raw)); });
    ws.send(JSON.stringify({ id: 1, method, args }));
  });
}

function fakeReq(remoteAddress, headers) {
  const rawHeaders = [];
  for (const [k, v] of Object.entries(headers)) for (const one of [].concat(v)) rawHeaders.push(k, one);
  const flat = {};
  for (const [k, v] of Object.entries(headers)) flat[k] = Array.isArray(v) ? v.join(', ') : v;
  return { socket: { remoteAddress }, headers: flat, rawHeaders };
}

// ── flags and startup refusals ──────────────────────────────────────────────

test('--auth tailscale parses repeatable and comma-separated --allow-login and --origin', () => {
  const a = parseArgs(['--auth', 'tailscale', '--allow-login', 'a@x.test,b@x.test', '--allow-login', 'c@x.test', '--origin', ORIGIN]);
  assert.equal(a.auth, 'tailscale');
  assert.deepEqual(a.allowLogins, ['a@x.test', 'b@x.test', 'c@x.test']);
  assert.equal(a.origin, ORIGIN);
  assert.equal(parseArgs([]).auth, 'token', 'token mode stays the default');
  assert.throws(() => parseArgs(['--auth', 'magic']), /--auth/);
});

test('--auth tailscale startup refusals: routable bind, missing --origin, missing --allow-login, token file, bad origin', async (t) => {
  const { dir, home, profile } = scratch();
  const realHome = process.env.HOME;
  process.env.HOME = home;
  t.after(() => { if (realHome === undefined) delete process.env.HOME; else process.env.HOME = realHome; fs.rmSync(dir, { recursive: true, force: true }); });
  const base = ['--profile', profile, '--port', '0', '--auth', 'tailscale'];
  await assert.rejects(main([...base, '--bind', '100.85.55.92', '--allow-login', LOGIN, '--origin', ORIGIN]), /loopback/);
  await assert.rejects(main([...base, '--bind', '0.0.0.0', '--allow-login', LOGIN, '--origin', ORIGIN]), /loopback/);
  await assert.rejects(main([...base, '--bind', '127.0.0.1', '--allow-login', LOGIN]), /--origin/);
  await assert.rejects(main([...base, '--bind', '127.0.0.1', '--origin', ORIGIN]), /--allow-login/);
  const token = path.join(dir, 'web.token');
  fs.writeFileSync(token, 'x\n');
  await assert.rejects(main([...base, '--bind', '127.0.0.1', '--allow-login', LOGIN, '--origin', ORIGIN, '--token-file', token]), /--token-file/);
  for (const bad of ['http://pentacle-host.example-tailnet.ts.net', `${ORIGIN}/app`, 'not a url']) {
    await assert.rejects(main([...base, '--bind', '127.0.0.1', '--allow-login', LOGIN, '--origin', bad]), /--origin/);
  }
  // Tailscale-only flags are refused in token mode rather than silently ignored.
  await assert.rejects(main(['--profile', profile, '--port', '0', '--allow-login', LOGIN]), /--auth tailscale/);
});

// ── identity predicate (unit) ───────────────────────────────────────────────

test('isTailnetAddress accepts Tailscale CGNAT and IPv6 ranges only', () => {
  for (const ip of ['100.64.0.1', '100.104.128.92', '100.127.255.254', 'fd7a:115c:a1e0::1', 'fd7a:115c:a1e0:ab12::99']) {
    assert.equal(isTailnetAddress(ip), true, ip);
  }
  for (const ip of ['100.63.255.255', '100.128.0.1', '10.0.0.5', '127.0.0.1', '::1', 'fd7a:115c:a1e1::1', '', 'nonsense', '100.104.128.92, 100.70.128.35']) {
    assert.equal(isTailnetAddress(ip), false, ip);
  }
});

test('identity predicate: full valid fixtures pass; each single-field variation fails; non-loopback peer fails', () => {
  const auth = createTailscaleAuth({ allowLogins: [LOGIN], origin: ORIGIN });
  for (const peer of ['127.0.0.1', '::1', '::ffff:127.0.0.1']) {
    assert.equal(auth.isAuthed(fakeReq(peer, VALID_V4)), true, `v4 fixture via ${peer}`);
    assert.equal(auth.isAuthed(fakeReq(peer, VALID_V6)), true, `v6 fixture via ${peer}`);
  }
  for (const [name, change] of IDENTITY_CASES) {
    assert.equal(auth.isAuthed(fakeReq('127.0.0.1', vary(VALID_V4, change))), false, name);
  }
  for (const peer of ['100.104.128.92', '192.168.1.5', undefined]) {
    assert.equal(auth.isAuthed(fakeReq(peer, VALID_V4)), false, `non-loopback peer ${peer}`);
  }
});

// ── real host: HTTP ─────────────────────────────────────────────────────────

test('HTTP: valid identity is served cookie-free; each single-field variation is 401 JSON; /login is 404', async (t) => {
  const host = await startHost(t, TS_ARGS);
  for (const valid of [VALID_V4, VALID_V6]) {
    const ok = await get(host.port, '/api/config', valid);
    assert.equal(ok.status, 200, 'control: full valid header set is served');
    assert.equal(JSON.parse(ok.body).appName, 'PentacleTsAuthTest');
    assert.equal(ok.headers['set-cookie'], undefined, 'no cookie is ever set');
    const page = await get(host.port, '/', valid);
    assert.notEqual(page.status, 302, 'no login redirect');
    assert.notEqual(page.status, 401);
  }
  for (const [name, change] of IDENTITY_CASES) {
    for (const p of ['/api/config', '/']) {
      const r = await get(host.port, p, vary(VALID_V4, change));
      assert.equal(r.status, 401, `${name} ${p}`);
      assert.deepEqual(JSON.parse(r.body), { error: 'tailnet identity required' }, `${name} ${p} body`);
    }
  }
  const stale = await get(host.port, '/', { cookie: 'pentacle_web=0123456789abcdef' });
  assert.equal(stale.status, 401, 'a stale token cookie without identity headers is refused');
  assert.equal((await get(host.port, '/login', VALID_V4)).status, 404, '/login does not exist in this mode');
  const loginNoId = await get(host.port, '/login');
  assert.notEqual(loginNoId.status, 200, '/login never serves a form');
});

// ── real host: websocket upgrade ────────────────────────────────────────────

test('WS: valid identity + canonical Origin opens; identity variations 401; Origin variations 403', async (t) => {
  const host = await startHost(t, TS_ARGS);
  for (const valid of [VALID_V4, VALID_V6]) {
    const ok = await tryWs(host.port, { ...valid, Origin: ORIGIN });
    assert.equal(ok.outcome, 'open', 'control: full valid set with canonical Origin opens');
    ok.ws.close();
  }
  for (const [name, change] of IDENTITY_CASES) {
    const r = await tryWs(host.port, { ...vary(VALID_V4, change), Origin: ORIGIN });
    assert.deepEqual(r, { outcome: 'rejected', status: 401 }, name);
  }
  for (const [name, origin] of [['missing', undefined], ['null', 'null'], ['hostile', 'https://evil.example'], ['http scheme', `http://${HOST}`]]) {
    const headers = { ...VALID_V4 };
    if (origin !== undefined) headers.Origin = origin;
    const r = await tryWs(host.port, headers);
    assert.deepEqual(r, { outcome: 'rejected', status: 403 }, `origin ${name}`);
  }
  const stale = await tryWs(host.port, { Cookie: 'pentacle_web=0123456789abcdef', Origin: ORIGIN });
  assert.deepEqual(stale, { outcome: 'rejected', status: 401 }, 'stale cookie without identity headers');
});

// ── forwarded proto: same-origin admission through the proxy ────────────────

test('micStartSameOrigin honors X-Forwarded-Proto from a loopback peer only', () => {
  const req = (peer, headers) => ({ socket: { remoteAddress: peer }, headers: { host: HOST, origin: ORIGIN, ...headers } });
  assert.equal(micStartSameOrigin(req('127.0.0.1', { 'x-forwarded-proto': 'https' })), true, 'loopback proxy with https');
  assert.equal(micStartSameOrigin(req('::ffff:127.0.0.1', { 'x-forwarded-proto': 'https' })), true);
  assert.equal(micStartSameOrigin(req('127.0.0.1', {})), false, 'no forwarded proto: plain http expected');
  assert.equal(micStartSameOrigin(req('100.104.128.92', { 'x-forwarded-proto': 'https' })), false, 'forwarded proto from a non-loopback peer is ignored');
  assert.equal(micStartSameOrigin(req('127.0.0.1', { 'x-forwarded-proto': 'https', origin: 'https://evil.example' })), false, 'hostile Origin');
  assert.equal(micStartSameOrigin(req('127.0.0.1', { 'x-forwarded-proto': 'https, http' })), false, 'a forwarded-proto list is not trusted');
});

for (const mode of ['tailscale', 'token']) test(`mic:start-server and provider-relogin:* reach their handlers through the proxy (${mode} mode)`, async (t) => {
  let extra = TS_ARGS;
  let cookie = null;
  if (mode === 'token') {
    const tokenDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-web-ts-token-'));
    t.after(() => fs.rmSync(tokenDir, { recursive: true, force: true }));
    const token = path.join(tokenDir, 'web.token');
    fs.writeFileSync(token, 'FIXTURE_WEB_TOKEN\n', { mode: 0o600 });
    extra = ['--bind', '127.0.0.1', '--token-file', token];
  }
  const host = await startHost(t, extra);
  if (mode === 'token') {
    const r = await fetch(`http://127.0.0.1:${host.port}/login`, { method: 'POST', body: 'token=FIXTURE_WEB_TOKEN', headers: { 'content-type': 'application/x-www-form-urlencoded' }, redirect: 'manual' });
    cookie = r.headers.get('set-cookie').split(';')[0];
  }
  const invoked = [];
  const methods = ['mic:start-server', 'provider-relogin:hosts'];
  for (const m of methods) host.handlers[m].handler = () => { invoked.push(m); return { fixture: m }; };
  // Exactly what tailscale serve sends: Host preserved, forwarded https.
  const proxied = { ...VALID_V4, Host: HOST, 'x-forwarded-host': HOST, ...(cookie ? { Cookie: cookie } : {}) };
  const ok = await tryWs(host.port, { ...proxied, Origin: ORIGIN });
  assert.equal(ok.outcome, 'open');
  for (const m of methods) {
    const reply = await request(ok.ws, m);
    assert.deepEqual(reply, { id: 1, ok: true, result: { fixture: m } }, `${m} handler invoked and its result returned`);
  }
  ok.ws.close();
  assert.deepEqual(invoked, methods);
  const hostile = await tryWs(host.port, { ...proxied, Origin: 'https://evil.example' });
  if (mode === 'tailscale') {
    assert.deepEqual(hostile, { outcome: 'rejected', status: 403 }, 'hostile Origin refused at the upgrade');
  } else {
    // Token mode keeps its existing upgrade rule; the per-method gate refuses.
    assert.equal(hostile.outcome, 'open');
    const reply = await request(hostile.ws, 'mic:start-server');
    assert.equal(reply.error.code, 'mic_origin_refused');
    hostile.ws.close();
  }
  assert.deepEqual(invoked, methods, 'no handler ran for the hostile Origin');
});
