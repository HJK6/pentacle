// Walk: spawn_new_chat_send_sequence — the operator's "create a new chat" path:
// drive the REAL new-session wizard (#btn-new -> pick machine -> profile),
// which spawns + attaches + opens chat, then send a turn and assert the reply.
const { awaitChatReady, sendOneTurn } = require('../lib/flows');
const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: [], providers: ['claude', 'codex'] };
async function run(ctx) {
  const provider = ctx.provider === 'codex' ? 'codex' : 'claude';
  ctx.assert('new-session button clicked', await ctx.click('#btn-new'));
  await ctx.waitFor(`document.getElementById('new-session-overlay').style.display !== 'none'`, { label: 'wizard open' });
  // Step 1: pick the local machine.
  ctx.assert('machine "local" picked', await ctx.click('.new-session-option[data-loc="local"]'));
  await ctx.waitFor(`!!document.querySelector('#spawn-provider')`, { label: 'profile step shown' });
  if (provider === 'claude') {
    ctx.assert('provider selected', await ctx.eval(`(() => {
      const select = document.querySelector('#spawn-provider');
      if (!select || ![...select.options].some((option) => option.value === 'claude')) return false;
      select.value = 'claude';
      select.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    })()`));
  }
  await ctx.screenshot('wizard-profile-step');
  // Step 2: submit the explicit profile — this calls newSession (spawn -> attach -> chat).
  const seq = ctx.beaconSeq();
  ctx.assert('profile submitted', await ctx.click('#new-session-spawn'));
  const spawned = await ctx.awaitBeacon(
    (b) => b.seq > seq && b.name === 'session:spawn' && b.data && b.data.kind === 'chat',
    { timeoutMs: 30000, label: 'session:spawn (chat) from wizard' },
  );
  const streamId = spawned.data.streamId;
  const slot = spawned.slot;
  ctx.assert('wizard spawned a chat', !!streamId && typeof slot === 'number', spawned.data);
  if (spawned.data.sessionName) ctx.trackSpawned(streamId, spawned.data.sessionName, 'local');
  await awaitChatReady(ctx, { slot, streamId });
  await ctx.screenshot('wizard-chat-open');
  await sendOneTurn(ctx, { slot, streamId, text: 'Please reply with exactly: NEW-CHAT-OK', label: 'send' });
}
module.exports = { SCENARIO_META, run };

