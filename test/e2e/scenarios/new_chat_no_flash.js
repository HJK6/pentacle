// Walk: new_chat_no_flash
// public-ui-regression — Bug 1 (new-chat flash).
//
// Proves the reported rendering defect is gone: opening chat B into a slot that
// was just showing chat A must NEVER paint A's transcript for a frame.
//
// The flash is a SUB-FRAME transient, so polled DOM sampling (~250ms) would
// routinely miss it. We prove it DETERMINISTICALLY with the render-time
// `chat:slot_painted` beacon (emitted by renderSlotChat on every paint, incl.
// the cleared/empty path), not DOM sampling:
//
//   PRIMARY gate (negative window): over the window after binding B into the
//   slot, assert NO `chat:slot_painted` beacon for that slot painted a CONCRETE
//   stream other than the one it is bound to (paintedStreamId !== boundStreamId),
//   i.e. A's content never paints into the B-bound slot; AND assert the first
//   paint of the newly-bound B was preceded by a clear (clearedBeforePaint).
//
//   Secondary (corroborating): the rendered list's data-stream-id tag resolves
//   to B's stream, and before/after screenshots.
//
// The setup creates two real throwaway claude sessions so the streams are
// present, local, strong-resolvable, and cleanup-owned by the runner. The actual
// A->B check still exercises the real attachSession/renderSlotChat path and the
// same deterministic negative beacon gate.
const { spawnThrowawayChat } = require('../lib/flows');

const SCENARIO_META = {
  target_compat: ['hostc', 'hosta', 'hostb'],
  requires: [],
  // Provider-agnostic read path; runs once (claude as driver) in the matrix.
  providers: ['claude', 'codex'],
};

async function run(ctx) {
  const spawnA = await spawnThrowawayChat(ctx, 'claude');
  const spawnB = await spawnThrowawayChat(ctx, 'claude');
  const A = {
    streamId: spawnA.streamId,
    sessionName: spawnA.sessionName,
    host: 'hostc',
    provider: 'claude',
    domHost: spawnA.hostId || 'local',
  };
  const B = {
    streamId: spawnB.streamId,
    sessionName: spawnB.sessionName,
    host: 'hostc',
    provider: 'claude',
    domHost: spawnB.hostId || 'local',
  };
  ctx.assert('setup spawned two distinct throwaway streams', A.streamId && B.streamId && A.streamId !== B.streamId, { A, B });
  ctx.log(`A=${A.sessionName} (${A.streamId})  B=${B.sessionName} (${B.streamId})`);

  // 2) Bind chat A into its slot via the same shared path used by the
  // sidebar click (assignToSlot -> attachSession). Throwaway creation already
  // proved both sessions exist and are cleanup-owned by the runner.
  let slot = spawnA.slot;
  await ctx.eval(`(async () => {
    await attachSession(${slot}, ${JSON.stringify(A.sessionName)}, ${JSON.stringify(A.sessionName)}, ${JSON.stringify(A.domHost)});
    return true;
  })()`);
  slot = await ctx.waitFor(
    `(() => { const i = state.slots.findIndex(s => s && s.name === ${JSON.stringify(A.sessionName)}); return i >= 0 ? i : false; })()`,
    { timeoutMs: 20000, label: 'A attached to a slot' },
  );
  ctx.assert('A attached to a slot', typeof slot === 'number' && slot >= 0, { slot });
  await ctx.waitFor(`document.querySelector('#cell-${slot}').classList.contains('occupied')`, {
    timeoutMs: 10000,
    label: 'cell occupied (A)',
  });

  // 3) Toggle the slot to Chat view and wait for A to actually PAINT (beacon).
  const beforeA = ctx.beaconSeq();
  ctx.assert('chat view toggle clicked (A)', await ctx.click(`.cell-view-toggle[data-slot="${slot}"][data-mode="chat"]`), { slot });
  const aPaint = await ctx.awaitBeacon(
    (b) => b.seq > beforeA && b.name === 'chat:slot_painted' && b.slot === slot
      && b.data && b.data.paintedStreamId === A.streamId,
    { timeoutMs: 20000, label: 'chat:slot_painted for A (concrete)' },
  );
  // The picks must STRONG-resolve (host+id), so boundStreamId is authoritative.
  // If A only resolved via the id-only fallback, the gate's boundStreamId would
  // be unreliable for these picks — skip rather than assert on a weak signal.
  if (aPaint.data.boundStreamId !== A.streamId) {
    return { skip: `A did not strong-resolve (boundStreamId=${aPaint.data.boundStreamId}); flash gate needs host+id sessions` };
  }
  ctx.assert('A painted its own stream (bound==painted)', aPaint.data.boundStreamId === A.streamId, aPaint.data);
  await ctx.screenshot('A-open');

  // 4+5) REBIND THE SAME SLOT to B — the real operator action of replacing a
  //    slot's chat. We call the shared attachSession(slot, ...) directly
  //    (exactly what assignToSlot -> the sidebar click invokes; assignToSlot
  //    would otherwise pick a DIFFERENT empty slot, and the maximized-slot UI
  //    path reparents the list out of #cell-${slot}). This exercises the real
  //    rebind, incl. attachSession -> detachSlot, which MUST preserve the
  //    previous binding for the leak guard. Open the negative window IMMEDIATELY
  //    before the rebind so it spans the entire A->B transition.
  const bHostId = B.domHost || await ctx.eval(`(() => {
    if (typeof _streamHostToHostId === 'function') return _streamHostToHostId(${JSON.stringify(B.host)}) || null;
    return null;
  })()`) || B.host || 'local';
  const fromSeq = ctx.beaconSeq();
  await ctx.eval(`(async () => {
    await attachSession(${slot}, ${JSON.stringify(B.sessionName)}, ${JSON.stringify(B.sessionName)}, ${JSON.stringify(bHostId)});
    return true;
  })()`);
  await ctx.waitFor(
    `(() => { const s = state.slots[${slot}]; return s && s.name === ${JSON.stringify(B.sessionName)}; })()`,
    { timeoutMs: 20000, label: 'slot rebound to B' },
  );
  // attachSession resets the slot to terminal view — toggle back to chat to paint B.
  ctx.assert('chat view toggle clicked (B)', await ctx.click(`.cell-view-toggle[data-slot="${slot}"][data-mode="chat"]`), { slot });

  // 6) Wait for B to PAINT its own stream.
  const bPaint = await ctx.awaitBeacon(
    (b) => b.seq > fromSeq && b.name === 'chat:slot_painted' && b.slot === slot
      && b.data && b.data.paintedStreamId === B.streamId,
    { timeoutMs: 20000, label: 'chat:slot_painted for B (concrete)' },
  );

  // 7) PRIMARY GATE — negative window. Over the whole A->B transition, NO paint
  //    for this slot may write a CONCRETE stream other than the one it is bound
  //    to. (paintedStreamId null/'' = the cleared/empty path, which is allowed.)
  await ctx.assertNoBeacon(
    (b) => b.name === 'chat:slot_painted' && b.slot === slot
      && b.data && b.data.paintedStreamId && b.data.paintedStreamId !== b.data.boundStreamId,
    { fromSeq, windowMs: 1500, label: 'slot painted a stream other than the one it is bound to (flash)' },
  );
  ctx.assert('no cross-stream paint during A->B transition (negative window)', true);

  // 8) The first paint of the newly-bound B was preceded by a clear.
  const slotPaintsAfter = ctx.beaconsSince(fromSeq)
    .filter((b) => b.name === 'chat:slot_painted' && b.slot === slot);
  ctx.assert('at least one slot paint after rebind', slotPaintsAfter.length > 0, { count: slotPaintsAfter.length });
  ctx.assert('first paint after rebind was preceded by a clear (clearedBeforePaint)',
    slotPaintsAfter[0].data && slotPaintsAfter[0].data.clearedBeforePaint === true, slotPaintsAfter[0].data);
  // And the concrete B paint itself never carried A's stream.
  ctx.assert('B paint bound==painted', bPaint.data.boundStreamId === B.streamId, bPaint.data);

  // Note: this happy-path open did not necessarily reach the id-only fallback
  // (when B's host+id stream is snapshot-resident, leaked=0). Phase 2 below
  // forces that window deterministically and HARD-asserts the leak suppression.
  const happyPathLeaks = slotPaintsAfter.filter((b) => b.data && b.data.leaked === true).length;
  ctx.log(`happy-path leaked-beacon count (informational): ${happyPathLeaks}`);

  // 9) Secondary (corroborating, NOT the gate) DOM check: the rendered list is
  //    tagged with B's stream, never A's. The beacon assertions above are the
  //    deterministic gate; this DOM read is a soft corroboration (a remote
  //    session's transcript can lag a poll window post-maximize, so a strict
  //    waitFor here would be flaky). Poll briefly, then softAssert.
  await ctx.eval(`(async () => {
    const current = state.slots[${slot}];
    if (!current || current.name !== ${JSON.stringify(B.sessionName)}) {
      await attachSession(${slot}, ${JSON.stringify(B.sessionName)}, ${JSON.stringify(B.sessionName)}, ${JSON.stringify(bHostId)});
    }
    updateSlotViewMode(${slot}, 'chat');
    return true;
  })()`);
  let tag = null;
  for (let i = 0; i < 24; i += 1) {
    tag = await ctx.eval(`(() => { const el = document.querySelector('#cell-${slot} .slot-chat-list'); return el ? (el.dataset.streamId || '') : null; })()`);
    if (tag === B.streamId) break;
    await ctx.eval('new Promise(r => setTimeout(r, 250))');
  }
  ctx.softAssert('rendered list data-stream-id is B, never A (corroborating)', tag === B.streamId && tag !== A.streamId, { tag, A: A.streamId, B: B.streamId });

  await ctx.screenshot('B-open');

  // ───────────────────────────────────────────────────────────────────────
  // Phase 2 — DISCRIMINATING forced-window test (exercises the REAL
  // renderSlotChat leak-suppression branch, app.js ~1104-1130).
  //
  // The happy-path open above did not necessarily reach the id-only fallback:
  // both streams were snapshot-resident, so renderSlotChat strong-resolved B
  // immediately (leaked=0) and the suppression branch had no coverage. Here we
  // force the EXACT not-yet-present window the operator hits — a desktop
  // session bound with a shared/blank name whose own host+id stream is absent —
  // and HARD-assert the guard paints EMPTY (never the previous stream's stale
  // transcript). A pre-fix renderSlotChat (no guard) paints the old stream's
  // hero here, so this assertion DISCRIMINATES the fix. The only synthetic part
  // is the crafted slot BINDING (state.slots[slot]); the resolved store entry
  // (P, the stream the slot is currently showing) is real.
  //
  // We anchor on the LOCAL hostc session A (not the remote B) as the "previous"
  // painted stream P: A's detail loads reliably in the harness, so the forced
  // window is deterministic regardless of a remote session's transcript lag.
  const fnOk = await ctx.eval(`typeof renderSlotChat === 'function' && typeof state === 'object'`);
  ctx.assert('renderSlotChat reachable from page scope (forced-window harness)', fnOk === true);

  // These are FORCED renders (not user navigation), so pin the slot to chat mode
  // up front — renderSlotChat() returns early when the slot is not in chat view.
  await ctx.eval(`(() => { state.slotViewModes[${slot}] = 'chat'; return true; })()`);
  const P = A;                              // local, reliably-loading stream
  const pHostId = A.domHost || A.host || 'local';

  // Establish P as the slot's committed/painted stream (prevStreamId === P)
  // before forcing the window.
  await ctx.eval(`(() => {
    state.slots[${slot}] = { name: ${JSON.stringify(P.sessionName)}, displayName: ${JSON.stringify(P.sessionName)}, hostId: ${JSON.stringify(pHostId)} };
    renderSlotChat(${slot});
    return true;
  })()`);
  await ctx.waitFor(
    `(() => { const el = document.querySelector('#cell-${slot} .slot-chat-list'); return el && el.dataset.streamId === ${JSON.stringify(P.streamId)}; })()`,
    { timeoutMs: 15000, label: 'slot committed to P (A)' },
  );

  // (2a) LEAK: bind a session that SHARES P's name on a BOGUS host, so the
  // strong host+id match is absent and findStreamSessionForDesktopSession falls
  // back to id-only -> resolves to P (== prevStreamId). Expect EMPTY + leaked.
  const beforeLeak = ctx.beaconSeq();
  await ctx.eval(`(() => {
    state.slots[${slot}] = { name: ${JSON.stringify(P.sessionName)}, displayName: ${JSON.stringify(P.sessionName)}, hostId: '__leak_probe_host__' };
    renderSlotChat(${slot});
    return true;
  })()`);
  const leakPaint = await ctx.awaitBeacon(
    (b) => b.seq > beforeLeak && b.name === 'chat:slot_painted' && b.slot === slot,
    { timeoutMs: 10000, label: 'forced-leak chat:slot_painted' },
  );
  // Content discriminator (valid pre- AND post-fix): post-fix paints the empty
  // placeholder (no session hero); a pre-fix renderSlotChat paints P's hero.
  const leakDom = await ctx.eval(`(() => {
    const el = document.querySelector('#cell-${slot} .slot-chat-list');
    return { streamId: el ? el.dataset.streamId : null,
             hasHero: !!(el && el.querySelector('.slot-chat-session-hero')),
             hasEmpty: !!(el && el.querySelector('.slot-chat-empty')) };
  })()`);
  ctx.assert('forced leak paints EMPTY, not the stale transcript (no hero, placeholder shown)',
    leakDom.hasHero === false && leakDom.hasEmpty === true, leakDom);
  ctx.assert('forced leak: list data-stream-id is empty', leakDom.streamId === '', leakDom);
  // Beacon discriminator (post-fix only): the guard flagged the id-only leak.
  ctx.assert('forced leak: beacon leaked===true', leakPaint.data.leaked === true, leakPaint.data);
  ctx.assert('forced leak: paintedStreamId null + boundStreamId null',
    leakPaint.data.paintedStreamId === null && leakPaint.data.boundStreamId === null, leakPaint.data);
  ctx.assert('forced leak: resolvedStreamId is the OLD stream P (id-only fallback)',
    leakPaint.data.resolvedStreamId === P.streamId, leakPaint.data);

  // Re-commit the slot to P (real binding) so prevStreamId === P for (2b).
  await ctx.eval(`(() => {
    state.slots[${slot}] = { name: ${JSON.stringify(P.sessionName)}, displayName: ${JSON.stringify(P.sessionName)}, hostId: ${JSON.stringify(pHostId)} };
    renderSlotChat(${slot});
    return true;
  })()`);
  await ctx.waitFor(
    `(() => { const el = document.querySelector('#cell-${slot} .slot-chat-list'); return el && el.dataset.streamId === ${JSON.stringify(P.streamId)}; })()`,
    { timeoutMs: 10000, label: 're-committed slot to P' },
  );

  // (2b) FALSE-POSITIVE GUARD (fix #1): bind a DISTINCT desktop session (a
  // different name) whose STRONG host+id match still resolves to P (its
  // displayName equals P's session_name) — i.e. resolvedStreamId === prevStreamId
  // === boundStreamId. The `resolvedStreamId !== boundStreamId` term makes this
  // NOT a leak, so the slot MUST still paint P (never blanked). Without that
  // term it would be wrongly suppressed and the slot permanently blanked.
  const beforeFp = ctx.beaconSeq();
  await ctx.eval(`(() => {
    state.slots[${slot}] = { name: ${JSON.stringify(P.sessionName + '__fp_distinct')}, displayName: ${JSON.stringify(P.sessionName)}, hostId: ${JSON.stringify(pHostId)} };
    renderSlotChat(${slot});
    return true;
  })()`);
  const fpPaint = await ctx.awaitBeacon(
    (b) => b.seq > beforeFp && b.name === 'chat:slot_painted' && b.slot === slot,
    { timeoutMs: 10000, label: 'false-positive chat:slot_painted' },
  );
  const fpDom = await ctx.eval(`(() => {
    const el = document.querySelector('#cell-${slot} .slot-chat-list');
    return { streamId: el ? el.dataset.streamId : null, hasHero: !!(el && el.querySelector('.slot-chat-session-hero')) };
  })()`);
  ctx.assert('false-positive guard: legit same-stream rebind STILL paints (not blanked)',
    fpPaint.data.leaked === false && fpPaint.data.paintedStreamId === P.streamId, fpPaint.data);
  ctx.assert('false-positive guard: list data-stream-id == P with hero painted',
    fpDom.streamId === P.streamId && fpDom.hasHero === true, fpDom);

  await ctx.screenshot('forced-window-discriminator');
}

module.exports = { SCENARIO_META, run };
