// ── Notifications desktop adapter ────────────────────────────
// The shared board supplies the cards; this adapter renders its read-only
// display mode, keeps the desktop persistent-feed poll, and
//   • keeps the `notification` broadcast-frame subscription (platform plumbing
//     the shared board must not own — the Pi uses SSE instead).
//
// Mirrors specs.js / pi-control.js so the jsdom test can require() and drive
// mount/update/pollFn/unmount directly.

(function(root) {
'use strict';

const POLL_INTERVAL_MS = 20000;
const IDLE_POLL_INTERVAL_MS = 60000;
const DEFAULT_NOTIFICATION_LIMIT = 100;
const TERMINAL_STATES = ['acked', 'answered', 'spawned', 'resolved', 'expired', 'done', 'failed'];

function sharedBoard() {
  const w = (typeof window !== 'undefined') ? window : root;
  const TD = (w && w.TriforceDashboards) || null;
  return TD && TD.boards && TD.boards.notifications ? TD : null;
}

function firingHistory(notification) {
  const raw = notification && Array.isArray(notification.firing_history)
    ? notification.firing_history
    : [];
  return raw.filter((item) => typeof item === 'string' && item.trim()).slice();
}

function ensureFiringHistoryDetails(container, notifications) {
  if (!container || !Array.isArray(notifications)) return;
  for (const notification of notifications) {
    const id = notification && notification.notification_id;
    if (!id) continue;
    const history = firingHistory(notification);
    const count = Number(notification.firing_count || history.length || 0);
    if (history.length <= 1 && count <= 1) continue;
    const row = container.querySelector(`.notification-row[data-notification-id="${window.CSS && window.CSS.escape ? window.CSS.escape(String(id)) : String(id).replace(/"/g, '\\"')}"]`);
    if (!row || row.querySelector('[data-role="firing-history"]')) continue;
    const details = document.createElement('details');
    details.dataset.role = 'firing-history';
    details.style.cssText = 'margin-top:8px;font-size:11px;color:#9fb0a8;';
    const summary = document.createElement('summary');
    summary.textContent = `Fired ${count || history.length} times; latest ${history[history.length - 1] || notification.last_fired_at || notification.updated_at || ''}`;
    summary.style.cssText = 'cursor:pointer;';
    const list = document.createElement('ol');
    list.style.cssText = 'margin:6px 0 0 18px;padding:0;line-height:1.35;';
    for (const stamp of history.slice().reverse()) {
      const item = document.createElement('li');
      item.textContent = stamp;
      list.appendChild(item);
    }
    details.appendChild(summary);
    details.appendChild(list);
    row.appendChild(details);
  }
}

function ensureConsentSecurityActions(container, notifications) {
  if (!container || !Array.isArray(notifications)) return;
  for (const notification of notifications) {
    if (notification?.producer !== 'consent.security.v1' || !notification.offer_id) continue;
    const row=Array.from(container.querySelectorAll('.notification-row')).find(item=>item.dataset.notificationId===notification.notification_id);
    if (!row || row.querySelector('[data-role="consent-security-review"]')) continue;
    const button=document.createElement('button');button.dataset.role='consent-security-review';button.textContent='Review setup / host recovery';
    button.addEventListener('click',()=>window.showApprovalKeyOffer?.(notification.offer_id));row.appendChild(button);
  }
}

function mount(container) {
  const TD = sharedBoard();
  let refs;
  if (TD) {
    refs = TD.mountBoard(container, TD.boards.notifications, undefined, { mode: 'display' });
  } else {
    if (container) container.innerHTML = '<div style="padding:14px;color:#5e6d65;">Notifications unavailable — shared dashboard layer not loaded.</div>';
    refs = { __fallback: true };
  }
  // Subscribe to `notification` broadcast frames (desktop plumbing; the Pi uses
  // SSE). unmount() removes the entry.
  const listeners = (window.__pentacleNotificationsListeners = window.__pentacleNotificationsListeners || []);
  if (refs) refs.__pentacleNotificationContainer = container;
  refs.pushListener = () => { if (typeof window.refreshDashboardNow === 'function') window.refreshDashboardNow(); };
  listeners.push(refs.pushListener);
  return refs;
}

function update(refs, data) {
  const TD = sharedBoard();
  if (TD && refs && !refs.__fallback) {
    TD.updateBoard(refs, TD.boards.notifications, data);
    ensureFiringHistoryDetails(refs.__pentacleNotificationContainer || refs.container, data && data.notifications);
    ensureConsentSecurityActions(refs.__pentacleNotificationContainer || refs.container, data && data.notifications);
  }
}

function unmount(refs) {
  if (!refs) return;
  const listeners = window.__pentacleNotificationsListeners || [];
  const idx = listeners.indexOf(refs.pushListener);
  if (idx >= 0) listeners.splice(idx, 1);
  refs.pushListener = null;
  const TD = sharedBoard();
  if (TD && !refs.__fallback) TD.unmountBoard(refs, TD.boards.notifications);
}

async function pollFn(refs) {
  // Default view is a persistent feed. The shared board still exposes the
  // original showResolved boolean, but desktop relabels it as "Hide resolved".
  const states = (refs && refs.showResolved)
    ? ['open', 'running']
    : ['open', 'running', ...TERMINAL_STATES];
  const args = { states, limit: DEFAULT_NOTIFICATION_LIMIT };
  try {
    const reply = await window.cc.notificationList(args);
    if (!reply || reply.ok === false) {
      return { error: (reply && reply.error) || 'Unknown notification.list error' };
    }
    return reply;
  } catch (e) {
    return { error: (e && e.message) || String(e) };
  }
}

const dashboard = {
  id: 'notifications',
  name: 'Notifications',
  description: 'Read-only updates from agents and system services; respond through Agents chats.',
  color: '#f5b78a',
  mount, update, unmount, pollFn,
  pollInterval: POLL_INTERVAL_MS,
  idlePollInterval: IDLE_POLL_INTERVAL_MS,
  idleFn: (data) => !data || !Array.isArray(data.notifications) || data.notifications.length === 0,
};

if (root && root.DASHBOARDS) root.DASHBOARDS.push(dashboard);

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    dashboard,
    mount, update, unmount, pollFn,
    _test: { TERMINAL_STATES, DEFAULT_NOTIFICATION_LIMIT },
  };
}

})(typeof window !== 'undefined' ? window : null);
