// Walk: optimistic_id_reconcile
// Public regression scenario for optimistic-send reconciliation.
//
// Proves end-to-end, against the real Electron app + daemon, that a sent USER
// message reconciles with its server echo through the canonical chat-core
// (3d93d9c) ID-first matcher + render-stability contract:
//   - the renderer owns the optimistic_id and stamps it on the optimistic row;
//   - the same optimistic_id round-trips on the reconcile telemetry;
//   - the reconcile happens IN PLACE — the kept USER row preserves the optimistic
//     identity (id) and records the correlated daemon sequence
//     (correlatedDaemonSeq) instead of spawning a duplicate server bubble.
// This exercises the same path covered deterministically by the jsdom
// chat_store_controller tests ("ID-first reconcile…"), but on the live wire.
const { spawnThrowawayChat } = require('../lib/flows');

const SCENARIO_META = { target_compat: ['hostc'], requires: ['claude'], providers: ['claude'] };

async function run(ctx) {
  const provider = ctx.provider || 'claude';
  const { slot, streamId } = await spawnThrowawayChat(ctx, provider);
  const cell = `#cell-${slot}`;
  const sid = JSON.stringify(streamId);
  // A unique sentinel so we can pinpoint exactly one USER bubble for this send.
  const sentinel = `OPTIMISTIC-RECONCILE-${Date.now()}`;

  const before = ctx.beaconSeq();
  await ctx.waitFor(
    `(() => { const b = document.querySelector('${cell} .slot-chat-compose-send'); return b && !b.disabled; })()`,
    { timeoutMs: 15000, label: 'composer send enabled' },
  );
  await ctx.type(`${cell} .slot-chat-compose-input`, `Please reply with exactly: ${sentinel}`);
  await ctx.click(`${cell} .slot-chat-compose-send`);

  // 1) The renderer inserts the optimistic USER row and emits the insert beacon
  //    carrying the renderer-owned optimistic_id.
  const insert = await ctx.awaitBeacon(
    (b) => b.seq > before && b.name === 'chat.compose.optimistic_insert' && b.data && b.data.stream_id === streamId && !b.data.queued,
    { timeoutMs: 10000, label: 'optimistic insert (with optimistic_id)' },
  );
  const optimisticId = insert.data && insert.data.optimistic_id;
  ctx.assert('reconcile: renderer stamped an optimistic_id on the send', !!optimisticId, { insert: insert.data });

  // The optimistic row is present in the store stamped with that id (best-effort
  // — a very fast reconcile may already have pruned it, which is fine).
  await ctx.waitFor(
    `(() => { const s = window.PentacleChatStore.getState().optimisticSends || {};
      const e = s[${JSON.stringify(optimisticId)}];
      return !!(e && e.optimistic_id === ${JSON.stringify(optimisticId)}); })()`,
    { timeoutMs: 5000, label: 'optimistic send recorded in store with its id' },
  ).catch(() => false);
  await ctx.screenshot('optimistic-inserted');

  // 2) The server USER echo reconciles it (proves the live round-trip reached the
  //    matcher). A FAILED beacon would localize a rejected send.
  const reconciled = await ctx.awaitBeacon(
    (b) => b.seq > before
      && (b.name === 'chat.compose.optimistic_reconciled' || b.name === 'chat.compose.optimistic_failed')
      && b.data && b.data.stream_id === streamId,
    { timeoutMs: 45000, label: 'reconcile or fail' },
  );
  ctx.assert('reconcile: optimistic reconciled (not failed)', reconciled.name === 'chat.compose.optimistic_reconciled', {
    got: reconciled.name, detail: reconciled.data,
  });
  // 2a) The reconcile telemetry carries the SAME optimistic_id (id round-trip).
  ctx.assert('reconcile: reconciled by the same optimistic_id the renderer owned',
    reconciled.data && reconciled.data.optimistic_id === optimisticId,
    { inserted: optimisticId, reconciled: reconciled.data && reconciled.data.optimistic_id });

  // 3) Render-stability contract in the live store: exactly one USER row for the
  //    sentinel, and it PRESERVES the optimistic identity (id) while carrying the
  //    correlated daemon sequence — i.e. reconciled in place, no duplicate bubble.
  const rowState = await ctx.waitFor(
    `(() => {
      const d = window.PentacleChatStore.selectSessionDetail(${sid}, { visibleCount: 260 });
      const items = (d && d.transcriptItems) || [];
      const mine = items.filter(it => it && it.isUser && /${sentinel}/.test(String(it.text || '')));
      if (mine.length !== 1) return false;
      const row = mine[0];
      // The optimistic in-flight entry must be gone (pruned) once reconciled.
      const optGone = !(window.PentacleChatStore.getState().optimisticSends || {})[${JSON.stringify(optimisticId)}];
      if (!optGone) return false;
      return JSON.stringify({
        count: mine.length,
        id: row.id,
        optimisticId: row.optimisticId,
        correlatedDaemonSeq: row.correlatedDaemonSeq,
        sendState: row.sendState || null,
      });
    })()`,
    { timeoutMs: 30000, label: 'single reconciled USER row in store' },
  );
  const rs = JSON.parse(rowState);
  ctx.log('reconciled row: ' + rowState);
  ctx.assert('reconcile: exactly one USER bubble (no duplicate)', rs.count === 1, rs);
  ctx.assert('reconcile: kept row preserves the optimistic identity (render stability)',
    rs.optimisticId === optimisticId && rs.id === optimisticId, rs);
  ctx.assert('reconcile: kept row correlated to a daemon sequence',
    Number.isFinite(Number(rs.correlatedDaemonSeq)), rs);
  ctx.assert('reconcile: no lingering sending affordance on the confirmed bubble',
    !rs.sendState || rs.sendState === undefined || rs.sendState === null, rs);

  // DOM: the user bubble is present and not stuck in a sending state.
  await ctx.waitFor(
    `!document.querySelector('${cell} .slot-chat-row.is-user.is-sending')`,
    { timeoutMs: 20000, label: 'no sending row lingers in DOM' },
  );
  await ctx.screenshot('optimistic-reconciled');
}

module.exports = { SCENARIO_META, run };
