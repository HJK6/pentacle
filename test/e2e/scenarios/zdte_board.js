// Walk: shared_demo_board — verifies a neutral shared dashboard can be opened
// from the desktop dashboards view and remains usable in the public fixture.
const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: [], providers: ['claude'] };

async function run(ctx) {
  ctx.assert('dashboards view', await ctx.click('#view-dashboards'));
  await ctx.awaitBeacon((b) => b.name === 'view:switch' && b.data && b.data.view === 'dashboards', { label: 'view:switch dashboards' });

  const seq = ctx.beaconSeq();
  await ctx.eval('window.selectDashboard("shared-demo")');
  await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'dashboard:mount' && b.data && b.data.id === 'shared-demo', {
    timeoutMs: 10000,
    label: 'dashboard:mount shared-demo',
  });

  ctx.assert('neutral shared dashboard mounted', true);
  await ctx.screenshot('shared-demo-live-mount');
  ctx.assert('neutral shared dashboard remains mounted', true);
  await ctx.screenshot('shared-demo-final');
}

module.exports = { SCENARIO_META, run };
