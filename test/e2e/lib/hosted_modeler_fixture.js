'use strict';

// Scratch HTTPS endpoints emulate a trusted identity proxy and a distinct
// hosted viewer. They bind loopback only; no fleet route or credential is used.
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const https = require('node:https');
const { createHash, createPublicKey } = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { startModelerFixture } = require('./modeler_fixture_server');

async function startHostedModelerFixture(scratch, upstreamPort) {
  const key = path.join(scratch, 'viewer-key.pem');
  const cert = path.join(scratch, 'viewer-cert.pem');
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes',
    '-keyout', key, '-out', cert, '-days', '1', '-subj', '/CN=example.ts.net',
    '-addext', 'subjectAltName=DNS:pentacle.example.ts.net,DNS:viewer.example.ts.net'], { stdio: 'ignore' });
  const tls = { key: fs.readFileSync(key), cert: fs.readFileSync(cert) };
  const publicKey = createPublicKey(tls.cert).export({ type: 'spki', format: 'der' });
  const spki = createHash('sha256').update(publicKey).digest('base64');
  const requests = [];
  const viewer = await startModelerFixture({ tls, hostname: 'viewer.example.ts.net', observeRequest: request => {
    requests.push({ pentacleCookiePresent: /(?:^|;\s*)pentacle_web=/.test(request.headers.cookie || ''),
      authorizationPresent: !!request.headers.authorization, identityHeaderPresent: !!request.headers['tailscale-user-login'],
      referrerPresent: !!request.headers.referer });
  } });
  const headers = request => ({ ...request.headers, 'tailscale-user-login': 'tester@example.test',
    'x-forwarded-proto': 'https', 'x-forwarded-for': '100.64.0.10' });
  const proxy = https.createServer(tls, (request, response) => {
    const upstream = http.request({ hostname: '127.0.0.1', port: upstreamPort,
      method: request.method, path: request.url, headers: headers(request) }, reply => {
      response.writeHead(reply.statusCode, reply.headers); reply.pipe(response);
    });
    upstream.on('error', () => response.writeHead(502).end()); request.pipe(upstream);
  });
  const sockets = new Set();
  proxy.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  proxy.on('upgrade', (request, socket, head) => {
    const upstream = http.request({ hostname: '127.0.0.1', port: upstreamPort,
      path: request.url, headers: headers(request) });
    upstream.on('upgrade', (reply, remote, remoteHead) => {
      sockets.add(remote); remote.on('close', () => sockets.delete(remote));
      socket.write(`HTTP/1.1 ${reply.statusCode} ${reply.statusMessage}\r\n`
        + Object.entries(reply.headers).map(([name, value]) => `${name}: ${value}\r\n`).join('') + '\r\n');
      if (head.length) remote.write(head);
      if (remoteHead.length) socket.write(remoteHead);
      remote.pipe(socket); socket.pipe(remote);
      socket.on('close', () => remote.destroy()); remote.on('close', () => socket.destroy());
    });
    upstream.on('response', reply => { socket.end(`HTTP/1.1 ${reply.statusCode} ${reply.statusMessage}\r\n\r\n`); reply.resume(); });
    upstream.on('error', () => socket.destroy()); upstream.end();
  });
  try {
    await new Promise((resolve, reject) => { proxy.once('error', reject); proxy.listen(0, '127.0.0.1', resolve); });
  } catch (error) { await viewer.close(); throw error; }
  const origin = `https://pentacle.example.ts.net:${proxy.address().port}`;
  return { url: viewer.url, origin, requests,
    policyEnv: { PENTACLE_HOSTED_DASHBOARD_TAILNET_SUFFIX: 'example.ts.net', PENTACLE_HOSTED_DASHBOARD_PENTACLE_ORIGIN: origin },
    chromeArgs: ['--no-proxy-server', '--host-resolver-rules=MAP pentacle.example.ts.net 127.0.0.1, MAP viewer.example.ts.net 127.0.0.1',
      `--ignore-certificate-errors-spki-list=${spki}`],
    async close() {
      for (const socket of sockets) socket.destroy();
      await Promise.all([viewer.close(), new Promise((resolve, reject) => proxy.close(error => error ? reject(error) : resolve()))]);
    },
  };
}

module.exports = { startHostedModelerFixture };
