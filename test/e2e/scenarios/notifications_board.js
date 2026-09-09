// Walk: notifications_board — verifies the Notifications dashboard renders from
// the shared shared-dashboards definition (cards + action buttons + toggle),
// that interactive mode wires resolve actions (ctx.actions), and that display
// mode hides the interactiveOnly action buttons + show-resolved toggle.
const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: [], providers: ['claude'] };

const FIXTURE = JSON.stringify({
  notifications: [
    { notification_id: 'n1', created_at: '2026-05-26T12:00:00Z', producer: 'sample-importer', severity: 'warning', title: 'Import paused', body: 'No new items in 30m', state: 'open', actions: [{ kind: 'ack' }] },
    { notification_id: 'n2', created_at: '2026-05-26T11:00:00Z', producer: 'orchestrator', severity: 'critical', title: 'Approve spawn?', state: 'open', actions: [{ kind: 'yes_no' }] },
    { notification_id: 'n3', created_at: '2026-05-26T10:00:00Z', producer: 'x', severity: 'info', title: 'Old one', state: 'resolved', actions: [{ kind: 'ack' }] },
  ],
});

const PROBE = `(() => {
  const TD = window.PublicDashDashboards;
  if (!TD || !TD.boards.notifications) return JSON.stringify({ error: 'no shared notifications board' });
  const FIX = ${FIXTURE};
  function run(mode) {
    const c = document.createElement('div'); document.body.appendChild(c);
    const resolved = [];
    const refs = TD.mountBoard(c, TD.boards.notifications, FIX, {
      mode,
      actions: { resolve: (id, a, o) => { resolved.push([id, a, o]); return Promise.resolve({ ok: true }); }, refreshNow() {} },
    });
    const out = {
      rows: c.querySelectorAll('.notification-row').length,
      actionButtons: c.querySelectorAll('button[data-action]').length,
      toggle: !!c.querySelector('[data-role="show-resolved"]'),
      terminalDeEmphasized: (() => { const r = c.querySelector('.notification-row[data-state="resolved"]'); return r ? r.style.opacity : null; })(),
    };
    if (mode === 'interactive') {
      const ack = c.querySelector('button[data-action="ack"]');
      if (ack) ack.click();
      out.resolvedCall = resolved.length ? resolved[0][1] : null;
    }
    TD.unmountBoard(refs, TD.boards.notifications); c.remove();
    return out;
  }
  return JSON.stringify({ interactive: run('interactive'), display: run('display') });
})()`;

async function run(ctx) {
  ctx.assert('dashboards view', await ctx.click('#view-dashboards'));
  await ctx.awaitBeacon((b) => b.name === 'view:switch' && b.data && b.data.view === 'dashboards', { label: 'view:switch dashboards' });

  await ctx.eval(`window.selectDashboard('shared-demo')`);
  const seq = ctx.beaconSeq();
  await ctx.eval(`window.selectDashboard('notifications')`);
  await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'dashboard:mount' && b.data && b.data.id === 'notifications', { timeoutMs: 10000, label: 'dashboard:mount notifications' });

  ctx.assert('notifications renders shared .notifications-shell', await ctx.eval(`!!document.querySelector('.notifications-shell')`));
  ctx.assert('notification list region present', await ctx.eval(`!!document.querySelector('[data-role="notification-list"]')`));
  await ctx.screenshot('notifications-live-mount');

  const r = JSON.parse(await ctx.eval(PROBE));
  ctx.assert('interactive: 3 notification rows', r.interactive.rows === 3);
  ctx.assert('interactive: action buttons present (ack + yes/no = 3)', r.interactive.actionButtons === 3);
  ctx.assert('interactive: show-resolved toggle present', r.interactive.toggle === true);
  ctx.assert('interactive: terminal row de-emphasized', r.interactive.terminalDeEmphasized === '0.55');
  ctx.assert('interactive: ack click fires resolve action', r.interactive.resolvedCall === 'ack');
  ctx.assert('display: action buttons hidden (interactiveOnly)', r.display.actionButtons === 0);
  ctx.assert('display: show-resolved toggle hidden', r.display.toggle === false);
  ctx.assert('display: cards still rendered', r.display.rows === 3);
  await ctx.screenshot('notifications-final');
}

module.exports = { SCENARIO_META, run };
