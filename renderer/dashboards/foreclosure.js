// ── Foreclosure Pipeline Dashboard (Bartimaeus only) ─────────
// Self-registering — pushes to window.DASHBOARDS on load.
// Not included for Tars/Lews — this file is specific to our setup.

(function() {

function mount(container) {
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

  const flow = document.createElement('div');
  flow.className = 'pipeline-flow';

  const stageDefs = [
    { id: 'scrape', label: 'Scrape' },
    { id: 'cad', label: 'CAD' },
    { id: 'ps', label: 'PropStream' },
    { id: 'qualify', label: 'Qualify' },
    { id: 'skipmatrix', label: 'SkipMatrix' },
    { id: 'prod', label: 'Prod' },
  ];

  const stageEls = {};
  stageDefs.forEach((def, i) => {
    const box = document.createElement('div');
    box.className = 'pipeline-stage';
    box.dataset.stageId = def.id;
    const num = document.createElement('div');
    num.className = 'stage-number';
    num.textContent = '—';
    const lbl = document.createElement('div');
    lbl.className = 'stage-label';
    lbl.textContent = def.label;
    const sec = document.createElement('div');
    sec.className = 'stage-secondary';
    box.appendChild(num);
    box.appendChild(lbl);
    box.appendChild(sec);
    flow.appendChild(box);
    stageEls[def.id] = { box, num, sec };
    if (i < stageDefs.length - 1) {
      const arrow = document.createElement('div');
      arrow.className = 'pipeline-arrow';
      arrow.textContent = '→';
      flow.appendChild(arrow);
    }
  });

  const rejectedBox = document.createElement('div');
  rejectedBox.className = 'pipeline-rejected';
  const rejectedHeader = document.createElement('div');
  rejectedHeader.style.cssText = 'font-size:11px;font-weight:700;color:var(--red);margin-bottom:8px;text-transform:uppercase;letter-spacing:0.5px';
  rejectedHeader.textContent = 'Rejected — 0';
  const rejectedReasons = document.createElement('div');
  rejectedReasons.className = 'pipeline-rejected-pills';
  rejectedBox.appendChild(rejectedHeader);
  rejectedBox.appendChild(rejectedReasons);

  const statesBox = document.createElement('div');
  statesBox.className = 'pipeline-states-section';
  const statesHeader = document.createElement('div');
  statesHeader.style.cssText = 'font-size:11px;font-weight:700;color:var(--fg-dim);margin-bottom:8px;text-transform:uppercase;letter-spacing:0.5px';
  statesHeader.textContent = 'Qualified by State';
  const qualifiedStates = document.createElement('div');
  qualifiedStates.className = 'pipeline-states-pills';
  statesBox.appendChild(statesHeader);
  statesBox.appendChild(qualifiedStates);

  container.appendChild(header);
  container.appendChild(flow);
  container.appendChild(rejectedBox);
  container.appendChild(statesBox);

  const retryHandler = () => {
    if (typeof window.retryDashboardPoll === 'function') window.retryDashboardPoll();
  };
  retryBtn.addEventListener('click', retryHandler);

  return {
    batchLabel, statusBadge, lastUpdated, retryBtn, _retryHandler: retryHandler,
    scrapeStage: stageEls.scrape, cadStage: stageEls.cad, psStage: stageEls.ps,
    qualifyStage: stageEls.qualify,
    skipmatrixStage: stageEls.skipmatrix, prodStage: stageEls.prod,
    rejectedHeader, rejectedReasons, qualifiedStates,
  };
}

// Map state machine stage names → SkipMatrix box sub-state label
const SM_LABELS = {
  skipmatrix_csv:           'csv',
  skipmatrix_submit:        'submit',
  skipmatrix_confirm:       'confirm',
  skipmatrix_invoice:       'invoice',
  skipmatrix_pay:           'pay',
  skipmatrix_paid_confirm:  'paid',
  skipmatrix_results:       'results',
};

const SM_STAGE_IDS = Object.keys(SM_LABELS);

function _stagesByName(stages) {
  const out = {};
  (stages || []).forEach(s => { out[s.stage] = s; });
  return out;
}

function _isBlocked(state) {
  return state === 'failed' || state === 'blocked_qa' || state === 'blocked_circuit_breaker';
}

function update(refs, data) {
  const { batchLabel, scrapeStage, cadStage, psStage, qualifyStage,
          skipmatrixStage, prodStage,
          rejectedHeader, rejectedReasons, qualifiedStates } = refs;

  // Prefer state-machine batch (orchestrator's view), fall back to leads_dev batch
  const summary = data.pipeline_summary || {};
  const smBatch = summary.state_machine_batch || data.batch || '';
  batchLabel.textContent = smBatch ? `Batch ${smBatch}` : '';

  const smStages = _stagesByName(data.pipeline_stages);

  // ─── Scrape box ───
  // Show total scraped + ingested counts from the scrape stage's metrics if
  // present (state machine view). Otherwise fall back to leads_dev counts.
  const scrapeRow = smStages['scrape'];
  const scrapeMetrics = (scrapeRow && scrapeRow.metrics) || {};
  const totalScraped = scrapeMetrics.total_scraped != null
    ? scrapeMetrics.total_scraped
    : (data.scraped || 0);
  const totalIngested = scrapeMetrics.total_ingested != null
    ? scrapeMetrics.total_ingested
    : (data.scraped || 0);
  const totalDuped = scrapeMetrics.total_duped || 0;
  scrapeStage.num.textContent = _fmt(totalScraped);
  scrapeStage.sec.textContent = totalDuped > 0
    ? `${_fmt(totalIngested)} new / ${_fmt(totalDuped)} dupe`
    : `${_fmt(totalIngested)} ingested`;
  scrapeStage.box.classList.toggle('active', scrapeRow && scrapeRow.state === 'running');
  scrapeStage.box.classList.toggle('success', scrapeRow && scrapeRow.state === 'complete');
  scrapeStage.box.classList.toggle('blocked', scrapeRow && _isBlocked(scrapeRow.state));

  // ─── CAD ───
  const cadP = data.cad_pending || 0, cadC = data.cad_complete || 0;
  cadStage.num.textContent = _fmt(cadP);
  cadStage.sec.textContent = `${_fmt(cadC)} done`;
  const cadRow = smStages['cad'];
  cadStage.box.classList.toggle('active', cadP > 0 || (cadRow && cadRow.state === 'running'));
  cadStage.box.classList.toggle('success', cadRow && cadRow.state === 'complete');
  cadStage.box.classList.toggle('blocked', cadRow && _isBlocked(cadRow.state));

  // ─── PropStream ───
  const psP = data.ps_pending || 0, psC = data.ps_complete || 0;
  psStage.num.textContent = _fmt(psP);
  psStage.sec.textContent = `${_fmt(psC)} done`;
  const psRow = smStages['propstream'];
  psStage.box.classList.toggle('active', psP > 0 || (psRow && psRow.state === 'running'));
  psStage.box.classList.toggle('success', psRow && psRow.state === 'complete');
  psStage.box.classList.toggle('blocked', psRow && _isBlocked(psRow.state));

  // ─── Qualify ───
  const qP = data.qualify_pending || 0, qual = data.qualified || 0, rej = data.rejected || 0;
  qualifyStage.num.textContent = _fmt(qP);
  qualifyStage.sec.textContent = `${_fmt(qual)} pass / ${_fmt(rej)} fail`;
  const qRow = smStages['qualify'];
  qualifyStage.box.classList.toggle('active', qP > 0 || (qRow && qRow.state === 'running'));
  qualifyStage.box.classList.toggle('success', qRow && qRow.state === 'complete');
  qualifyStage.box.classList.toggle('blocked', qRow && _isBlocked(qRow.state));

  // ─── SkipMatrix (collapsed: 7 sub-stages) ───
  // Find the first sub-stage that isn't complete, show it as the secondary
  // label. If all are complete, show "done". If none have started, show "—".
  let smCurrent = null;
  let smState = null;
  let smAllComplete = true;
  let smAnyStarted = false;
  for (const id of SM_STAGE_IDS) {
    const r = smStages[id];
    if (!r) { smAllComplete = false; continue; }
    if (r.state !== 'complete') {
      smAllComplete = false;
      if (!smCurrent) {
        smCurrent = id;
        smState = r.state;
      }
    }
    if (r.state !== 'waiting') smAnyStarted = true;
  }
  const smCsvRows = (smStages.skipmatrix_csv && smStages.skipmatrix_csv.metrics && smStages.skipmatrix_csv.metrics.csv_rows) || 0;
  if (smAllComplete) {
    skipmatrixStage.num.textContent = _fmt(smCsvRows || qual || 0);
    skipmatrixStage.sec.textContent = 'submitted';
    skipmatrixStage.box.classList.add('success');
    skipmatrixStage.box.classList.remove('active', 'blocked');
  } else if (smCurrent) {
    skipmatrixStage.num.textContent = _fmt(smCsvRows || qual || 0);
    const subLabel = SM_LABELS[smCurrent] || smCurrent;
    skipmatrixStage.sec.textContent = `${subLabel} (${smState})`;
    skipmatrixStage.box.classList.toggle('active', smState === 'running' || smState === 'waiting_email');
    skipmatrixStage.box.classList.toggle('blocked', _isBlocked(smState));
    skipmatrixStage.box.classList.remove('success');
  } else {
    skipmatrixStage.num.textContent = '—';
    skipmatrixStage.sec.textContent = '';
    skipmatrixStage.box.classList.remove('active', 'success', 'blocked');
  }

  // ─── Prod (prod_hydrate_promote) ───
  const prodRow = smStages['prod_hydrate_promote'];
  if (prodRow) {
    const prodMetrics = prodRow.metrics || {};
    const promotedCount = prodMetrics.created_owners || prodMetrics.promoted || 0;
    if (prodRow.state === 'complete') {
      prodStage.num.textContent = _fmt(promotedCount);
      prodStage.sec.textContent = 'promoted';
      prodStage.box.classList.add('success');
      prodStage.box.classList.remove('active', 'blocked');
    } else if (prodRow.state === 'running') {
      prodStage.num.textContent = _fmt(promotedCount);
      prodStage.sec.textContent = 'promoting';
      prodStage.box.classList.add('active');
      prodStage.box.classList.remove('success', 'blocked');
    } else if (_isBlocked(prodRow.state)) {
      prodStage.num.textContent = '!';
      prodStage.sec.textContent = prodRow.state;
      prodStage.box.classList.add('blocked');
      prodStage.box.classList.remove('active', 'success');
    } else {
      prodStage.num.textContent = '—';
      prodStage.sec.textContent = '';
      prodStage.box.classList.remove('active', 'success', 'blocked');
    }
  } else {
    prodStage.num.textContent = '—';
    prodStage.sec.textContent = '';
    prodStage.box.classList.remove('active', 'success', 'blocked');
  }

  rejectedHeader.textContent = `Rejected — ${_fmt(rej)}`;

  const reasons = Object.entries(data.rejection_reasons || {}).sort((a,b) => b[1]-a[1]).slice(0,8).map(([key,value]) => ({key,value}));
  reconcilePills(rejectedReasons, reasons, 'reason', (k,v) => `${k} (${v})`);

  const states = Object.entries(data.qualified_by_state || {}).sort((a,b) => b[1]-a[1]).slice(0,10).map(([key,value]) => ({key,value}));
  reconcilePills(qualifiedStates, states, 'state', (k,v) => `${k}: ${v}`);
}

function unmount(refs) {
  if (refs && refs.retryBtn && refs._retryHandler)
    refs.retryBtn.removeEventListener('click', refs._retryHandler);
}

// ── Self-register ──
window.DASHBOARDS.push({
  id: 'foreclosure-pipeline',
  name: 'Foreclosure Pipeline',
  description: 'Live scraper → hydration → staging flow',
  color: 'var(--green)',
  mount, update, unmount,
  pollFn: () => window.cc.getPipelineStats(),
  pollInterval: 10000,
});

})();
