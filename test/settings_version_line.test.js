// Issue #13: show the running desktop/web build SHA and the connected daemon's
// runtime SHA as one unobtrusive line in the Settings footer. These assert the
// cross-mode wiring (shared window.cc bridge) at the source level, matching the
// repo's renderer-test idiom (see sidebar_usage_footer.test.js).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const read = (relativePath) => fs.readFileSync(path.join(root, relativePath), 'utf8');

test('settings footer declares the version line before the reload note', () => {
  const html = read('renderer/index.html');
  assert.match(html, /id="settings-version-line"/);
  assert.ok(
    html.indexOf('id="settings-version-line"') < html.indexOf('settings-reload-note'),
    'version line should precede the reload note in the footer',
  );
});

test('the version line is styled muted and unobtrusive', () => {
  const css = read('renderer/styles.css');
  assert.match(css, /\.settings-version-line\s*\{[^}]*font-size: 11px/);
  assert.match(css, /\.settings-version-line\s*\{[^}]*color: var\(--fg-dim\)/);
});

test('renderer builds "Desktop <build> · Daemon <runtime>" from the live snapshot', () => {
  const app = read('renderer/app.js');
  // Short SHA in the label, full SHA on hover.
  assert.match(app, /sha\.slice\(0, 7\) : 'unknown'/);
  assert.match(app, /`Desktop \$\{shortSha\(build\)\} · Daemon \$\{shortSha\(runtime\)\}`/);
  // The hover title gates on the same validity check as the label (no raw leak).
  assert.match(app, /versionLineEl\.title = `Desktop build: \$\{fullSha\(build\)\}\\nDaemon runtime: \$\{fullSha\(runtime\)\}`/);
  assert.doesNotMatch(app, /title = `Desktop build: \$\{build \|\| 'unknown'\}/);
  // Both SHAs come from the chat-stream snapshot (one place, both modes).
  assert.match(app, /window\.cc\?\.getChatStreamState\?\.\(\)/);
  assert.match(app, /snap\.build_sha/);
  assert.match(app, /snap\.runtime_sha/);
  // Refreshed each time the settings panel opens.
  const openIdx = app.indexOf('function open() {');
  const closeIdx = app.indexOf('const close = () =>', openIdx);
  assert.notEqual(openIdx, -1);
  assert.ok(app.slice(openIdx, closeIdx).includes('refreshVersionLine()'));
});

test('shortSha and fullSha both read "unknown" for an empty or malformed SHA', () => {
  const app = read('renderer/app.js');
  // Extract the three formatter one-liners from the settings panel and evaluate
  // them in isolation (they are pure). Guards the regression where a malformed
  // non-empty SHA showed verbatim in the hover title while the label said unknown.
  const start = app.indexOf('const isSha = (sha)');
  const endMarker = "const fullSha = (sha) => (isSha(sha) ? sha : 'unknown');";
  const end = app.indexOf(endMarker, start);
  assert.ok(start !== -1 && end !== -1, 'formatter helpers present');
  const src = app.slice(start, end + endMarker.length);
  const sandbox = {};
  vm.runInNewContext(`${src}\nglobalThis.__out = { shortSha, fullSha };`, sandbox);
  const { shortSha, fullSha } = sandbox.__out;

  const valid = 'a'.repeat(40);
  assert.equal(shortSha(valid), 'aaaaaaa');
  assert.equal(fullSha(valid), valid);
  for (const bad of ['', 'garbage', 'abc123', 'A'.repeat(40) + 'x', '$Format:%H$']) {
    assert.equal(shortSha(bad), 'unknown', `shortSha(${JSON.stringify(bad)})`);
    assert.equal(fullSha(bad), 'unknown', `fullSha(${JSON.stringify(bad)})`);
  }
});

test('chat-stream client resolves its build SHA across packaged/web/source', () => {
  const client = read('main/chat_stream_client.js');
  assert.match(client, /function resolveBuildSha\(\)/);
  assert.match(client, /pentacleBuildSha/); // packaged Electron
  assert.match(client, /build-sha\.txt/); // archived web release (export-subst)
  assert.match(client, /rev-parse', 'HEAD'/); // source checkout fallback
});

test('chat-stream client surfaces both SHAs in the snapshot (build + daemon runtime)', () => {
  const client = read('main/chat_stream_client.js');
  assert.match(client, /this\._runtimeSha = typeof msg\.runtime_sha === 'string' \? msg\.runtime_sha\.trim\(\) : ''/);
  assert.match(client, /build_sha: this\._buildSha,/);
  assert.match(client, /runtime_sha: this\._runtimeSha,/);
});

test('build-sha.txt carries the export-subst placeholder stamped on git archive', () => {
  assert.equal(read('build-sha.txt').trim(), '$Format:%H$');
  assert.match(read('.gitattributes'), /^build-sha\.txt export-subst$/m);
});
