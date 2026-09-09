// Walk: notifications_run_command — verifies the Notifications dashboard renders
// the run_command action button + the running/done/failed result states in the
// REAL Electron renderer (shared shared-dashboards), and that clicking the
// run_command button fires resolve(id, 'run_command') through ctx.actions.
// Companion to notifications_board.js; covers public-ui-regression.
const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: [], providers: ['claude'] };

const FIXTURE = JSON.stringify({
  notifications: [
    { notification_id: 'rc_open', created_at: '2026-05-29T12:00:00Z', producer: 'demo', severity: 'info',
      title: 'Run demo job', state: 'open',
      actions: [{ kind: 'run_command', label: 'Accept', command_id: 'demo_file_write', args: { out: '/tmp/x', text: 'hi' } }] },
    { notification_id: 'rc_running', created_at: '2026-05-29T11:59:00Z', producer: 'demo', severity: 'info',
      title: 'Running job', state: 'running',
      actions: [{ kind: 'run_command', label: 'Accept', command_id: 'demo_file_write', args: {} }] },
    { notification_id: 'rc_done', created_at: '2026-05-29T11:58:00Z', producer: 'demo', severity: 'info',
      title: 'Done job', state: 'done',
      actions: [{ kind: 'run_command', label: 'Accept', command_id: 'demo_file_write', args: {} }],
      resolution: { result: { command_id: 'demo_file_write', exit_code: 0, stdout_tail: 'wrote /tmp/x', stderr_tail: '', timed_out: false } } },
    { notification_id: 'rc_failed', created_at: '2026-05-29T11:57:00Z', producer: 'demo', severity: 'warning',
      title: 'Failed job', state: 'failed',
      actions: [{ kind: 'run_command', label: 'Accept', command_id: 'demo_file_write', args: {} }],
      resolution: { result: { command_id: 'demo_file_write', exit_code: 7, stdout_tail: '', stderr_tail: 'boom &<bad>', timed_out: false, spawned_stream_id: 'hosta:codex-hosta-9' } } },
  ],
});

const PROBE = `(() => {
  const TD = window.PublicDashDashboards;
  if (!TD || !TD.boards.notifications) return JSON.stringify({ error: 'no shared notifications board' });
  const FIX = ${FIXTURE};
  const c = document.createElement('div'); document.body.appendChild(c);
  const resolved = [];
  const refs = TD.mountBoard(c, TD.boards.notifications, FIX, {
    mode: 'interactive',
    actions: { resolve: (id, a, o) => { resolved.push([id, a, o]); return Promise.resolve({ ok: true }); }, refreshNow() {} },
  });
  const rowOf = (id) => c.querySelector('.notification-row[data-notification-id="' + id + '"]');
  const btnsIn = (id) => { const r = rowOf(id); return r ? r.querySelectorAll('button[data-action]').length : -1; };
  const runBtn = rowOf('rc_open') && rowOf('rc_open').querySelector('button[data-action="run_command"]');
  if (runBtn) runBtn.click();
  const html = c.innerHTML;
  const out = {
    openHasRunButton: !!runBtn,
    openRunLabel: runBtn ? runBtn.textContent.trim() : null,
    runningButtons: btnsIn('rc_running'),
    doneButtons: btnsIn('rc_done'),
    failedButtons: btnsIn('rc_failed'),
    resolvedAction: resolved.length ? resolved[0][1] : null,
    resolvedId: resolved.length ? resolved[0][0] : null,
    doneShowsStdout: html.includes('wrote /tmp/x'),
    failedShowsStderr: html.includes('boom'),
    failedShowsInvestigator: html.includes('hosta:codex-hosta-9'),
    noRawScript: !html.includes('<bad>'),       // stderr '<bad>' must be escaped
    escapedAmp: html.includes('boom &amp;') || html.includes('&amp;'),
  };
  TD.unmountBoard(refs, TD.boards.notifications); c.remove();
  return JSON.stringify(out);
})()`;

async function run(ctx) {
  ctx.assert('dashboards view', await ctx.click('#view-dashboards'));
  await ctx.awaitBeacon((b) => b.name === 'view:switch' && b.data && b.data.view === 'dashboards', { label: 'view:switch dashboards' });
  await ctx.eval(`window.selectDashboard('shared-demo')`);
  const seq = ctx.beaconSeq();
  await ctx.eval(`window.selectDashboard('notifications')`);
  await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'dashboard:mount' && b.data && b.data.id === 'notifications', { timeoutMs: 10000, label: 'dashboard:mount notifications' });
  ctx.assert('notifications shell present', await ctx.eval(`!!document.querySelector('.notifications-shell')`));

  const r = JSON.parse(await ctx.eval(PROBE));
  ctx.assert('open run_command row renders a button', r.openHasRunButton === true);
  ctx.assert('run_command button label = Accept', r.openRunLabel === 'Accept');
  ctx.assert('clicking run_command fires resolve action=run_command', r.resolvedAction === 'run_command');
  ctx.assert('resolve carries the right notification id', r.resolvedId === 'rc_open');
  ctx.assert('running row shows NO action buttons', r.runningButtons === 0);
  ctx.assert('done row shows NO action buttons (terminal)', r.doneButtons === 0);
  ctx.assert('failed row shows NO action buttons (terminal)', r.failedButtons === 0);
  ctx.assert('done row shows stdout tail', r.doneShowsStdout === true);
  ctx.assert('failed row shows stderr tail', r.failedShowsStderr === true);
  ctx.assert('failed row shows investigator stream id', r.failedShowsInvestigator === true);
  ctx.assert('result text is HTML-escaped (no raw <bad>)', r.noRawScript === true);
  await ctx.screenshot('notifications-run-command');
}

module.exports = { SCENARIO_META, run };

