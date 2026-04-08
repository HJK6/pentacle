// ══════════════════════════════════════════════════════════════
// ── 0DTE Trading Dashboard (prebuilt) ────────────────────────
// ══════════════════════════════════════════════════════════════
// Self-registering — pushes to window.DASHBOARDS on load.
// Ships with Pentacle for all bots running the 0DTE pipeline.
// Requires: preload bridge `window.cc.get0dteStats()`
//           IPC handler `dashboard:0dte-stats`

(function() {

// ET market schedule
const DTE_SCHEDULE = {
  GATEWAY_START: 9, MARKET_OPEN: 9.5, ENTRY_START: 10,
  ENTRY_END: 14, FORCED_EXIT: 14.917, MARKET_CLOSE: 15,
};

function _dteState() {
  const now = new Date();
  const day = now.getDay();
  const h = now.getHours() + now.getMinutes() / 60;
  if (day === 0 || day === 6) return 'complete';
  if (h < DTE_SCHEDULE.GATEWAY_START) return 'pre-market';
  if (h < DTE_SCHEDULE.MARKET_OPEN) return 'initializing';
  if (h < DTE_SCHEDULE.ENTRY_START) return 'waiting';
  if (h < DTE_SCHEDULE.FORCED_EXIT) return 'trading';
  if (h < DTE_SCHEDULE.MARKET_CLOSE) return 'closing';
  return 'complete';
}

function _nextSessionTime() {
  const now = new Date();
  const target = new Date(now);
  target.setHours(DTE_SCHEDULE.GATEWAY_START, 0, 0, 0);
  if (now >= target || now.getDay() === 0 || now.getDay() === 6) {
    target.setDate(target.getDate() + 1);
    while (target.getDay() === 0 || target.getDay() === 6)
      target.setDate(target.getDate() + 1);
  }
  return target;
}

function _formatCountdown(ms) {
  if (ms <= 0) return '0s';
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m ${sec}s`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
}

const STATE_LABELS = {
  'pre-market': { text: 'Pre-Market', cls: 'stale', desc: 'Next session in' },
  'initializing': { text: 'Initializing', cls: 'loading', desc: 'Gateway connecting, TWS starting' },
  'waiting': { text: 'Waiting', cls: 'loading', desc: 'Market open — waiting for entry window' },
  'trading': { text: 'Trading Live', cls: 'live', desc: 'Actively trading' },
  'closing': { text: 'Closing', cls: 'stale', desc: 'Forced exit in progress' },
  'complete': { text: 'Day Complete', cls: 'stale', desc: 'Next session in' },
};

function mount(container) {
  container.innerHTML = '';

  const banner = document.createElement('div');
  banner.className = 'dte-banner';
  banner.innerHTML = `
    <div class="dte-banner-state"></div>
    <div class="dte-banner-desc"></div>
    <div class="dte-banner-countdown"></div>
  `;
  container.appendChild(banner);

  const header = document.createElement('div');
  header.className = 'dte-header';
  header.innerHTML = `
    <div class="pipeline-title-row">
      <h2 class="pipeline-title" style="margin:0">0DTE Trading</h2>
      <span class="dte-date"></span>
      <span class="pipeline-status live">Gateway</span>
    </div>
    <div class="dte-market-bar">
      <span class="dte-market-item">SPX <strong class="dte-spx">—</strong></span>
      <span class="dte-market-item">VIX <strong class="dte-vix">—</strong></span>
      <span class="dte-market-item dte-cycle-item">Cycle <strong class="dte-cycle">—</strong></span>
    </div>
  `;
  container.appendChild(header);

  const grid = document.createElement('div');
  grid.className = 'dte-stats-grid';
  ['account', 'pnl', 'activity', 'positions'].forEach(id => {
    const label = { account: 'Portfolio', pnl: 'Daily P&L', activity: 'Activity', positions: 'Positions' }[id];
    const card = document.createElement('div');
    card.className = 'dte-stat-card';
    card.dataset.card = id;
    card.innerHTML = `<div class="dte-card-label">${label}</div><div class="dte-card-value">—</div><div class="dte-card-sub">—</div>`;
    grid.appendChild(card);
  });
  container.appendChild(grid);

  const flow = document.createElement('div');
  flow.className = 'dte-flow';
  flow.innerHTML = `
    <div class="dte-flow-stage"><div class="dte-flow-num dte-signals-num">0</div><div class="dte-flow-label">Signals</div></div>
    <div class="dte-flow-arrow">→</div>
    <div class="dte-flow-stage"><div class="dte-flow-num dte-orders-num">0</div><div class="dte-flow-label">Orders</div></div>
    <div class="dte-flow-arrow">→</div>
    <div class="dte-flow-stage"><div class="dte-flow-num dte-fills-num" style="color:var(--green)">0</div><div class="dte-flow-label">Filled</div></div>
    <div class="dte-flow-arrow">→</div>
    <div class="dte-flow-stage"><div class="dte-flow-num dte-cancelled-num" style="color:var(--fg-dim)">0</div><div class="dte-flow-label">Cancelled</div></div>
    <div style="margin-left:auto;text-align:right">
      <div class="dte-fill-rate">—%</div>
      <div class="dte-flow-label">Fill Rate</div>
    </div>
  `;
  container.appendChild(flow);

  const tableWrap = document.createElement('div');
  tableWrap.className = 'dte-table-wrap';
  tableWrap.innerHTML = `
    <div class="dte-table-header">Today's Trades</div>
    <table class="dte-table"><thead><tr><th>IC</th><th>Put Spread</th><th>Call Spread</th><th>Size</th><th>Credit</th><th>Time</th></tr></thead>
    <tbody class="dte-trades-body"></tbody></table>
    <div class="dte-empty-msg" style="display:none">No trades today</div>
  `;
  container.appendChild(tableWrap);

  const posWrap = document.createElement('div');
  posWrap.className = 'dte-table-wrap';
  posWrap.innerHTML = `
    <div class="dte-table-header">Open Positions</div>
    <table class="dte-table"><thead><tr><th>Symbol</th><th>Pos</th><th>Avg Cost</th><th>Mkt Value</th><th>P&L</th></tr></thead>
    <tbody class="dte-positions-body"></tbody></table>
    <div class="dte-positions-empty" style="display:none">No open positions</div>
  `;
  container.appendChild(posWrap);

  const countdownTimer = setInterval(() => {
    const state = _dteState();
    const info = STATE_LABELS[state];
    banner.querySelector('.dte-banner-state').textContent = info.text;
    banner.querySelector('.dte-banner-state').className = 'dte-banner-state dte-state-' + info.cls;

    if (state === 'pre-market' || state === 'complete') {
      banner.querySelector('.dte-banner-countdown').textContent = _formatCountdown(_nextSessionTime() - new Date());
      banner.querySelector('.dte-banner-desc').textContent = info.desc;
      banner.style.display = '';
    } else if (state === 'initializing' || state === 'waiting') {
      banner.querySelector('.dte-banner-countdown').textContent = '';
      banner.querySelector('.dte-banner-desc').textContent = info.desc;
      banner.style.display = '';
    } else if (state === 'closing') {
      const close = new Date(); close.setHours(15, 0, 0, 0);
      banner.querySelector('.dte-banner-countdown').textContent = _formatCountdown(close - new Date());
      banner.querySelector('.dte-banner-desc').textContent = info.desc;
      banner.style.display = '';
    } else {
      banner.style.display = 'none';
    }
  }, 1000);

  return {
    _countdownTimer: countdownTimer, banner,
    dateEl: header.querySelector('.dte-date'),
    statusEl: header.querySelector('.pipeline-status'),
    spxEl: header.querySelector('.dte-spx'),
    vixEl: header.querySelector('.dte-vix'),
    cycleEl: header.querySelector('.dte-cycle'),
    cardAccount: grid.querySelector('[data-card="account"]'),
    cardPnl: grid.querySelector('[data-card="pnl"]'),
    cardActivity: grid.querySelector('[data-card="activity"]'),
    cardPositions: grid.querySelector('[data-card="positions"]'),
    signalsNum: flow.querySelector('.dte-signals-num'),
    ordersNum: flow.querySelector('.dte-orders-num'),
    fillsNum: flow.querySelector('.dte-fills-num'),
    cancelledNum: flow.querySelector('.dte-cancelled-num'),
    fillRate: flow.querySelector('.dte-fill-rate'),
    tradesBody: tableWrap.querySelector('.dte-trades-body'),
    tradesEmpty: tableWrap.querySelector('.dte-empty-msg'),
    posBody: posWrap.querySelector('.dte-positions-body'),
    posEmpty: posWrap.querySelector('.dte-positions-empty'),
  };
}

function update(refs, d) {
  if (d.error || d._skip) return;

  const state = _dteState();
  refs.dateEl.textContent = d.date || '';
  if (state === 'complete' || state === 'pre-market') {
    refs.statusEl.textContent = 'Market Closed';
    refs.statusEl.className = 'pipeline-status stale';
  } else {
    const gwOk = d.gateway_status === 'ok';
    refs.statusEl.textContent = gwOk ? 'Gateway Live' : (d.gateway_status || 'Offline');
    refs.statusEl.className = 'pipeline-status ' + (gwOk ? 'live' : 'error');
  }
  refs.spxEl.textContent = d.spx ? `$${Number(d.spx).toLocaleString(undefined, {minimumFractionDigits:2})}` : '—';
  refs.vixEl.textContent = d.vix ? d.vix.toFixed(1) : (d.vix1d ? d.vix1d.toFixed(1) : '—');
  refs.cycleEl.textContent = d.data_age_ms ? `${(d.data_age_ms/1000).toFixed(1)}s` : '—';

  const $v = (card, val, sub) => {
    card.querySelector('.dte-card-value').textContent = val;
    card.querySelector('.dte-card-sub').textContent = sub;
  };

  $v(refs.cardAccount,
    d.portfolio_value ? `$${Number(d.portfolio_value).toLocaleString(undefined, {maximumFractionDigits:0})}` : '—',
    d.buying_power ? `BP: $${Number(d.buying_power).toLocaleString(undefined, {maximumFractionDigits:0})}` : '');

  const pnl = (d.realized_pnl || 0) + (d.unrealized_pnl || 0);
  const pnlPct = d.portfolio_value > 0 ? (pnl / d.portfolio_value * 100) : 0;
  refs.cardPnl.querySelector('.dte-card-value').textContent = `$${pnl >= 0 ? '+' : ''}${pnl.toFixed(0)}`;
  refs.cardPnl.querySelector('.dte-card-value').style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
  refs.cardPnl.querySelector('.dte-card-sub').textContent =
    `${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}% | R: $${(d.realized_pnl||0).toFixed(0)} U: $${(d.unrealized_pnl||0).toFixed(0)}`;

  $v(refs.cardActivity, `${d.orders_filled || 0} filled`,
    `${d.signals_received || 0} signals | ${d.orders_placed || 0} attempted`);
  $v(refs.cardPositions, `${d.open_ics || 0} ICs`,
    `${d.open_legs || 0} legs | Credit: $${(d.total_credit||0).toFixed(0)}`);

  refs.signalsNum.textContent = _fmt(d.signals_received);
  refs.ordersNum.textContent = _fmt(d.orders_placed);
  refs.fillsNum.textContent = _fmt(d.orders_filled);
  refs.cancelledNum.textContent = _fmt(d.orders_cancelled);
  refs.fillRate.textContent = `${d.fill_rate || 0}%`;

  const entries = d.entries || [];
  if (entries.length === 0) {
    refs.tradesBody.innerHTML = '';
    refs.tradesEmpty.style.display = '';
  } else {
    refs.tradesEmpty.style.display = 'none';
    refs.tradesBody.innerHTML = entries.map(e => `<tr>
      <td>${e.label}</td><td>${e.put_spread}</td><td>${e.call_spread}</td>
      <td>${e.size}</td><td>$${e.credit}</td><td>${(e.time || '').slice(11, 19)}</td>
    </tr>`).join('');
  }

  const positions = d.positions || [];
  if (positions.length === 0) {
    refs.posBody.innerHTML = '';
    refs.posEmpty.style.display = '';
  } else {
    refs.posEmpty.style.display = 'none';
    refs.posBody.innerHTML = positions.map(p => {
      const pv = p.unrealizedPnL || 0;
      return `<tr>
        <td>${(p.symbol || '').trim()}</td>
        <td>${p.position > 0 ? '+' : ''}${p.position}</td>
        <td>$${(p.avgCost || 0).toFixed(2)}</td>
        <td>$${(p.marketValue || 0).toFixed(2)}</td>
        <td class="${pv >= 0 ? 'dte-pnl-pos' : 'dte-pnl-neg'}">$${pv >= 0 ? '+' : ''}${pv.toFixed(2)}</td>
      </tr>`;
    }).join('');
  }
}

function unmount(refs) {
  if (refs && refs._countdownTimer) clearInterval(refs._countdownTimer);
}

let _finalFetched = false;
function pollFn() {
  const state = _dteState();
  if (state === 'pre-market') { _finalFetched = false; return Promise.resolve({ _skip: true }); }
  if (state === 'complete') {
    if (!_finalFetched) { _finalFetched = true; return window.cc.get0dteStats(); }
    return Promise.resolve({ _skip: true });
  }
  _finalFetched = false;
  return window.cc.get0dteStats();
}

// ── Self-register ──
window.DASHBOARDS.push({
  id: '0dte-trading',
  name: '0DTE Trading',
  description: 'SPX iron condor pipeline — live P&L, positions, signals',
  color: 'var(--blue)',
  mount, update, unmount, pollFn,
  pollInterval: 10000,
});

})();
