// Walk: reconnect_survival — a send in flight across a daemon reconnect still
// reconciles, and the send path keeps working after the reconnect.
const { spawnThrowawayChat, sendOneTurn } = require('../lib/flows');
const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: [], providers: ['claude'] };
async function run(ctx) {
  const { slot, streamId } = await spawnThrowawayChat(ctx, 'claude');
  // Slots default to Terminal view: a compose-send only dispatches from Chat
  // view, so select it first.
  ctx.assert('slot in chat view', await ctx.click(`.cell-view-toggle[data-slot="${slot}"][data-mode="chat"]`));

  // 1) Start a send and capture the optimistic insert, THEN force a reconnect
  //    while it is in flight.
  const before = ctx.beaconSeq();
  await ctx.waitFor(`(() => { const b = document.querySelector('#cell-${slot} .slot-chat-compose-send'); return b && !b.disabled; })()`, { label: 'composer enabled' });
  await ctx.type(`#cell-${slot} .slot-chat-compose-input`, 'Please reply with exactly: RECONNECT-OK');
  ctx.assert('send clicked', await ctx.click(`#cell-${slot} .slot-chat-compose-send`));
  await ctx.awaitBeacon((b) => b.seq > before && b.name === 'chat.compose.optimistic_insert', { timeoutMs: 10000, label: 'optimistic insert (in flight)' });

  const connectedBefore = await ctx.eval(`state.chatStream.connected === true`);
  ctx.assert('connected before reconnect', connectedBefore);
  const fr = await ctx.eval(`(async () => { return await window.PentacleHarnessActions.forceReconnect(); })()`);
  ctx.assert('forceReconnect invoked', fr && fr.ok !== false, fr);
  // The drop may be brief; observe it if we can (soft), then require recovery.
  ctx.softAssert('ws connection dropped', await ctx.waitFor(`state.chatStream.connected === false`, { timeoutMs: 8000, label: 'connected=false' }).then(() => true).catch(() => false));
  await ctx.waitFor(`state.chatStream.connected === true`, { timeoutMs: 30000, label: 'reconnected (connected=true)' });
  ctx.assert('reconnected', true);

  // 2) The in-flight send SURVIVES: it reconciles (not failed) + reply renders.
  const reconciled = await ctx.awaitBeacon(
    (b) => b.seq > before && (b.name === 'chat.compose.optimistic_reconciled' || b.name === 'chat.compose.optimistic_failed'),
    { timeoutMs: 60000, label: 'in-flight send reconcile or fail' },
  );
  ctx.assert('in-flight send survived reconnect (reconciled)', reconciled.name === 'chat.compose.optimistic_reconciled', { got: reconciled.name });
  await ctx.screenshot('after-reconnect');

  // 3) The send path keeps working AFTER a reconnect: a fresh turn round-trips.
  await ctx.waitFor(`window.PentacleChatStore.getTurnPhase(${JSON.stringify(streamId)}) === 'idle'`, { timeoutMs: 90000, label: 'turn settled' });
  await sendOneTurn(ctx, { slot, streamId, text: 'Please reply with exactly: POST-RECONNECT-OK', label: 'post-reconnect send' });
}
module.exports = { SCENARIO_META, run };
