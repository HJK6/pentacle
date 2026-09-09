// Walk: slot_maximize_minimize — open a session, maximize the slot, minimize back.
const { openExistingAgentIntoChat } = require('../lib/flows');
const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: [], providers: ['claude'] };
async function run(ctx) {
  const opened = await openExistingAgentIntoChat(ctx);
  if (opened.skip) return opened;
  const { slot } = opened;
  let seq = ctx.beaconSeq();
  ctx.assert('maximize clicked', await ctx.click(`#header-${slot} .cell-maximize`));
  await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'slot:maximize' && b.slot === slot, { label: 'slot:maximize' });
  await ctx.waitFor(`document.querySelector('.grid').classList.contains('maximized')`, { label: 'grid maximized' });
  ctx.assert('slot maximized (DOM + telemetry)', true);
  await ctx.screenshot('maximized');
  seq = ctx.beaconSeq();
  ctx.assert('minimize clicked', await ctx.click(`#header-${slot} .cell-maximize`));
  await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'slot:minimize', { label: 'slot:minimize' });
  await ctx.waitFor(`!document.querySelector('.grid').classList.contains('maximized')`, { label: 'grid restored' });
  ctx.assert('slot minimized', true);
}
module.exports = { SCENARIO_META, run };

