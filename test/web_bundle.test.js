const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {
  build, scriptSources, styleSources, EXTERNAL_CSS, EXTERNAL_SCRIPTS, BUNDLE_ANCHOR, OUT, RENDERER,
} = require('../scripts/build-web.js');

const INDEX_HTML = fs.readFileSync(path.join(RENDERER, 'index.html'), 'utf8');
const ENTRY = fs.readFileSync(path.join(RENDERER, 'web_entry.js'), 'utf8');

let built = false;
async function ensureBuilt() {
  if (!built) { await build(); built = true; }
}

test('every script index.html loads is either external or in web_entry.js, in order', () => {
  const fromIndex = scriptSources(INDEX_HTML);
  const bundled = [...ENTRY.matchAll(/require\('\.\/([^']+)'\)/g)]
    .map((m) => m[1])
    .filter((p) => p !== 'web_cc');

  // The web page reproduces index.html's script list exactly: the prebuilt
  // browser-ready files stay <script src> tags, the rest are bundled, and the
  // bundle takes app.js's place.
  const covered = fromIndex.filter((src) => !EXTERNAL_SCRIPTS[src] && src !== BUNDLE_ANCHOR);
  assert.deepEqual(bundled.filter((p) => p !== BUNDLE_ANCHOR), covered,
    'a script added to renderer/index.html must be added to renderer/web_entry.js or EXTERNAL_SCRIPTS');
  assert.equal(bundled[bundled.length - 1], BUNDLE_ANCHOR, 'app.js runs last, as in index.html');

  for (const src of Object.keys(EXTERNAL_SCRIPTS)) {
    assert.ok(fromIndex.includes(src), `EXTERNAL_SCRIPTS lists ${src}, which index.html no longer loads`);
  }
});

test('window.cc installs before app.js in the entry', () => {
  // app.js reads window.cc and window.HOST at module scope.
  const ccAt = ENTRY.indexOf("require('./web_cc').installWebCc()");
  const appAt = ENTRY.indexOf("require('./app.js')");
  assert.ok(ccAt >= 0, 'the entry must install window.cc');
  assert.ok(appAt > ccAt, 'app.js runs after window.cc exists');
});

test('the bundle has no runtime Electron or Node requires', async () => {
  await ensureBuilt();
  const bundle = fs.readFileSync(path.join(OUT, 'bundle.js'), 'utf8');

  for (const mod of ['electron', 'fs', 'path', 'os', 'child_process', 'node:fs', 'node:path']) {
    assert.equal(bundle.includes(`require("${mod}")`), false, `bundle still requires ${mod}`);
    assert.equal(bundle.includes(`require('${mod}')`), false, `bundle still requires ${mod}`);
  }
  // config-loader reads the config off disk; the browser gets it injected.
  assert.equal(bundle.includes('config-loader.js'), false, 'the disk config loader leaked into the bundle');
  assert.match(bundle, /__PENTACLE_CONFIG__/, 'the injected-config shim must be in the bundle');
});

test('web.html references only files served from the bundle directory', async () => {
  await ensureBuilt();
  const html = fs.readFileSync(path.join(OUT, 'web.html'), 'utf8');
  const refs = [...scriptSources(html), ...styleSources(html)];

  assert.ok(refs.length > 0);
  for (const ref of refs) {
    assert.ok(!ref.startsWith('/') && !ref.startsWith('..') && !/^[a-z]+:/i.test(ref),
      `web.html must not reference ${ref} outside the bundle directory`);
    assert.ok(fs.existsSync(path.join(OUT, ref)), `web.html references a missing file: ${ref}`);
  }
  const pageScripts = scriptSources(html);
  assert.deepEqual(pageScripts, [...Object.values(EXTERNAL_SCRIPTS), 'bundle.js'].filter((s2) => pageScripts.includes(s2)),
    'the page loads the external scripts plus the bundle, in index.html order');
  assert.equal(pageScripts[pageScripts.length - 1], 'bundle.js', 'the bundle runs last');
});

test('web.html carries the config injection point and keeps the desktop markup', async () => {
  await ensureBuilt();
  const html = fs.readFileSync(path.join(OUT, 'web.html'), 'utf8');

  assert.match(html, /<!--PENTACLE_CONFIG-->/);
  assert.ok(html.indexOf('<!--PENTACLE_CONFIG-->') < html.indexOf('<script src="bundle.js">'),
    'the config must be injected before the bundle runs');
  // The page is derived from index.html, so the app's DOM must survive verbatim.
  for (const id of ['titlebar-text', 'settings-list', 'toast-container']) {
    assert.ok(html.includes(`id="${id}"`), `web.html lost #${id} from index.html`);
  }
});

test('every external script is copied in beside the bundle', async () => {
  await ensureBuilt();
  for (const local of Object.values(EXTERNAL_SCRIPTS)) {
    assert.ok(fs.existsSync(path.join(OUT, local)), `missing external script copy: ${local}`);
  }
});

test('every stylesheet index.html pulls from outside renderer/ is copied in', async () => {
  await ensureBuilt();
  for (const href of styleSources(INDEX_HTML)) {
    const local = EXTERNAL_CSS[href] || href;
    assert.ok(fs.existsSync(path.join(OUT, local)), `missing stylesheet copy: ${local}`);
  }
});
