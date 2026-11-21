'use strict';

const boardPadding = {
  desktop: { left: 40, right: 40, top: 56, bottom: 72 },
  phone: { left: 14, right: 14, top: 40, bottom: 70 },
};
const screenPadding = {
  desktop: { left: 64, right: 64, top: 44, bottom: 72 },
  phone: { left: 12, right: 12, top: 34, bottom: 66 },
};

function boardBounds(nodes) {
  let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
  for (const { x, y, w, h } of nodes) {
    if (![x, y, w, h].every(Number.isFinite) || w <= 0 || h <= 0) continue;
    left = Math.min(left, x);
    top = Math.min(top, y);
    right = Math.max(right, x + w);
    bottom = Math.max(bottom, y + h);
  }
  if (left === Infinity) return { x: 0, y: 0, w: 1, h: 1 };
  return { x: left, y: top, w: right - left, h: bottom - top };
}

function fitRect(rect, viewport, padding, limit) {
  const width = Math.max(1, viewport.width - padding.left - padding.right);
  const height = Math.max(1, viewport.height - padding.top - padding.bottom);
  const z = Math.min(width / rect.w, height / rect.h, limit);
  return {
    z,
    x: padding.left + (width - rect.w * z) / 2 - rect.x * z,
    y: padding.top + (height - rect.h * z) / 2 - rect.y * z,
  };
}

function fitBoard(nodes, viewport, phone) {
  return fitRect(boardBounds(nodes), viewport, phone ? boardPadding.phone : boardPadding.desktop, 1);
}

function fitScreen(screen, viewport, phone) {
  return fitRect(screen, viewport, phone ? screenPadding.phone : screenPadding.desktop, 1.25);
}

function zoomCamera(camera, factor, anchor, fitZoom) {
  const z = Math.max(fitZoom / 2, Math.min(2.5, camera.z * factor));
  return {
    z,
    x: anchor.x - (anchor.x - camera.x) * z / camera.z,
    y: anchor.y - (anchor.y - camera.y) * z / camera.z,
  };
}

function stepScreen(screens, current, step) {
  const activeIndex = screens.findIndex(screen => screen.view === current);
  const index = activeIndex === -1 ? (step < 0 ? screens.length - 1 : 0) : activeIndex + step;
  return screens[index] ?? null;
}

function keepScreenLabel(width, zoom, phone) {
  return !phone || width * zoom >= 80;
}

function getDesignTarget(projects, target) {
  if (!target) return { project: null, design: null, screen: null, missing: false };
  const project = projects.find(item => item.id === target.project_id);
  const design = project?.designs.find(item => item.id === target.design_id);
  if (!design) return { project: null, design: null, screen: null, missing: true };
  const screen = design.nodes.find(node => node.type === 'screen' && node.view === target.view_id);
  return { project, design, screen: screen ?? null, missing: false };
}

function arrowPorts(from, to) {
  if (to.x >= from.x + from.w) {
    const gap = Math.min(26, (to.x - from.x - from.w) / 3);
    return [
      { x: from.x + from.w + gap, y: from.y + from.h / 2, dx: 1, dy: 0 },
      { x: to.x - gap, y: to.y + to.h / 2, dx: -1, dy: 0 },
    ];
  }
  if (from.x >= to.x + to.w) {
    const gap = Math.min(26, (from.x - to.x - to.w) / 3);
    return [
      { x: from.x - gap, y: from.y + from.h / 2, dx: -1, dy: 0 },
      { x: to.x + to.w + gap, y: to.y + to.h / 2, dx: 1, dy: 0 },
    ];
  }
  if (to.y >= from.y) {
    const gap = Math.max(0, Math.min(26, (to.y - from.y - from.h) / 3));
    return [
      { x: from.x + from.w * 0.72, y: from.y + from.h + gap, dx: 0, dy: 1 },
      { x: to.x + to.w * 0.72, y: to.y - gap, dx: 0, dy: -1 },
    ];
  }
  const gap = Math.max(0, Math.min(26, (from.y - to.y - to.h) / 3));
  return [
    { x: from.x + from.w * 0.72, y: from.y - gap, dx: 0, dy: -1 },
    { x: to.x + to.w * 0.72, y: to.y + to.h + gap, dx: 0, dy: 1 },
  ];
}

function cubicAt(t, start, p1, p2, end) {
  const u = 1 - t;
  const a = u ** 3, b = 3 * u ** 2 * t, c = 3 * u * t ** 2, d = t ** 3;
  return {
    x: a * start.x + b * p1.x + c * p2.x + d * end.x,
    y: a * start.y + b * p1.y + c * p2.y + d * end.y,
  };
}

function arrowGeometry(from, to) {
  const [start, end] = arrowPorts(from, to);
  const offset = Math.min(220, Math.hypot(end.x - start.x, end.y - start.y) / 2);
  const p1 = { x: start.x + start.dx * offset, y: start.y + start.dy * offset };
  const p2 = { x: end.x + end.dx * offset, y: end.y + end.dy * offset };
  let length = 0, previous = start;
  for (let step = 1; step <= 12; step++) {
    const next = cubicAt(step / 12, start, p1, p2, end);
    length += Math.hypot(next.x - previous.x, next.y - previous.y);
    previous = next;
  }
  const label = cubicAt(start.dx ? 0.5 : 0.38, start, p1, p2, end);
  const d = `M${start.x},${start.y} C${p1.x},${p1.y} ${p2.x},${p2.y} ${end.x},${end.y}`;
  return { d, start, end, label, length };
}

module.exports = {
  arrowGeometry, boardBounds, fitBoard, fitScreen, getDesignTarget,
  keepScreenLabel, stepScreen, zoomCamera,
};
