'use strict';

// Compute a safe off-screen origin for a visible test window. Keeping the
// window outside every display lets a renderer continue painting while a
// local visual or timing test remains invisible to the user.
const DEFAULT_CLEARANCE = 20000;

function boundsOf(display) {
  return (display && (display.bounds || display)) || null;
}

function computeOffscreenOrigin(displays, winSize, clearance = DEFAULT_CLEARANCE) {
  const rects = (displays || []).map(boundsOf).filter(Boolean);
  if (!rects.length) {
    return { x: -(winSize.width + clearance), y: -(winSize.height + clearance) };
  }
  const minX = Math.min(...rects.map((rect) => rect.x));
  const minY = Math.min(...rects.map((rect) => rect.y));
  return { x: minX - winSize.width - clearance, y: minY - winSize.height - clearance };
}

function rectIsOffscreen(rect, displays) {
  const rects = (displays || []).map(boundsOf).filter(Boolean);
  return rects.every((bounds) => (
    rect.x + rect.width <= bounds.x
    || rect.x >= bounds.x + bounds.width
    || rect.y + rect.height <= bounds.y
    || rect.y >= bounds.y + bounds.height
  ));
}

module.exports = { computeOffscreenOrigin, rectIsOffscreen, DEFAULT_CLEARANCE };
