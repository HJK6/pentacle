// Walk: working_timer_daemon_elapsed
// public-ui-regression (B2)
//
// Proves B2 end-to-end: the chat-slot working timer is driven by the daemon's
// authoritative per-turn elapsed (working.state.elapsed_ms via
// PentacleChatStore.getWorkingElapsedMs), not a client clock seeded from a
// parsed label. Drives a real claude turn with a bash sleep to hold a multi-
// second working window, then asserts the DISPLAYED timer equals the daemon
// elapsed within tolerance (and that getWorkingElapsedMs is a real number — the
// legacy parsed-label clock would leave no daemon anchor).
const { spawnThrowawayChat } = require('../lib/flows');

const SCENARIO_META = { target_compat: ['hostc'], requires: ['claude'], providers: ['claude'] };

const SLEEP_PROMPT = [
  'Use the Bash tool to run EXACTLY this command and nothing else first:',
  'sleep 10 && echo TIMER-OK',
  'Do not reply until it finishes; then reply with exactly: TIMER-DONE.',
].join('\n');

async function run(ctx) {
  const { slot, streamId } = await spawnThrowawayChat(ctx, 'claude');
  const cell = `#cell-${slot}`;
  const sid = JSON.stringify(streamId);

  const before = ctx.beaconSeq();
  await ctx.waitFor(
    `(() => { const b = document.querySelector('${cell} .slot-chat-compose-send'); return b && !b.disabled; })()`,
    { timeoutMs: 15000, label: 'composer send enabled' },
  );
  await ctx.type(`${cell} .slot-chat-compose-input`, SLEEP_PROMPT);
  await ctx.click(`${cell} .slot-chat-compose-send`);
  await ctx.awaitBeacon((b) => b.seq > before && b.name === 'chat.compose.optimistic_insert', {
    timeoutMs: 10000, label: 'optimistic insert',
  });

  // Working timer appears while the turn runs.
  await ctx.waitFor(
    `!!document.querySelector('${cell} .slot-chat-status-badge.is-working .slot-chat-status-timer')`,
    { timeoutMs: 30000, label: 'working timer visible' },
  );

  // Sample the live timer + reducer working state every ~750ms across the turn
  // (ctx.eval awaits async IIFEs, same as spawnThrowaway). Captures the FULL
  // working window so the daemon-elapsed behavior is observable + asserted, not
  // raced. Each sample pairs the DISPLAYED timer with getWorkingElapsedMs and the
  // raw reducer elapsed_ms.
  const series = await ctx.eval(`(async () => {
    const sid = ${sid};
    const out = [];
    for (let i = 0; i < 24; i++) {
      const st = window.PentacleChatStore.getState();
      const ws = st.workingStates && st.workingStates[sid];
      const el = document.querySelector('${cell} .slot-chat-status-timer');
      const txt = el ? (el.textContent || '').trim() : null;
      const m = txt ? txt.match(/^(?:(\\d+)m )?(\\d+)s$/) : null;
      out.push({
        i,
        phase: window.PentacleChatStore.getTurnPhase(sid),
        daemonMs: window.PentacleChatStore.getWorkingElapsedMs(sid),
        wsElapsed: ws ? ws.elapsed_ms : null,
        timer: txt,
        displayedSec: m ? (m[1] ? Number(m[1]) * 60 : 0) + Number(m[2]) : null,
      });
      if (i > 2 && window.PentacleChatStore.getTurnPhase(sid) === 'idle') break;
      await new Promise((r) => setTimeout(r, 750));
    }
    return out;
  })()`, { timeoutMs: 30000 });
  ctx.log('B2 series: ' + JSON.stringify(series));

  // During the working window the daemon anchor must be present (a number) for at
  // least one sample — proving the timer is daemon-driven, not the legacy
  // parsed-label clock (which leaves no anchor → null throughout).
  const anchored = series.filter((s) => typeof s.daemonMs === 'number');
  ctx.assert('B2: getWorkingElapsedMs is a number during the turn (daemon-driven, not legacy clock)',
    anchored.length > 0, { samples: series.length, anchored: anchored.length });

  // At every daemon-anchored sample the DISPLAYED timer matches the daemon elapsed
  // within ~2s (the displayed value IS the daemon value, interpolated).
  const matched = anchored.filter((s) => s.displayedSec !== null && Math.abs(s.displayedSec - Math.round(s.daemonMs / 1000)) <= 2);
  ctx.assert('B2: displayed timer matches the daemon elapsed within ~2s', matched.length === anchored.length,
    { anchored: anchored.length, matched: matched.length, sample: anchored[anchored.length - 1] });

  // The daemon elapsed advances across the working window (live, not frozen).
  const maxMs = Math.max(...anchored.map((s) => s.daemonMs));
  const minMs = Math.min(...anchored.map((s) => s.daemonMs));
  ctx.assert('B2: daemon elapsed advances during the turn', maxMs > minMs, { minMs, maxMs });
  await ctx.screenshot('timer-vs-daemon');

  // After the turn settles, the timer is gone and the daemon anchor is cleared.
  await ctx.waitFor(`window.PentacleChatStore.getTurnPhase(${sid}) === 'idle'`, { timeoutMs: 90000, label: 'turn settled to idle' });
  const afterIdle = await ctx.waitFor(
    `(window.PentacleChatStore.getWorkingElapsedMs(${sid}) === null) ? true : false`,
    { timeoutMs: 20000, label: 'daemon anchor cleared at idle' },
  ).catch(() => false);
  ctx.assert('B2: daemon elapsed anchor cleared once idle (timer resets per turn)', afterIdle === true);
  await ctx.screenshot('idle-after-turn');
}

module.exports = { SCENARIO_META, run };

