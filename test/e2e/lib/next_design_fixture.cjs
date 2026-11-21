'use strict';

const path = require('node:path');
const { HTML, revision } = require('../../fixtures/design_library.cjs');
const { publishRevision } = require('../../../lib/design_library.cjs');

const project = 'delivery';
const board = 'delivery-board';
const prototype = 'prototype';
const phone = 'phone';
const legacy = 'legacy';
const label = 'Delivery board';
const route = '/' + project + '/' + board;
const observedHtml = HTML.replace('</body>', `<script>
  window.addEventListener('message', event => {
    if (event.source !== window.parent || event.data !== 'read-design-fixture') return;
    const button = document.getElementById('session').getBoundingClientRect();
    window.parent.postMessage({
      type: 'design-fixture-state',
      state: document.querySelector('#state').textContent,
      button: [(button.left + button.width / 2) / innerWidth, (button.top + button.height / 2) / innerHeight],
    }, '*');
  });
</script></body>`);

function fixtureBoard(generation) {
  return {
    id: board, title: label, nodes: [
      { type: 'section', title: 'Order flow', x: -120, y: -130, w: 2480, h: 860 },
      { type: 'screen', view: 'home', x: 0, y: 0 },
      { type: 'screen', view: prototype, x: 1300, y: 0 },
      { type: 'arrow', from: 'home', to: prototype, label: 'Open order' },
      { type: 'note', text: generation === 'b' ? 'New delivery note' : 'Delivery note', x: 460, y: 870, w: 540, h: 180 },
    ],
  };
}

async function stageFixture(scratch, { generation = 'a', additional = true } = {}) {
  const stage = path.join(scratch, 'next-design-' + generation);
  const delivery = await revision(stage, {
    id: project,
    html: observedHtml,
    importedAt: generation === 'a' ? '2026-01-01T00:00:00Z' : '2026-01-02T00:00:00Z',
    configure(drop) {
      const first = drop.views[0];
      drop.title = 'Delivery';
      drop.source_revision = 'web-gate-' + generation;
      drop.views = [
        { ...first, title: 'Orders', group: 'order-flow' },
        { ...first, id: prototype, title: 'Order prototype', variant: 'prototype', group: 'order-flow' },
        { ...first, id: 'solo', title: 'Order confirmation', variant: 'prototype' },
        { ...first, id: phone, title: 'Phone order', group: 'phone-flow', viewport: { width: 390, height: 844 } },
        { ...first, id: 'phone-detail', title: 'Phone detail', group: 'phone-flow', viewport: { width: 390, height: 844 } },
      ];
      drop.designs = [
        fixtureBoard(generation),
        { id: 'order-prototype', title: 'Single prototype', nodes: [{ type: 'screen', view: 'solo' }] },
        { id: 'phone-orders', title: 'Phone orders', nodes: [{ type: 'screen', view: phone }, { type: 'screen', view: 'phone-detail' }] },
      ];
      if (generation === 'b') {
        drop.views.push({ ...first, id: 'new-screen', title: 'Updated order' });
        drop.designs.push({ id: 'delivery-update', title: 'Delivery update', nodes: [{ type: 'screen', view: 'new-screen' }] });
      }
    },
  });
  const library = path.join(scratch, 'design-library');
  publishRevision({ dir: delivery.dir, library });
  if (additional) {
    const older = await revision(stage, { id: legacy, html: observedHtml, legacyPreparation: true, configure(drop) {
      drop.title = 'Archive';
      const first = drop.views[0];
      drop.views = [
        { ...first, title: 'Old Orders', variant: 'prototype' },
        { ...first, id: 'detail', title: 'Old Detail' },
      ];
      drop.designs = [{ id: 'old-orders', title: 'Old orders', nodes: [
        { type: 'screen', view: 'home' }, { type: 'screen', view: 'detail' },
      ] }];
    } });
    publishRevision({ dir: older.dir, library });
  }
  return { library, revision: delivery.manifest.revision_id };
}

module.exports = { stageFixture, project, prototype, phone, legacy, route, label };
