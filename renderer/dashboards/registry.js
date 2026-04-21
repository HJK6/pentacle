// ── Dashboard Registry ─────────────────────────────────────────
// Dashboards self-register by pushing to window.DASHBOARDS.
// Each dashboard file (0dte.js, foreclosure.js, etc.) pushes its own entry.
// Only loaded dashboards appear — absent files just don't register.

window.DASHBOARDS = [];

// ── Shared Utilities ──────────────────────────────────────────

function reconcilePills(container, items, keyAttr, formatFn) {
  const existing = new Map();
  container.querySelectorAll(`[data-${keyAttr}]`).forEach(el => {
    existing.set(el.dataset[keyAttr], el);
  });

  const seen = new Set();
  items.forEach(({ key, value }) => {
    seen.add(key);
    if (existing.has(key)) {
      existing.get(key).textContent = formatFn(key, value);
    } else {
      const pill = document.createElement('span');
      pill.className = keyAttr === 'reason' ? 'rejected-pill' : 'state-pill';
      pill.dataset[keyAttr] = key;
      pill.textContent = formatFn(key, value);
      container.appendChild(pill);
    }
  });

  existing.forEach((el, key) => {
    if (!seen.has(key)) el.remove();
  });
}

function _fmt(n) {
  if (n === null || n === undefined) return '0';
  return Number(n).toLocaleString();
}

// ── Staleness badge helper (dashboard-hub spec §5.3) ───────────
// Takes the IPC payload (which carries _transport_stale, _data_stale,
// _updated_at, _age_sec when the handler wraps a hub envelope).
// Returns a small status element suitable for placement near a dashboard
// title. No-op if the payload doesn't carry the staleness fields (old
// dashboards pre-hub migration pass through unchanged).
function renderStalenessBadge(container, payload) {
  if (!container) return;
  if (!payload || (payload._transport_stale === undefined && payload._data_stale === undefined && payload._updated_at === undefined)) {
    container.textContent = '';
    return;
  }
  const age = payload._age_sec;
  const fmtAge = (s) => {
    if (s == null) return '';
    if (s < 60) return `${Math.floor(s)}s ago`;
    if (s < 3600) return `${Math.floor(s/60)}m ago`;
    return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m ago`;
  };
  let label, color, title;
  if (payload._transport_stale) {
    label = `⚠ Bart unreachable — last known ${fmtAge(age)}`;
    color = '#d4a300'; // yellow
    title = 'Dashboard Hub WebSocket is not connected. Showing disk-cache data.';
  } else if (payload._data_stale) {
    label = `⚠ producer idle — data is ${fmtAge(age)}`;
    color = '#d4740a'; // orange
    title = 'Hub is reachable, but the producer has not published recent data.';
  } else {
    label = `● live · updated ${fmtAge(age)}`;
    color = '#4ec275'; // green
    title = 'Hub connected, producer publishing fresh data.';
  }
  container.style.cssText = 'color:' + color + ';font-size:12px;';
  container.title = title;
  container.textContent = label;
}
