'use strict';

// Drive the real composer and queued-row controls with deterministic bridges.
// The disposable native-provider journey separately proves work/queue preservation.
const { WORKSPACE_KEY, waitFor, reloadWith } = require('./workspace_scenario');
const json = JSON.stringify;
const probe = cell => `(() => {
  const root = document.getElementById(${json(cell)});
  const visible = selector => { const el = root?.querySelector(selector); return !!el && !el.hidden && getComputedStyle(el).display !== 'none'; };
  return { compose: !!root?.querySelector('.slot-chat-compose'),
    mode: !!root?.querySelector('.slot-chat-send-mode'), stop: visible('.slot-chat-compose-stop'),
    send: !!root?.querySelector('.slot-chat-compose-send'),
    sendDisabled: root?.querySelector('.slot-chat-compose-send')?.disabled ?? null,
    sendNow: root?.querySelectorAll('.slot-chat-send-now').length || 0 };
})()`;

async function agentChatSendDefaultQueueState(ctx) {
  const { session, report, cdp, fixture } = ctx;
  if (!fixture) { report.note('agent-chat-send-default-queue-state: hermetic-only scenario skipped'); return; }
  const agent = fixture.streamId;
  const composite = 'local:web-gate-assistant';
  await reloadWith(ctx, { [WORKSPACE_KEY]: json({ paneCount: 4, activeSlot: 0 }) });
  await waitFor(ctx, 'typeof window.focusStreamId === "function" && !!window.PentacleChatStore', 'app and chat store ready');
  const openChat = async (streamId, cell) => {
    await waitFor(ctx, `window.focusStreamId(${json(streamId)}) === true`, `focus ${streamId}`);
    await waitFor(ctx, `document.getElementById(${json(cell)})?.classList.contains('occupied') === true`, `${streamId} occupies ${cell}`);
    if (streamId !== composite) {
      await waitFor(ctx, `!!document.querySelector('#header-${cell.slice(5)} [data-mode="chat"]')`, 'chat toggle');
      await session.eval(`document.querySelector('#header-${cell.slice(5)} [data-mode="chat"]').click()`);
    }
    await waitFor(ctx, `!!document.querySelector(${json('#' + cell + ' .slot-chat-compose-send')})`, `${cell} composer`);
  };
  try {
    await openChat(agent, 'cell-0');
    const idle = await session.eval(probe('cell-0'));
    report.ok('an idle agent has one ordinary Send and no chooser', idle.compose && idle.send && !idle.mode && !idle.stop, idle);
    await session.eval(`(() => {
      window.__normalSends = []; window.__queueInterrupts = []; window.__queuedActions = [];
      const store = window.PentacleChatStore;
      store.applyFrame({ type: 'session.inventory', sessions: store.getState().sessions.map(row => row.stream_id === ${json(agent)}
        ? { ...row, capabilities: { ...row.capabilities, queued_send_now: true } } : row) });
      store.setQueuedSendNowBridge(async args => { window.__queuedActions.push(args); return { ok: true, confirm: 'interrupt_unconfirmed' }; });
      store.setSendBridge(async args => { window.__normalSends.push(args); return { ok: true, provider_queued: args.text !== 'working probe' }; });
      store.setCancelBridge(async args => { window.__queueInterrupts.push(args); return { ok: true, confirm: 'interrupt_unconfirmed' }; });
      store.sendTurn(${json(agent)}, 'working probe'); return true;
    })()`);
    await waitFor(ctx, `window.__normalSends.length === 1 && window.PentacleChatStore.getTurnPhase(${json(agent)}) !== 'idle'`, 'agent turn in flight');
    const draft = text => session.eval(`(() => { const input = document.querySelector('#cell-0 .slot-chat-compose-input');
      input.value = ${json(text)}; input.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
    const enter = alt => session.eval(`document.querySelector('#cell-0 .slot-chat-compose-input').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', altKey: ${!!alt}, bubbles: true, cancelable: true })), true`);
    await draft('first queued message');
    await enter(false);
    await waitFor(ctx, 'window.__normalSends.length === 2', 'Enter dispatches without waiting for idle');
    await draft('review with em before we implement');
    await enter(true);
    await waitFor(ctx, 'window.__normalSends.length === 3', 'Alt+Enter is also an ordinary send');
    await draft('keep this unsent draft');
    await waitFor(ctx, "document.querySelectorAll('#cell-0 .slot-chat-send-now').length === 2", 'both provider-queued rows have Send now');
    const helperRow = await session.eval(`(() => {
      const row = [...document.querySelectorAll('#cell-0 .slot-chat-row.is-user')]
        .find(el => el.textContent.includes('review with em before we implement'));
      return { visible: !!row && getComputedStyle(row).display !== 'none',
        queued: !!row?.querySelector('.slot-chat-send-status.is-queued') };
    })()`);
    report.ok('helper-shaped operator send stays visible with its queued receipt', helperRow.visible && helperRow.queued, helperRow);
    const working = await session.eval(probe('cell-0'));
    report.ok('a working agent has no chooser and keeps Stop and Send with a draft', !working.mode && working.stop && working.send && !working.sendDisabled, working);
    const sends = await session.eval('window.__normalSends.map(args => ({ text: args.text, urgent: "urgent" in args }))');
    report.ok('Enter and Alt+Enter dispatch immediately without urgent', sends.length === 3 && sends.every(row => !row.urgent), sends);
    const queued = await session.eval(`(() => ({
      controls: [...document.querySelectorAll('#cell-0 .slot-chat-send-now')].map(b => ({ disabled: b.disabled, text: b.textContent })),
      reasons: [...document.querySelectorAll('#cell-0 .slot-chat-send-now-reason')].map(e => e.textContent),
      urgent: !!document.querySelector('#cell-0 .slot-chat-compose-urgent'),
    }))()`);
    report.ok('qualified queued rows offer minimal Send now without warnings or new-message urgency',
      queued.controls.length === 2 && queued.controls.every(b => !b.disabled && b.text === 'Send now')
      && queued.reasons.length === 0 && !queued.urgent, queued);
    await session.eval(`document.querySelectorAll('#cell-0 .slot-chat-send-now').forEach(b => { b.click(); b.click(); }); true`);
    await cdp.sleep(100);
    const preserved = await session.eval(`({ sends: window.__normalSends.length, interrupts: window.__queueInterrupts.length,
      draft: document.querySelector('#cell-0 .slot-chat-compose-input').value,
      queued: document.querySelectorAll('#cell-0 .slot-chat-send-status.is-queued').length, actions: window.__queuedActions })`);
    report.ok('repeated pending controls preserve each original and draft without invoking Stop or repasting', preserved.sends === 3
      && preserved.interrupts === 0 && preserved.draft === 'keep this unsent draft' && preserved.queued === 2
      && preserved.actions.length === 2 && new Set(preserved.actions.map(a => a.originalRequestId)).size === 2, preserved);
    // The separate queued-action bridge does not consume the explicit Stop budget.
    await session.eval(`document.querySelector('#cell-0 .slot-chat-compose-stop').click(); true`);
    await waitFor(ctx, 'window.__queueInterrupts.length === 1', 'explicit Stop still dispatches once');
    report.ok('Stop retains its generation fence', await session.eval('typeof window.__queueInterrupts[0].expectedSessionGeneration === "string"'), {});
    await session.eval(`(() => {
      window.__normalSends.slice(1).forEach((args, index) => window.PentacleChatStore.applyFrame({ type: 'chat.event', event: {
        daemon_seq: 900001 + index, host: 'local', provider: 'claude', stream_id: ${json(agent)}, session_name: ${json(agent.split(':')[1])},
        timestamp: new Date().toISOString(), kind: 'USER', text: args.text, optimistic_id: args.optimisticId, request_id: args.requestId,
        raw: { receipt_state: 'landed', receipt_delivery: 'landed' },
      } })); return true;
    })()`);
    await waitFor(ctx, "document.querySelectorAll('#cell-0 .slot-chat-send-now').length === 0 && !!document.querySelector('#cell-0 .slot-chat-send-status.is-sent')", 'processing echoes resolve queued rows to Sent');
    report.ok('processing echoes remove the queued-row actions without another send', await session.eval('window.__normalSends.length === 3'), {});
    await openChat(composite, 'cell-1');
    await session.eval(`window.PentacleChatStore.sendTurn(${json(composite)}, 'composite queue probe'), true`);
    await cdp.sleep(100);
    const compositeProbe = await session.eval(probe('cell-1'));
    report.ok('composite has no chooser, Stop, or queued-row interrupt even with an anomalous queue receipt', compositeProbe.compose && !compositeProbe.mode && !compositeProbe.stop && compositeProbe.sendNow === 0, compositeProbe);
  } finally {
    await reloadWith(ctx, { [WORKSPACE_KEY]: json({ paneCount: 4, activeSlot: 0 }) });
  }
}

module.exports = { agentChatSendDefaultQueueState };
