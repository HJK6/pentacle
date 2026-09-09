'use strict';

const STREAM_ID = 'mock-host:mock-session';
const SESSION_NAME = 'mock-session';

function seqExpr(streamId = STREAM_ID) {
  return `(() => {
    const d = window.PentacleChatStore.selectSessionDetail(${JSON.stringify(streamId)}, { visibleCount: 260 });
    const items = (d && d.transcriptItems) || [];
    return items.map((it) => Number(it?.event?.daemon_seq ?? it?.daemon_seq ?? it?.seq ?? it?.id ?? it?.eventKey))
      .filter((seq) => Number.isFinite(seq) && seq > 0);
  })()`;
}

function flowExpr(streamId = STREAM_ID) {
  return `(() => {
    const h = window.PentacleHarness && window.PentacleHarness.chat;
    if (!h || typeof h.flowCounts !== 'function') return null;
    const c = h.flowCounts().get(${JSON.stringify(streamId)});
    return c ? JSON.parse(JSON.stringify(c)) : null;
  })()`;
}

async function openMockSession(ctx, { streamId = STREAM_ID, sessionName = SESSION_NAME } = {}) {
  await ctx.waitFor(
    `window.PentacleChatStore.selectChatList('all').some((s) => s.streamId === ${JSON.stringify(streamId)})`,
    { timeoutMs: 30000, label: 'mock session appears in chat list' },
  );
  await ctx.waitFor(
    `!!document.querySelector(${JSON.stringify(`.session-item[data-name="${sessionName}"]`)})`,
    { timeoutMs: 30000, label: 'mock session appears in sidebar' },
  );
  ctx.assert('mock session clicked', await ctx.click(`.session-item[data-name="${sessionName}"]`), { sessionName });
  const slot = await ctx.waitFor(
    `(() => { const i = state.slots.findIndex(s => s && (s.name === ${JSON.stringify(sessionName)} || s.session === ${JSON.stringify(sessionName)})); return i >= 0 ? i : false; })()`,
    { timeoutMs: 20000, label: 'mock session attached to a slot' },
  );
  await ctx.waitFor(`document.querySelector('#cell-${slot}').classList.contains('occupied')`, {
    timeoutMs: 10000,
    label: 'mock slot occupied',
  });
  const beforeRender = ctx.beaconSeq();
  await ctx.click(`.cell-view-toggle[data-slot="${slot}"][data-mode="chat"]`);
  await ctx.waitFor(`!!document.querySelector('#cell-${slot} .slot-chat-list')`, {
    timeoutMs: 15000,
    label: 'mock chat transcript mounted',
  });
  return { slot, streamId, sessionName, beforeRender };
}

async function awaitRenderedSeqs(ctx, seqs, { streamId = STREAM_ID, afterSeq = 0, timeoutMs = 30000 } = {}) {
  for (const seq of seqs) {
    await ctx.awaitBeacon(
      (b) => b.seq > afterSeq
        && b.name === 'chat:event_rendered'
        && b.data
        && b.data.stream_id === streamId
        && Number(b.data.seq) === Number(seq),
      { timeoutMs, label: `chat:event_rendered seq ${seq}` },
    );
  }
}

async function awaitTranscriptOrder(ctx, expected, { streamId = STREAM_ID, timeoutMs = 30000 } = {}) {
  const order = await ctx.waitFor(
    `(() => {
      const seqs = ${seqExpr(streamId)};
      const expected = ${JSON.stringify(expected)};
      return seqs.length >= expected.length ? JSON.stringify(seqs) : false;
    })()`,
    { timeoutMs, label: `transcript order ${expected.join(',')}` },
  );
  return JSON.parse(order);
}

async function awaitFlow(ctx, predicate, { streamId = STREAM_ID, timeoutMs = 30000, label = 'mock flow counts' } = {}) {
  const encoded = String(predicate);
  const value = await ctx.waitFor(
    `(() => {
      const c = ${flowExpr(streamId)};
      if (!c) return false;
      const pred = ${encoded};
      return pred(c) ? JSON.stringify(c) : false;
    })()`,
    { timeoutMs, label },
  );
  return JSON.parse(value);
}

module.exports = {
  STREAM_ID,
  SESSION_NAME,
  seqExpr,
  flowExpr,
  openMockSession,
  awaitRenderedSeqs,
  awaitTranscriptOrder,
  awaitFlow,
};
