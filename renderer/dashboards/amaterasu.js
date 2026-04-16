// ── Amaterasu OCR Queue Dashboard ─────────────────────────────
// Self-registering — pushes to window.DASHBOARDS on load.
//
// Visualizes the OCR batch queue running on Amaterasu's Windows desktop:
//   - SQS depth (visible + in-flight)
//   - Active batches with progress (docs_done / docs_total) and errors
//   - Recent completed batches (last 24h)
//
// Data source: land-bot/scripts/amaterasu_ocr_stats.py via IPC
// (window.cc.getAmaterasuOcrStats — wired in preload.js).

(function() {

function _fmt(n) {
  if (n === null || n === undefined) return '–';
  return Number(n).toLocaleString();
}

function _shortBatch(id) {
  if (!id) return '';
  // batch IDs are like 1776363344540_fc1951b0 — just show last 10 chars
  return id.length > 12 ? id.slice(-10) : id;
}

function _agoSec(iso) {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (isNaN(then)) return null;
  return Math.max(0, Math.floor((Date.now() - then) / 1000));
}

function _fmtDuration(sec) {
  if (sec === null || sec === undefined) return '–';
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec/60)}m ${sec%60}s`;
  const h = Math.floor(sec/3600);
  const m = Math.floor((sec%3600)/60);
  return `${h}h ${m}m`;
}

function mount(container) {
  container.innerHTML = '';
  const root = document.createElement('div');
  root.className = 'amaterasu-dashboard';
  root.style.cssText = 'padding:16px;font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:13px;color:#ddd;';

  // Header
  const header = document.createElement('div');
  header.style.cssText = 'display:flex;align-items:baseline;gap:12px;margin-bottom:12px;';
  const title = document.createElement('h2');
  title.textContent = 'Amaterasu OCR Queue';
  title.style.cssText = 'margin:0;font-size:16px;font-weight:600;color:#fff;';
  const subtitle = document.createElement('span');
  subtitle.className = 'amaterasu-subtitle';
  subtitle.style.cssText = 'color:#888;font-size:12px;';
  header.appendChild(title);
  header.appendChild(subtitle);
  root.appendChild(header);

  // Top-line metrics row
  const metrics = document.createElement('div');
  metrics.className = 'amaterasu-metrics';
  metrics.style.cssText = 'display:flex;gap:20px;margin-bottom:16px;padding:10px 14px;background:#1a1a1a;border-radius:6px;';
  root.appendChild(metrics);

  // Active batches section
  const activeSec = document.createElement('div');
  activeSec.style.cssText = 'margin-bottom:20px;';
  const activeTitle = document.createElement('h3');
  activeTitle.textContent = 'Active batches';
  activeTitle.style.cssText = 'margin:0 0 8px 0;font-size:13px;color:#aaa;font-weight:600;text-transform:uppercase;letter-spacing:0.4px;';
  activeSec.appendChild(activeTitle);
  const activeBody = document.createElement('div');
  activeBody.className = 'amaterasu-active-body';
  activeSec.appendChild(activeBody);
  root.appendChild(activeSec);

  // Completed batches section
  const doneSec = document.createElement('div');
  const doneTitle = document.createElement('h3');
  doneTitle.textContent = 'Recent completed';
  doneTitle.style.cssText = 'margin:0 0 8px 0;font-size:13px;color:#aaa;font-weight:600;text-transform:uppercase;letter-spacing:0.4px;';
  doneSec.appendChild(doneTitle);
  const doneBody = document.createElement('div');
  doneBody.className = 'amaterasu-done-body';
  doneSec.appendChild(doneBody);
  root.appendChild(doneSec);

  container.appendChild(root);
  return { root, subtitle, metrics, activeBody, doneBody };
}

function update(refs, data) {
  if (!refs || !data) return;
  const { subtitle, metrics, activeBody, doneBody } = refs;

  if (data.error) {
    subtitle.textContent = `error: ${data.error}`;
    subtitle.style.color = '#d66';
    return;
  }

  subtitle.textContent = `updated ${_agoSec(data.generated_at) || 0}s ago`;
  subtitle.style.color = '#888';

  // Metrics row
  const sqs = data.sqs || {};
  const totals = data.totals || {};
  const metricItems = [
    { label: 'SQS visible', val: _fmt(sqs.visible) },
    { label: 'in-flight', val: _fmt(sqs.in_flight) },
    { label: 'active batches', val: _fmt(totals.active_count) },
    { label: 'completed (24h)', val: _fmt(totals.completed_24h) },
    { label: 'docs processed (24h)', val: _fmt(totals.docs_processed_24h) },
  ];
  metrics.innerHTML = metricItems.map(m =>
    `<div><div style="color:#888;font-size:11px;text-transform:uppercase;letter-spacing:0.3px;">${m.label}</div>`
    + `<div style="font-size:18px;font-weight:600;color:#fff;margin-top:2px;">${m.val}</div></div>`
  ).join('');

  // Active batches — table
  const active = data.active_batches || [];
  if (active.length === 0) {
    activeBody.innerHTML = '<div style="padding:12px;color:#666;font-style:italic;">No active batches.</div>';
  } else {
    activeBody.innerHTML = '';
    const table = document.createElement('table');
    table.style.cssText = 'width:100%;border-collapse:collapse;font-size:12px;';
    table.innerHTML = `
      <thead>
        <tr style="border-bottom:1px solid #333;text-align:left;color:#888;">
          <th style="padding:6px 8px;font-weight:500;">Friend</th>
          <th style="padding:6px 8px;font-weight:500;">Batch ID</th>
          <th style="padding:6px 8px;font-weight:500;">Progress</th>
          <th style="padding:6px 8px;font-weight:500;text-align:right;">Done</th>
          <th style="padding:6px 8px;font-weight:500;text-align:right;">Errors</th>
          <th style="padding:6px 8px;font-weight:500;text-align:right;">Elapsed</th>
          <th style="padding:6px 8px;font-weight:500;text-align:right;">Rate</th>
        </tr>
      </thead>
      <tbody></tbody>`;
    const tbody = table.querySelector('tbody');
    for (const b of active) {
      const tr = document.createElement('tr');
      tr.style.cssText = 'border-bottom:1px solid #222;';
      const total = b.docs_total;
      const done = b.docs_done || 0;
      const err = b.docs_error || 0;
      const pct = total ? (done / total) * 100 : 0;
      const elapsed = _agoSec(b.started_at);
      const rate = (elapsed && done) ? (done / (elapsed / 60)).toFixed(1) : '–';
      const progressBar = total
        ? `<div style="display:flex;align-items:center;gap:6px;">
             <div style="flex:1;height:6px;background:#222;border-radius:3px;overflow:hidden;">
               <div style="width:${pct.toFixed(1)}%;height:100%;background:${err > 0 ? '#c80' : '#090'};"></div>
             </div>
             <span style="color:#aaa;font-variant-numeric:tabular-nums;min-width:44px;">${pct.toFixed(0)}%</span>
           </div>`
        : '<span style="color:#666;">unknown total</span>';
      tr.innerHTML = `
        <td style="padding:8px;color:#ccc;">${b.friend_id || ''}</td>
        <td style="padding:8px;color:#888;font-family:monospace;font-size:11px;" title="${b.batch_id}">${_shortBatch(b.batch_id)}</td>
        <td style="padding:8px;min-width:180px;">${progressBar}</td>
        <td style="padding:8px;text-align:right;color:#ddd;font-variant-numeric:tabular-nums;">${_fmt(done)}${total ? ` / ${_fmt(total)}` : ''}</td>
        <td style="padding:8px;text-align:right;color:${err > 0 ? '#e66' : '#666'};font-variant-numeric:tabular-nums;">${_fmt(err)}</td>
        <td style="padding:8px;text-align:right;color:#aaa;font-variant-numeric:tabular-nums;">${_fmtDuration(elapsed)}</td>
        <td style="padding:8px;text-align:right;color:#aaa;font-variant-numeric:tabular-nums;">${rate}/min</td>`;
      tbody.appendChild(tr);
    }
    activeBody.appendChild(table);
  }

  // Completed batches
  const done = data.recent_completed || [];
  if (done.length === 0) {
    doneBody.innerHTML = '<div style="padding:12px;color:#666;font-style:italic;">No completed batches in the window.</div>';
  } else {
    doneBody.innerHTML = '';
    const table = document.createElement('table');
    table.style.cssText = 'width:100%;border-collapse:collapse;font-size:12px;';
    table.innerHTML = `
      <thead>
        <tr style="border-bottom:1px solid #333;text-align:left;color:#888;">
          <th style="padding:6px 8px;font-weight:500;">Friend</th>
          <th style="padding:6px 8px;font-weight:500;">Batch ID</th>
          <th style="padding:6px 8px;font-weight:500;">Status</th>
          <th style="padding:6px 8px;font-weight:500;text-align:right;">Docs</th>
          <th style="padding:6px 8px;font-weight:500;text-align:right;">Errors</th>
          <th style="padding:6px 8px;font-weight:500;text-align:right;">Elapsed</th>
          <th style="padding:6px 8px;font-weight:500;text-align:right;">Finished</th>
        </tr>
      </thead>
      <tbody></tbody>`;
    const tbody = table.querySelector('tbody');
    for (const b of done) {
      const tr = document.createElement('tr');
      tr.style.cssText = 'border-bottom:1px solid #222;';
      const statusColor = b.status === 'success' ? '#0c0'
        : b.status === 'partial_success' ? '#c80'
        : b.status === 'error' ? '#e66' : '#888';
      tr.innerHTML = `
        <td style="padding:8px;color:#ccc;">${b.friend_id || ''}</td>
        <td style="padding:8px;color:#888;font-family:monospace;font-size:11px;" title="${b.batch_id}">${_shortBatch(b.batch_id)}</td>
        <td style="padding:8px;color:${statusColor};">${b.status || '–'}</td>
        <td style="padding:8px;text-align:right;color:#ddd;font-variant-numeric:tabular-nums;">${_fmt(b.count)}</td>
        <td style="padding:8px;text-align:right;color:${(b.error_count || 0) > 0 ? '#e66' : '#666'};font-variant-numeric:tabular-nums;">${_fmt(b.error_count || 0)}</td>
        <td style="padding:8px;text-align:right;color:#aaa;font-variant-numeric:tabular-nums;">${_fmtDuration(b.elapsed_sec)}</td>
        <td style="padding:8px;text-align:right;color:#888;font-variant-numeric:tabular-nums;">${_fmtDuration(_agoSec(b.completed_at))} ago</td>`;
      tbody.appendChild(tr);
    }
    doneBody.appendChild(table);
  }
}

function unmount(refs) {
  // nothing to clean up — no listeners or timers owned by this dashboard
}

function _isIdle(data) {
  if (!data || data.error) return false;
  return (data.active_batches || []).length === 0;
}

// ── Self-register ─────────────────────────────────────────────
// Visible everywhere that has IPC wired up. The main process gates on
// script availability (pipeline-stats pattern).
window.DASHBOARDS.push({
  id: 'amaterasu-ocr',
  name: 'Amaterasu OCR',
  description: 'OCR batch queue on Amaterasu (Windows) with progress + completion stats',
  color: 'var(--yellow, #d4a300)',
  mount, update, unmount,
  pollFn: () => window.cc.getAmaterasuOcrStats(),
  pollInterval: 5000,         // 5s when active
  idlePollInterval: 30000,    // 30s when queue is empty
  idleFn: _isIdle,
});

})();
