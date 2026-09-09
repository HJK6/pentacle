// Walk: specs_board — verifies the specs dashboard renders from the shared
// shared-dashboards definition (kanban + Specs/Epics toggle) and that the
// toggle switches to the epics view. The harness opens
// the board and telemetry + DOM assertions confirm it.
const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: [], providers: ['claude'] };
async function run(ctx) {
  ctx.assert('dashboards view', await ctx.click('#view-dashboards'));
  await ctx.awaitBeacon((b) => b.name === 'view:switch' && b.data && b.data.view === 'dashboards', { label: 'view:switch dashboards' });

  const seq = ctx.beaconSeq();
  await ctx.eval(`window.selectDashboard('specs')`);
  await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'dashboard:mount' && b.data && b.data.id === 'specs', { timeoutMs: 10000, label: 'dashboard:mount specs' });

  // Shared-board markers prove the board rendered through the shared layer.
  ctx.assert('specs renders shared .specs-shell', await ctx.eval(`!!document.querySelector('.specs-shell')`));
  ctx.assert('Specs/Epics toggle present', await ctx.eval(`!!document.querySelector('.specs-toggle button[data-view="epics"]')`));
  ctx.assert('kanban present in specs view', await ctx.eval(`!!document.querySelector('.specs-shell .kanban-row')`));
  await ctx.screenshot('specs-kanban');

  // Toggle to epics and confirm the view swaps in place.
  await ctx.eval(`document.querySelector('.specs-toggle button[data-view="epics"]').click()`);
  await ctx.waitFor(`!!document.querySelector('.specs-shell .epics-grid')`, { label: 'epics view' });
  ctx.assert('epics view renders on toggle', true);
  await ctx.screenshot('specs-epics');
}
module.exports = { SCENARIO_META, run };
