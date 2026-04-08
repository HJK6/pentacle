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
