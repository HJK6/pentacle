const test = require('node:test');
const assert = require('node:assert/strict');

const {
  computeOffscreenOrigin,
  rectIsOffscreen,
} = require('../main/alt_session_offscreen');

// The isolated alt-session acceptance window (PUBLIC_WALK_ALT) must land fully
// off EVERY display on ANY monitor topology so the user's live desktop never
// sees it (Loop Containment). Regression guard for the 2026-09 topology change that
// left a fixed off-screen coordinate on-screen (centered) after the display moved.

const WIN = { width: 1600, height: 1000 };

// (a) current hostb: single 2560x1440 at origin
const SINGLE = [{ bounds: { x: 0, y: 0, width: 2560, height: 1440 } }];
// (b) dual monitor with a negative-x left display
const DUAL = [
  { bounds: { x: 0, y: 0, width: 2560, height: 1440 } },
  { bounds: { x: -1920, y: 0, width: 1920, height: 1080 } },
];
// (c) a far-negative display that a fixed -32000 origin would land INSIDE
const FAR_NEG = [
  { bounds: { x: -33000, y: -33000, width: 3840, height: 2160 } },
  { bounds: { x: 0, y: 0, width: 2560, height: 1440 } },
];

function originToRect(origin) {
  return { x: origin.x, y: origin.y, width: WIN.width, height: WIN.height };
}

for (const [name, displays] of [['single', SINGLE], ['dual', DUAL], ['far-negative', FAR_NEG]]) {
  test(`computeOffscreenOrigin is disjoint from all displays: ${name}`, () => {
    const origin = computeOffscreenOrigin(displays, WIN);
    const rect = originToRect(origin);
    assert.equal(rectIsOffscreen(rect, displays), true,
      `origin ${JSON.stringify(origin)} must be off every display`);
    for (const d of displays) {
      const b = d.bounds;
      const overlaps = rect.x < b.x + b.width && rect.x + rect.width > b.x &&
        rect.y < b.y + b.height && rect.y + rect.height > b.y;
      assert.equal(overlaps, false, `must not overlap display ${JSON.stringify(b)}`);
    }
  });
}

test('a fixed -32000 origin is NOT universally safe (why topology-aware is required)', () => {
  const fixed = { x: -32000, y: -32000, width: WIN.width, height: WIN.height };
  // -32000 lands inside the far-negative display -> proves a constant is unsafe.
  assert.equal(rectIsOffscreen(fixed, FAR_NEG), false);
  // the derived origin stays off-screen on the same topology.
  assert.equal(rectIsOffscreen(originToRect(computeOffscreenOrigin(FAR_NEG, WIN)), FAR_NEG), true);
});

test('origin lands clearly off-screen (left <= -10000) so coarse isolation guards pass', () => {
  // The isolated-probe readback guard rejects a visible window unless it is clearly
  // off-screen. On the single-monitor topology the derived origin must be well into
  // the negative quadrant, matching the proven-honored hostb session-1 regime.
  const origin = computeOffscreenOrigin(SINGLE, WIN);
  assert.ok(origin.x <= -10000, `origin.x ${origin.x} must be <= -10000`);
  assert.ok(origin.y <= -10000, `origin.y ${origin.y} must be <= -10000`);
});

test('empty/absent topology falls back to an off-screen negative origin', () => {
  const origin = computeOffscreenOrigin([], WIN);
  assert.ok(origin.x < 0 && origin.y < 0);
});

test('rectIsOffscreen detects an on-screen (centered) rect as NOT off-screen', () => {
  const centered = { x: (2560 - 1600) / 2, y: (1440 - 1000) / 2, width: 1600, height: 1000 };
  assert.equal(rectIsOffscreen(centered, SINGLE), false);
});
