const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const WebSocket = require('ws');

const { main } = require('../server/index.js');

const TMUX = (() => {
  try { execFileSync('tmux', ['-V'], { stdio: 'ignore' }); return true; } catch { return false; }
})();

// A throwaway profile: loopback-only, no daemon (the client just retries in the
// background), no dashboard hub. The host must serve and dispatch regardless.
function writeProfile(dir) {
  const file = path.join(dir, 'profile.js');
  fs.writeFileSync(file, `'use strict';
module.exports = {
  appName: 'PentacleWebTest',
  agents: { codex: { command: 'codex' } },
  workingDirectory: '${dir.replace(/\\/g, '\\\\')}',
  chatStream: { url: 'ws://127.0.0.1:1', hosts: ['local'], token: 'never-ship-me', tokenPath: '${path.join(dir, 'no-token').replace(/\\/g, '\\\\')}' },
};
`);
  return file;
}

function connect(port) {
  const ws = new WebSocket(`ws://127.0.0.1:${port}/cc`);
  const pending = new Map();
  const pushes = [];
  const waiters = [];
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw.toString());
    if (msg.event) {
      pushes.push(msg);
      for (const w of waiters.splice(0)) w();
      return;
    }
    const resolve = pending.get(msg.id);
    if (resolve) { pending.delete(msg.id); resolve(msg); }
  });
  let nextId = 1;
  return {
    ws,
    pushes,
    ready: new Promise((resolve, reject) => { ws.once('open', resolve); ws.once('error', reject); }),
    call(method, ...args) {
      const id = nextId++;
      return new Promise((resolve) => {
        pending.set(id, resolve);
        ws.send(JSON.stringify({ id, method, args }));
      });
    },
    fire(method, ...args) { ws.send(JSON.stringify({ method, args })); },
    async waitForPush(predicate, timeoutMs = 15000) {
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        const hit = pushes.find(predicate);
        if (hit) return hit;
        if (Date.now() > deadline) throw new Error('timed out waiting for a push');
        await new Promise((r) => { waiters.push(r); setTimeout(r, 100); });
      }
    },
    close() { ws.close(); },
  };
}

test('the headless host serves the cc surface over a websocket', { skip: !TMUX && 'tmux is not installed' }, async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-web-test-'));
  const home = path.join(dir, 'home');
  fs.mkdirSync(home);
  const realHome = process.env.HOME;
  process.env.HOME = home;                    // keep state files out of the real profile
  const host = await main(['--profile', writeProfile(dir), '--port', '0']);
  const { port } = host;
  const client = connect(port);
  await client.ready;

  const sessionName = `ptest-web-unit-${process.pid}`;
  t.after(async () => {
    client.close();
    try { execFileSync('tmux', ['kill-session', '-t', `=${sessionName}`], { stdio: 'ignore' }); } catch {}
    await host.close();
    if (realHome === undefined) delete process.env.HOME; else process.env.HOME = realHome;
  });

  // get-config answers with the computed config, matching GET /api/config.
  const cfg = await client.call('get-config');
  assert.equal(cfg.ok, true);
  assert.equal(cfg.result.appName, 'PentacleWebTest');
  assert.ok(Array.isArray(cfg.result.hostIds) && cfg.result.hostIds.includes('local'));
  assert.equal(cfg.result.token, undefined, 'the host must never ship the daemon token to a browser');
  assert.equal(cfg.result.chatStream.tokenPath, undefined);

  const viaHttp = await fetch(`http://127.0.0.1:${port}/api/config`).then((r) => r.json());
  assert.deepEqual(viaHttp.hostIds, cfg.result.hostIds);
  assert.equal(viaHttp.appName, cfg.result.appName);

  // Channels the browser must answer itself are refused rather than served —
  // routing the clipboard here would read the HOST's clipboard.
  const refused = await client.call('clipboard:read-text');
  assert.equal(refused.ok, false);
  assert.equal(refused.error.code, 'web_local');

  const missing = await client.call('pty:check-session', sessionName, 'local');
  assert.equal(missing.result, false, 'a session that does not exist must not report live');

  execFileSync('tmux', ['new-session', '-d', '-s', sessionName, 'sh', '-c', 'stty raw -echo; exec cat']);
  const present = await client.call('pty:check-session', sessionName, 'local');
  assert.equal(present.result, true);

  const created = await client.call('pty:create', 0, sessionName, 'local', 80, 24);
  assert.equal(created.ok, true);
  assert.match(String(created.result), /^%\d+$/, 'pty:create returns the tmux pane id');

  client.fire('pty:write', 0, 'WEBOK-UNIT\r');
  const data = await client.waitForPush(
    (p) => p.event === 'pty:data' && p.args[0] === 0 && String(p.args[1]).includes('WEBOK-UNIT'),
  );
  assert.equal(data.args[0], 0, 'pty:data carries the slot it belongs to');

  client.fire('pty:resize', 0, 100, 30);

  const killed = await client.call('pty:kill', 0);
  assert.equal(killed.ok, true);
});

test('an unbuilt bundle answers with a build hint instead of a stack trace', async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-web-test-'));
  const home = path.join(dir, 'home');
  fs.mkdirSync(home);
  const realHome = process.env.HOME;
  process.env.HOME = home;
  const host = await main(['--profile', writeProfile(dir), '--port', '0']);
  const { port } = host;
  t.after(async () => {
    await host.close();
    if (realHome === undefined) delete process.env.HOME; else process.env.HOME = realHome;
  });

  const res = await fetch(`http://127.0.0.1:${port}/`);
  const body = await res.text();
  // Either the bundle is built (200 with the page) or it is not (503 + hint);
  // both are correct, a 500 or a stack trace is not.
  assert.ok(res.status === 200 || res.status === 503, `unexpected status ${res.status}`);
  if (res.status === 503) assert.match(body, /npm run build:web/);

  const traversal = await fetch(`http://127.0.0.1:${port}/../../package.json`, { redirect: 'manual' });
  assert.ok(traversal.status === 403 || traversal.status === 404, `traversal returned ${traversal.status}`);
});
