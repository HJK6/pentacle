// Walk: switch_view — chats <-> dashboards view switch; assert panels + telemetry.
const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: [], providers: ['claude'] };
async function run(ctx) {
  let seq = ctx.beaconSeq();
  ctx.assert('dashboards clicked', await ctx.click('#view-dashboards'));
  await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'view:switch' && b.data && b.data.view === 'dashboards', { label: 'view:switch dashboards' });
  await ctx.waitFor(`document.getElementById('dashboard-content').style.display !== 'none'`, { label: 'dashboards visible' });
  ctx.assert('dashboards view shown', true);
  await ctx.screenshot('dashboards');
  seq = ctx.beaconSeq();
  ctx.assert('chats clicked', await ctx.click('#view-chats'));
  await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'view:switch' && b.data && b.data.view === 'chats', { label: 'view:switch chats' });
  await ctx.waitFor(`document.querySelector('.grid').style.display !== 'none'`, { label: 'grid visible' });
  ctx.assert('chats view restored', true);
}
module.exports = { SCENARIO_META, run };

