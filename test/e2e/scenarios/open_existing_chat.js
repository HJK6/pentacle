// Walk: open_existing_chat
// public-ui-regression (Phase A trivial walk)
//
// Proves the real read path end-to-end: pick a live session from the store that
// has transcript content, click its sidebar item (real DOM click -> assignToSlot
// -> attachSession), toggle that slot to Chat view (real per-slot toggle), and
// assert the transcript renders — via DOM rows AND via harness render telemetry
// (chat:event_rendered beacons) — then screenshot. No synthetic state.

const SCENARIO_META = {
  // Read-only render proof; runs anywhere the app + a live session exist.
  target_compat: ['hostc', 'hosta', 'hostb'],
  requires: [],
  providers: ['claude', 'codex'],
};

const { buildTranscriptRowProofScript } = require('../lib/render_proofs');

async function run(ctx) {
  // 0) The session inventory snapshot lands just after `connected`; wait for the
  //    store's chat list to populate before picking.
  await ctx.waitFor(`window.PentacleChatStore.selectChatList('all').length > 0`, {
    timeoutMs: 20000,
    label: 'chat list populated from snapshot',
  });

  await ctx.waitFor(`document.querySelectorAll('.session-item[data-name]:not(.schedule-item)').length > 0`, {
    timeoutMs: 20000,
    label: 'sidebar session items rendered',
  });

  // 1) Pick a real AGENT session (not a bare TMUX session), preferring a local
  //    (hostc) claude/codex chat. Source candidates from the REAL sidebar DOM,
  //    then cross-reference the store for stream metadata. Store-only selection
  //    can choose the running harness agent's own stream, which has no clickable
  //    sidebar item and is not a valid user-open target.
  const pick = await ctx.eval(`(() => {
    const list = window.PentacleChatStore.selectChatList('all');
    const byName = new Map(list.map(s => [s.sessionName, s]));
    const items = [...document.querySelectorAll('.session-item[data-name]:not(.schedule-item)')];
    const isAgent = (s) => /^(claude|codex)-/.test(s.sessionName || '') || (s.provider && s.provider !== 'TMUX');
    const score = (s) => (s.host === 'hostc' ? 100 : s.host === 'hosta' ? 10 : 1)
      + (/^claude-/.test(s.sessionName) ? 5 : /^codex-/.test(s.sessionName) ? 4 : 0);
    const agents = items.map((el) => {
      const s = byName.get(el.dataset.name);
      if (!s || !s.streamId || !isAgent(s)) return null;
      return { streamId: s.streamId, sessionName: s.sessionName, host: s.host, provider: s.provider, domHost: el.dataset.host || null };
    }).filter(Boolean);
    agents.sort((a, b) => score(b) - score(a));
    return agents[0] || null;
  })()`);

  if (!pick) return { skip: 'no live agent session to open' };
  ctx.log(`picked ${pick.sessionName} (${pick.streamId}) host=${pick.host} provider=${pick.provider}`);

  // 2) Click the sidebar item — the real user action (assignToSlot -> attachSession).
  const clicked = await ctx.click(`.session-item[data-name="${pick.sessionName}"]`);
  ctx.assert('sidebar item clicked', clicked, { sessionName: pick.sessionName });

  // 3) Wait for the session to land in a slot (occupied). Resolve the slot index.
  const slot = await ctx.waitFor(
    `(() => { const i = state.slots.findIndex(s => s && (s.name === ${JSON.stringify(pick.sessionName)} || s.session === ${JSON.stringify(pick.sessionName)})); return i >= 0 ? i : false; })()`,
    { timeoutMs: 20000, label: 'session attached to a slot' },
  );
  ctx.assert('attached to a slot', typeof slot === 'number' && slot >= 0, { slot });
  await ctx.waitFor(`document.querySelector('#cell-${slot}').classList.contains('occupied')`, {
    timeoutMs: 10000,
    label: 'cell occupied',
  });

  // 4) Toggle that slot to Chat view (the per-slot toggle only exists when occupied).
  const beforeRender = ctx.beaconSeq();
  const toggled = await ctx.click(`.cell-view-toggle[data-slot="${slot}"][data-mode="chat"]`);
  ctx.assert('chat view toggle clicked', toggled, { slot });

  // 4b) Opening the chat triggers requestStreamEvents (lazy load); wait for the
  //     transcript to populate in the store. We require real CONVERSATIONAL
  //     bubbles (user/assistant), not merely transcriptItems > 0 — the
  //     empty-transcript bug (public-ui-regression)
  //     still produced exactly one row, the session-summary fallback divider, so
  //     a `> 0` check passed while the screen was effectively blank. The fetched
  //     history must reach the reducer-backed store the chat UI renders from.
  const bubbles = await ctx.waitFor(
    `(() => { try { const d = window.PentacleChatStore.selectSessionDetail(${JSON.stringify(pick.streamId)}); const items = (d && d.transcriptItems) || []; const n = items.filter(i => i.isUser || i.tone === 'assistant').length; return n > 0 ? n : false; } catch (e) { return false; } })()`,
    { timeoutMs: 20000, label: 'conversational bubbles lazy-loaded into store' },
  );
  ctx.assert('transcript bubbles lazy-loaded (store)', bubbles > 0, { bubbles });

  // 5a) Assert the transcript renders in the REAL DOM (rows present in this cell).
  const rowCount = await ctx.waitFor(
    buildTranscriptRowProofScript(`#cell-${slot}`),
    { timeoutMs: 15000, label: 'transcript rows rendered in cell DOM' },
  );
  ctx.assert('transcript rows rendered (DOM)', rowCount > 0, { rowCount });

  // 5b) Assert the render is proven by harness TELEMETRY (a render beacon fired
  //     after we toggled to chat). This is the "proven with telemetry" bar.
  const renderBeacon = await ctx.awaitBeacon(
    (b) => b.seq > beforeRender && b.name === 'chat:event_rendered',
    { timeoutMs: 15000, label: 'chat:event_rendered beacon' },
  );
  ctx.assert('render proven by telemetry', !!renderBeacon, { beacon: renderBeacon && renderBeacon.name });

  await ctx.screenshot('open_existing_chat');
}

module.exports = { SCENARIO_META, run };

