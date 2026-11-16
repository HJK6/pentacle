// Walk: queued_messages
// public-ui-regression
//
// Proves native queue end-to-end: a message sent while a turn is in flight is
// dispatched immediately to the provider CLI, never held in Pentacle's local
// queue, and reconciles as a real sent user bubble.
const { spawnThrowawayChat } = require('../lib/flows');

const SCENARIO_META = { target_compat: ['hostc'], requires: ['claude'], providers: ['claude'] };

const SLEEP_PROMPT = [
  'Use the Bash tool to run EXACTLY this command and nothing else first:',
  'sleep 10 && echo TURN1-OK',
  'Do not reply until it finishes; then reply with exactly: TURN1-DONE.',
].join('\n');
const NATIVE_QUEUE_TEXT = 'Please reply with exactly: OK-NATIVE-QUEUE';

async function run(ctx) {
  const { slot, streamId } = await spawnThrowawayChat(ctx, 'claude');
  const cell = `#cell-${slot}`;
  const sid = JSON.stringify(streamId);

  // Turn 1 — long-running (holds the turn "working").
  const before1 = ctx.beaconSeq();
  await ctx.waitFor(
    `(() => { const b = document.querySelector('${cell} .slot-chat-compose-send'); return b && !b.disabled; })()`,
    { timeoutMs: 15000, label: 'composer send enabled' },
  );
  await ctx.type(`${cell} .slot-chat-compose-input`, SLEEP_PROMPT);
  await ctx.click(`${cell} .slot-chat-compose-send`);
  await ctx.awaitBeacon((b) => b.seq > before1 && b.name === 'chat.compose.optimistic_insert', {
    timeoutMs: 10000, label: 'turn1 optimistic insert',
  });
  await ctx.waitFor(`window.PentacleChatStore.getTurnPhase(${sid}) !== 'idle'`, { timeoutMs: 30000, label: 'turn1 in flight' });

  // Composer is NOT disabled mid-turn — confirm we can send into native queue.
  const enabled = await ctx.eval(`(() => { const b = document.querySelector('${cell} .slot-chat-compose-send'); return !!(b && !b.disabled); })()`);
  ctx.assert('native queue: composer stays enabled while a turn is in flight', enabled === true);

  // Turn 2 — sent while turn 1 is working → must dispatch immediately, not hold.
  const before2 = ctx.beaconSeq();
  await ctx.type(`${cell} .slot-chat-compose-input`, NATIVE_QUEUE_TEXT);
  await ctx.click(`${cell} .slot-chat-compose-send`);
  const sendBeacon = await ctx.awaitBeacon(
    (b) => b.seq > before2 && b.name === 'chat.compose.optimistic_insert' && !(b.data && b.data.queued === true),
    { timeoutMs: 10000, label: 'native optimistic insert' },
  );
  ctx.assert('native queue: a send during a turn is inserted without local queued telemetry', !!sendBeacon, { data: sendBeacon && sendBeacon.data });

  // No queued affordance in the DOM, and turn 1 still owns the visible turn.
  await ctx.waitFor(
    `!!document.querySelector('${cell} .slot-chat-row.is-user.is-sending .slot-chat-send-status')`,
    { timeoutMs: 8000, label: 'sending affordance visible' },
  );
  await ctx.waitFor(
    `!document.querySelector('${cell} .slot-chat-row.is-user.is-queued')`,
    { timeoutMs: 8000, label: 'no queued affordance visible' },
  );
  ctx.assert('native queue: turn 1 still in flight after send (send did not disturb it)',
    await ctx.eval(`window.PentacleChatStore.getTurnPhase(${sid}) !== 'idle'`) === true);
  await ctx.screenshot('native-queue-while-working');

  // The native-queued message actually went out — its server echo reconciles it.
  await ctx.awaitBeacon(
    (b) => b.seq > before2 && b.name === 'chat.compose.optimistic_reconciled',
    { timeoutMs: 120000, label: 'native-queued message reconciled' },
  );
  const sentBubble = await ctx.waitFor(`(() => {
    const rows = document.querySelectorAll('${cell} .slot-chat-row.is-user .slot-chat-user-bubble');
    for (const r of rows) { if ((r.textContent || '').includes('OK-NATIVE-QUEUE')) return true; }
    return false;
  })()`, { timeoutMs: 20000, label: 'native-queued text rendered as a sent bubble' }).catch(() => false);
  ctx.assert('native queue: the message rendered as a real sent bubble', sentBubble === true);
  await ctx.screenshot('native-queue-sent');
}

module.exports = { SCENARIO_META, run };

