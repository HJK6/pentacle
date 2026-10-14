'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const esbuild = require('esbuild');
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
  const unminified = await esbuild.build({
    entryPoints: [path.resolve(__dirname, '../renderer/src/glasses_entry.tsx')],
    bundle: true, format: 'iife', platform: 'browser', target: 'chrome134',
    define: { 'process.env.NODE_ENV': '"production"' }, minify: false, write: false,
  });
  assert.ok(Buffer.byteLength(bundle) < unminified.outputFiles[0].contents.length,
    'the default glasses build must be smaller than its unminified production equivalent');
  const map = JSON.parse(fs.readFileSync(path.join(OUT, 'glasses.bundle.js.map'), 'utf8'));
  assert.equal(Object.hasOwn(map, 'sourcesContent'), false, 'source contents remain excluded');
});
