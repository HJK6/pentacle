'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { SIDEBAR_MIN, SLOT_MIN, sidebarBounds, clampSidebarWidth,
  splitForDifference, normalizeSidebarWidth } = require('../renderer/sidebar_resizer');

test('two rows constrain sidebar at the first slot floor', () => {
  const bounds = sidebarBounds(1600, [0, 400]);
  assert.deepEqual(bounds, { min: SIDEBAR_MIN, max: 1600 - (2 * SLOT_MIN + 400 + 1) });
  assert.equal(clampSidebarWidth(1400, bounds), bounds.max);
  assert.equal(clampSidebarWidth(100, bounds), SIDEBAR_MIN);
});

test('equal and unequal rows exchange half the sidebar delta in both directions', () => {
  for (const difference of [0, 180, -320]) {
    for (const delta of [140, -100]) {
      const before = splitForDifference(1000, difference);
      const after = splitForDifference(1000 - delta, difference);
      assert.equal(after.left - before.left, -delta / 2);
      assert.equal(after.right - before.right, -delta / 2);
      assert.equal(after.left - after.right, difference);
    }
  }
});

test('narrow one-slot bounds and saved preference remain separate', () => {
  const preferred = normalizeSidebarWidth(600);
  assert.equal(preferred, 600);
  assert.equal(clampSidebarWidth(preferred, sidebarBounds(620, [], false)), 400);
  assert.equal(clampSidebarWidth(preferred, sidebarBounds(1600, [], false)), 600);
});
