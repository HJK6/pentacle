// Walk: ui_review_board — verifies the UI Review dashboard renders from the
// shared shared-dashboards definition (stats + filters + list + preview),
// that a fixture drives filtering/selection in interactive mode, and that
// display mode hides the (interactiveOnly) filter controls.
const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: [], providers: ['claude'] };

const FIXTURE = JSON.stringify({
  source: 'hub',
  generatedAt: '2026-05-26T12:00:00Z',
  artifacts: [
    { id: 'x1', artifactKey: 'x1', title: 'Welcome screen', repo: 'public-app', machine: 'hosta', tags: ['access'], url: 'about:blank', updatedAt: '2026-05-26T11:00:00Z' },
    { id: 'x2', artifactKey: 'x2', title: 'Preferences panel', repo: 'sample-library', machine: 'hostc', tags: ['preferences'], url: 'about:blank', updatedAt: '2026-05-26T10:00:00Z' },
  ],
});

const PROBE = `(() => {
  const TD = window.PublicDashDashboards;
  if (!TD || !TD.boards['ui-review']) return JSON.stringify({ error: 'no shared ui-review board' });
  const FIX = ${FIXTURE};
  function run(mode) {
    const c = document.createElement('div'); document.body.appendChild(c);
    const refs = TD.mountBoard(c, TD.boards['ui-review'], FIX, { mode });
    const out = {
      filters: c.querySelectorAll('[data-role="filters"] [data-filter]').length,
      artifacts: c.querySelectorAll('[data-artifact-id]').length,
      stats: c.querySelectorAll('[data-role="stats"] .ui-review-stat').length,
      preview: !!c.querySelector('[data-role="preview"]'),
    };
    // interactive: apply a query filter and confirm the list narrows.
    if (mode === 'interactive') {
      const q = c.querySelector('[data-filter="query"]');
      if (q) { q.value = 'login'; q.dispatchEvent(new Event('input')); }
      out.afterFilter = c.querySelectorAll('[data-artifact-id]').length;
    }
    TD.unmountBoard(refs, TD.boards['ui-review']); c.remove();
    return out;
  }
  return JSON.stringify({ interactive: run('interactive'), display: run('display') });
})()`;

async function run(ctx) {
  ctx.assert('dashboards view', await ctx.click('#view-dashboards'));
  await ctx.awaitBeacon((b) => b.name === 'view:switch' && b.data && b.data.view === 'dashboards', { label: 'view:switch dashboards' });

  await ctx.eval(`window.selectDashboard('shared-demo')`);
  const seq = ctx.beaconSeq();
  await ctx.eval(`window.selectDashboard('ui-review')`);
  await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'dashboard:mount' && b.data && b.data.id === 'ui-review', { timeoutMs: 10000, label: 'dashboard:mount ui-review' });

  ctx.assert('ui-review renders shared .ui-review-dashboard', await ctx.eval(`!!document.querySelector('.ui-review-dashboard')`));
  ctx.assert('stats + list + preview regions present', await ctx.eval(`!!document.querySelector('[data-role="stats"]') && !!document.querySelector('[data-role="list"]') && !!document.querySelector('[data-role="preview"]')`));
  await ctx.screenshot('ui-review-live-mount');

  const r = JSON.parse(await ctx.eval(PROBE));
  ctx.assert('interactive: 4 filter controls present', r.interactive.filters === 4);
  ctx.assert('interactive: fixture lists 2 artifacts', r.interactive.artifacts === 2);
  ctx.assert('interactive: query filter narrows to 1', r.interactive.afterFilter === 1);
  ctx.assert('display: filter controls hidden (interactiveOnly)', r.display.filters === 0);
  ctx.assert('display: list + preview still rendered', r.display.artifacts === 2 && r.display.preview === true);
  await ctx.screenshot('ui-review-final');
}

module.exports = { SCENARIO_META, run };

