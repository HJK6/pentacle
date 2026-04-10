// ── Foreclosure Pipeline Dashboard (Bartimaeus only) ─────────
// Self-registering — pushes to window.DASHBOARDS on load.
// Not included for Tars/Lews — this file is specific to our setup.
//
// The foreclosure flow is split into two sub-pipelines, toggleable via tabs
// at the top (like GitHub Actions workflow selector):
//
//   1. SCRAPING   — scrape → cad → propstream → qualify
//                   ends when all leads are in staging (qualified) or rejected
//   2. SKIPTRACE  — skipmatrix_csv → submit → confirm → invoice → pay →
//                   paid_confirm → results → prod_hydrate_promote
//                   ends when qualified leads are hydrated in prod
//
// Each stage renders as a box with a state icon (○ pending, ◐ running,
// ✉ waiting_email, ✓ complete, ✗ error), label, and a small secondary metric.
// Overall pipeline status rolls up to an icon next to each tab label.

(function() {

// ───────────────────────── data constants ───────────────────────────

const PIPELINES = [
  {
    id: 'scraping',
    label: 'Scraping',
    stages: [
      { id: 'scrape',     label: 'Scrape' },
      { id: 'cad',        label: 'CAD' },
      { id: 'propstream', label: 'PropStream' },
      { id: 'qualify',    label: 'Qualify' },
    ],
  },
  {
    id: 'skiptrace',
    label: 'Skiptrace',
    stages: [
      { id: 'skipmatrix_csv',          label: 'CSV' },
      { id: 'skipmatrix_submit',       label: 'Submit' },
      { id: 'skipmatrix_confirm',      label: 'Confirm' },
      { id: 'skipmatrix_invoice',      label: 'Invoice' },
      { id: 'skipmatrix_pay',          label: 'Pay' },
      { id: 'skipmatrix_paid_confirm', label: 'Paid' },
      { id: 'skipmatrix_results',      label: 'Results' },
      { id: 'prod_hydrate_promote',    label: 'Prod' },
    ],
  },
];

// State → { icon, cls, label } for the status badge on each stage box.
// Falls back to "pending" for unknown states.
const STATE_MAP = {
  waiting:                 { icon: '○', cls: 'pending',  label: 'pending' },
  running:                 { icon: '◐', cls: 'running',  label: 'running' },
  waiting_email:           { icon: '✉', cls: 'waiting',  label: 'waiting for email' },
  complete:                { icon: '✓', cls: 'success',  label: 'complete' },
  failed:                  { icon: '✗', cls: 'error',    label: 'failed' },
  blocked_qa:              { icon: '!', cls: 'error',    label: 'blocked — QA' },
  blocked_circuit_breaker: { icon: '⏸', cls: 'blocked',  label: 'blocked — another batch in flight' },
};

function _stateMeta(state) {
  return STATE_MAP[state] || { icon: '○', cls: 'pending', label: state || 'pending' };
}

// Roll up a list of stage rows to a single overall pipeline state.
// Priority: error > running > waiting_email > pending > complete.
// (i.e. if anything is broken, show error; if anything is running, show
// running; only mark the whole pipeline complete when every stage is complete.)
function _rollup(stageRows) {
  let hasError = false, hasRunning = false, hasWaitingEmail = false, hasPending = false;
  let allComplete = stageRows.length > 0;
  for (const r of stageRows) {
    const st = r ? r.state : 'waiting';
    if (st !== 'complete') allComplete = false;
    if (st === 'failed' || st === 'blocked_qa') hasError = true;
    else if (st === 'running') hasRunning = true;
    else if (st === 'waiting_email') hasWaitingEmail = true;
    else if (st === 'waiting' || st === 'blocked_circuit_breaker') hasPending = true;
  }
  if (hasError) return 'failed';
  if (allComplete) return 'complete';
  if (hasRunning) return 'running';
  if (hasWaitingEmail) return 'waiting_email';
  if (hasPending) return 'waiting';
  return 'waiting';
}

function _stagesByName(stages) {
  const out = {};
  (stages || []).forEach(s => { out[s.stage] = s; });
  return out;
}

function _fmt(n) {
  if (n == null || n === '' || isNaN(n)) return '—';
  return Number(n).toLocaleString();
}

// ───────────────────────── mount ─────────────────────────

function mount(container) {
  // Root wrapper. `activeTab` tracks the currently-displayed tab;
  // `userPinned` flips to 'true' when the user clicks a tab so update()
  // knows to stop auto-switching on poll. Until the user picks, we auto-
  // switch the active tab to follow whichever pipeline has an in-progress
  // stage (so reopening the dashboard always lands on the live work).
  const root = document.createElement('div');
  root.className = 'foreclosure-dashboard';
  root.dataset.activeTab = 'scraping';
  root.dataset.userPinned = 'false';

  // ── Header ──
  const header = document.createElement('div');
  header.className = 'pipeline-header';

  const titleRow = document.createElement('div');
  titleRow.className = 'pipeline-title-row';
  const title = document.createElement('h2');
  title.className = 'pipeline-title';
  title.textContent = 'Foreclosure Pipeline';
  const batchLabel = document.createElement('span');
  batchLabel.className = 'pipeline-batch-label';
  titleRow.appendChild(title);
  titleRow.appendChild(batchLabel);

  const metaRow = document.createElement('div');
  metaRow.className = 'pipeline-meta';
  const statusBadge = document.createElement('span');
  statusBadge.className = 'pipeline-status loading';
  statusBadge.textContent = 'Loading...';
  const lastUpdated = document.createElement('span');
  lastUpdated.className = 'pipeline-updated';
  const retryBtn = document.createElement('button');
  retryBtn.className = 'sb-btn';
  retryBtn.textContent = 'Retry';
  retryBtn.style.display = 'none';
  retryBtn.style.fontSize = '10px';
  retryBtn.style.padding = '4px 10px';
  metaRow.appendChild(statusBadge);
  metaRow.appendChild(lastUpdated);
  metaRow.appendChild(retryBtn);

  header.appendChild(titleRow);
  header.appendChild(metaRow);

  // ── Tab selector (one button per pipeline) ──
  const tabs = document.createElement('div');
  tabs.className = 'pipeline-tabs';
  const tabEls = {};
  PIPELINES.forEach(p => {
    const btn = document.createElement('button');
    btn.className = 'pipeline-tab';
    btn.dataset.pipelineId = p.id;
    const icon = document.createElement('span');
    icon.className = 'pipeline-tab-icon pending';
    icon.textContent = '○';
    const txt = document.createElement('span');
    txt.className = 'pipeline-tab-label';
    txt.textContent = p.label;
    btn.appendChild(icon);
    btn.appendChild(txt);
    btn.addEventListener('click', () => {
      root.dataset.activeTab = p.id;
      root.dataset.userPinned = 'true';  // stop auto-switching once user picks
      _refreshTabActive(tabEls, p.id);
      _refreshViewVisibility(viewEls, p.id);
    });
    tabs.appendChild(btn);
    tabEls[p.id] = { btn, icon, label: txt };
  });

  // ── One view per pipeline (only active one is visible) ──
  const viewEls = {};
  PIPELINES.forEach(p => {
    const view = document.createElement('div');
    view.className = 'pipeline-view';
    view.dataset.pipelineId = p.id;

    const flow = document.createElement('div');
    flow.className = 'pipeline-flow';

    const stageEls = {};
    p.stages.forEach((def, i) => {
      const box = document.createElement('div');
      box.className = 'pipeline-stage';
      box.dataset.stageId = def.id;

      const iconEl = document.createElement('div');
      iconEl.className = 'stage-icon pending';
      iconEl.textContent = '○';

      const lbl = document.createElement('div');
      lbl.className = 'stage-label';
      lbl.textContent = def.label;

      const sec = document.createElement('div');
      sec.className = 'stage-secondary';

      box.appendChild(iconEl);
      box.appendChild(lbl);
      box.appendChild(sec);
      flow.appendChild(box);

      stageEls[def.id] = { box, iconEl, sec };

      if (i < p.stages.length - 1) {
        const arrow = document.createElement('div');
        arrow.className = 'pipeline-arrow';
        arrow.textContent = '→';
        flow.appendChild(arrow);
      }
    });

    view.appendChild(flow);

    // Contextual footers per pipeline
    if (p.id === 'scraping') {
      const rejectedBox = document.createElement('div');
      rejectedBox.className = 'pipeline-rejected';
      const rejectedHeader = document.createElement('div');
      rejectedHeader.className = 'pipeline-section-header rejected';
      rejectedHeader.textContent = 'Rejected — 0';
      const rejectedReasons = document.createElement('div');
      rejectedReasons.className = 'pipeline-rejected-pills';
      rejectedBox.appendChild(rejectedHeader);
      rejectedBox.appendChild(rejectedReasons);

      const statesBox = document.createElement('div');
      statesBox.className = 'pipeline-states-section';
      const statesHeader = document.createElement('div');
      statesHeader.className = 'pipeline-section-header';
      statesHeader.textContent = 'Qualified by State';
      const qualifiedStates = document.createElement('div');
      qualifiedStates.className = 'pipeline-states-pills';
      statesBox.appendChild(statesHeader);
      statesBox.appendChild(qualifiedStates);

      // Auction date bucket pills — shows the distribution of auction
      // dates for the qualified leads. Backed by pipeline_stats.py's
      // auction_date_buckets dict.
      const auctionBox = document.createElement('div');
      auctionBox.className = 'pipeline-states-section';
      const auctionHeader = document.createElement('div');
      auctionHeader.className = 'pipeline-section-header';
      auctionHeader.textContent = 'Auction Dates';
      const auctionPills = document.createElement('div');
      auctionPills.className = 'pipeline-states-pills';
      auctionBox.appendChild(auctionHeader);
      auctionBox.appendChild(auctionPills);

      view.appendChild(rejectedBox);
      view.appendChild(statesBox);
      view.appendChild(auctionBox);

      viewEls[p.id] = {
        view, stageEls, rejectedHeader, rejectedReasons, qualifiedStates,
        auctionHeader, auctionPills,
      };
    } else {
      // Skiptrace: summary line with invoice/pay/prod details
      const summaryBox = document.createElement('div');
      summaryBox.className = 'pipeline-skiptrace-summary';
      const summaryLine = document.createElement('div');
      summaryLine.className = 'pipeline-skiptrace-summary-line';
      summaryBox.appendChild(summaryLine);
      view.appendChild(summaryBox);

      viewEls[p.id] = { view, stageEls, summaryLine };
    }

    root.appendChild(view);
  });

  // Reorder: header, tabs, views
  container.appendChild(header);
  container.appendChild(tabs);
  PIPELINES.forEach(p => container.appendChild(viewEls[p.id].view));

  _refreshTabActive(tabEls, 'scraping');
  _refreshViewVisibility(viewEls, 'scraping');

  const retryHandler = () => {
    if (typeof window.retryDashboardPoll === 'function') window.retryDashboardPoll();
  };
  retryBtn.addEventListener('click', retryHandler);

  return {
    root, batchLabel, statusBadge, lastUpdated, retryBtn, _retryHandler: retryHandler,
    tabEls, viewEls,
  };
}

function _refreshTabActive(tabEls, activeId) {
  Object.entries(tabEls).forEach(([id, t]) => {
    t.btn.classList.toggle('active', id === activeId);
  });
}

function _refreshViewVisibility(viewEls, activeId) {
  Object.entries(viewEls).forEach(([id, v]) => {
    v.view.style.display = id === activeId ? '' : 'none';
  });
}

// ───────────────────────── update ─────────────────────────

function update(refs, data) {
  const { root, batchLabel, tabEls, viewEls } = refs;

  // Prefer state-machine batch (orchestrator's view), fall back to leads_dev
  const summary = data.pipeline_summary || {};
  const smBatch = summary.state_machine_batch || data.batch || '';
  batchLabel.textContent = smBatch ? `Batch ${smBatch}` : '';

  const smStages = _stagesByName(data.pipeline_stages);

  // ── Paint tab rollup icons + figure out which pipeline is "current" ──
  // Current = first pipeline (in canonical order) that isn't complete. If
  // every pipeline is complete, the last one wins (so a fully-done batch
  // defaults to showing the skiptrace view, not scraping).
  let currentPipelineId = null;
  PIPELINES.forEach(p => {
    const rows = p.stages.map(s => smStages[s.id]);
    const rollupState = _rollup(rows);
    const meta = _stateMeta(rollupState);
    const tab = tabEls[p.id];
    tab.icon.textContent = meta.icon;
    tab.icon.className = `pipeline-tab-icon ${meta.cls}${rollupState === 'running' ? ' spin' : ''}`;
    tab.btn.title = `${p.label}: ${meta.label}`;
    if (currentPipelineId == null && rollupState !== 'complete') {
      currentPipelineId = p.id;
    }
  });
  if (currentPipelineId == null) {
    currentPipelineId = PIPELINES[PIPELINES.length - 1].id;
  }

  // ── Auto-switch to the current pipeline unless the user has pinned one ──
  // Once the user clicks a tab, `userPinned` stays 'true' for the rest of
  // this mount's lifetime and we respect their choice on every poll. A fresh
  // mount (e.g. reopening the dashboard) starts with userPinned='false' so
  // it lands on whichever pipeline has in-progress work.
  if (root.dataset.userPinned !== 'true' && root.dataset.activeTab !== currentPipelineId) {
    root.dataset.activeTab = currentPipelineId;
    _refreshTabActive(tabEls, currentPipelineId);
    _refreshViewVisibility(viewEls, currentPipelineId);
  }

  // ── Paint stage boxes for both pipelines (even hidden one — it's cheap
  // and means flipping the tab shows current state immediately) ──
  PIPELINES.forEach(p => {
    const v = viewEls[p.id];
    p.stages.forEach(def => {
      const row = smStages[def.id];
      const state = row ? row.state : 'waiting';
      const meta = _stateMeta(state);
      const el = v.stageEls[def.id];

      el.iconEl.textContent = meta.icon;
      el.iconEl.className = `stage-icon ${meta.cls}${state === 'running' ? ' spin' : ''}`;

      el.box.classList.remove('active', 'success', 'blocked', 'pending', 'error', 'waiting');
      el.box.classList.add(meta.cls);

      const errText = row && row.error ? row.error.slice(0, 200) : '';
      el.box.title = errText || meta.label;

      // Secondary metric per stage
      el.sec.textContent = _secondaryText(def.id, row, data);
    });
  });

  // ── Scraping view: rejected pills + qualified by state ──
  const s = viewEls.scraping;
  const rej = data.rejected || 0;
  s.rejectedHeader.textContent = `Rejected — ${_fmt(rej)}`;
  const reasons = Object.entries(data.rejection_reasons || {})
    .sort((a,b) => b[1]-a[1]).slice(0,14)
    .map(([key,value]) => ({ key, value }));
  reconcilePills(s.rejectedReasons, reasons, 'reason', (k,v) => `${k} (${v})`);

  const states = Object.entries(data.qualified_by_state || {})
    .sort((a,b) => b[1]-a[1]).slice(0,10)
    .map(([key,value]) => ({ key, value }));
  reconcilePills(s.qualifiedStates, states, 'state', (k,v) => `${k}: ${v}`);

  // Auction date buckets — canonical order (past → 6mo+ → unparseable)
  // with zero-count buckets hidden so the row isn't noisy when the early
  // pipeline hasn't populated data yet.
  const auction = data.auction_date_buckets || {};
  const totalAuction = Object.values(auction).reduce((a,b) => a + (Number(b)||0), 0);
  s.auctionHeader.textContent = totalAuction > 0
    ? `Auction Dates — ${_fmt(totalAuction)} in staging`
    : 'Auction Dates';
  const bucketLabels = {
    'past':        'past',
    'within_2w':   '< 2w',
    '2_4w':        '2–4w',
    '4_8w':        '4–8w',
    '8w_6mo':      '8w–6mo',
    '6mo+':        '6mo+',
    'unparseable': 'unparsed',
  };
  const bucketOrder = ['past','within_2w','2_4w','4_8w','8w_6mo','6mo+','unparseable'];
  const auctionPills = bucketOrder
    .filter(k => (auction[k] || 0) > 0)
    .map(k => ({ key: k, value: auction[k] }));
  reconcilePills(
    s.auctionPills, auctionPills, 'auction',
    (k, v) => `${bucketLabels[k] || k}: ${v}`,
  );

  // ── Skiptrace view: summary line ──
  const sk = viewEls.skiptrace;
  sk.summaryLine.innerHTML = _skiptraceSummary(smStages);
}

// Given a stage id + its state-machine row + the full data blob, return the
// secondary text that appears beneath the stage label. Keep this concise — it
// has to fit in ~100px width inside the stage box.
function _secondaryText(stageId, row, data) {
  const m = (row && row.metrics) || {};
  const st = row ? row.state : 'waiting';

  switch (stageId) {
    case 'scrape': {
      const tot = m.total_scraped != null ? m.total_scraped : (data.scraped || 0);
      const dup = m.total_duped || 0;
      return dup > 0 ? `${_fmt(tot)} · ${_fmt(dup)} dupe` : `${_fmt(tot)} scraped`;
    }
    case 'cad': {
      const pending = data.cad_pending || 0;
      const done = data.cad_complete || m.cad_hydrated || 0;
      return pending > 0 ? `${_fmt(pending)} pending` : `${_fmt(done)} done`;
    }
    case 'propstream': {
      const pending = data.ps_pending || 0;
      const done = data.ps_complete || m.propstream_hydrated || 0;
      return pending > 0 ? `${_fmt(pending)} pending` : `${_fmt(done)} done`;
    }
    case 'qualify': {
      // Prefer the POST-filter count — what actually made it through all
      // scraping + CSV-generation filters (i.e. the leads we paid to
      // skip-trace). Fall back to the raw staging count if we're pre-CSV.
      const qual = data.qualified_actual_count != null
        ? data.qualified_actual_count
        : (m.qualified != null ? m.qualified : (data.qualified || 0));
      const rej  = m.rejected != null ? m.rejected : (data.rejected || 0);
      return `${_fmt(qual)} pass · ${_fmt(rej)} fail`;
    }

    case 'skipmatrix_csv': {
      const rows = m.csv_rows || 0;
      if (st === 'running') return `iter ${m.iterations || 1}…`;
      return rows > 0 ? `${_fmt(rows)} rows` : '';
    }
    case 'skipmatrix_submit': {
      if (st === 'complete') return 'submitted';
      if (st === 'running') return 'uploading';
      return '';
    }
    case 'skipmatrix_confirm':
      if (st === 'waiting_email') return 'awaiting email';
      if (st === 'complete') return 'confirmed';
      return '';
    case 'skipmatrix_invoice':
      if (m.amount_usd) return `$${Number(m.amount_usd).toFixed(2)}`;
      if (st === 'waiting_email') return 'awaiting email';
      return '';
    case 'skipmatrix_pay':
      if (st === 'running') return 'awaiting approval';
      if (st === 'complete') return 'paid';
      return '';
    case 'skipmatrix_paid_confirm':
      if (st === 'waiting_email') return 'awaiting email';
      if (st === 'complete') return 'confirmed';
      return '';
    case 'skipmatrix_results': {
      const hits = m.hit_count || 0;
      if (st === 'complete') return hits > 0 ? `${_fmt(hits)} hits` : 'done';
      if (st === 'waiting_email') return 'awaiting email';
      return '';
    }
    case 'prod_hydrate_promote': {
      const promoted = m.created_owners || m.promoted || 0;
      if (st === 'complete') return `${_fmt(promoted)} promoted`;
      if (st === 'running')  return 'promoting…';
      return '';
    }
  }
  return '';
}

// Build the skiptrace summary footer (CSV rows, invoice amount, hits, promoted)
function _skiptraceSummary(smStages) {
  const csv = smStages.skipmatrix_csv;
  const invoice = smStages.skipmatrix_invoice;
  const results = smStages.skipmatrix_results;
  const prod = smStages.prod_hydrate_promote;

  const parts = [];
  const csvRows = csv && csv.metrics && csv.metrics.csv_rows;
  if (csvRows) parts.push(`<span class="sk-key">CSV</span> ${_fmt(csvRows)} rows`);

  const amt = invoice && invoice.metrics && invoice.metrics.amount_usd;
  if (amt) parts.push(`<span class="sk-key">Invoice</span> $${Number(amt).toFixed(2)}`);

  const hits = results && results.metrics && results.metrics.hit_count;
  if (hits != null) parts.push(`<span class="sk-key">Hits</span> ${_fmt(hits)}`);

  const promoted = prod && prod.metrics && (prod.metrics.created_owners || prod.metrics.promoted);
  if (promoted != null) parts.push(`<span class="sk-key">Promoted</span> ${_fmt(promoted)}`);

  if (!parts.length) return '<span class="sk-empty">Waiting for skiptrace to start…</span>';
  return parts.join(' · ');
}

function unmount(refs) {
  if (refs && refs.retryBtn && refs._retryHandler)
    refs.retryBtn.removeEventListener('click', refs._retryHandler);
}

// ── Self-register ──
window.DASHBOARDS.push({
  id: 'foreclosure-pipeline',
  name: 'Foreclosure Pipeline',
  description: 'Scraping + skiptrace flow with stage status',
  color: 'var(--green)',
  mount, update, unmount,
  pollFn: () => window.cc.getPipelineStats(),
  pollInterval: 10000,
});

})();
