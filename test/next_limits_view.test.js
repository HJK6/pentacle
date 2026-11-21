'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const {
  formatPercent,
  machineCpuPercent,
  mountLimitsView,
  renderLimitsView,
  usagePercent,
} = require('../renderer/next/limits_view');

const GIB = 1024 ** 3;
const NOW = Date.parse('2026-11-20T00:00:00.000Z');

function sampleHost(overrides = {}) {
  return {
    cpu_usage_pct: 43.4,
    memory_used_bytes: 18 * GIB,
    memory_total_bytes: 50 * GIB,
    disk_used_bytes: 3 * GIB,
    disk_total_bytes: 4 * GIB,
    sampled_at: new Date(NOW).toISOString(),
    ...overrides,
  };
}

test('limits and machines render as the compact approved popover rows', () => {
  const dom = new JSDOM('<!doctype html><body></body>');
  const { document } = dom.window;
  const view = renderLimitsView(document, {
    limits: [
      { id: 'usage-a', label: 'Usage A', pct: 26 },
      { id: 'usage-b', label: 'Usage B', pct: 0 },
      { id: 'usage-c', label: 'Usage C', pct: null },
    ],
    hosts: {
      'machine-alpha': sampleHost(),
      'machine-beta': sampleHost({ cpu_usage_pct: null, memory_used_bytes: null, disk_total_bytes: 0 }),
    },
  }, {
    now: () => NOW,
    hostLabel: host => ({ 'machine-alpha': 'Machine Alpha', 'machine-beta': 'Machine Beta' })[host],
  });

  assert.deepEqual([...view.querySelectorAll('h3')].map(heading => heading.textContent), ['Limits', 'Machines']);
  assert.equal(view.querySelectorAll('.prow').length, 3);
  assert.equal(view.querySelector('.prow[data-limit-id="usage-a"] .num').textContent, '26%');
  assert.equal(view.querySelector('.prow[data-limit-id="usage-a"] .bar i').style.width, '26%');
  assert.equal(view.querySelector('.prow[data-limit-id="usage-c"] .num').textContent, '—');
  assert.equal(view.querySelector('.prow[data-limit-id="usage-c"] .bar').getAttribute('aria-valuetext'), '—');

  assert.equal(view.querySelector('.hrow.hd').textContent, 'CPURAMDisk');
  assert.equal(view.querySelectorAll('.hrow:not(.hd)').length, 2);
  const first = view.querySelector('.hrow:not(.hd)');
  assert.equal(first.querySelector('.machine-name').textContent, 'Machine Alpha');
  assert.deepEqual([...first.querySelectorAll('.num')].map(cell => cell.textContent), ['43%', '36%', '75%']);
  assert.deepEqual([...first.children].map(cell => cell.getAttribute('role')), ['rowheader', 'cell', 'cell', 'cell']);

  const second = view.querySelectorAll('.hrow:not(.hd)')[1];
  assert.deepEqual([...second.querySelectorAll('.num')].map(cell => cell.textContent), ['--', '--', '--']);
  assert.equal(view.querySelector('.machines-table').getAttribute('role'), 'table');
  assert.equal(view.querySelector('.machines-table').getAttribute('aria-label'), 'Machine statistics');
  assert.equal(view.querySelector('.machine-stat-card'), null);
  assert.equal(view.textContent.includes('Up '), false);
  dom.window.close();
});

test('percent formatting mirrors the current web for missing, bounded and capacity-derived values', () => {
  assert.equal(formatPercent(43.5), '44%');
  assert.equal(formatPercent(null), '--');
  assert.equal(formatPercent(-0.1), '--');
  assert.equal(formatPercent(100.1), '--');
  assert.equal(formatPercent(null, '—'), '—');
  assert.equal(usagePercent(150, 100), 100);
  assert.equal(usagePercent(-4, 100), 0);
  assert.equal(usagePercent(4, 0), null);
  assert.equal(usagePercent(null, 100), null);
  assert.equal(machineCpuPercent(sampleHost({ cpu_usage_pct: 101 }), NOW, 90_000), null);
  assert.equal(machineCpuPercent(sampleHost(), NOW + 90_001, 90_000), null);
});

test('machines keep local-first, configured roster, then alphabetical fallback order', () => {
  const dom = new JSDOM('<!doctype html><body></body>');
  const document = dom.window.document;
  const view = renderLimitsView(document, {
    hosts: {
      'machine-zeta': sampleHost(),
      'machine-roster-b': sampleHost(),
      'machine-alpha': sampleHost(),
      'machine-roster-a': sampleHost(),
      'machine-local': sampleHost(),
    },
  }, {
    now: () => NOW,
    localHost: 'machine-local',
    hostOrder: ['machine-roster-b', 'machine-roster-a'],
    hostLabel: host => host,
  });

  assert.deepEqual(
    [...view.querySelectorAll('.hrow:not(.hd)')].map(row => row.dataset.machineHost),
    ['machine-local', 'machine-roster-b', 'machine-roster-a', 'machine-alpha', 'machine-zeta'],
  );
  dom.window.close();
});

test('mount updates current values, reevaluates stale CPU data, and destroys its timer and DOM', () => {
  const dom = new JSDOM('<!doctype html><body><div id="popover"></div></body>');
  const container = dom.window.document.querySelector('#popover');
  let now = NOW;
  let scheduled = null;
  let cleared = null;
  const view = mountLimitsView(container, {
    limits: [{ id: 'usage-a', label: 'Usage A', pct: 18 }],
    hosts: { 'machine-alpha': sampleHost() },
    now: () => now,
    staleAfterMs: 90_000,
    staleRefreshMs: 30_000,
    setInterval(callback, ms) {
      scheduled = { callback, ms };
      return 7;
    },
    clearInterval(id) { cleared = id; },
  });

  assert.equal(container.querySelector('.hrow:not(.hd) .num').textContent, '43%');
  assert.equal(scheduled.ms, 30_000);
  now += 90_001;
  scheduled.callback();
  assert.equal(container.querySelector('.hrow:not(.hd) .num').textContent, '--');

  view.update({ hosts: { 'machine-alpha': sampleHost({ cpu_usage_pct: 51.6, sampled_at: new Date(now).toISOString() }) } });
  assert.equal(container.querySelector('.hrow:not(.hd) .num').textContent, '52%');
  view.destroy();
  assert.equal(cleared, 7);
  assert.equal(container.childElementCount, 0);
  scheduled.callback();
  view.update({ limits: [{ id: 'usage-b', label: 'Usage B', pct: 99 }] });
  assert.equal(container.childElementCount, 0);
  view.destroy();
  dom.window.close();
});
