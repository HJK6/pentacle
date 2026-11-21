'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
test('next stylesheet keeps colour and typeface literals in its theme tokens', () => {
  const css = fs.readFileSync(path.join(__dirname, '../renderer/next/style.css'), 'utf8');
  const marker = '/* Theme tokens end here. */';
  assert.ok(css.includes(marker));
  const rules = css.split(marker)[1].replace(/\/\*[\s\S]*?\*\//g, '');
  assert.doesNotMatch(rules, /#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?)\s*\(|font-family\s*:/i);
});
