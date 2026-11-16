// Walk: toggle_terminal_chat — per-slot Chat<->Terminal toggle both directions.
const { openExistingAgentIntoChat } = require('../lib/flows');
const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: [], providers: ['claude'] };
async function run(ctx) {
  const opened = await openExistingAgentIntoChat(ctx); // ends in chat view
  if (opened.skip) return opened;
  const { slot } = opened;
  let seq = ctx.beaconSeq();
  ctx.assert('toggle->terminal clicked', await ctx.click(`.cell-view-toggle[data-slot="${slot}"][data-mode="terminal"]`));
  await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'slot:viewmode' && b.slot === slot && b.data.mode === 'terminal', { label: 'viewmode terminal' });
  await ctx.waitFor(`state.slotViewModes[${slot}] === 'terminal'`, { label: 'terminal mode' });
  ctx.assert('switched to terminal', true);
  await ctx.screenshot('terminal-view');
  seq = ctx.beaconSeq();
  ctx.assert('toggle->chat clicked', await ctx.click(`.cell-view-toggle[data-slot="${slot}"][data-mode="chat"]`));
  await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'slot:viewmode' && b.slot === slot && b.data.mode === 'chat', { label: 'viewmode chat' });
  await ctx.waitFor(`state.slotViewModes[${slot}] === 'chat'`, { label: 'chat mode' });
  await ctx.waitFor(`!!document.querySelector('#cell-${slot} [class*=slot-chat]')`, { label: 'chat transcript present' });
  ctx.assert('switched back to chat (transcript renders)', true);
}
module.exports = { SCENARIO_META, run };

