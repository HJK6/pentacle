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
      { id: 'staging',    label: 'Staging' },
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

// Stages that emit a drill-down breakdown from pipeline_stats.py. Only
// these are clickable. Skiptrace stages and prod_hydrate_promote don't
// ship per-state data today (could add later).
const _STAGES_WITH_BREAKDOWN = ['scrape', 'cad', 'propstream', 'qualify', 'staging'];

// Column definitions per stage — drives both the header and the per-row
// cell extraction for the drill-down table.
const _BREAKDOWN_COLUMNS = {
  // scrape is rendered specially (state group + nested source rows); this
  // definition is just a stub for the header — actual rendering done inline.
  scrape:     [{k:'state',label:'State / source'}, {k:'count',label:'Count',num:true}],
  cad:        [{k:'state',label:'State'}, {k:'done',label:'Done',num:true}, {k:'miss',label:'Miss',num:true}, {k:'pending',label:'Pending',num:true}, {k:'skipped',label:'Skipped',num:true}],
  propstream: [{k:'state',label:'State'}, {k:'done',label:'Done',num:true}, {k:'pending',label:'Pending',num:true}, {k:'skipped',label:'Skipped',num:true}],
  qualify:    [{k:'state',label:'State'}, {k:'qualified',label:'Qual.',num:true}, {k:'rejected',label:'Rej.',num:true}, {k:'top_reason',label:'Top reason'}],
  staging:    [{k:'state',label:'State'}, {k:'new',label:'New',num:true}, {k:'preexisting',label:'Pre',num:true}],
};

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

function _prodPromoteSummary(metrics) {
  const m = metrics || {};
  const promoted = m.created_owners || m.promoted || 0;
  const inProd = m.prod_total_count || m.promoted_total_count || 0;
  const excluded = m.stale_window_excluded || 0;
  if (inProd > 0) {
    if (promoted > 0) return `${_fmt(inProd)} in prod · ${_fmt(promoted)} new`;
    if (excluded > 0) return `${_fmt(inProd)} in prod · ${_fmt(excluded)} excluded`;
    return `${_fmt(inProd)} in prod`;
  }
  return `${_fmt(promoted)} promoted`;
}

function _elapsed(seconds) {
  if (seconds == null) return '';
  seconds = Math.max(0, Math.floor(seconds));
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s > 0 ? `${m}m${s}s` : `${m}m`;
}

// ───────────────────────── mount ─────────────────────────

function mount(container) {
  // Root wrapper. `activeTab` tracks the currently-displayed tab;
  // `userPinned` flips to 'true' when the user clicks a tab so update()
  // knows to stop auto-switching on poll.
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
  // Batch selector — populated from data.all_batches on first poll.
  const batchSelect = document.createElement('select');
  batchSelect.className = 'pipeline-batch-select';
  batchSelect.style.display = 'none';
  batchSelect.addEventListener('change', () => {
    const picked = batchSelect.value || '';
    root.dataset.selectedBatch = picked;
    // Show a visible "loading" state so the user sees something is happening
    // even if the backend takes a moment.
    if (statusBadge) {
      statusBadge.textContent = 'Switching batch...';
      statusBadge.className = 'pipeline-status loading';
    }
    if (typeof window.retryDashboardPoll === 'function') {
      window.retryDashboardPoll();
    } else {
      console.warn('[foreclosure-dashboard] window.retryDashboardPoll missing');
    }
  });
  titleRow.appendChild(title);
  titleRow.appendChild(batchLabel);
  titleRow.appendChild(batchSelect);

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

      // Only stages with breakdown data are clickable.
      if (_STAGES_WITH_BREAKDOWN.includes(def.id)) {
        box.classList.add('clickable');
        box.addEventListener('click', () => {
          const latest = root._latestData;
          if (!latest) return;
          const stageRow = (latest.pipeline_stages || []).find(x => x.stage === def.id);
          _openStageModal(def.id, def.label, stageRow ? (stageRow.metrics || {}) : {});
        });
      }

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

  // Reorder: header, tabs, views — appended to root (not container directly)
  // so `.foreclosure-dashboard` wrapper is actually in the DOM. Without this
  // root is detached and pollFn's document.querySelector('.foreclosure-dashboard')
  // returns null → dataset.selectedBatch unreadable → batch switcher no-ops.
  root.appendChild(header);
  root.appendChild(tabs);
  PIPELINES.forEach(p => root.appendChild(viewEls[p.id].view));
  container.appendChild(root);

  _refreshTabActive(tabEls, 'scraping');
  _refreshViewVisibility(viewEls, 'scraping');

  const retryHandler = () => {
    if (typeof window.retryDashboardPoll === 'function') window.retryDashboardPoll();
  };
  retryBtn.addEventListener('click', retryHandler);

  return {
    root, batchLabel, batchSelect, statusBadge, lastUpdated, retryBtn,
    _retryHandler: retryHandler,
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
  const { root, batchLabel, batchSelect, tabEls, viewEls } = refs;

  // Prefer state-machine batch (orchestrator's view), fall back to leads_dev
  const summary = data.pipeline_summary || {};
  const smBatch = summary.state_machine_batch || data.batch || '';
  batchLabel.textContent = smBatch ? `Batch ${smBatch}` : '';

  // ── Batch selector options ──
  if (batchSelect && Array.isArray(data.all_batches)) {
    const current = root.dataset.selectedBatch || '';
    const want = ['', ...data.all_batches];
    const have = Array.from(batchSelect.options).map(o => o.value);
    const same = want.length === have.length && want.every((v, i) => v === have[i]);
    if (!same) {
      batchSelect.innerHTML = '';
      want.forEach(b => {
        const opt = document.createElement('option');
        opt.value = b;
        opt.textContent = b || 'Current';
        batchSelect.appendChild(opt);
      });
    }
    batchSelect.value = current;
    batchSelect.style.display = data.all_batches.length > 1 ? '' : 'none';
  }

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

  // Cache the most recent payload so a stage-box click can open the modal
  // with current data without requiring a fresh poll.
  root._latestData = data;

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
      const q = data.scraper_queue || {};
      if (q.total > 0) {
        if (q.running_job) {
          return `${q.running_job.state}/${q.running_job.scraper_name} (${_elapsed(q.running_job.elapsed_seconds)})`;
        }
        const parts = [`${_fmt(q.completed)}/${_fmt(q.total)} done`];
        if (q.failed > 0) parts.push(`${_fmt(q.failed)} fail`);
        if (q.pending > 0) parts.push(`${_fmt(q.pending)} pending`);
        return parts.join(' · ');
      }
      // Fallback for pre-queue batches
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
      // Pre-staging view of the qualify step: how many rows made it
      // through the filters (pass), how many were rejected, and how
      // many of the passing rows ALREADY exist in prod.properties
      // (preexisting — they'll be dedup'd during the promote/staging
      // step, so they're not net-new leads but also not failures).
      // `qualified` here is the raw leads_dev.qualified=TRUE count,
      // NOT the post-dedup staging count (that's the Staging stage).
      const qual = data.qualified != null ? data.qualified : (m.qualified || 0);
      const rej  = m.rejected != null ? m.rejected : (data.rejected || 0);
      const pre  = data.qualified_preexisting_prod || 0;
      if (pre > 0) return `${_fmt(qual)} pass · ${_fmt(rej)} fail · ${_fmt(pre)} pre`;
      return `${_fmt(qual)} pass · ${_fmt(rej)} fail`;
    }

    case 'staging': {
      // Final count of net-new leads that actually landed in Lightsail
      // properties_staging. This is the "real" yield — everything from
      // the qualify stage minus rows dedup'd against prod. Source:
      // pipeline_stats.py -> stats.qualified_actual_count (= len of
      // properties_staging rows for this batch).
      const newCount = (m.staged_new != null ? m.staged_new : null);
      const staged = newCount != null ? newCount : (data.qualified_actual_count || 0);
      const pre = m.preexisting_prod != null ? m.preexisting_prod : (data.qualified_preexisting_prod || 0);
      if (pre > 0) return `${_fmt(staged)} new · ${_fmt(pre)} pre`;
      return `${_fmt(staged)} new`;
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
      if (st === 'complete') return _prodPromoteSummary(m);
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

  if (prod && prod.metrics) {
    parts.push(`<span class="sk-key">Prod</span> ${_prodPromoteSummary(prod.metrics)}`);
  }

  if (!parts.length) return '<span class="sk-empty">Waiting for skiptrace to start…</span>';
  return parts.join(' · ');
}

// ───────── Drill-down modal ─────────

function _closeStageModal() {
  const m = document.querySelector('.pipeline-stage-modal');
  if (m) m.remove();
  document.removeEventListener('keydown', _modalKeydownHandler);
}

function _modalKeydownHandler(e) {
  if (e.key === 'Escape') _closeStageModal();
}

function _openStageModal(stageId, stageLabel, metrics) {
  _closeStageModal();  // single-modal

  const overlay = document.createElement('div');
  overlay.className = 'pipeline-stage-modal';
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) _closeStageModal();
  });

  const panel = document.createElement('div');
  panel.className = 'pipeline-stage-modal-panel';

  const header = document.createElement('div');
  header.className = 'pipeline-stage-modal-header';
  const title = document.createElement('h3');
  title.textContent = `${stageLabel || stageId} — by state`;
  const closeBtn = document.createElement('button');
  closeBtn.className = 'pipeline-stage-modal-close';
  closeBtn.textContent = '×';
  closeBtn.title = 'Close (Esc)';
  closeBtn.addEventListener('click', _closeStageModal);
  header.appendChild(title);
  header.appendChild(closeBtn);
  panel.appendChild(header);

  const body = document.createElement('div');
  body.className = 'pipeline-stage-modal-body';
  const rows = Array.isArray(metrics.breakdown) ? metrics.breakdown : [];
  const cols = _BREAKDOWN_COLUMNS[stageId] || [];

  if (!rows.length) {
    const empty = document.createElement('div');
    empty.className = 'pipeline-stage-modal-empty';
    empty.textContent = 'No data yet for this stage.';
    body.appendChild(empty);
  } else if (stageId === 'scrape') {
    // Grouped render: state parent row + nested per-source detail rows.
    const table = document.createElement('table');
    const thead = document.createElement('thead');
    const trh = document.createElement('tr');
    ['State / source', 'Count'].forEach((t, i) => {
      const th = document.createElement('th');
      th.textContent = t;
      if (i === 1) th.className = 'num';
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    rows.forEach(g => {
      const stateRow = document.createElement('tr');
      stateRow.className = 'pipeline-modal-state-row';
      const stateCell = document.createElement('td');
      stateCell.textContent = g.state;
      stateCell.className = 'pipeline-modal-state-label';
      const totalCell = document.createElement('td');
      totalCell.className = 'num pipeline-modal-state-total';
      totalCell.textContent = _fmt(g.total);
      stateRow.appendChild(stateCell);
      stateRow.appendChild(totalCell);
      tbody.appendChild(stateRow);
      (g.sources || []).forEach(src => {
        const tr = document.createElement('tr');
        tr.className = 'pipeline-modal-source-row';
        const st = document.createElement('td');
        st.textContent = src.source;
        st.className = 'pipeline-modal-source-label';
        const ct = document.createElement('td');
        ct.className = 'num';
        ct.textContent = _fmt(src.count);
        tr.appendChild(st);
        tr.appendChild(ct);
        tbody.appendChild(tr);
      });
    });
    table.appendChild(tbody);
    body.appendChild(table);
  } else {
    const table = document.createElement('table');
    const thead = document.createElement('thead');
    const trh = document.createElement('tr');
    cols.forEach(c => {
      const th = document.createElement('th');
      th.textContent = c.label;
      if (c.num) th.className = 'num';
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    rows.forEach(r => {
      const tr = document.createElement('tr');
      cols.forEach(c => {
        const td = document.createElement('td');
        let v = r[c.k];
        if (c.num) td.className = 'num';
        if (v == null || v === '') v = '—';
        else if (c.num) v = _fmt(v);
        td.textContent = v;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);

      // Extra detail row for qualify: every rejection reason per state
      if (stageId === 'qualify' && Array.isArray(r.reasons) && r.reasons.length) {
        const detail = document.createElement('tr');
        detail.className = 'pipeline-stage-modal-detail';
        const cell = document.createElement('td');
        cell.colSpan = cols.length;
        const pills = r.reasons
          .map(x => `<span class="pipeline-reason-pill">${x.reason} (${_fmt(x.count)})</span>`)
          .join(' ');
        cell.innerHTML = `<span class="pipeline-reason-label">All reasons:</span> ${pills}`;
        detail.appendChild(cell);
        tbody.appendChild(detail);
      }
    });
    table.appendChild(tbody);
    body.appendChild(table);
  }

  panel.appendChild(body);
  overlay.appendChild(panel);
  document.body.appendChild(overlay);
  document.addEventListener('keydown', _modalKeydownHandler);
}

function unmount(refs) {
  if (refs && refs.retryBtn && refs._retryHandler)
    refs.retryBtn.removeEventListener('click', refs._retryHandler);
}

// Dashboard is "idle" when every stage in the canonical list is complete.
// In that state there's nothing to refresh and we drop to the slow poll
// interval until a new batch starts running a stage again.
function _isIdle(data) {
  const stages = data && data.pipeline_stages;
  if (!stages || !stages.length) return false;
  return stages.every(s => s.state === 'complete');
}

// ── Self-register ──
// Show when we have direct access to the cache (Bart host) OR when we're a
// client pointing at Bart (reads the SSD cache over ssh via main.js IPC).
// Any other standalone host (no remote config, not Bart) has nothing to read.
const MAC_MINI_HOST_PREFIX = 'Bartimaeuss-Mac-mini';
const _host = (window.HOST && window.HOST.hostname) || '';
const _isClient = !!(window.HOST && window.HOST.isClient);
const _hasRemote = !!(window.HOST && window.HOST.hasRemote);
const _showForeclosure = (!_isClient && _host.startsWith(MAC_MINI_HOST_PREFIX))
  || (_isClient && _hasRemote);
if (_showForeclosure) {
  window.DASHBOARDS.push({
    id: 'foreclosure-pipeline',
    name: 'Foreclosure Pipeline',
    description: 'Scraping + skiptrace flow with stage status',
    color: 'var(--green)',
    mount, update, unmount,
    pollFn: () => {
      const root = document.querySelector('.foreclosure-dashboard');
      const pinned = root && root.dataset.selectedBatch;
      return window.cc.getPipelineStats(pinned || undefined);
    },
    pollInterval: 10000,        // 10s when a stage is active
    idlePollInterval: 60000,    // 60s when everything is complete
    idleFn: _isIdle,
  });
}

})();
