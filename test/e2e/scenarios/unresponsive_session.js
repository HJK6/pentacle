'use strict';

const {
  STREAM_ID,
  openMockSession,
  awaitRenderedSeqs,
} = require('../lib/mock_chat_scenarios');

const SCENARIO_META = {
  target_compat: ['hostc'],
  requires: [],
  providers: ['codex', 'claude'],
  scripted_daemon_fixture: 'mock_chat_event_render_order',
};

function inventoryFrame(status) {
  return `(() => {
    const streamId = ${JSON.stringify(STREAM_ID)};
    const current = state.chatStream.sessions.find((session) => session.stream_id === streamId);
    if (!current || !window.PentacleChatStore || typeof window.PentacleChatStore.applyFrame !== 'function'
      || typeof applyChatStreamPayload !== 'function') return false;
    const session = { ...current, ...${JSON.stringify(status)} };
    const frame = { type: 'session.inventory', sessions: [session] };
    window.PentacleChatStore.applyFrame(frame);
    applyChatStreamPayload(frame);
    return true;
  })()`;
}

async function run(ctx) {
  const opened = await openMockSession(ctx);
  await awaitRenderedSeqs(ctx, [1, 2, 3], { streamId: STREAM_ID, afterSeq: opened.beforeRender });
  const retainedText = 'first by daemon order';
  await ctx.waitFor(
    `document.querySelector('#cell-${opened.slot} .slot-chat-list')?.textContent.includes(${JSON.stringify(retainedText)})`,
    { timeoutMs: 15000, label: 'baseline transcript rendered before timeout inventory frame' },
  );

  ctx.assert('timeout inventory frame applied', await ctx.eval(inventoryFrame({
    pane_status: 'pane_unresponsive',
    pane_status_reason: 'tmux_probe_timeout',
    pane_status_since: '2026-08-01T12:00:00Z',
    working: true,
  })) === true);
  await ctx.waitFor(
    `document.querySelector('#cell-${opened.slot} .slot-chat-status-badge.is-unresponsive')?.textContent.trim() === 'Session unresponsive — tmux did not answer'`,
    { timeoutMs: 10000, label: 'exact unresponsive session-status label rendered' },
  );
  await ctx.waitFor(
    `document.querySelector('#cell-${opened.slot} .cosmic-status-tag')?.textContent.trim() === 'Unresponsive'`,
    { timeoutMs: 10000, label: 'cosmic header shows Unresponsive' },
  );
  ctx.assert('retained transcript remains visible while unresponsive', await ctx.eval(
    `document.querySelector('#cell-${opened.slot} .slot-chat-list')?.textContent.includes(${JSON.stringify(retainedText)}) === true`,
  ) === true);
  await ctx.screenshot('unresponsive-session');

  ctx.assert('recovery inventory frame applied', await ctx.eval(inventoryFrame({
    pane_status: 'pane_confirmed',
    pane_status_reason: null,
    pane_status_since: null,
    working: false,
  })) === true);
  await ctx.waitFor(
    `!document.querySelector('#cell-${opened.slot} .slot-chat-status-badge.is-unresponsive')`,
    { timeoutMs: 10000, label: 'unresponsive label clears on recovered inventory frame' },
  );
  ctx.assert('transcript remains visible after recovery', await ctx.eval(
    `document.querySelector('#cell-${opened.slot} .slot-chat-list')?.textContent.includes(${JSON.stringify(retainedText)}) === true`,
  ) === true);
  await ctx.screenshot('recovered-session');
}

module.exports = { SCENARIO_META, run };

