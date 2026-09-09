'use strict';

const { openMockSession } = require('../lib/mock_chat_scenarios');

const SCENARIO_META = {
  target_compat: ['hostc'],
  requires: [],
  providers: ['codex', 'claude'],
  scripted_daemon_fixture: 'native_queue_send_gating',
};

async function run(ctx) {
  const opened = await openMockSession(ctx);
  const cell = `#cell-${opened.slot}`;
  const streamId = opened.streamId;
  const firstText = 'first active turn';
  const secondText = 'send during active turn across reconnect';

  const patched = await ctx.eval(`(() => {
    window.__nativeQueueSends = [];
    window.PentacleChatStore.setSendBridge(async (args) => {
      window.__nativeQueueSends.push(args);
      if (String(args.text || '').includes('across reconnect')) {
        throw new Error('socket closed during send');
      }
      return { ok: true };
    });
    return true;
  })()`);
  ctx.assert('native queue fixture patched the renderer send bridge', patched === true);

  await ctx.type(`${cell} .slot-chat-compose-input`, firstText);
  await ctx.click(`${cell} .slot-chat-compose-send`);
  await ctx.waitFor(
    `window.__nativeQueueSends && window.__nativeQueueSends.length === 1`,
    { timeoutMs: 10000, label: 'first send dispatched' },
  );
  await ctx.eval(`(() => {
    const event = {
      daemon_seq: 81,
      host: 'mock-host',
      provider: 'codex',
      session_id: 'mock-session',
      session_name: 'mock-session',
      stream_id: ${JSON.stringify(streamId)},
      timestamp: new Date().toISOString(),
      kind: 'ASSIST',
      text: 'working on it',
      raw: {}
    };
    window.PentacleChatStore.applyFrame({ type: 'chat.event', event });
    if (typeof renderSlotChat === 'function') renderSlotChat(${Number(opened.slot)});
    return true;
  })()`);
  await ctx.waitFor(
    `window.PentacleChatStore.getTurnPhase(${JSON.stringify(streamId)}) === 'working'`,
    { timeoutMs: 10000, label: 'first turn working' },
  );

  const beforeSecond = ctx.beaconSeq();
  await ctx.type(`${cell} .slot-chat-compose-input`, secondText);
  await ctx.click(`${cell} .slot-chat-compose-send`);
  await ctx.awaitBeacon(
    (b) => b.seq > beforeSecond && b.name === 'chat.compose.optimistic_insert' && b.data && b.data.stream_id === streamId,
    { timeoutMs: 10000, label: 'mid-turn optimistic insert' },
  );
  await ctx.waitFor(
    `window.__nativeQueueSends && window.__nativeQueueSends.length === 2`,
    { timeoutMs: 10000, label: 'second send dispatched immediately' },
  );
  await ctx.waitFor(
    `(() => {
      const sends = window.PentacleChatStore.getState().optimisticSends || {};
      const second = Object.values(sends).find((send) => send && send.text === ${JSON.stringify(secondText)});
      return second && second.status === 'indeterminate' ? true : false;
    })()`,
    { timeoutMs: 10000, label: 'second send marked indeterminate after reconnect-like failure' },
  );

  const stateBeforeEcho = await ctx.eval(`(() => {
    const state = window.PentacleChatStore.getState();
    const sends = state.optimisticSends || {};
    const first = Object.values(sends).find((send) => send && send.text === ${JSON.stringify(firstText)});
    const second = Object.values(sends).find((send) => send && send.text === ${JSON.stringify(secondText)});
    return {
      firstId: first && first.optimistic_id,
      secondId: second && second.optimistic_id,
      secondQueued: second && second.turn_queued === true,
      turn: window.PentacleChatStore.getTurnState(${JSON.stringify(streamId)})
    };
  })()`);
  ctx.assert('mid-turn send did not use the old queued hold', stateBeforeEcho.secondQueued === false, stateBeforeEcho);
  ctx.assert('active working turn remains the first send', stateBeforeEcho.turn?.optimisticId === stateBeforeEcho.firstId, stateBeforeEcho);
  ctx.assert('mid-turn send has its own optimistic id', !!stateBeforeEcho.secondId, stateBeforeEcho);
  ctx.assert('queued CSS state is absent', await ctx.eval(`document.querySelectorAll('${cell} .slot-chat-row.is-queued').length === 0`));

  await ctx.eval(`(() => {
    const state = window.PentacleChatStore.getState();
    const sends = state.optimisticSends || {};
    const second = Object.values(sends).find((send) => send && send.text === ${JSON.stringify(secondText)});
    window.PentacleChatStore.applyFrame({ type: '__reconnect', generation: second ? second.socket_generation : 0, next_generation: 99 });
    const event = {
      daemon_seq: 82,
      host: 'mock-host',
      provider: 'codex',
      session_id: 'mock-session',
      session_name: 'mock-session',
      stream_id: ${JSON.stringify(streamId)},
      timestamp: new Date().toISOString(),
      kind: 'USER',
      text: ${JSON.stringify(secondText)},
      optimistic_id: second && second.optimistic_id,
      raw: {}
    };
    window.PentacleChatStore.applyFrame({ type: 'chat.event', event });
    if (typeof renderSlotChat === 'function') renderSlotChat(${Number(opened.slot)});
    return true;
  })()`);
  await ctx.waitFor(
    `(() => {
      const sends = window.PentacleChatStore.getState().optimisticSends || {};
      return !Object.values(sends).some((send) => send && send.text === ${JSON.stringify(secondText)});
    })()`,
    { timeoutMs: 10000, label: 'reconnect server echo reconciled the mid-turn send' },
  );
  await ctx.waitFor(
    `(() => {
      const d = window.PentacleChatStore.selectSessionDetail(${JSON.stringify(streamId)}, { visibleCount: 260 });
      const rows = (d && d.transcriptItems) || [];
      return rows.some((row) => row && row.isUser && row.text === ${JSON.stringify(secondText)} && row.pending === false);
    })()`,
    { timeoutMs: 10000, label: 'mid-turn send landed as a non-pending user row' },
  );
  await ctx.screenshot('native-queue-send-gating');
}

module.exports = { SCENARIO_META, run };

