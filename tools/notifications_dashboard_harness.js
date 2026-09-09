#!/usr/bin/env node
/**
 * Local renderer harness for the Notifications dashboard.
 *
 * The harness evaluates the dashboard scripts in a browser-like jsdom context,
 * mounts a synthetic record, verifies the public actions, and emits local test
 * telemetry. It never contacts a service or uses a live notification feed.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
const DASH = path.join(ROOT, 'renderer', 'dashboards');
const T0 = Date.now();
const telemetry = [];
const checks = [];
function log(event, extra = {}) {
  const rec = { t: ((Date.now() - T0) / 1000).toFixed(4), event, ...extra };
  telemetry.push(rec);
  console.log(`  [${rec.t}s] ${event}: ${JSON.stringify(extra)}`);
}
function check(name, ok, detail = '') {
  checks.push({ name, ok: !!ok, detail });
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
}

// Reproduce the renderer's CommonJS-compatible browser context.
const shim = `<script>
  var module = { exports: {} };
  var exports = module.exports;
  var require = function () { return new Proxy(function(){}, { get: function(){ return function(){}; }, apply: function(){ return {}; } }); };
</script>`;

const SCRIPTS = ['registry.js', 'specs.js', 'notifications.js'];
const scriptTags = shim + '\n' + SCRIPTS.map((f) => {
  const code = fs.readFileSync(path.join(DASH, f), 'utf8');
  return `<script>\n${code}\n</script>`;
}).join('\n');

const dom = new JSDOM(
  `<!doctype html><html><body><div id="dashboard-list"></div><div id="mount"></div>${scriptTags}</body></html>`,
  { runScripts: 'dangerously' },
);
const win = dom.window;

console.log('\n=== Notifications dashboard — local renderer harness (hostb) ===\n');

const ids = (win.DASHBOARDS || []).map((d) => d.id);
log('dashboards_registered', { ids });
const notif = (win.DASHBOARDS || []).find((d) => d.id === 'notifications');
check('window.DASHBOARDS exists (registry.js ran)', Array.isArray(win.DASHBOARDS));
check('control dashboard "specs" registered', ids.includes('specs'));
check('NOTIFICATIONS dashboard registered via browser <script> path', !!notif,
  notif ? `name="${notif.name}"` : 'MISSING from window.DASHBOARDS');

if (!notif) {
  finish();
}

check('notifications profile has mount/pollFn/update', notif &&
  typeof notif.mount === 'function' && typeof notif.pollFn === 'function' && typeof notif.update === 'function');

let refreshCalls = 0;
win.refreshDashboardNow = () => { refreshCalls += 1; };
const sample = {
  notification_id: 'n-fixture-1', producer: 'sample-agent.action_items', severity: 'warning',
  title: 'synthetic action requires review', body: 'fixture notification body', state: 'open',
  created_at: new Date().toISOString(),
  actions: [{ kind: 'ack' }, { kind: 'spawn_worker', provider: 'example', host: 'hosta', prompt: 'inspect fixture' }],
  resolution: null,
};
let resolveArgs = null;
win.cc = {
  notificationList: async () => ({ ok: true, notifications: [sample] }),
  notificationResolve: async (id, kind, opts) => { resolveArgs = { id, kind, opts }; return { ok: true, notification: { ...sample, state: 'acked' } }; },
};
const container = win.document.getElementById('mount');
const refs = notif.mount(container);

(async () => {
  const data = await notif.pollFn(refs);
  log('pollFn', { ok: data && data.ok !== false, count: (data.notifications || []).length });
  notif.update(refs, data);
  const html = container.innerHTML;
  check('mount+update renders the synthetic notification',
    html.includes('synthetic action requires review') && /warning/i.test(html));
  check('open record renders action buttons (ack + spawn_worker)',
    /data-action="ack"/.test(html) && /data-action="spawn_worker"/.test(html));

  const listeners = win.__pentacleNotificationsListeners || [];
  log('listeners_registered', { count: listeners.length });
  check('mount registered a live-update listener', listeners.length >= 1);
  if (listeners.length) {
    listeners[0]({ type: 'notification', notification: { ...sample, title: 'updated fixture' } });
    check('notification frame triggers refreshDashboardNow', refreshCalls >= 1, `refreshCalls=${refreshCalls}`);
  }

  const ackBtn = container.querySelector('[data-action="ack"]');
  if (ackBtn) {
    ackBtn.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await new Promise((r) => setTimeout(r, 20));
    check('clicking Acknowledge calls notificationResolve(id, "ack")',
      resolveArgs && resolveArgs.id === 'n-fixture-1' && resolveArgs.kind === 'ack',
      JSON.stringify(resolveArgs));
  }
  finish();
})();

function finish() {
  const passed = checks.filter((c) => c.ok).length;
  console.log(`\n=== RESULT: ${passed}/${checks.length} checks passed ===`);
  checks.filter((c) => !c.ok).forEach((c) => console.log(`  FAILED: ${c.name} ${c.detail}`));
  const out = path.join(require('os').tmpdir(), 'notif-dashboard-telemetry.jsonl');
  fs.writeFileSync(out, telemetry.map((r) => JSON.stringify(r)).join('\n'));
  console.log(`  telemetry: ${out}`);
  process.exit(passed === checks.length ? 0 : 1);
}
