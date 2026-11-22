'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {
  arrowGeometry, boardBounds, fitBoard, fitScreen, getDesignTarget,
  keepScreenLabel, stepScreen, zoomCamera,
} = require('../renderer/next/design_board');

const screen = (view, x, y, w = 390, h = 844) => ({ type: 'screen', view, x, y, w, h });
const home = screen('home', 0, 0);
const detail = screen('detail', 850, 0);
const done = screen('done', 850, 1200);

test('Design camera fits labeled bounds, zooms about its pointer and clamps to the board limits', () => {
  const nodes = [home, detail, done, { type: 'section', x: -70, y: -90, w: 1380, h: 2200 }];
  const viewport = { width: 700, height: 780 };
  assert.deepEqual(boardBounds(nodes), { x: -70, y: -90, w: 1380, h: 2200 });
  const fit = fitBoard(nodes, viewport, false);
  assert.ok(fit.z > 0 && fit.z < 0.4);
  const anchor = { x: 280, y: 350 };
  const zoom = zoomCamera(fit, 1.4, anchor, fit.z);
  assert.ok(Math.abs((anchor.x - zoom.x) / zoom.z - (anchor.x - fit.x) / fit.z) < 1e-8);
  assert.ok(Math.abs((anchor.y - zoom.y) / zoom.z - (anchor.y - fit.y) / fit.z) < 1e-8);
  assert.equal(zoomCamera(fit, 0.001, anchor, fit.z).z, fit.z / 2);
  assert.equal(zoomCamera(fit, 100, anchor, fit.z).z, 2.5);
  assert.ok(fitScreen(home, viewport, true).z > fit.z);
  assert.equal(keepScreenLabel(390, 0.2, true), false);
  assert.equal(keepScreenLabel(390, 0.21, true), true);
  assert.equal(keepScreenLabel(390, 0.02, false), true);
});

test('Design steps through author order in either direction and stops at each end', () => {
  const steps = [detail, home, done];
  assert.equal(stepScreen(steps, null, 1), detail);
  assert.equal(stepScreen(steps, null, -1), done);
  assert.equal(stepScreen(steps, 'home', 1), done);
  assert.equal(stepScreen(steps, 'home', -1), detail);
  assert.equal(stepScreen(steps, 'done', 1), null);
  assert.equal(stepScreen(steps, 'detail', -1), null);
  assert.equal(stepScreen([], null, 1), null);
});

test('arrows point between screen edges and their labels disappear when zoom leaves insufficient room', () => {
  const right = arrowGeometry(home, detail);
  assert.ok(right.start.x > home.w);
  assert.ok(right.end.x < detail.x);
  assert.ok(right.start.y === right.end.y);
  assert.match(right.d, /^M.+ C/);
  assert.ok(right.length > 100);
  assert.ok(right.length * 0.1 < 62);
  assert.ok(right.length * 0.6 > 62);

  const down = arrowGeometry(detail, done);
  assert.ok(down.start.y > detail.h && down.end.y < done.y);
  const moved = arrowGeometry(home, { ...detail, x: 1000, y: 250 });
  assert.notEqual(moved.d, right.d);
});

test('Design addresses resolve current boards, missing screens and missing designs independently', () => {
  const design = { id: 'journey', nodes: [home, detail] };
  const project = { id: 'fixture', designs: [design] };
  const target = { project_id: project.id, design_id: design.id };
  assert.deepEqual(getDesignTarget([project], target), { project, design, screen: null, missing: false });
  assert.equal(getDesignTarget([project], { ...target, view_id: 'detail' }).screen, detail);
  assert.equal(getDesignTarget([project], { ...target, view_id: 'missing' }).screen, null);
  assert.equal(getDesignTarget([project], { ...target, design_id: 'missing' }).missing, true);
  assert.deepEqual(getDesignTarget([project], null), { project: null, design: null, screen: null, missing: false });
});
