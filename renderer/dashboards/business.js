// ── Business Pipeline Dashboard (desktop adapter) ─────────────
// Self-contained renderer for the Lead Lock business pipeline consumer.

(function() {
'use strict';

const rootWindow = typeof window !== 'undefined' ? window : null;
const STAGES = [
  { key: 'scrape', label: 'Scrape', countKey: 'scraped' },
  { key: 'enrich', label: 'Enrich', countKey: 'enriched', fallbackCountKey: 'deduped' },
  { key: 'qualify', label: 'Qualify', countKey: 'qualified' },
  { key: 'promote', label: 'Promote', countKey: 'promoted' },
];
const TOTALS = [
  ['scraped', 'Scraped'],
  ['deduped', 'Deduped'],
  ['qualified', 'Qualified'],
  ['promoted', 'Promoted'],
];
const DEFAULT_SOURCES = ['bbb', 'facebook', 'gmaps'];

function fmt(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '0';
  return Number(n).toLocaleString();
}

function stageState(data, stageKey) {
  const stages = Array.isArray(data && data.pipeline_stages) ? data.pipeline_stages : [];
  return stages.find((stage) => stage && stage.stage === stageKey) || { stage: stageKey, state: 'waiting' };
}

function countFor(data, stageDef) {
  if (!data) return 0;
  if (data[stageDef.countKey] !== undefined && data[stageDef.countKey] !== null) return data[stageDef.countKey];
  if (stageDef.fallbackCountKey) return data[stageDef.fallbackCountKey] || 0;
  return 0;
}

function clearChildren(el) {
  while (el && el.firstChild) el.removeChild(el.firstChild);
}

function makeEl(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function setText(refs, key, value) {
  if (refs && refs[key]) refs[key].textContent = value;
}

function buildStage(stageDef) {
  const stage = makeEl('div', 'pipeline-stage business-stage');
  stage.dataset.stage = stageDef.key;
  stage.appendChild(makeEl('div', 'pipeline-stage-name', stageDef.label));
  const counts = makeEl('div', 'pipeline-stage-counts');
  const count = makeEl('div', 'pipeline-count');
  count.appendChild(makeEl('div', 'pipeline-count-num', '0'));
  count.appendChild(makeEl('div', 'pipeline-count-label', 'count'));
  const state = makeEl('div', 'business-stage-state', 'waiting');
  counts.appendChild(count);
  counts.appendChild(state);
  stage.appendChild(counts);
  return stage;
}

function appendMetric(parent, key, label) {
  const item = makeEl('div', 'business-total');
  item.dataset.metric = key;
  item.appendChild(makeEl('div', 'business-total-value', '0'));
  item.appendChild(makeEl('div', 'business-total-label', label));
  parent.appendChild(item);
}

function renderRows(tbody, rows, emptyText) {
  clearChildren(tbody);
  if (!rows.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 4;
    cell.className = 'business-empty';
    cell.textContent = emptyText;
    row.appendChild(cell);
    tbody.appendChild(row);
    return;
  }
  rows.forEach((rowData) => {
    const row = document.createElement('tr');
    rowData.forEach((cellData, idx) => {
      const cell = document.createElement(idx === 0 ? 'th' : 'td');
      cell.textContent = cellData;
      if (idx > 0) cell.className = 'business-num';
      row.appendChild(cell);
    });
    tbody.appendChild(row);
  });
}

function renderVariableRows(tbody, rows, emptyText, colspan) {
  clearChildren(tbody);
  if (!rows.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = colspan;
    cell.className = 'business-empty';
    cell.textContent = emptyText;
    row.appendChild(cell);
    tbody.appendChild(row);
    return;
  }
  rows.forEach((rowData) => {
    const row = document.createElement('tr');
    rowData.forEach((cellData, idx) => {
      const cell = document.createElement(idx === 0 ? 'th' : 'td');
      cell.textContent = cellData;
      if (typeof cellData === 'number') cell.className = 'business-num';
      row.appendChild(cell);
    });
    tbody.appendChild(row);
  });
}

function areaRows(byArea) {
  const entries = Object.entries(byArea || {}).sort(([a], [b]) => a.localeCompare(b));
  const totals = { scraped: 0, qualified: 0, promoted: 0 };
  const rows = entries.map(([area, stats]) => {
    totals.scraped += Number(stats && stats.scraped) || 0;
    totals.qualified += Number(stats && stats.qualified) || 0;
    totals.promoted += Number(stats && stats.promoted) || 0;
    return [area, fmt(stats && stats.scraped), fmt(stats && stats.qualified), fmt(stats && stats.promoted)];
  });
  if (rows.length) rows.push(['All areas', fmt(totals.scraped), fmt(totals.qualified), fmt(totals.promoted)]);
  return rows;
}

function sourceRows(bySource) {
  const sources = [...new Set([...DEFAULT_SOURCES, ...Object.keys(bySource || {})])].sort();
  return sources.map((source) => {
    const stats = (bySource && bySource[source]) || {};
    return [source, fmt(stats.scraped), fmt(stats.qualified), fmt(stats.promoted)];
  });
}

function firstValue(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== '');
}

function telemetryFor(job) {
  const counters = (job && job.counters) || {};
  return (job && (job.telemetry || job.coverage)) || counters.telemetry || counters.coverage || {};
}

function activeJobs(queue) {
  return queueJobs(queue).filter((job) => ['pending', 'running', 'awaiting_async'].includes(job.status));
}

function healthFor(job) {
  const telemetry = telemetryFor(job);
  const session = firstValue(job.session_health, telemetry.session_health, job.session_status, telemetry.session_status);
  const challenges = Number(firstValue(job.challenged, telemetry.challenged, job.challenges, telemetry.challenges, 0)) || 0;
  if (session && challenges) return `${session} · ${challenges} challenged`;
  if (session) return String(session);
  if (challenges) return `${challenges} challenged`;
  return '—';
}

function activeRunRows(queue) {
  return activeJobs(queue).map((job) => {
    const telemetry = telemetryFor(job);
    return [
      job.run_id || job.id || job.job_id || job.batch || 'run',
      job.source || job.platform || 'unknown',
      job.area || job.metro || 'all',
      String(job.status || 'unknown'),
      fmt(firstValue(job.pages, job.pages_scraped, telemetry.pages, telemetry.pages_scraped, telemetry.pages_visited)),
      fmt(firstValue(job.records_ingested, job.ingested, telemetry.unique_ingested)),
      fmt(firstValue(job.errors, telemetry.errors, job.error ? 1 : 0)),
      healthFor(job),
    ];
  });
}

function coverageReports(data) {
  const reports = Array.isArray(data && data.coverage_reports) ? [...data.coverage_reports] : [];
  queueJobs(data && data.scraper_queue).forEach((job) => {
    const telemetry = telemetryFor(job);
    if (firstValue(job.advertised_total, telemetry.advertised_total) !== undefined) {
      reports.push({ ...telemetry, ...job });
    }
  });
  return reports;
}

function coverageRows(data) {
  return coverageReports(data).map((report) => {
    const advertised = Number(firstValue(report.advertised_total, 0)) || 0;
    const ingested = Number(firstValue(report.unique_ingested, report.records_ingested, report.ingested, 0)) || 0;
    const pct = Number(firstValue(report.coverage_pct, advertised ? (ingested / advertised) * 100 : 0)) || 0;
    return [
      report.source || report.platform || 'unknown',
      report.area || report.metro || 'all',
      fmt(advertised),
      fmt(ingested),
      `${pct.toFixed(1)}%`,
      report.status || (pct >= 90 ? 'target met' : 'below target'),
    ];
  });
}

function recentErrorRows(data) {
  const errors = Array.isArray(data && data.recent_errors) ? [...data.recent_errors] : [];
  queueJobs(data && data.scraper_queue).forEach((job) => {
    if (job.error) errors.push(job);
  });
  return errors
    .sort((a, b) => String(b.updated_at || b.ended_at || '').localeCompare(String(a.updated_at || a.ended_at || '')))
    .slice(0, 10)
    .map((entry) => [
      entry.updated_at || entry.ended_at || entry.finished_at || '—',
      entry.source || entry.platform || 'unknown',
      entry.area || entry.metro || 'all',
      entry.error || entry.message || 'Unknown scrape error',
    ]);
}

function crmRows(counts) {
  const environments = Object.keys(counts || {});
  const sources = [...new Set(environments.flatMap((environment) => Object.keys(counts[environment] || {})))].sort();
  return sources.map((source) => [source, fmt(counts.staging && counts.staging[source]), fmt(counts.prod && counts.prod[source])]);
}

function queueJobs(queue) {
  const q = queue || {};
  if (Array.isArray(q.jobs) && q.jobs.length) return q.jobs;
  const jobs = [];
  if (q.running_job) jobs.push(q.running_job);
  if (Array.isArray(q.failed_jobs)) jobs.push(...q.failed_jobs);
  return jobs;
}

function clearData(refs) {
  refs.stageWrap.querySelectorAll('.business-stage').forEach((stageEl) => {
    stageEl.dataset.state = 'waiting';
    stageEl.classList.remove('pipeline-stage-active');
    const pulse = stageEl.querySelector('.pipeline-stage-pulse');
    if (pulse) pulse.remove();
    stageEl.querySelector('.pipeline-count-num').textContent = '0';
    stageEl.querySelector('.business-stage-state').textContent = 'waiting';
  });
  TOTALS.forEach(([key]) => {
    const el = refs.totals.querySelector(`[data-metric="${key}"] .business-total-value`);
    if (el) el.textContent = '0';
  });
  renderRows(refs.areaBody, [], 'No area data yet');
  renderRows(refs.sourceBody, [], 'No source data yet');
  renderVariableRows(refs.jobBody, [], 'No active scrape runs', 8);
  renderVariableRows(refs.coverageBody, [], 'No coverage reports yet', 6);
  renderVariableRows(refs.errorBody, [], 'No recent scrape errors', 4);
  renderVariableRows(refs.crmBody, [], 'No CRM source counts yet', 3);
  setText(refs, 'scopeName', 'All leads');
  setText(refs, 'scopeCount', 'Waiting for business data');
  setText(refs, 'queueSummary', '0 running · 0 pending · 0 complete · 0 failed');
  setText(refs, 'note', '');
}

function mount(container) {
  if (!container) return {};
  container.innerHTML = '';

  const style = document.createElement('style');
  style.textContent = `
    .business-dashboard { padding:16px; }
    .business-dashboard .pipeline-header { margin-bottom:18px; }
    .business-stage { min-width:150px; }
    .business-stage-state { margin-top:6px; font-size:11px; font-weight:700; text-transform:uppercase; color:var(--fg-dim); }
    .business-stage[data-state="running"] .business-stage-state { color:var(--blue); }
    .business-stage[data-state="complete"] .business-stage-state { color:var(--green); }
    .business-stage[data-state="failed"] .business-stage-state,
    .business-stage[data-state="blocked"] .business-stage-state { color:var(--red); }
    .business-totals { display:grid; grid-template-columns:repeat(4,minmax(110px,1fr)); gap:10px; margin:12px 0 18px; }
    .business-total { border:1px solid var(--border); background:var(--bg2); border-radius:8px; padding:12px; }
    .business-total-value { color:#fff; font-size:22px; line-height:1; font-weight:700; }
    .business-total-label { margin-top:5px; color:var(--fg-dim); font-size:10px; text-transform:uppercase; letter-spacing:0.5px; }
    .business-table { width:100%; border-collapse:collapse; font-size:12px; }
    .business-table th,
    .business-table td { padding:6px 0; border-bottom:1px solid rgba(30,57,40,0.5); }
    .business-table th { color:var(--fg); text-align:left; font-weight:600; }
    .business-table thead th { color:var(--fg-dim); font-size:10px; text-transform:uppercase; letter-spacing:0.5px; }
    .business-table td { color:var(--fg); }
    .business-table .business-num { text-align:right; font-variant-numeric:tabular-nums; }
    .business-empty { color:var(--fg-dim); text-align:center; }
    .business-note { margin-top:8px; min-height:16px; color:var(--yellow); font-size:12px; }
    .business-scroll { overflow-x:auto; }
    .business-scroll .business-table { min-width:720px; }
    @media (max-width: 760px) {
      .business-totals { grid-template-columns:repeat(2,minmax(110px,1fr)); }
      .business-dashboard .pipeline-header { align-items:flex-start; flex-direction:column; gap:10px; }
    }
  `;

  const root = makeEl('div', 'business-dashboard pipeline-dashboard');
  const header = makeEl('div', 'pipeline-header');
  const titleRow = makeEl('div', 'pipeline-title-row');
  titleRow.appendChild(makeEl('div', 'pipeline-title', 'Business Pipeline'));
  header.appendChild(titleRow);
  const meta = makeEl('div', 'pipeline-meta');
  const staleness = makeEl('span', 'pipeline-updated');
  const summary = makeEl('span', 'pipeline-updated');
  meta.appendChild(staleness);
  meta.appendChild(summary);
  header.appendChild(meta);
  root.appendChild(header);

  const stageWrap = makeEl('div', 'pipeline-stages');
  STAGES.forEach((stageDef, idx) => {
    if (idx) stageWrap.appendChild(makeEl('div', 'pipeline-arrow', '→'));
    stageWrap.appendChild(buildStage(stageDef));
  });
  root.appendChild(stageWrap);

  const totals = makeEl('div', 'business-totals');
  TOTALS.forEach(([key, label]) => appendMetric(totals, key, label));
  root.appendChild(totals);

  const details = makeEl('div', 'pipeline-details');
  const grid = makeEl('div', 'pipeline-details-grid');
  const areaCard = makeEl('section', 'pipeline-detail-card pipeline-detail-card-wide');
  areaCard.appendChild(makeEl('div', 'pipeline-detail-title', 'Batch Funnel by Metro'));
  const areaTable = makeEl('table', 'business-table');
  areaTable.innerHTML = '<thead><tr><th>Area</th><th class="business-num">Scraped</th><th class="business-num">Qualified</th><th class="business-num">Promoted</th></tr></thead><tbody></tbody>';
  areaCard.appendChild(areaTable);
  const sourceCard = makeEl('section', 'pipeline-detail-card');
  sourceCard.appendChild(makeEl('div', 'pipeline-detail-title', 'Source Breakdown'));
  const sourceTable = makeEl('table', 'business-table');
  sourceTable.innerHTML = '<thead><tr><th>Source</th><th class="business-num">Scraped</th><th class="business-num">Qualified</th><th class="business-num">Promoted</th></tr></thead><tbody></tbody>';
  sourceCard.appendChild(sourceTable);
  const crmCard = makeEl('section', 'pipeline-detail-card');
  crmCard.appendChild(makeEl('div', 'pipeline-detail-title', 'CRM Source Counts'));
  const crmTable = makeEl('table', 'business-table');
  crmTable.innerHTML = '<thead><tr><th>Source</th><th class="business-num">Staging</th><th class="business-num">Prod</th></tr></thead><tbody></tbody>';
  crmCard.appendChild(crmTable);
  const scopeCard = makeEl('section', 'pipeline-detail-card');
  scopeCard.appendChild(makeEl('div', 'pipeline-detail-title', 'Scope'));
  scopeCard.appendChild(makeEl('div', 'pipeline-batch-name', 'All leads'));
  scopeCard.appendChild(makeEl('div', 'pipeline-batch-count', 'Waiting for business data'));
  scopeCard.appendChild(makeEl('div', 'business-queue-summary', '0 running · 0 pending · 0 complete · 0 failed'));
  scopeCard.appendChild(makeEl('div', 'business-note'));
  const jobsCard = makeEl('section', 'pipeline-detail-card pipeline-detail-card-wide business-scroll');
  jobsCard.appendChild(makeEl('div', 'pipeline-detail-title', 'Active Scrape Runs'));
  const jobsTable = makeEl('table', 'business-table');
  jobsTable.innerHTML = '<thead><tr><th>Run</th><th>Platform</th><th>Metro</th><th>Status</th><th class="business-num">Pages</th><th class="business-num">Ingested</th><th class="business-num">Errors</th><th>Challenge / Session</th></tr></thead><tbody></tbody>';
  jobsCard.appendChild(jobsTable);
  const coverageCard = makeEl('section', 'pipeline-detail-card pipeline-detail-card-wide business-scroll');
  coverageCard.appendChild(makeEl('div', 'pipeline-detail-title', 'Coverage vs Advertised'));
  const coverageTable = makeEl('table', 'business-table');
  coverageTable.innerHTML = '<thead><tr><th>Platform</th><th>Metro</th><th class="business-num">Advertised</th><th class="business-num">Unique Ingested</th><th>Coverage</th><th>Status</th></tr></thead><tbody></tbody>';
  coverageCard.appendChild(coverageTable);
  const errorsCard = makeEl('section', 'pipeline-detail-card pipeline-detail-card-wide business-scroll');
  errorsCard.appendChild(makeEl('div', 'pipeline-detail-title', 'Recent Errors'));
  const errorsTable = makeEl('table', 'business-table');
  errorsTable.innerHTML = '<thead><tr><th>Time</th><th>Platform</th><th>Metro</th><th>Error</th></tr></thead><tbody></tbody>';
  errorsCard.appendChild(errorsTable);
  grid.appendChild(areaCard);
  grid.appendChild(sourceCard);
  grid.appendChild(crmCard);
  grid.appendChild(scopeCard);
  grid.appendChild(jobsCard);
  grid.appendChild(coverageCard);
  grid.appendChild(errorsCard);
  details.appendChild(grid);
  root.appendChild(details);

  container.appendChild(style);
  container.appendChild(root);

  return {
    container,
    root,
    style,
    staleness,
    summary,
    stageWrap,
    totals,
    areaBody: areaTable.querySelector('tbody'),
    sourceBody: sourceTable.querySelector('tbody'),
    crmBody: crmTable.querySelector('tbody'),
    jobBody: jobsTable.querySelector('tbody'),
    coverageBody: coverageTable.querySelector('tbody'),
    errorBody: errorsTable.querySelector('tbody'),
    scopeName: scopeCard.querySelector('.pipeline-batch-name'),
    scopeCount: scopeCard.querySelector('.pipeline-batch-count'),
    queueSummary: scopeCard.querySelector('.business-queue-summary'),
    note: scopeCard.querySelector('.business-note'),
  };
}

function update(refs, data) {
  if (!refs || !refs.root) return;
  if (!data || data.error) {
    setText(refs, 'summary', data && data.error ? data.error : 'No data');
    clearData(refs);
    return;
  }

  if (typeof renderStalenessBadge === 'function') renderStalenessBadge(refs.staleness, data);
  setText(refs, 'summary', data.pipeline_summary && data.pipeline_summary.current_stage
    ? `${data.pipeline_summary.current_stage}: ${data.pipeline_summary.current_state || 'unknown'}`
    : '');

  refs.stageWrap.querySelectorAll('.business-stage').forEach((stageEl) => {
    const def = STAGES.find((s) => s.key === stageEl.dataset.stage);
    const stage = stageState(data, def.key);
    const state = stage.state || 'waiting';
    stageEl.dataset.state = state;
    stageEl.classList.toggle('pipeline-stage-active', state === 'running');
    const pulse = stageEl.querySelector('.pipeline-stage-pulse');
    if (state === 'running' && !pulse) stageEl.appendChild(makeEl('span', 'pipeline-stage-pulse'));
    if (state !== 'running' && pulse) pulse.remove();
    stageEl.querySelector('.pipeline-count-num').textContent = fmt(countFor(data, def));
    stageEl.querySelector('.business-stage-state').textContent = state;
  });

  TOTALS.forEach(([key]) => {
    const el = refs.totals.querySelector(`[data-metric="${key}"] .business-total-value`);
    if (el) el.textContent = fmt(data[key]);
  });
  renderRows(refs.areaBody, areaRows(data.by_area), 'No area data yet');
  renderRows(refs.sourceBody, sourceRows(data.by_source), 'No source data yet');
  renderVariableRows(refs.crmBody, crmRows(data.crm_source_counts || data.crm_counts || {}), 'No CRM source counts yet', 3);
  renderVariableRows(refs.jobBody, activeRunRows(data.scraper_queue), 'No active scrape runs', 8);
  renderVariableRows(refs.coverageBody, coverageRows(data), 'No coverage reports yet', 6);
  renderVariableRows(refs.errorBody, recentErrorRows(data), 'No recent scrape errors', 4);
  const queue = data.scraper_queue || {};
  setText(refs, 'scopeName', data.all_leads ? 'All leads' : (data.batch || 'Current snapshot'));
  setText(refs, 'scopeCount', `${fmt(data.scraped)} scraped · ${fmt(data.qualified)} qualified · ${fmt(data.promoted)} promoted`);
  setText(refs, 'queueSummary', `${fmt(queue.running)} running · ${fmt(queue.pending)} pending · ${fmt(queue.completed)} complete · ${fmt(queue.failed)} failed`);
  setText(refs, 'note', '');
}

function unmount(refs) {
  if (!refs) return;
  if (refs.container) refs.container.innerHTML = '';
}

function _isIdle(data) {
  const stages = data && data.pipeline_stages;
  if (!stages || !stages.length) return false;
  return stages.every((stage) => stage.state === 'complete');
}

function pollFn() {
  return rootWindow.cc.getBusinessPipelineStats();
}

if (rootWindow) {
  const _isClient = !!(rootWindow.HOST && rootWindow.HOST.isClient);
  const _hasRemote = !!(rootWindow.HOST && rootWindow.HOST.hasRemote);
  const _hasDashboardHub = !!(rootWindow.HOST && rootWindow.HOST.hasDashboardHub);
  const _showBusiness = _hasDashboardHub
    || (_isClient && _hasRemote);
  if (_showBusiness && rootWindow.DASHBOARDS) {
    rootWindow.DASHBOARDS.push({
      id: 'business-pipeline',
      name: 'Business Pipeline',
      description: 'Lead Lock business scrape → enrich → qualify → promote',
      color: 'var(--blue)',
      mount, update, unmount,
      pollFn,
      pollInterval: 10000,
      idlePollInterval: 60000,
      idleFn: _isIdle,
    });
  }
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { mount, update, unmount, pollFn, _isIdle };
}

})();
