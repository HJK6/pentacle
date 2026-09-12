'use strict';
// Regression: the Limits (#usage-section) body rule is an ID selector, which
// outranks `.sidebar-collapsible-section.is-collapsed .sidebar-section-body`
// unless it excludes the collapsed state. Without :not(.is-collapsed) the
// section can never collapse (observed 2026-09-12 in web mode and desktop).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

test('usage section display rule excludes the collapsed state', () => {
  const css = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');
  const blocks = css.match(/#usage-section[^{]*\{[^}]*display:\s*grid[^}]*\}/g) || [];
  assert.ok(blocks.length >= 1, 'expected a #usage-section display:grid rule');
  for (const block of blocks) {
    const selector = block.slice(0, block.indexOf('{'));
    assert.match(selector, /#usage-section:not\(\.is-collapsed\)/, `selector must exclude .is-collapsed: ${selector.trim()}`);
  }
  assert.match(css, /\.sidebar-collapsible-section\.is-collapsed \.sidebar-section-body\s*\{\s*display:\s*none/, 'collapse rule present');
});
