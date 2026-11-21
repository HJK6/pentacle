'use strict';

const DEFAULT_STALE_AFTER_MS = 90_000;
const DEFAULT_STALE_REFRESH_MS = 30_000;

function finiteNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function validPercent(value) {
  const number = finiteNumber(value);
  return number === null || number < 0 || number > 100 ? null : number;
}

function formatPercent(value, unavailable = '--') {
  const number = validPercent(value);
  return number === null ? unavailable : `${Math.round(number)}%`;
}

function usagePercent(used, total) {
  const usedNumber = finiteNumber(used);
  const totalNumber = finiteNumber(total);
  if (usedNumber === null || totalNumber === null || totalNumber <= 0) return null;
  return Math.max(0, Math.min(100, usedNumber / totalNumber * 100));
}

function currentTime(now) {
  const value = typeof now === 'function' ? now() : now;
  const number = finiteNumber(value);
  return number === null ? Date.now() : number;
}

function machineCpuPercent(stats, now, staleAfterMs) {
  const sampledAt = Date.parse(String(stats?.sampled_at || ''));
  if (!Number.isFinite(sampledAt) || currentTime(now) - sampledAt > staleAfterMs) return null;
  return validPercent(stats?.cpu_usage_pct);
}

function makeElement(document, tag, className, parent) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (parent) parent.appendChild(element);
  return element;
}

function textElement(document, tag, className, parent, text) {
  const element = makeElement(document, tag, className, parent);
  element.textContent = String(text ?? '');
  return element;
}

function appendMeter(document, parent, label, value, unavailable = '--') {
  const number = value === null ? null : validPercent(value);
  const meter = makeElement(document, 'span', 'bar', parent);
  meter.setAttribute('role', 'meter');
  meter.setAttribute('aria-label', `${label} usage`);
  meter.setAttribute('aria-valuemin', '0');
  meter.setAttribute('aria-valuemax', '100');
  if (number === null) meter.setAttribute('aria-valuetext', unavailable);
  else meter.setAttribute('aria-valuenow', String(number));
  const fill = makeElement(document, 'i', '', meter);
  fill.style.width = `${number === null ? 0 : number}%`;
  return meter;
}

function safeLimits(limits) {
  return Array.isArray(limits) ? limits.filter(limit => limit && typeof limit === 'object' && !Array.isArray(limit)) : [];
}

function safeHosts(hosts) {
  return hosts && typeof hosts === 'object' && !Array.isArray(hosts) ? Object.entries(hosts)
    .filter(([, stats]) => stats && typeof stats === 'object' && !Array.isArray(stats)) : [];
}

function orderHosts(entries, localHost, hostOrder) {
  const local = String(localHost || '').toLowerCase();
  const roster = new Map((Array.isArray(hostOrder) ? hostOrder : []).map((host, index) => [
    String(host || '').toLowerCase(), index,
  ]));
  return entries.sort(([leftHost], [rightHost]) => {
    const left = String(leftHost).toLowerCase();
    const right = String(rightHost).toLowerCase();
    if (left === local && right !== local) return -1;
    if (right === local && left !== local) return 1;
    const leftOrder = roster.has(left) ? roster.get(left) : Number.MAX_SAFE_INTEGER;
    const rightOrder = roster.has(right) ? roster.get(right) : Number.MAX_SAFE_INTEGER;
    return leftOrder - rightOrder || left.localeCompare(right);
  });
}

function hostDisplayName(host, stats, hostLabel) {
  if (typeof hostLabel === 'function') {
    try {
      const label = hostLabel(host, stats);
      if (typeof label === 'string' && label.trim()) return label.trim();
    } catch (_) {
      // A presentation callback must not prevent the rest of the statistics from rendering.
    }
  }
  return String(host);
}

function appendLimits(document, root, limits) {
  const section = makeElement(document, 'section', 'limits-section', root);
  textElement(document, 'h3', '', section, 'Limits');
  const rows = makeElement(document, 'div', 'limits-rows', section);
  for (const limit of safeLimits(limits)) {
    const label = String(limit.label ?? limit.id ?? '');
    const pct = validPercent(limit.pct);
    const row = makeElement(document, 'div', 'prow', rows);
    if (limit.id !== undefined) row.dataset.limitId = String(limit.id);
    textElement(document, 'span', 'limit-label', row, label);
    appendMeter(document, row, `${label} limit`, pct, '—');
    textElement(document, 'span', 'num', row, formatPercent(pct, '—'));
  }
}

function appendMachines(document, root, hosts, options) {
  const section = makeElement(document, 'section', 'machines-section', root);
  textElement(document, 'h3', '', section, 'Machines');
  const table = makeElement(document, 'div', 'machines-table', section);
  table.setAttribute('role', 'table');
  table.setAttribute('aria-label', 'Machine statistics');

  const header = makeElement(document, 'div', 'hrow hd', table);
  header.setAttribute('role', 'row');
  const blank = makeElement(document, 'span', '', header);
  blank.setAttribute('aria-hidden', 'true');
  for (const label of ['CPU', 'RAM', 'Disk']) {
    textElement(document, 'span', '', header, label).setAttribute('role', 'columnheader');
  }

  const now = currentTime(options.now);
  const staleAfterMs = Math.max(0, finiteNumber(options.staleAfterMs) ?? DEFAULT_STALE_AFTER_MS);
  const entries = orderHosts(safeHosts(hosts), options.localHost, options.hostOrder);
  for (const [host, stats] of entries) {
    const row = makeElement(document, 'div', 'hrow', table);
    row.setAttribute('role', 'row');
    row.dataset.machineHost = String(host);
    textElement(document, 'span', 'machine-name', row, hostDisplayName(host, stats, options.hostLabel))
      .setAttribute('role', 'rowheader');
    textElement(document, 'span', 'num', row, formatPercent(machineCpuPercent(stats, now, staleAfterMs)))
      .setAttribute('role', 'cell');
    textElement(document, 'span', 'num', row,
      formatPercent(usagePercent(stats.memory_used_bytes, stats.memory_total_bytes)))
      .setAttribute('role', 'cell');
    textElement(document, 'span', 'num', row,
      formatPercent(usagePercent(stats.disk_used_bytes, stats.disk_total_bytes)))
      .setAttribute('role', 'cell');
  }
}

function renderLimitsView(document, data = {}, options = {}) {
  if (!document || typeof document.createElement !== 'function') throw new TypeError('document is required');
  const root = makeElement(document, 'div', 'limits-view');
  appendLimits(document, root, data.limits);
  appendMachines(document, root, data.hosts, options);
  return root;
}

function mountLimitsView(container, options = {}) {
  if (!container || !container.ownerDocument || typeof container.replaceChildren !== 'function') {
    throw new TypeError('a DOM container is required');
  }
  const document = container.ownerDocument;
  const data = {
    limits: Array.isArray(options.limits) ? options.limits : [],
    hosts: options.hosts && typeof options.hosts === 'object' ? options.hosts : {},
  };
  const now = options.now || Date.now;
  const renderOptions = { ...options, now };
  const refreshMs = Math.max(0, finiteNumber(options.staleRefreshMs) ?? DEFAULT_STALE_REFRESH_MS);
  let destroyed = false;

  function render() {
    if (destroyed) return;
    container.replaceChildren(renderLimitsView(document, data, renderOptions));
  }

  render();
  const timer = refreshMs > 0
    ? (options.setInterval || setInterval)(render, refreshMs)
    : null;

  return {
    update(next = {}) {
      if (destroyed) return;
      if (Object.prototype.hasOwnProperty.call(next, 'limits')) data.limits = next.limits;
      if (Object.prototype.hasOwnProperty.call(next, 'hosts')) data.hosts = next.hosts;
      render();
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      if (timer !== null) (options.clearInterval || clearInterval)(timer);
      container.replaceChildren();
    },
  };
}

module.exports = {
  DEFAULT_STALE_AFTER_MS,
  DEFAULT_STALE_REFRESH_MS,
  formatPercent,
  machineCpuPercent,
  mountLimitsView,
  renderLimitsView,
  usagePercent,
};
