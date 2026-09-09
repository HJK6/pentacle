// Walk: packaged_launch_smoke — launch the INSTALLED /Applications/Pentacle.app
// (electron-builder bundle + its own config resolution + launch path — the gap
// the dev-driven walks don't cover) and assert it actually opens a usable window
// + renders + connects. This catches the class of launch crashes (e.g. an
// incomplete config: `CONFIG.dark.bg` undefined) that produce "clicked the app,
// nothing happened". Run with: bin/pentacle-walk packaged_launch_smoke --packaged
const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: ['packaged_app'], providers: ['claude'] };
async function run(ctx) {
  // The runner's connectReady already proved: a page target exists (window came
  // up — not a main-process launch crash), the harness armed, and the daemon
  // connected. Now assert the UI actually RENDERED (the crash left a blank/no window).
  await ctx.waitFor(
    `!!document.querySelector('.titlebar') && !!document.body && document.body.innerText.trim().length > 0`,
    { timeoutMs: 20000, label: 'packaged app UI rendered (titlebar + body content)' },
  );
  const info = await ctx.eval(`({
    theme: document.documentElement.dataset.theme || 'dark',
    sidebarItems: document.querySelectorAll('.session-item').length,
    connected: (typeof state !== 'undefined' && state.chatStream) ? state.chatStream.connected : null,
    bodyLen: document.body.innerText.trim().length,
  })`);
  ctx.log('packaged launch: ' + JSON.stringify(info));
  ctx.assert('packaged app launched + rendered a usable window', info.bodyLen > 0, info);
  ctx.softAssert('packaged app connected to chat_streamd', info.connected === true, info);
  await ctx.screenshot('packaged-launch');
}
module.exports = { SCENARIO_META, run };

