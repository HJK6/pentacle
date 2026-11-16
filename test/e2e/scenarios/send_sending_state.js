// Walk: send_sending_state
// public-ui-regression (B1)
//
// Proves B1 end-to-end in the real app: a sent message shows a "sending…"
// affordance (no fabricated timer) on the optimistic USER row until the daemon
// confirms it, then resolves to a normal bubble. Asserts from DOM + telemetry on
// a real turn. Throwaway session is tracked for runner teardown.
const { spawnThrowawayChat } = require('../lib/flows');

const SCENARIO_META = { target_compat: ['hostc'], requires: ['codex'], providers: ['codex'] };

async function run(ctx) {
  const { slot, streamId } = await spawnThrowawayChat(ctx, ctx.provider || 'codex');
  const cell = `#cell-${slot}`;

  const before = ctx.beaconSeq();
  await ctx.waitFor(
    `(() => { const b = document.querySelector('${cell} .slot-chat-compose-send'); return b && !b.disabled; })()`,
    { timeoutMs: 15000, label: 'composer send enabled' },
  );
  await ctx.type(`${cell} .slot-chat-compose-input`, 'Please reply with exactly: OK-SENDING');
  await ctx.click(`${cell} .slot-chat-compose-send`);
  await ctx.awaitBeacon((b) => b.seq > before && b.name === 'chat.compose.optimistic_insert', {
    timeoutMs: 10000,
    label: 'optimistic insert',
  });

  // The optimistic USER row shows a "sending…" affordance BEFORE the daemon echo
  // reconciles it (the daemon round-trip — pane paste + capture — takes long
  // enough that this is observable).
  const sawSending = await ctx.waitFor(
    `!!document.querySelector('${cell} .slot-chat-row.is-user.is-sending .slot-chat-send-status')`,
    { timeoutMs: 9000, label: 'sending affordance visible' },
  ).catch(() => false);
  ctx.assert('B1: optimistic row shows a sending affordance before confirm', sawSending === true);

  const label = await ctx.eval(
    `((document.querySelector('${cell} .slot-chat-send-status.is-sending') || {}).textContent || '').trim()`,
  );
  ctx.assert('B1: affordance reads "sending…" with NO fabricated timer (no digits)',
    /sending/i.test(label) && !/\d/.test(label), { label });
  await ctx.screenshot('sending-state');

  // Daemon confirms (server USER echo reconciles) → the sending affordance is
  // gone and the row is an ordinary sent bubble.
  const reconciled = await ctx.awaitBeacon(
    (b) => b.seq > before && (b.name === 'chat.compose.optimistic_reconciled' || b.name === 'chat.compose.optimistic_failed'),
    { timeoutMs: 45000, label: 'reconcile or fail' },
  );
  ctx.assert('B1: send reconciled (not failed)', reconciled.name === 'chat.compose.optimistic_reconciled', {
    got: reconciled.name, detail: reconciled.data,
  });
  await ctx.waitFor(
    `!document.querySelector('${cell} .slot-chat-row.is-user.is-sending')`,
    { timeoutMs: 20000, label: 'sending affordance resolved' },
  );
  ctx.assert('B1: sending state resolved to a normal bubble after confirm', true);
  // The user text still renders as a sent bubble (no send-status node remains on it).
  const userBubbles = await ctx.eval(`document.querySelectorAll('${cell} .slot-chat-row.is-user .slot-chat-user-bubble').length`);
  ctx.assert('B1: user bubble present after confirm', userBubbles >= 1, { userBubbles });
  await ctx.screenshot('resolved-bubble');
}

module.exports = { SCENARIO_META, run };

