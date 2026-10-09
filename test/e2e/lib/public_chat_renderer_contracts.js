'use strict';
const { reloadDashboardPage } = require('./dashboard_scenario');

// Fresh renderer-only frames and fake bridges through the real composer.
// These checks make no provider, transport or steering-ledger delivery claim.
async function publicChatRendererContracts(ctx) {
  const { session, report, cdp, timeoutMs, fixture } = ctx;
  if (!fixture) throw new Error('public renderer contract requires the owned seeded fixture');
  const { waitForValue } = require('./web_scenarios');
  const stream = JSON.stringify(fixture.streamId);
  const reload = async () => {
    await reloadDashboardPage(ctx);
    await waitForValue(session, cdp, '!!(window.PentacleChatStore && window.PentacleChatView && window.cc)', Boolean,
      { timeoutMs, label: 'fresh renderer store after reload' });
  };
  await reload();
  try {
    await waitForValue(session, cdp, `window.PentacleChatStore.getState().sessions.some(row => row.stream_id === ${stream})`, Boolean,
      { timeoutMs, label: 'owned fixture inventory ready' });
    const ordering = await session.eval(`(() => {
      const store = window.PentacleChatStore;
      const row = store.getState().sessions.find(row => row.stream_id === ${stream});
      store.applyFrame({ type: 'snapshot', sessions: [row], events: [], drafts: {} });
      const event = (daemon_seq, kind, text) => ({ daemon_seq, kind, text,
        stream_id: ${stream}, host: row.host, provider: row.provider, session_name: row.session_name,
        session_id: 'synthetic-renderer-session', timestamp: new Date(1700000000000 + daemon_seq * 1000).toISOString() });
      const frames = [event(1, 'USER', 'Synthetic first request'),
        event(2, 'ASSIST', 'Synthetic first reply'), event(3, 'USER', 'Synthetic steering request')];
      for (const event of frames) store.applyFrame({ type: 'chat.event', event });
      store.applyFrame({ type: 'chat.event', event: frames[1] });
      const container = document.createElement('div');
      document.body.appendChild(container);
      try {
        window.PentacleChatView.renderStreamTranscript(${stream}, container);
        return { text: container.textContent,
          sequences: store.getState().events.filter(event => event.stream_id === ${stream}).map(event => event.daemon_seq) };
      } finally { container.remove(); }
    })()`);
    const order = ['Synthetic first request','Synthetic first reply','Synthetic steering request'].map(text => ordering.text.indexOf(text));
    report.ok('renderer fresh seq1/2/3 remain ordered and duplicate raw frame renders once',
      JSON.stringify(ordering.sequences) === '[1,2,3]' && order.every(index => index >= 0)
        && order[0] < order[1] && order[1] < order[2]
        && ordering.text.split('Synthetic first reply').length === 2, ordering);
    report.note('steering assertion is merged renderer order only; no steering-ledger or provider-delivery proof');

    await session.eval(`(() => {
      window.__publicRenderer = { sends: [], cancels: [], forbidden: [] };
      window.cc.createPty = async () => '%unused-public-renderer-fixture';
      for (const name of ['chatSend','chatSendCorrelated','chatInterrupt']) window.cc[name] = () => {
        window.__publicRenderer.forbidden.push(name); throw new Error('unexpected live provider bridge: '+name);
      };
      window.PentacleChatStore.setSendBridge(async args => {
        window.__publicRenderer.sends.push(args); return { ok: true };
      });
      window.PentacleChatStore.setCancelBridge(async args => {
        window.__publicRenderer.cancels.push(args);
        return { ok: true, interrupted: true,
          confirm: window.__publicRenderer.cancels.length === 1 ? 'interrupt_unconfirmed' : 'interrupted' };
      });
      window.focusStreamId(${stream});
      return true;
    })()`);
    await waitForValue(session, cdp, '!!document.querySelector("#header-0 [data-mode=chat]")', Boolean,
      { timeoutMs, label: 'owned fixture chat mode control' });
    await session.eval('document.querySelector("#header-0 [data-mode=chat]").click()');
    await waitForValue(session, cdp, '!!document.querySelector("#cell-0 .slot-chat-compose-input")', Boolean,
      { timeoutMs, label: 'real composer mounted' });
    const send = async text => session.eval(`(() => {
      const input = document.querySelector('#cell-0 .slot-chat-compose-input');
      input.value = ${JSON.stringify(text)}; input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
      return true;
    })()`);
    const escape = async () => session.eval(`(() => {
      const input = document.querySelector('#cell-0 .slot-chat-compose-input');
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
      return true;
    })()`);
    await send('Synthetic stop request');
    await waitForValue(session, cdp, 'window.__publicRenderer.sends.length', n => n === 1,
      { timeoutMs, label: 'real composer reached fake send bridge' });
    await escape();
    const retry = await waitForValue(session, cdp, `(() => ({
      state: window.PentacleChatStore.getInterruptState(${stream}),
      button: document.querySelector('#cell-0 .slot-chat-cancel-btn')?.textContent,
      calls: window.__publicRenderer.cancels.length,
    }))()`, value => value?.state?.retryable === true && value.button === 'Retry stop',
    { timeoutMs, label: 'unconfirmed interrupt exposes retry' });
    report.ok('ESC unconfirmed retains retryable stop state through real control',
      retry.calls === 1 && retry.state.confirm === 'interrupt_unconfirmed'
        && retry.button === 'Retry stop', retry);
    await escape();
    const resolved = await waitForValue(session, cdp,
      `window.PentacleChatStore.getInterruptState(${stream})`, state => state?.confirm === 'interrupted',
      { timeoutMs, label: 'second ESC reaches confirmed fake receipt' });
    report.ok('second ESC retries once and consumes confirmed stop receipt',
      resolved.retryable === false && await session.eval('window.__publicRenderer.cancels.length') === 2, resolved);

    await send('Synthetic returned draft');
    await waitForValue(session, cdp, 'window.__publicRenderer.sends.length', n => n === 2,
      { timeoutMs, label: 'second real composer send captured' });
    await session.eval(`(() => {
      const request = window.__publicRenderer.sends[1];
      const store = window.PentacleChatStore;
      const row = store.getState().sessions.find(row => row.stream_id === ${stream});
      store.applyFrame({ type: 'chat.event', event: {
        daemon_seq: 4, stream_id: ${stream}, host: row.host, provider: row.provider,
        session_name: row.session_name, session_id: 'synthetic-renderer-session',
        timestamp: new Date().toISOString(), kind: 'USER', text: request.text,
        optimistic_id: request.optimisticId,
        raw: { returned_to_prompt: true, user_delivery_state: 'returned_to_prompt' },
      } });
      return true;
    })()`);
    const returned = await waitForValue(session, cdp, `(() => ({
      draft: document.querySelector('#cell-0 .slot-chat-compose-input')?.value,
      text: document.querySelector('#cell-0 .slot-chat-list')?.textContent || '',
      recovery: window.PentacleChatStore.getReturnedToPromptDraft(${stream}),
      forbidden: window.__publicRenderer.forbidden,
    }))()`, value => value?.draft === 'Synthetic returned draft',
    { timeoutMs, label: 'returned-to-prompt draft restored in composer' });
    report.ok('returned-to-prompt restores draft without a duplicate transcript bubble',
      returned.recovery?.text === returned.draft && !returned.text.includes('Synthetic returned draft')
        && returned.forbidden.length === 0, returned);

    // Provider-queued send (spec_pentacle__chat_queued_message_state_2026_10 C6):
    // the daemon's send.result for a prompt held in the provider's native queue
    // arrives BEFORE the send bridge promise resolves; the row must read Queued
    // (not "sending…", and not regressed by the late bridge resolution) and flip
    // to Sent when the correlated USER echo lands. Renderer frames only: this
    // makes no provider or daemon queue-detection claim.
    // Probe of THIS message's row (earlier rows keep their own labels).
    const probe = `(() => {
      const rows = [...document.querySelectorAll('#cell-0 .slot-chat-row.is-user')]
        .filter(row => (row.textContent || '').includes('Synthetic provider queued'));
      const node = rows.length ? rows[rows.length - 1].querySelector('.slot-chat-send-status') : null;
      return { text: node ? node.textContent : null, cls: node ? node.className : '',
        sending: !!(node && node.classList.contains('is-sending')),
        queued: !!(node && node.classList.contains('is-queued')) };
    })()`;
    await session.eval(`(() => {
      window.__publicRenderer.resolveQueuedBridge = null;
      window.PentacleChatStore.setSendBridge(args => new Promise(resolve => {
        window.__publicRenderer.sends.push(args);
        window.__publicRenderer.resolveQueuedBridge = () => resolve({ ok: true, provider_queued: true });
      }));
      return true;
    })()`);
    await send('Synthetic provider queued');
    await waitForValue(session, cdp, 'window.__publicRenderer.sends.length', n => n === 3,
      { timeoutMs, label: 'provider-queued composer send captured' });
    await waitForValue(session, cdp, probe, value => value?.text === 'sending…',
      { timeoutMs, label: 'unacknowledged send reads sending…' });
    await session.eval(`(() => {
      const request = window.__publicRenderer.sends[2];
      window.PentacleChatStore.applyFrame({ type: 'send.result', request_id: request.requestId, delivery: 'landed',
        submission_confirmed: true, provider_queued: true, reason: 'provider_queued' });
      window.__publicRenderer.resolveQueuedBridge();
      return true;
    })()`);
    const queued = await waitForValue(session, cdp, probe, value => value?.text === 'Queued',
      { timeoutMs, label: 'provider-queued send reads Queued' });
    await new Promise(resolve => setTimeout(resolve, 150));
    const stillQueued = await session.eval(probe);
    report.ok('provider-queued send.result shows Queued and survives the late send-bridge resolution',
      queued.queued === true && queued.sending === false && stillQueued.text === 'Queued' && stillQueued.queued === true,
      { queued, stillQueued });
    await session.eval(`(() => {
      const request = window.__publicRenderer.sends[2];
      const store = window.PentacleChatStore;
      const row = store.getState().sessions.find(row => row.stream_id === ${stream});
      store.applyFrame({ type: 'chat.event', event: {
        daemon_seq: 5, stream_id: ${stream}, host: row.host, provider: row.provider,
        session_name: row.session_name, session_id: 'synthetic-renderer-session',
        timestamp: new Date().toISOString(), kind: 'USER', text: request.text,
        optimistic_id: request.optimisticId,
        raw: { receipt_state: 'landed', receipt_delivery: 'landed' },
      } });
      return true;
    })()`);
    const sent = await waitForValue(session, cdp, probe, value => value?.text === 'Sent',
      { timeoutMs, label: 'correlated USER echo flips Queued to Sent' });
    report.ok('correlated USER echo flips the provider-queued row to Sent with no is-queued left',
      sent.queued === false, sent);
  } finally {
    await reload();
  }
}
module.exports = { publicChatRendererContracts };
