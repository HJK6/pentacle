'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { build, OUT } = require('../scripts/build-web');
const { serveStatic } = require('../server');

test('built /glasses aliases serve the second entry and inject the existing host config', async () => {
  await build();
  const frozen = { files: new Map() };
  for (const file of ['glasses.html', 'glasses.css', 'glasses.bundle.js']) frozen.files.set(file, fs.readFileSync(path.join(OUT, file)));
  const config = { features: { chatUi: true }, hostname: 'host-a' };
  for (const route of ['/glasses', '/glasses/', '/glasses.html']) {
    let status, headers, body;
    const res = { writeHead(code, values) { status = code; headers = values; return this; }, end(value) { body = String(value); } };
    serveStatic(res, route, config, frozen);
    assert.equal(status, 200);
    assert.match(headers['content-type'], /text\/html/);
    assert.match(body, /width=600,height=600/);
    assert.match(body, /window\.__PENTACLE_CONFIG__=/);
    assert.match(body, /glasses\.bundle\.js/);
    assert.doesNotMatch(body, /<!--PENTACLE_CONFIG-->/);
  }
  const html = String(frozen.files.get('glasses.html'));
  for (const match of html.matchAll(/(?:href|src)="(\/[^\"]+)"/g)) {
    assert.ok(fs.existsSync(path.join(OUT, match[1])), `built resource missing: ${match[1]}`);
  }
  const bundle = String(frozen.files.get('glasses.bundle.js'));
  assert.doesNotMatch(bundle, /require\(["'](?:electron|node:fs|child_process)["']\)/);
  assert.match(bundle, /getAssistantBinding/);
  assert.match(bundle, /chatSendCorrelated/);
});
