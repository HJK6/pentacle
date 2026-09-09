// Bounded smoke for the catalog-backed manual spawn wizard. The full chat
// sequence scenario covers attach/send; this keeps the release smoke to one
// profile selection and one real spawn.
const SCENARIO_META = { target_compat: ['hostb'], requires: [], providers: ['codex'] };

async function run(ctx) {
  ctx.assert('new-session button clicked', await ctx.click('#btn-new'));
  await ctx.waitFor(`document.getElementById('new-session-overlay').style.display !== 'none'`, { label: 'wizard open' });
  ctx.assert('machine selected', await ctx.click('.new-session-option[data-loc="local"]'));
  await ctx.waitFor(`!!document.querySelector('#spawn-provider') && !!document.querySelector('#spawn-model') && !!document.querySelector('#spawn-effort')`, { label: 'four-control profile loaded' });
  ctx.assert('factory default is Codex Sol high', await ctx.eval(`(() => document.querySelector('#spawn-provider').value === 'codex' && document.querySelector('#spawn-model').value === 'gpt-5.6-sol' && document.querySelector('#spawn-effort').value === 'high')()`));
  const seq = ctx.beaconSeq();
  ctx.assert('profile submitted', await ctx.click('#new-session-spawn'));
  const spawned = await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'session:spawn' && b.data?.kind === 'chat', { timeoutMs: 30000, label: 'profile spawn' });
  ctx.assert('profile spawned one chat', !!spawned.data?.streamId, spawned.data);
  if (spawned.data?.sessionName) ctx.trackSpawned(spawned.data.streamId, spawned.data.sessionName, 'local');
}

module.exports = { SCENARIO_META, run };

