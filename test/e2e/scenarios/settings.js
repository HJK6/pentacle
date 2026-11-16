// Walk: settings — open the titlebar Settings panel, flip a live feature flag,
// assert it persists + applies live, then restore. Replaces the old
// theme_toggle walk (Pentacle is dark-only; the gear opens Settings now).
const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: [], providers: ['claude'] };

const SWITCH = `.settings-row[data-flag="dashboards"] .settings-switch`;
const checked = (sel) => `document.querySelector(${JSON.stringify(sel)})?.getAttribute('aria-checked')`;

async function run(ctx) {
  let seq = ctx.beaconSeq();

  // Open the panel.
  ctx.assert('settings gear clicked', await ctx.click('#settings-btn'));
  await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'settings:open', { label: 'settings:open beacon' });
  await ctx.waitFor(`document.getElementById('settings-overlay').style.display !== 'none'`, { label: 'settings panel visible' });
  ctx.assert('settings panel opened', true);
  await ctx.screenshot('settings-open');

  // Flip the Dashboards flag (a live-apply flag) and assert the switch + beacon.
  const before = await ctx.eval(checked(SWITCH));
  seq = ctx.beaconSeq();
  ctx.assert('dashboards toggle clicked', await ctx.click(SWITCH));
  const b = await ctx.awaitBeacon((x) => x.seq > seq && x.name === 'settings:toggle' && x.data && x.data.key === 'dashboards', { label: 'settings:toggle beacon' });
  const after = await ctx.waitFor(`${checked(SWITCH)} !== ${JSON.stringify(before)} ? ${checked(SWITCH)} : false`, { label: 'switch flipped' });
  ctx.assert('dashboards flag flipped (DOM + telemetry)', after && after !== before, { before, after, beacon: b.data });

  // Live apply: when off, the Chats/Dashboards view switcher is hidden.
  const expectHidden = after === 'false';
  await ctx.waitFor(
    `(document.querySelector('.view-switcher')?.style.display === 'none') === ${expectHidden}`,
    { label: 'dashboards live applied' });
  ctx.assert('dashboards applied live', true);

  // Restore original state.
  seq = ctx.beaconSeq();
  await ctx.click(SWITCH);
  await ctx.waitFor(`${checked(SWITCH)} === ${JSON.stringify(before)}`, { label: 'switch restored' });
  ctx.assert('dashboards flag restored', true);

  // Close the panel.
  ctx.assert('done clicked', await ctx.click('#settings-close'));
  await ctx.waitFor(`document.getElementById('settings-overlay').style.display === 'none'`, { label: 'settings panel closed' });
  ctx.assert('settings panel closed', true);
}
module.exports = { SCENARIO_META, run };

