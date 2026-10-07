'use strict';
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

// Test-only loopback fixture: no proxying, upstream calls, credentials or data.
async function startModelerFixture() {
  const body = fs.readFileSync(path.join(__dirname, '../../fixtures/modeler_viewer.html'));
  const server = http.createServer((request, response) => {
    if (request.url !== '/modeler_viewer.html') { response.writeHead(404).end(); return; }
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
    response.end(body);
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  return { url: `http://127.0.0.1:${server.address().port}/modeler_viewer.html`,
    close: () => new Promise((resolve, reject) => { server.close(error => error ? reject(error) : resolve()); server.closeAllConnections(); }) };
}
module.exports = { startModelerFixture };
