'use strict';
const http = require('node:http');
const https = require('node:https');
const fs = require('node:fs');
const path = require('node:path');

// Test-only loopback fixture: no proxying, upstream calls, credentials or data.
async function startModelerFixture({ tls = null, hostname = '127.0.0.1', observeRequest = () => {} } = {}) {
  const body = fs.readFileSync(path.join(__dirname, '../../fixtures/modeler_viewer.html'));
  const handler = (request, response) => {
    observeRequest(request);
    if (request.url !== '/modeler_viewer.html') { response.writeHead(404).end(); return; }
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
    response.end(body);
  };
  const server = tls ? https.createServer(tls, handler) : http.createServer(handler);
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  return { url: `${tls ? 'https' : 'http'}://${hostname}:${server.address().port}/modeler_viewer.html`,
    close: () => new Promise((resolve, reject) => { server.close(error => error ? reject(error) : resolve()); server.closeAllConnections(); }) };
}
module.exports = { startModelerFixture };
