'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { JSDOM } = require('jsdom');

// Render the real card function with fixed host data, then measure its CSS in
// Chrome. This avoids a hand-written HTML copy that could pass after a markup
// regression.
function cardMarkup() {
  const source = fs.readFileSync(path.join(__dirname, '../../../renderer/app.js'), 'utf8');
  const start = source.indexOf('function renderHostsStats(');
  const code = source.slice(start, source.indexOf('\n}', start) + 2);
  const document = new JSDOM('<div id="machine-stats-section"></div><div id="machine-stats-footer"></div>').window.document;
  const gib = 1024 ** 3;
  const context = {
    document, state: { chatStream: { hostsStats: {} } }, HOST_IDS: ['thoth'],
    streamHostForHostId: id => id === 'local' ? 'thoth' : id,
    _streamHostToHostId: id => id, getSourceForSession: () => 'Thoth',
    getSourceColorForSession: () => 'yellow', esc: String,
    machineSigilMarkup: () => '<svg aria-hidden="true"></svg>',
    statUsagePct: (used, total) => used / total * 100,
    machineStatsIsStale: () => false,
    statNumber: value => value == null ? null : Number(value),
    fmtStatPct: value => value == null ? '--' : Math.round(value) + '%',
    fmtStatBytes: value => Math.round(value / gib) + ' GB',
    fmtStatUptime: () => '2h', usageBarClass: () => 'low',
  };
  vm.runInNewContext(code, context);
  context.renderHostsStats({ thoth: {
    cpu_load_1m: 2.59, cpu_usage_pct: 24.3,
    memory_used_bytes: 12 * gib, memory_total_bytes: 48 * gib,
    disk_used_bytes: 125 * gib, disk_total_bytes: 500 * gib,
    uptime_seconds: 7200, sampled_at: new Date().toISOString(),
  } });
  return document.getElementById('machine-stats-footer').innerHTML;
}

async function machineStatsLayout(session) {
  const markup = cardMarkup();
  const results = [];
  try {
    await session.eval(`(() => { const panel = document.createElement('div'); panel.id = 'machine-stats-layout-proof'; panel.style.cssText = 'position:fixed;left:-9999px;top:0;visibility:hidden'; panel.innerHTML = ${JSON.stringify(markup)}; document.body.append(panel); })()`);
    for (const width of [230, 150]) {
      const result = await session.eval(`(() => {
        const panel = document.getElementById('machine-stats-layout-proof');
        panel.style.width = '${width}px';
        const card = panel.querySelector('.machine-stat-card');
        const rows = [...card.querySelectorAll('.machine-stat-row')];
        const sameLine = row => { const children = [...row.children]; return children.every(child => Math.abs(child.getBoundingClientRect().top - children[0].getBoundingClientRect().top) < 2); };
        return {
          width: ${width}, cardWidth: card.getBoundingClientRect().width,
          text: rows.map(row => row.textContent),
          sameLine: rows.map(sameLine),
          overflow: card.scrollWidth > card.clientWidth + 1 || rows.some(row => row.scrollWidth > row.clientWidth + 1),
        };
      })()`);
      result.pass = result.text.length === 3
        && result.text[0] === 'CPU24%'
        && result.text[1] === 'RAM25%12 GB / 48 GB'
        && result.text[2] === 'Storage25%125 GB / 500 GB'
        && !result.overflow
        && (width === 230 ? result.sameLine.every(Boolean) : !result.sameLine[2]);
      results.push(result);
    }
  } finally {
    await session.eval(`document.getElementById('machine-stats-layout-proof')?.remove()`);
  }
  return results;
}

module.exports = { cardMarkup, machineStatsLayout };
