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
    { id: 'scraped', label: 'Scraped' },
    { id: 'cad', label: 'CAD' },
    { id: 'ps', label: 'PropStream' },
    { id: 'qualify', label: 'Qualify' },
    { id: 'promoted', label: 'Staging' },
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
    scrapedStage: stageEls.scraped, cadStage: stageEls.cad, psStage: stageEls.ps,
    qualifyStage: stageEls.qualify, promotedStage: stageEls.promoted,
    rejectedHeader, rejectedReasons, qualifiedStates,
  };
}

function update(refs, data) {
  const { batchLabel, scrapedStage, cadStage, psStage, qualifyStage, promotedStage,
          rejectedHeader, rejectedReasons, qualifiedStates } = refs;

  batchLabel.textContent = data.batch ? `Batch ${data.batch}` : '';
  scrapedStage.num.textContent = _fmt(data.scraped);
  scrapedStage.box.classList.remove('active', 'success');

  const cadP = data.cad_pending || 0, cadC = data.cad_complete || 0;
  cadStage.num.textContent = _fmt(cadP);
  cadStage.sec.textContent = `${_fmt(cadC)} done`;
  cadStage.box.classList.toggle('active', cadP > 0);

  const psP = data.ps_pending || 0, psC = data.ps_complete || 0;
  psStage.num.textContent = _fmt(psP);
  psStage.sec.textContent = `${_fmt(psC)} done`;
  psStage.box.classList.toggle('active', psP > 0);

  const qP = data.qualify_pending || 0, qual = data.qualified || 0, rej = data.rejected || 0;
  qualifyStage.num.textContent = _fmt(qP);
  qualifyStage.sec.textContent = `${_fmt(qual)} pass / ${_fmt(rej)} fail`;
  qualifyStage.box.classList.toggle('active', qP > 0);

  const prom = data.promoted || 0;
  promotedStage.num.textContent = _fmt(prom);
  promotedStage.sec.textContent = prom > 0 ? 'promoted' : '';
  promotedStage.box.classList.toggle('success', prom > 0);

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
