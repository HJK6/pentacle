// Walk: worked_for_toggle
//
// Verifies the desktop "Show turn duration" setting is default-off behavior at
// the render layer: completed turns still settle, but duration rows/annotations
// are hidden until the operator enables the live settings toggle.
const { openMockSession, awaitRenderedSeqs } = require('../lib/mock_chat_scenarios');

const STREAM_ID = 'mock-host:worked-for-toggle';
const SESSION_NAME = 'worked-for-toggle';

const SCENARIO_META = {
  target_compat: ['hostc'],
  requires: [],
  providers: ['claude', 'codex'],
  scripted_daemon_fixture: 'worked_for_toggle',
};

const SWITCH = `.settings-row[data-flag="showTurnDuration"] .settings-switch`;
const checked = (sel) => `document.querySelector(${JSON.stringify(sel)})?.getAttribute('aria-checked')`;

async function setShowTurnDuration(ctx, desired) {
  let openedHere = false;
  const overlayVisible = await ctx.eval(`document.getElementById('settings-overlay').style.display !== 'none'`);
  if (!overlayVisible) {
    const seq = ctx.beaconSeq();
    ctx.assert('settings gear clicked', await ctx.click('#settings-btn'));
    await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'settings:open', { label: 'settings:open beacon' });
    await ctx.waitFor(`document.getElementById('settings-overlay').style.display !== 'none'`, { label: 'settings panel visible' });
    openedHere = true;
  }

  const before = await ctx.eval(checked(SWITCH));
  ctx.assert('showTurnDuration switch is present', before === 'true' || before === 'false', { before });
  if ((before === 'true') !== desired) {
    const seq = ctx.beaconSeq();
    ctx.assert('showTurnDuration toggle clicked', await ctx.click(SWITCH));
    await ctx.awaitBeacon(
      (b) => b.seq > seq && b.name === 'settings:toggle' && b.data && b.data.key === 'showTurnDuration',
      { label: 'showTurnDuration settings:toggle beacon' },
    );
    await ctx.waitFor(`${checked(SWITCH)} === ${JSON.stringify(desired ? 'true' : 'false')}`, {
      label: `showTurnDuration switch ${desired ? 'on' : 'off'}`,
    });
  }

  if (openedHere) {
    ctx.assert('settings closed', await ctx.click('#settings-close'));
    await ctx.waitFor(`document.getElementById('settings-overlay').style.display === 'none'`, { label: 'settings panel closed' });
  }
}

function durationProbe(cell) {
  return `(() => {
    const root = document.querySelector(${JSON.stringify(cell)});
    if (!root) return { missing: true };
    const activityRows = [...root.querySelectorAll('.slot-chat-activity')].map((node) => node.textContent || '');
    return {
      divider: root.querySelectorAll('.slot-chat-terminal-divider').length,
      turnSummary: activityRows.filter((text) => /Turn complete/i.test(text)).length,
      timing: root.querySelectorAll('.slot-chat-annotation.is-timing').length,
      sendEnabled: !!root.querySelector('.slot-chat-compose-send') && !root.querySelector('.slot-chat-compose-send').disabled,
    };
  })()`;
}

async function run(ctx) {
  const original = await ctx.eval(`${checked(SWITCH)} || 'missing'`);
  await setShowTurnDuration(ctx, false);

  const opened = await openMockSession(ctx, { streamId: STREAM_ID, sessionName: SESSION_NAME });
  const { slot, streamId } = opened;
  const cell = `#cell-${slot}`;

  try {
    await awaitRenderedSeqs(ctx, [51], { streamId, afterSeq: opened.beforeRender });
    await ctx.waitFor(`(() => { const b = document.querySelector('${cell} .slot-chat-compose-send'); return b && !b.disabled; })()`, {
      timeoutMs: 15000,
      label: 'composer re-enabled with showTurnDuration off',
    });

    const off = await ctx.eval(durationProbe(cell));
    ctx.assert('duration visuals hidden with showTurnDuration off',
      off.divider === 0 && off.turnSummary === 0 && off.timing === 0 && off.sendEnabled === true,
      off);
    await ctx.screenshot('worked-for-toggle-off');

    const beforeDurationOn = ctx.beaconSeq();
    await setShowTurnDuration(ctx, true);
    await awaitRenderedSeqs(ctx, [52, 53, 54], { streamId, afterSeq: beforeDurationOn });
    await ctx.waitFor(`(() => {
      const p = ${durationProbe(cell)};
      return p.divider > 0 && p.turnSummary > 0 && p.timing > 0 && p.sendEnabled === true ? p : false;
    })()`, { timeoutMs: 10000, label: 'duration visuals render after enabling setting' });
    const on = await ctx.eval(durationProbe(cell));
    ctx.assert('duration visuals visible with showTurnDuration on and composer still usable',
      on.divider > 0 && on.turnSummary > 0 && on.timing > 0 && on.sendEnabled === true,
      on);
    await ctx.screenshot('worked-for-toggle-on');
  } finally {
    if (original === 'true' || original === 'false') {
      await setShowTurnDuration(ctx, original === 'true');
    }
  }
}

module.exports = { SCENARIO_META, run };

