// Pentacle Mobile testing dashboard. Self-registers with the existing dashboard registry.
(function pentacleMobileTestingDashboard() {
'use strict';

const rootWindow = typeof window !== 'undefined' ? window : null;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function valueText(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function table(columns, rows) {
  const element = node('table', 'pmt-table');
  const head = node('thead');
  const headRow = node('tr');
  columns.forEach((column) => headRow.appendChild(node('th', '', column)));
  head.appendChild(headRow);
  element.appendChild(head);
  const body = node('tbody');
  if (!rows.length) {
    const row = node('tr');
    const cell = node('td', 'pmt-empty', 'No records');
    cell.colSpan = columns.length;
    row.appendChild(cell);
    body.appendChild(row);
  } else {
    rows.forEach((values) => {
      const row = node('tr');
      values.forEach((value) => row.appendChild(node('td', '', valueText(value))));
      body.appendChild(row);
    });
  }
  element.appendChild(body);
  return element;
}

function panelState(panel) {
  const wrapper = node('div', 'pmt-source-state');
  if (!panel || panel.status === 'unavailable') {
    wrapper.classList.add('pmt-unknown');
    wrapper.appendChild(node('strong', '', 'UNKNOWN — source unreadable'));
  } else if (panel.status === 'partial') {
    wrapper.classList.add('pmt-partial');
    wrapper.appendChild(node('strong', '', 'Partial source visibility'));
  } else {
    return null;
  }
  const codes = Array.isArray(panel && panel.error_codes) ? panel.error_codes.slice(0, 10).join(', ') : 'source_unavailable';
  wrapper.appendChild(node('span', '', codes));
  return wrapper;
}

function resetPanel(ref, panel) {
  ref.body.replaceChildren();
  ref.updated.textContent = panel && panel.updated_at ? panel.updated_at : 'not observed';
  const state = panelState(panel);
  if (state) ref.body.appendChild(state);
}

function section(container, key, title) {
  const wrapper = node('section', `pmt-panel pmt-${key}`);
  const heading = node('div', 'pmt-panel-heading');
  heading.appendChild(node('h3', '', title));
  const updated = node('span', 'pmt-updated', 'not observed');
  heading.appendChild(updated);
  const body = node('div', 'pmt-panel-body');
  wrapper.append(heading, body);
  container.appendChild(wrapper);
  return { body, updated };
}

function mount(container) {
  container.replaceChildren();
  const style = node('style');
  style.textContent = `
    .pmt-dashboard{padding:16px;color:var(--fg);font-size:12px}.pmt-header{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:14px}.pmt-header h2{margin:0;color:#fff;font-size:20px}.pmt-meta{color:var(--fg-dim);text-align:right}.pmt-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.pmt-panel{border:1px solid var(--border);background:var(--bg2);border-radius:8px;min-width:0;overflow:hidden}.pmt-panel-heading{display:flex;justify-content:space-between;gap:8px;padding:10px 12px;border-bottom:1px solid var(--border)}.pmt-panel-heading h3{margin:0;color:#fff;font-size:13px}.pmt-updated{color:var(--fg-dim);font-size:10px}.pmt-panel-body{padding:10px 12px;overflow:auto}.pmt-table{width:100%;border-collapse:collapse;margin:6px 0 12px}.pmt-table th,.pmt-table td{text-align:left;padding:5px 7px;border-bottom:1px solid rgba(255,255,255,.07);vertical-align:top;overflow-wrap:anywhere}.pmt-table th{color:var(--fg-dim);font-size:10px;text-transform:uppercase}.pmt-empty{color:var(--fg-dim)}.pmt-source-state{display:flex;gap:8px;padding:7px 9px;margin-bottom:8px;border-radius:6px;background:#6e5b1d44;color:#f2cc60}.pmt-unknown{background:#7d252544;color:#ff9b9b}.pmt-subhead{margin:10px 0 4px;color:var(--fg-dim);font-size:10px;text-transform:uppercase}.pmt-list{margin:4px 0 10px;padding-left:18px}.pmt-json{overflow-wrap:anywhere;color:var(--fg-dim)}@media(max-width:900px){.pmt-grid{grid-template-columns:1fr}}
  `;
  const root = node('div', 'pmt-dashboard');
  const header = node('div', 'pmt-header');
  const title = node('div');
  title.append(node('h2', '', 'Pentacle Mobile Testing'), node('div', 'pmt-json', 'Live test execution, remaining work, and timing analytics'));
  const meta = node('div', 'pmt-meta', 'Waiting for Dashboard Hub');
  header.append(title, meta);
  const grid = node('div', 'pmt-grid');
  const refs = {
    container,
    meta,
    panels: {
      now_running: section(grid, 'now-running', 'Now Running'),
      latest_gate_runs: section(grid, 'latest-gate-runs', 'Latest Gate Runs'),
      whats_left: section(grid, 'whats-left', "What's Left"),
      time_estimates: section(grid, 'time-estimates', 'Time Estimates'),
      per_test_analytics: section(grid, 'per-test-analytics', 'Per-Test Analytics'),
      recent_closures: section(grid, 'recent-closures', 'Recent Closures'),
    },
  };
  root.append(header, grid);
  container.append(style, root);
  return refs;
}

function subhead(parent, text) {
  parent.appendChild(node('div', 'pmt-subhead', text));
}

function update(refs, data) {
  if (!refs) return;
  if (!data || data.error || data._reader_error) {
    refs.meta.textContent = data && (data.error || data._reader_error) ? (data.error || data._reader_error) : 'No dashboard data';
    Object.values(refs.panels).forEach((ref) => {
      resetPanel(ref, null);
    });
    return;
  }
  const stale = data._data_stale ? 'STALE' : 'LIVE';
  const transport = data._transport_stale ? ' · Hub disconnected' : '';
  refs.meta.textContent = `${stale}${transport} · ${data.generated_at || data._updated_at || 'timestamp unavailable'}`;
  const panels = data.panels || {};

  const now = panels.now_running;
  resetPanel(refs.panels.now_running, now);
  const nowBody = refs.panels.now_running.body;
  subhead(nowBody, 'Active lanes');
  nowBody.appendChild(table(['Lane', 'State', 'Phase', 'Last update'], (now && now.lanes || []).slice(0, 50).map((item) => [item.title, item.state, item.phase, item.last_update || item.updated_at])));
  subhead(nowBody, 'Simulator queue');
  nowBody.appendChild(node('div', 'pmt-json', now && now.sim_queue ? valueText(now.sim_queue) : 'UNKNOWN — sim-queue unreadable'));
  subhead(nowBody, 'Gate processes');
  nowBody.appendChild(table(['PID', 'Kind', 'Elapsed', 'Artifacts'], (now && now.gate_processes || []).slice(0, 20).map((item) => [item.pid, item.kind, item.elapsed, item.artifact_dir])));

  const gates = panels.latest_gate_runs;
  resetPanel(refs.panels.latest_gate_runs, gates);
  refs.panels.latest_gate_runs.body.appendChild(table(['Run', 'SHA', 'Verdict', 'Started', 'Artifacts'], (gates && gates.items || []).slice(0, 10).map((item) => [item.run_id, String(item.sha || '').slice(0, 12), item.status, item.started_at, item.artifact_path])));

  const left = panels.whats_left;
  resetPanel(refs.panels.whats_left, left);
  const leftBody = refs.panels.whats_left.body;
  leftBody.appendChild(node('div', 'pmt-json', valueText(left && left.state_counts || {})));
  subhead(leftBody, 'Unchecked acceptance criteria');
  const acceptance = node('ul', 'pmt-list');
  (left && left.unchecked_acceptance || []).slice(0, 100).forEach((item) => acceptance.appendChild(node('li', '', item)));
  if (!acceptance.childNodes.length) acceptance.appendChild(node('li', 'pmt-empty', 'No unchecked criteria observed'));
  leftBody.appendChild(acceptance);
  leftBody.appendChild(table(['State', 'Work item'], (left && left.items || []).slice(0, 50).map((item) => [item.status, item.title])));

  const times = panels.time_estimates;
  resetPanel(refs.panels.time_estimates, times);
  const estimate = times && times.estimate || {};
  refs.panels.time_estimates.body.appendChild(table(['Estimate ms', 'Last actual ms', 'Error ms'], [[estimate.total_estimate_ms, times && times.last_actual_ms, times && times.estimate_error_ms]]));
  refs.panels.time_estimates.body.appendChild(table(['Stage', 'Samples', 'Median ms', 'Last ms'], (times && times.stage_medians || []).slice(0, 50).map((item) => [item.stage, item.samples, item.median_ms, item.last_ms])));

  const tests = panels.per_test_analytics;
  resetPanel(refs.panels.per_test_analytics, tests);
  refs.panels.per_test_analytics.body.appendChild(table(['Test', 'Samples', 'Median', 'P90', 'Last', 'Error'], (tests && tests.items || []).slice(0, 20).map((item) => [item.test_id, item.samples, item.median_ms, item.p90_ms, item.last_ms, item.estimate_error_ms])));

  const closures = panels.recent_closures;
  resetPanel(refs.panels.recent_closures, closures);
  refs.panels.recent_closures.body.appendChild(table(['Completed', 'Work item'], (closures && closures.items || []).slice(0, 10).map((item) => [item.completed_at, item.title])));
}

function unmount(refs) {
  if (refs && refs.container) refs.container.replaceChildren();
}

async function pollFn() {
  const data = await rootWindow.cc.getPentacleMobileTestingStats();
  if (data && data.error) return { _reader_error: data.error };
  return data;
}

if (rootWindow && rootWindow.DASHBOARDS && rootWindow.HOST && rootWindow.HOST.hasDashboardHub) {
  rootWindow.DASHBOARDS.push({
    id: 'pentacle-mobile-testing',
    name: 'Pentacle Mobile Testing',
    description: 'Live test execution, gate results, remaining work, and timing analytics',
    color: 'var(--purple)',
    mount,
    update,
    unmount,
    pollFn,
    pollInterval: 1000,
  });
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { mount, update, unmount, pollFn };
}
})();
