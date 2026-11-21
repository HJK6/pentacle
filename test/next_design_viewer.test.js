'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { JSDOM } = require('jsdom');
const { createDesignViewer } = require('../renderer/next/design_viewer');

const home = { type: 'screen', view: 'first', title: 'First', x: 0, y: 0, w: 800, h: 600 };
const session = { ...home, view: 'second', title: 'Second', x: 1040 };
const target = { project_id: 'delivery', design_id: 'board', view_id: 'second' };

function sample(revision, names = ['board']) {
  const time = (index) => '2026-11-21T10:' + String(revision * 10 + index).padStart(2, '0') + ':00Z';
  return {
    schema: 'design-catalog.v1',
    projects: [{
      id: 'delivery',
      title: 'Delivery',
      revision_id: String(revision),
      manifest: {
        views: [
          { id: 'first', title: 'First', screenshots: [{ path: 'screenshots/first.png' }] },
          { id: 'second', title: 'Second', prototype: true, variant: 'prototype' },
        ],
      },
      designs: names.map((id, index) => ({
        id, title: id === 'board' ? 'Delivery board' : 'New design',
        changed_at: time(index), nodes: revision > 2 ? [home] : [home, session],
      })),
    }],
  };
}

function setup(t, options = {}) {
  const dom = new JSDOM('<main id="app"></main>', { url: 'http://example.test/next/design' });
  const { window } = dom;
  const root = window.document.getElementById('app');
  Object.defineProperties(window.HTMLElement.prototype, {
    clientWidth: { get: () => 900 },
    clientHeight: { get: () => 600 },
  });
  const requests = [], handled = [];
  const queue = [];
  const fetch = async (url, opts) => {
    requests.push({ url, opts });
    if (url !== '/api/designs') return Response.json({ document: '<p>Live sample</p>' });
    assert.ok(queue.length, 'test provides a catalog response');
    return queue.shift();
  };
  const viewer = createDesignViewer(root, { ...options, fetch, onNavigate: entry => handled.push(entry) });
  t.after(() => { viewer.close(); window.close(); });
  return {
    window, root, viewer, requests, handled,
    response: (body, revision) => queue.push(Response.json(body, { headers: { ETag: '"' + revision + '"' } })),
    status: status => queue.push(new Response(null, { status })),
  };
}

test('Design refresh updates Home conditionally and keeps its scroll and opened indicator', async t => {
  const ui = setup(t);
  ui.response(sample(1), 1);
  await ui.viewer.open();
  assert.equal(ui.root.querySelector('.design-tile-name').textContent, 'Delivery board');
  assert.ok(ui.root.querySelector('.design-unread'));
  assert.ok(ui.root.querySelector('.design-tile-prototype'));
  ui.root.querySelector('.design-tile').click();
  ui.root.querySelector('[aria-label="Back"]').click();
  assert.equal(ui.root.querySelector('.design-unread'), null);
  ui.root.querySelector('.design-home').scrollTop = 37;
  ui.status(304);
  await ui.viewer.refresh();
  assert.equal(ui.requests.at(-1).opts.headers['If-None-Match'], '"1"');
  assert.equal(ui.root.querySelector('.design-home').scrollTop, 37);

  const next = sample(2, ['board', 'new']);
  next.projects[0].designs[0].changed_at = sample(1).projects[0].designs[0].changed_at;
  ui.response(next, 2);
  await ui.viewer.refresh();
  assert.deepEqual([...ui.root.querySelectorAll('.design-tile-name')].map(el => el.textContent), ['New design', 'Delivery board']);
  assert.equal(ui.root.querySelectorAll('.design-unread').length, 1);
  assert.equal(ui.root.querySelector('.design-home').scrollTop, 37);
});

test('Design shows a pending revision without replacing a live board, then keeps camera and current screen', async t => {
  const ui = setup(t);
  ui.response(sample(1), 1);
  await ui.viewer.open(target);
  await new Promise(setImmediate);
  assert.deepEqual(ui.handled.at(-1), target);
  const world = ui.root.querySelector('.design-world');
  const transform = world.style.transform;
  const frame = ui.root.querySelector('iframe');
  assert.ok(frame);
  assert.equal(frame.getAttribute('sandbox'), 'allow-scripts');
  assert.equal(ui.root.querySelector('.design-update').hidden, true);
  ui.status(503);
  await ui.viewer.refresh();
  assert.equal(ui.root.querySelector('iframe'), frame);
  ui.response(sample(2), 2);
  await ui.viewer.refresh();
  assert.equal(ui.root.querySelector('iframe'), frame);
  assert.equal(ui.root.querySelector('.design-update').hidden, false);
  ui.root.querySelector('[aria-label="Show new version"]').click();
  await new Promise(setImmediate);
  assert.deepEqual(ui.handled.at(-1), target);
  assert.equal(ui.root.querySelector('.design-world').style.transform, transform);
  assert.notEqual(ui.root.querySelector('iframe'), frame);
  assert.equal(ui.requests.at(-1).url, '/api/designs/delivery/2/second');
  ui.response(sample(3), 3);
  await ui.viewer.refresh();
  ui.root.querySelector('[aria-label="Show new version"]').click();
  assert.deepEqual(ui.handled.at(-1), { project_id: 'delivery', design_id: 'board' });
});

test('Design retries a failed first load and resolves deleted targets at their supported depth', async t => {
  const ui = setup(t, { embedded: true });
  ui.status(503);
  await ui.viewer.open({ ...target, view_id: 'deleted' });
  assert.match(ui.root.textContent, /Design did not load/);
  ui.response(sample(1), 1);
  ui.root.querySelector('button[title="Try again"]').click();
  await new Promise(setImmediate);
  assert.deepEqual(ui.handled.at(-1), { project_id: 'delivery', design_id: 'board' });
  assert.equal(ui.root.querySelector('.design-board').hidden, false);
  assert.ok(ui.root.classList.contains('design-phone'));
  assert.equal(ui.root.querySelector('.design-card-actions'), null);
  ui.response(sample(1), 1);
  await ui.viewer.open({ ...target, design_id: 'deleted' });
  assert.equal(ui.handled.at(-1), null);
  assert.match(ui.root.querySelector('.design-notice').textContent, /no longer available/);
});
