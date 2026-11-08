'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
test('the Settings reload button is wired to window.cc.reloadApp, not a bare location.reload()', () => {
  const source = fs.readFileSync(path.join(root, 'renderer', 'app.js'), 'utf8');
  const start = source.indexOf("reloadBtn?.addEventListener('click'");
  assert.ok(start >= 0, 'the reload button click handler is present');
  const handler = source.slice(start, source.indexOf('});', start) + 3);
  assert.match(handler, /window\.cc\??\.reloadApp/,
    'the reload button routes through window.cc.reloadApp');
});
