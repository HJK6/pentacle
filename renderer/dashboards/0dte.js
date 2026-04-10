// ══════════════════════════════════════════════════════════════
// ── 0DTE Trading Dashboard ───────────────────────────────────
// ══════════════════════════════════════════════════════════════
// Reads snapshots from the 0dte-snapshots DynamoDB table via the
// dashboard:0dte-stats IPC handler. Multi-trader: a dropdown at the top
// switches between operators (bart, sai, etc.) sourced from the
// dashboard:0dte-list-traders handler.
//
// Snapshot shape (see ~/iv-rank-scanner/execution/trader/dashboard_publisher.py):
//   { trader_id, snapshot_ts, bot_version, host, trader_accounts,
//     circuit: { daily_pnl, entries_blocked, vix_breached, divergence_halt },
//     today_stats: { entries_attempted, entries_filled, tp_hits, sl_hits, realized_pnl },
//     last_scan: { decision_dt, scored_count, candidates_count, top_picks: [...] },
//     positions_by_account: { acct1: [TrackedPosition...] } }

(function() {

const _LS_KEY = 'pentacle.0dte.selected_trader';

function _fmtAge(sec) {
  if (sec === null || sec === undefined) return '—';
  if (sec < 60) return `${sec.toFixed(0)}s`;
  if (sec < 3600) return `${(sec / 60).toFixed(1)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}

function _ageColor(sec) {
  if (sec === null || sec === undefined) return 'var(--text-dim)';
  if (sec < 30) return 'var(--green)';
  if (sec < 90) return 'var(--yellow)';
  return 'var(--red)';
}

function _fmtMoney(v, signed = true) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  const sign = signed && n >= 0 ? '+' : '';
  return `${sign}$${n.toFixed(0)}`;
}

function _decimal(v) {
  // DynamoDB SDK returns Decimal — coerce to number
  if (v === null || v === undefined) return v;
  if (typeof v === 'number') return v;
  if (typeof v === 'string') return parseFloat(v);
  if (typeof v === 'object' && v !== null && 'N' in v) return parseFloat(v.N);
  return Number(v);
}

function _statusBadge(status) {
  if (!status) return { text: '—', cls: 'dim' };
  if (status === 'Filled') return { text: 'FILL', cls: 'green' };
  if (status === 'PreSubmitted' || status === 'Submitted') return { text: 'LIVE', cls: 'blue' };
  if (status === 'Cancelled' || status === 'ApiCancelled') return { text: 'CXLD', cls: 'red' };
  if (status === 'Inactive') return { text: 'INAC', cls: 'red' };
  return { text: status.slice(0, 4).toUpperCase(), cls: 'yellow' };
}

// ── DOM construction ──────────────────────────────────────────────────────

function mount(container) {
  container.innerHTML = '';
  container.style.cssText = 'padding:24px;color:var(--text);font-family:var(--font-mono);overflow:auto;height:100%;box-sizing:border-box';

  // Header bar with trader selector
  const header = document.createElement('div');
  header.style.cssText = 'display:flex;align-items:center;gap:24px;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid var(--border)';
  container.appendChild(header);

  const traderSelectWrap = document.createElement('div');
  traderSelectWrap.style.cssText = 'display:flex;align-items:center;gap:8px';
  const traderLabel = document.createElement('span');
  traderLabel.textContent = 'TRADER:';
  traderLabel.style.cssText = 'color:var(--text-dim);font-size:12px;letter-spacing:1px';
  const traderSelect = document.createElement('select');
  traderSelect.style.cssText = 'background:var(--bg-elev);color:var(--text);border:1px solid var(--border);padding:6px 12px;font-family:var(--font-mono);font-size:14px;border-radius:4px';
  traderSelectWrap.appendChild(traderLabel);
  traderSelectWrap.appendChild(traderSelect);
  header.appendChild(traderSelectWrap);

  const meta = document.createElement('div');
  meta.style.cssText = 'flex:1;display:flex;gap:24px;align-items:center;color:var(--text-dim);font-size:12px';
  header.appendChild(meta);

  const ageEl = document.createElement('span');
  ageEl.style.cssText = 'font-weight:bold;font-size:14px';
  meta.appendChild(ageEl);

  const botEl = document.createElement('span');
  meta.appendChild(botEl);

  const hostEl = document.createElement('span');
  meta.appendChild(hostEl);

  // Banners (error / divergence / VIX)
  const banner = document.createElement('div');
  banner.style.cssText = 'margin-bottom:16px;display:none;padding:12px 16px;border-radius:6px;font-weight:bold';
  container.appendChild(banner);

  // Top-level stats grid
  const statsGrid = document.createElement('div');
  statsGrid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-bottom:24px';
  container.appendChild(statsGrid);

  function makeStat(label) {
    const card = document.createElement('div');
    card.style.cssText = 'background:var(--bg-elev);padding:12px 16px;border-radius:6px;border:1px solid var(--border)';
    const lbl = document.createElement('div');
    lbl.textContent = label;
    lbl.style.cssText = 'color:var(--text-dim);font-size:11px;letter-spacing:1px;margin-bottom:4px';
    const val = document.createElement('div');
    val.style.cssText = 'font-size:22px;font-weight:bold';
    card.appendChild(lbl);
    card.appendChild(val);
    statsGrid.appendChild(card);
    return val;
  }

  const totalPnlVal = makeStat('TOTAL P&L');
  const unrealizedVal = makeStat('UNREALIZED');
  const realizedVal = makeStat('REALIZED');
  const filledVal = makeStat('FILLS TODAY');
  const tpVal = makeStat('TP HITS');
  const slVal = makeStat('SL HITS');
  const circuitVal = makeStat('CIRCUIT');

  // Last scan section
  const scanSection = document.createElement('div');
  scanSection.style.cssText = 'background:var(--bg-elev);padding:12px 16px;border-radius:6px;border:1px solid var(--border);margin-bottom:16px';
  const scanTitle = document.createElement('div');
  scanTitle.style.cssText = 'color:var(--text-dim);font-size:11px;letter-spacing:1px;margin-bottom:8px';
  scanSection.appendChild(scanTitle);
  const scanList = document.createElement('div');
  scanList.style.cssText = 'font-size:13px;line-height:1.6';
  scanSection.appendChild(scanList);
  container.appendChild(scanSection);

  // Positions section
  const posTitle = document.createElement('div');
  posTitle.textContent = 'OPEN POSITIONS';
  posTitle.style.cssText = 'color:var(--text-dim);font-size:11px;letter-spacing:1px;margin-bottom:8px;margin-top:8px';
  container.appendChild(posTitle);

  const posTableWrap = document.createElement('div');
  posTableWrap.style.cssText = 'background:var(--bg-elev);border-radius:6px;border:1px solid var(--border);overflow:hidden';
  container.appendChild(posTableWrap);

  // Footer with publish info
  const footer = document.createElement('div');
  footer.style.cssText = 'margin-top:24px;color:var(--text-dim);font-size:11px;text-align:center';
  container.appendChild(footer);

  const refs = {
    container, header, traderSelect, ageEl, botEl, hostEl, banner,
    totalPnlVal, unrealizedVal, realizedVal, filledVal, tpVal, slVal, circuitVal,
    scanTitle, scanList, posTableWrap, footer,
    selectedTrader: localStorage.getItem(_LS_KEY) || 'bart',
    knownTraders: [],
    lastSnapshotTs: 0,
  };

  // Initial trader list load + selection
  refreshTraderList(refs).then(() => {
    if (refs.knownTraders.length > 0 && !refs.knownTraders.includes(refs.selectedTrader)) {
      refs.selectedTrader = refs.knownTraders[0];
      localStorage.setItem(_LS_KEY, refs.selectedTrader);
    }
    renderTraderOptions(refs);
  });

  traderSelect.addEventListener('change', () => {
    refs.selectedTrader = traderSelect.value;
    localStorage.setItem(_LS_KEY, refs.selectedTrader);
    // Force a fresh fetch on switch (don't wait for next poll tick)
    pollFn(refs).then(d => update(refs, d));
  });

  return refs;
}

async function refreshTraderList(refs) {
  try {
    const resp = await window.cc.list0dteTraders();
    if (resp && Array.isArray(resp.traders)) {
      refs.knownTraders = resp.traders;
    }
  } catch (e) {
    // keep stale list
  }
}

function renderTraderOptions(refs) {
  refs.traderSelect.innerHTML = '';
  for (const t of refs.knownTraders) {
    const opt = document.createElement('option');
    opt.value = t;
    opt.textContent = t;
    if (t === refs.selectedTrader) opt.selected = true;
    refs.traderSelect.appendChild(opt);
  }
  if (refs.knownTraders.length === 0) {
    const opt = document.createElement('option');
    opt.value = refs.selectedTrader;
    opt.textContent = `${refs.selectedTrader} (no traders found)`;
    refs.traderSelect.appendChild(opt);
  }
}

// ── Update from a snapshot response ───────────────────────────────────────

function update(refs, resp) {
  if (!resp) return;
  if (resp._skip) return;

  // Refresh trader list every ~30 ticks (~2.5 min) so new operators show up
  refs._refreshCounter = (refs._refreshCounter || 0) + 1;
  if (refs._refreshCounter >= 30) {
    refs._refreshCounter = 0;
    refreshTraderList(refs).then(() => renderTraderOptions(refs));
  }

  if (resp.error) {
    refs.banner.textContent = `⚠ ${resp.error}`;
    refs.banner.style.background = 'var(--red-bg, rgba(255,0,0,0.15))';
    refs.banner.style.color = 'var(--red, #f55)';
    refs.banner.style.display = '';
    refs.ageEl.textContent = '— ERROR —';
    refs.ageEl.style.color = 'var(--red)';
    return;
  }

  const snap = resp.snapshot;
  const ageSec = resp.age_sec;
  refs.ageEl.textContent = `${_fmtAge(ageSec)} ago`;
  refs.ageEl.style.color = _ageColor(ageSec);

  if (!snap) {
    refs.banner.textContent = `No snapshots found for trader_id="${resp.trader_id}". Either the bot is not running or DASHBOARD_ENABLED is false in their .env.trader.`;
    refs.banner.style.background = 'var(--bg-elev)';
    refs.banner.style.color = 'var(--text-dim)';
    refs.banner.style.display = '';
    refs.botEl.textContent = '';
    refs.hostEl.textContent = '';
    refs.dayPnlVal.textContent = '—';
    refs.realizedVal.textContent = '—';
    refs.filledVal.textContent = '—';
    refs.tpVal.textContent = '—';
    refs.slVal.textContent = '—';
    refs.circuitVal.textContent = '—';
    refs.scanList.innerHTML = '';
    refs.posTableWrap.innerHTML = '';
    return;
  }

  refs.banner.style.display = 'none';
  refs.botEl.textContent = `bot=${snap.bot_version || '?'}`;
  refs.hostEl.textContent = `host=${snap.host || '?'}`;

  const circuit = snap.circuit || {};
  const todayStats = snap.today_stats || {};
  const lastScan = snap.last_scan || null;
  const positionsByAcct = snap.positions_by_account || {};

  // Stats grid
  const realized = _decimal(todayStats.realized_pnl) || 0;
  const unrealizedRaw = todayStats.unrealized_pnl;
  const unrealized = unrealizedRaw === null || unrealizedRaw === undefined ? null : _decimal(unrealizedRaw);
  const totalPnlRaw = todayStats.total_pnl;
  const totalPnl = totalPnlRaw === null || totalPnlRaw === undefined ? null : _decimal(totalPnlRaw);

  if (totalPnl !== null) {
    refs.totalPnlVal.textContent = _fmtMoney(totalPnl);
    refs.totalPnlVal.style.color = totalPnl >= 0 ? 'var(--green)' : 'var(--red)';
  } else {
    refs.totalPnlVal.textContent = _fmtMoney(realized);
    refs.totalPnlVal.style.color = realized >= 0 ? 'var(--green)' : 'var(--red)';
  }

  if (unrealized !== null) {
    refs.unrealizedVal.textContent = _fmtMoney(unrealized);
    refs.unrealizedVal.style.color = unrealized >= 0 ? 'var(--green)' : 'var(--red)';
  } else {
    refs.unrealizedVal.textContent = '—';
    refs.unrealizedVal.style.color = 'var(--text-dim)';
  }

  refs.realizedVal.textContent = _fmtMoney(realized);
  refs.realizedVal.style.color = realized >= 0 ? 'var(--green)' : 'var(--red)';

  const attempted = _decimal(todayStats.entries_attempted) || 0;
  const filled = _decimal(todayStats.entries_filled) || 0;
  refs.filledVal.textContent = `${filled}/${attempted}`;
  refs.filledVal.style.color = 'var(--text)';

  refs.tpVal.textContent = `${_decimal(todayStats.tp_hits) || 0}`;
  refs.tpVal.style.color = 'var(--green)';
  refs.slVal.textContent = `${_decimal(todayStats.sl_hits) || 0}`;
  refs.slVal.style.color = 'var(--red)';

  if (circuit.entries_blocked) {
    refs.circuitVal.textContent = 'BLOCKED';
    refs.circuitVal.style.color = 'var(--red)';
  } else if (circuit.divergence_halt) {
    refs.circuitVal.textContent = 'DIVERGENT';
    refs.circuitVal.style.color = 'var(--red)';
  } else if (circuit.vix_breached) {
    refs.circuitVal.textContent = 'VIX HALT';
    refs.circuitVal.style.color = 'var(--red)';
  } else {
    refs.circuitVal.textContent = 'OK';
    refs.circuitVal.style.color = 'var(--green)';
  }

  // Banner for halt states
  if (circuit.divergence_halt || circuit.vix_breached) {
    const flags = [];
    if (circuit.divergence_halt) flags.push('DIVERGENCE HALT — entries blocked until operator clears ~/.0dte/control/clear_divergence');
    if (circuit.vix_breached) flags.push('VIX CIRCUIT BREAKER — VIX ≥ 26 today');
    refs.banner.textContent = '⚠ ' + flags.join(' · ');
    refs.banner.style.background = 'rgba(255, 100, 100, 0.15)';
    refs.banner.style.color = 'var(--red, #f55)';
    refs.banner.style.display = '';
  }

  // Last scan section
  if (lastScan && lastScan.decision_dt) {
    const decTs = String(lastScan.decision_dt).slice(-8);
    refs.scanTitle.textContent = `LAST SCAN @ ${decTs} • scored=${_decimal(lastScan.scored_count) || 0} candidates=${_decimal(lastScan.candidates_count) || 0}`;
    refs.scanList.innerHTML = '';
    const picks = Array.isArray(lastScan.top_picks) ? lastScan.top_picks : [];
    for (const pick of picks.slice(0, 5)) {
      const row = document.createElement('div');
      const cal = (_decimal(pick.best_tp_cal_prob) || 0) * 100;
      const ev = _decimal(pick.best_ev) || 0;
      row.innerHTML = `→ <span style="color:var(--blue)">${pick.strategy || '?'}</span> tp${_decimal(pick.best_tp) || 0} cal=${cal.toFixed(1)}% EV=$${ev.toFixed(2)}`;
      refs.scanList.appendChild(row);
    }
    if (picks.length === 0) {
      refs.scanList.innerHTML = '<span style="color:var(--text-dim)">no candidates passed cutoffs</span>';
    }
  } else {
    refs.scanTitle.textContent = 'LAST SCAN — none yet';
    refs.scanList.innerHTML = '';
  }

  // Positions table per account
  refs.posTableWrap.innerHTML = '';
  const acctNames = Object.keys(positionsByAcct);
  if (acctNames.length === 0) {
    refs.posTableWrap.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text-dim)">no accounts</div>';
  }
  for (const acct of acctNames) {
    const positions = positionsByAcct[acct] || [];
    const open = positions.filter(p => !p.closed);
    const closed = positions.filter(p => p.closed);
    const closedPnl = closed.reduce((s, p) => s + (_decimal(p.close_pnl) || 0), 0);

    const acctHeader = document.createElement('div');
    acctHeader.style.cssText = 'padding:8px 16px;background:rgba(255,255,255,0.03);border-bottom:1px solid var(--border);display:flex;gap:24px;font-size:12px;color:var(--text-dim)';
    acctHeader.innerHTML = `
      <span style="color:var(--text);font-weight:bold">[${acct}]</span>
      <span>open=${open.length}</span>
      <span>closed=${closed.length}</span>
      <span>realized=<span style="color:${closedPnl >= 0 ? 'var(--green)' : 'var(--red)'}">${_fmtMoney(closedPnl)}</span></span>
    `;
    refs.posTableWrap.appendChild(acctHeader);

    if (open.length === 0) {
      const empty = document.createElement('div');
      empty.style.cssText = 'padding:16px;text-align:center;color:var(--text-dim);font-size:12px';
      empty.textContent = 'no open positions';
      refs.posTableWrap.appendChild(empty);
      continue;
    }

    const table = document.createElement('table');
    table.style.cssText = 'width:100%;border-collapse:collapse;font-size:12px';
    const thead = document.createElement('thead');
    thead.innerHTML = `
      <tr style="color:var(--text-dim);text-align:left">
        <th style="padding:8px 16px;font-weight:normal">STRATEGY</th>
        <th style="padding:8px;font-weight:normal">QTY</th>
        <th style="padding:8px;font-weight:normal">CREDIT</th>
        <th style="padding:8px;font-weight:normal">UNREAL</th>
        <th style="padding:8px;font-weight:normal">STRIKES</th>
        <th style="padding:8px;font-weight:normal">OCA</th>
        <th style="padding:8px;font-weight:normal">TP</th>
        <th style="padding:8px;font-weight:normal">SL</th>
        <th style="padding:8px 16px;font-weight:normal">ENTRY</th>
      </tr>
    `;
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    for (const p of open) {
      const tr = document.createElement('tr');
      tr.style.cssText = 'border-top:1px solid var(--border)';
      const strikes = p.strikes || {};
      const lp = _decimal(strikes.lp);
      const sp = _decimal(strikes.sp);
      const sc = _decimal(strikes.sc);
      const lc = _decimal(strikes.lc);
      const credit = _decimal(p.credit) || 0;
      const oca = String(p.oca_group || '').slice(-12);
      const tp = _statusBadge(p.tp_status);
      const sl = _statusBadge(p.sl_status);
      const entryTime = String(p.entry_time || '').slice(-8);
      const unrealRaw = p.unrealized_pnl;
      let unrealCell;
      if (unrealRaw === null || unrealRaw === undefined) {
        unrealCell = `<td style="padding:8px;color:var(--text-dim)">—</td>`;
      } else {
        const unr = _decimal(unrealRaw);
        const color = unr >= 0 ? 'var(--green)' : 'var(--red)';
        const sign = unr >= 0 ? '+' : '';
        unrealCell = `<td style="padding:8px;color:${color};font-weight:bold">${sign}$${unr.toFixed(0)}</td>`;
      }
      tr.innerHTML = `
        <td style="padding:8px 16px;color:var(--blue)">${p.strategy || '?'}</td>
        <td style="padding:8px">${_decimal(p.quantity) || 0}</td>
        <td style="padding:8px">$${credit.toFixed(2)}</td>
        ${unrealCell}
        <td style="padding:8px;color:var(--text-dim);font-size:11px">${lp}/${sp}P · ${sc}/${lc}C</td>
        <td style="padding:8px;color:var(--text-dim);font-size:11px">${oca}</td>
        <td style="padding:8px;color:var(--${tp.cls})">${tp.text}</td>
        <td style="padding:8px;color:var(--${sl.cls})">${sl.text}</td>
        <td style="padding:8px 16px;color:var(--text-dim);font-size:11px">${entryTime}</td>
      `;
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    refs.posTableWrap.appendChild(table);
  }

  // Footer
  const accountsStr = Array.isArray(snap.trader_accounts) ? snap.trader_accounts.join(', ') : '?';
  refs.footer.textContent = `accounts=${accountsStr}  •  table=0dte-snapshots  •  trader_id=${resp.trader_id}  •  refreshes every 5s`;
}

function unmount(refs) {
  // No timers to clean up; the parent app handles polling lifecycle.
}

function pollFn(refs) {
  // refs may be undefined if called before mount has completed (unlikely but safe)
  const trader = (refs && refs.selectedTrader) || localStorage.getItem(_LS_KEY) || 'bart';
  return window.cc.get0dteStats(trader);
}

// ── Self-register ──
window.DASHBOARDS.push({
  id: '0dte-trading',
  name: '0DTE Trading',
  description: 'SPX iron condor pipeline — multi-trader live snapshots',
  color: 'var(--blue)',
  mount, update, unmount, pollFn,
  pollInterval: 5000,
});

})();
