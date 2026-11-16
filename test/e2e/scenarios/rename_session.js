// Walk: rename_session — spawn a throwaway, rename via the modal, assert new label.
const { spawnThrowawayChat } = require('../lib/flows');
const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: [], providers: ['claude'] };
async function run(ctx) {
  const { slot, sessionName } = await spawnThrowawayChat(ctx, 'claude');
  const newName = 'E2E Renamed ' + Date.now();
  ctx.assert('rename btn clicked', await ctx.click(`#header-${slot} .cell-edit`));
  await ctx.waitFor(`document.getElementById('modal-overlay').style.display !== 'none'`, { label: 'rename modal open' });
  await ctx.type('#modal-input', newName);
  const seq = ctx.beaconSeq();
  ctx.assert('confirm clicked', await ctx.click('#modal-confirm'));
  await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'session:rename' && b.data && b.data.newName === newName, { label: 'session:rename' });
  await ctx.waitFor(`!!document.querySelector('.session-item[data-display="${newName}"]')`, { timeoutMs: 20000, label: 'sidebar shows new name' });
  ctx.assert('session renamed (sidebar + telemetry)', true);
  await ctx.screenshot('renamed');
}
module.exports = { SCENARIO_META, run };

