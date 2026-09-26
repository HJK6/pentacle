'use strict';
// spec_pentacle__web_update_available_refresh_icon_2026_09 — host build identity.
// The build id is a deterministic hash of the served web.html + JS + CSS: stable
// across byte-identical rebuilds (no nag on an unchanged-byte restart), changes
// when any served page/bundle/style byte changes, and ignores non-manifest
// churn (source maps/icons/fonts).

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { freezeWebDist, isManifestFile } = require('../server/web_build_id');

function makeDist(overrides = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-dist-'));
  const files = {
    'web.html': '<!doctype html><head><!--PENTACLE_CONFIG--></head><body></body>',
    'bundle.js': 'console.log("v1")',
    'styles.css': 'body{color:#fff}',
    'bundle.js.map': '{"version":3,"sources":["a"]}',
    'assets/favicon.png': 'PNGDATA',
    ...overrides,
  };
  for (const [rel, body] of Object.entries(files)) {
    const full = path.join(dir, rel);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, body);
  }
  return dir;
}

test('manifest membership: page + JS + CSS only', () => {
  assert.equal(isManifestFile('web.html'), true);
  assert.equal(isManifestFile('bundle.js'), true);
  assert.equal(isManifestFile('styles.css'), true);
  assert.equal(isManifestFile('bundle.js.map'), false);
  assert.equal(isManifestFile('assets/favicon.png'), false);
  assert.equal(isManifestFile('manifest.webmanifest'), false);
});

test('build id is deterministic across byte-identical dists (no nag on unchanged restart)', () => {
  const a = freezeWebDist(makeDist());
  const b = freezeWebDist(makeDist());
  assert.ok(a.buildId, 'a non-null id');
  assert.equal(a.buildId, b.buildId, 'identical bytes => identical id');
  // The frozen snapshot carries the served bytes (what serveStatic will return).
  assert.equal(a.files.get('web.html').toString().includes('<!--PENTACLE_CONFIG-->'), true);
});

test('changing a served bundle byte changes the id', () => {
  const base = freezeWebDist(makeDist());
  const changed = freezeWebDist(makeDist({ 'bundle.js': 'console.log("v2")' }));
  assert.notEqual(base.buildId, changed.buildId);
});

test('changing a served style byte changes the id', () => {
  const base = freezeWebDist(makeDist());
  const changed = freezeWebDist(makeDist({ 'styles.css': 'body{color:#000}' }));
  assert.notEqual(base.buildId, changed.buildId);
});

test('non-manifest churn (source map, icon) does not change the id', () => {
  const base = freezeWebDist(makeDist());
  const churn = freezeWebDist(makeDist({
    'bundle.js.map': '{"version":3,"sources":["different"]}',
    'assets/favicon.png': 'DIFFERENTPNG',
  }));
  assert.equal(base.buildId, churn.buildId, 'map/icon churn must not nag');
});

test('an unbuilt (empty/missing) dist yields a null id', () => {
  assert.equal(freezeWebDist(path.join(os.tmpdir(), 'pentacle-does-not-exist-xyz')).buildId, null);
});
