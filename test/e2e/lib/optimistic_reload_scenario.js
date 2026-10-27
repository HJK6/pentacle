'use strict';

const { WORKSPACE_KEY, waitFor, reloadWith } = require('./workspace_scenario');
const json = JSON.stringify;

// Real composer and page reload; the bridge holds dispatch pending so a delayed
// server USER echo can be delivered in a deterministic order without a provider.
async function optimisticReload(ctx) {
  const { session, report, fixture } = ctx;
  if (!fixture) throw new Error('optimistic-reload requires the isolated fixture daemon');
  const streamId = fixture.streamId;
  const reset = () => reloadWith(ctx, { [WORKSPACE_KEY]: json({ paneCount: 4, activeSlot: 0 }) });
  async function open() {
    await waitFor(ctx, '!!window.PentacleChatStore && typeof window.focusStreamId === "function"', 'chat store ready');
    await waitFor(ctx, `window.focusStreamId(${json(streamId)}) === true`, 'fixture chat focused');
    await waitFor(ctx, '!!document.querySelector(\'#header-0 [data-mode="chat"]\')', 'chat toggle');
    await session.eval('document.querySelector(\'#header-0 [data-mode="chat"]\').click(), true');
    await waitFor(ctx, "!!document.querySelector('#cell-0 .slot-chat-compose-send')", 'composer ready');
    await session.eval(`(() => {
      window.__reloadSends = [];
      window.PentacleChatStore.setSendBridge(args => {
        window.__reloadSends.push(args);
        return new Promise(() => {});
      }); return true;
    })()`);
  }
  async function send(text) {
    await session.eval(`(() => {
      const input = document.querySelector('#cell-0 .slot-chat-compose-input');
      input.value = ${json(text)};
      input.dispatchEvent(new Event('input', { bubbles: true }));
      document.querySelector('#cell-0 .slot-chat-compose-send').click(); return true;
    })()`);
    await waitFor(ctx, 'window.__reloadSends.length === 1', 'composer dispatched one message');
    return session.eval('window.__reloadSends[0]');
  }
  const echo = (send, seq) => session.eval(`window.PentacleChatStore.applyFrame({ type: 'chat.event', event: {
    host: 'local', provider: 'claude', stream_id: ${json(streamId)}, session_name: ${json(fixture.sessionName)},
    daemon_seq: ${seq}, timestamp: new Date().toISOString(), kind: 'USER', text: ${json(send.text)},
    optimistic_id: ${json(send.optimisticId)}, request_id: ${json(send.requestId)},
    raw: { receipt_state: 'landed', receipt_delivery: 'landed' },
  } }), true`);
  const probe = id => session.eval(`(() => {
    const store = window.PentacleChatStore;
    const pending = store.getState().optimisticSends?.[${json(id)}];
    const root = document.querySelector('#cell-0');
    return { pending: !!pending, status: pending?.status || null,
      rows: store.selectSessionDetail(${json(streamId)}).transcriptItems
        .filter(row => row.isUser).map(row => ({ text: row.text, optimisticId: row.optimisticId, sendState: row.sendState || null })),
      dom: root.querySelector('.slot-chat-list')?.textContent || '',
      sending: root.querySelectorAll('.slot-chat-send-status.is-sending').length };
  })()`);
  try {
    // Different text proves an old echo cannot overwrite the new message;
    // identical text also must retain its distinct submission/delivery identity.
    for (const sameText of [false, true]) {
      await reset();
      await open();
      const before = await send('reload identity previous message');
      await reset();
      await open();
      const text = sameText ? before.text : 'reload identity new pending message';
      const after = await send(text);
      await echo(before, 910001);
      await waitFor(ctx, `(() => {
        const rows = window.PentacleChatStore.selectSessionDetail(${json(streamId)}).transcriptItems.filter(row => row.isUser);
        const bubbles = [...document.querySelectorAll('#cell-0 .slot-chat-user-bubble')].map(node => node.textContent);
        return rows.length === bubbles.length && rows.every((row, index) => row.text === bubbles[index]);
      })()`, 'stale echo rendered to match store');
      const stale = await probe(after.optimisticId);
      report.note(`optimistic reload (${sameText ? 'same' : 'different'} text): ${json({
        beforeId: before.optimisticId, afterId: after.optimisticId, stale,
      })}`);
      report.ok('a delayed pre-reload echo leaves the new send pending and visible',
        stale.pending && stale.rows.some(row => row.optimisticId === after.optimisticId && row.text === text && ['queued', 'sending'].includes(row.sendState))
          && stale.dom.includes(text) && stale.sending === 1, stale);
      report.ok('a new ordinary send has a different ID after a real reload', before.optimisticId !== after.optimisticId, { before, after });
      await echo(after, 910002);
      await waitFor(ctx, `!window.PentacleChatStore.getState().optimisticSends?.[${json(after.optimisticId)}]`, 'own echo settled new send');
      await waitFor(ctx, "document.querySelectorAll('#cell-0 .slot-chat-send-status.is-sending').length === 0", 'own echo painted settled delivery');
      const settled = await probe(after.optimisticId);
      const rows = settled.rows.filter(row => row.optimisticId === before.optimisticId || row.optimisticId === after.optimisticId);
      report.ok('only the own echo settles the new row, preserving both submissions once',
        !settled.pending && settled.sending === 0 && rows.length === 2
          && rows.filter(row => row.optimisticId === after.optimisticId && row.text === text).length === 1, settled);
    }
  } finally {
    await reset();
  }
}

module.exports = { optimisticReload };
