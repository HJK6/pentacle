'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { serveStatic } = require('../server/index');
test.before(async () => { await require('../scripts/build-web').build(); });
test('the served stylesheet is the token-checked source', () => {
  const fs = require('node:fs'); const path = require('node:path');
  assert.deepEqual(fs.readFileSync(path.join(__dirname, '../renderer/dist/web/next/style.css')), fs.readFileSync(path.join(__dirname, '../renderer/next/style.css')));
});

test('next entry uses the frozen page and receives the injected configuration', () => {
  const frozen = { files: new Map([['next/index.html', Buffer.from('<head><!--PENTACLE_CONFIG--></head>')]]) };
  const response = { writeHead(status, headers) { this.status = status; this.headers = headers; return this; }, end(body) { this.body = String(body); return this; } };
  serveStatic(response, '/next/', { marker: 'next' }, frozen);
  assert.equal(response.status, 200);
  assert.match(response.body, /window\.__PENTACLE_CONFIG__=.*"marker":"next"/);
});

test('next redirects after authentication and has the same identity boundary as root', async t => {
  const fs = require('node:fs'); const os = require('node:os'); const path = require('node:path');
  const { main, createAuth } = require('../server/index');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'next-route-'));
  const profile = path.join(dir, 'profile.js'); const token = path.join(dir, 'token');
  fs.writeFileSync(profile, "module.exports={agents:{},hosts:{local:{kind:'local'}}}"); fs.writeFileSync(token, 'fixture-token');
  const host = await main(['--profile', profile, '--port', '0', '--token-file', token]);
  t.after(async () => { await host.close(); fs.rmSync(dir, { recursive: true, force: true }); });
  const base = `http://127.0.0.1:${host.port}`;
  for (const route of ['/', '/next', '/next/']) {
    const res = await fetch(base + route, { redirect: 'manual' });
    assert.equal(res.status, 302); assert.equal(res.headers.get('location'), '/login');
  }
  const cookie = createAuth('fixture-token').setCookieHeader().split(';')[0];
  const redirect = await fetch(base + '/next', { headers: { cookie }, redirect: 'manual' });
  assert.equal(redirect.status, 302); assert.equal(redirect.headers.get('location'), '/next/');
  const page = await fetch(base + '/next/', { headers: { cookie } });
  assert.equal(page.status, 200); assert.match(await page.text(), /window\.__PENTACLE_CONFIG__/);
});

test('next uses the same tailnet identity check as root', async t => {
  const fs = require('node:fs'); const os = require('node:os'); const path = require('node:path');
  const { main } = require('../server/index');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'next-identity-')); const profile = path.join(dir, 'profile.js');
  fs.writeFileSync(profile, "module.exports={agents:{},hosts:{local:{kind:'local'}}}");
  const host = await main(['--profile', profile, '--port', '0', '--auth', 'tailscale', '--allow-login', 'tester@example.test', '--origin', 'https://pentacle.example.ts.net']);
  t.after(async () => { await host.close(); fs.rmSync(dir, { recursive: true, force: true }); });
  const base = `http://127.0.0.1:${host.port}`;
  for (const route of ['/', '/next', '/next/']) assert.equal((await fetch(base + route, { redirect: 'manual' })).status, 401);
  const headers = { 'tailscale-user-login':'tester@example.test', 'x-forwarded-proto':'https', 'x-forwarded-for':'fd7a:115c:a1e0::1234:5678' };
  assert.equal((await fetch(base + '/next/', { headers })).status, 200);
  const redir = await fetch(base + '/next', { headers, redirect: 'manual' }); assert.equal(redir.status, 302); assert.equal(redir.headers.get('location'), '/next/');
});
