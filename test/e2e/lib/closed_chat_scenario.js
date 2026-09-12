'use strict';
const WebSocket = require('ws');

// Control only the hermetic seed sessions. No fixture token enters the page.
async function fixtureRequest(runtime, streamId, message) {
  const token = runtime.fixtureTokens?.[streamId];
  if (!token || !runtime.fixtureDaemonPort) throw new Error('isolated fixture authority missing');
  const ws = new WebSocket(`ws://127.0.0.1:${runtime.fixtureDaemonPort}`);
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(new Error('fixture RPC timeout')), 10000);
    const finish = (error, value) => {
      clearTimeout(timer);
      ws.close();
      error ? reject(error) : resolve(value);
    };
    ws.on('error', finish);
    ws.on('open', () => ws.send(JSON.stringify({ type: 'hello', client: 'test', subscribe: { all: true } })));
    ws.on('message', raw => {
      const frame = JSON.parse(raw);
      if (frame.type === 'snapshot') ws.send(JSON.stringify({ ...message,
        request_id: 'gate-fixture-control', from_stream_id: streamId, stream_token: token }));
      if (frame.request_id === 'gate-fixture-control') finish(null, frame);
    });
  });
}

async function ask(runtime, streamId) {
  const questionId = `gate-${streamId}`;
  return fixtureRequest(runtime, streamId, {
    type: 'prompt.ask',
    envelope: { schema_version: 1, question_id: questionId, title: 'Gate question', body: 'Keep this question?',
      dedup_key: questionId, producer_stream_id: streamId, response_mode: 'single_choice',
      options: [{ label: 'Yes', value: 'yes' }, { label: 'No', value: 'no' }] },
    actions: [true, false].map((choice, index) => ({ kind: 'yes_no', action_id: `a${index}`,
      label: choice ? 'Yes' : 'No', choice,
      value: { schema_version: 1, question_id: questionId, answer: choice ? 'yes' : 'no' } })),
  });
}

async function closedChatSlot(ctx) {
  const { session, report, cdp, fixture, runtime, timeoutMs } = ctx;
  if (!fixture || !runtime.fixtureTokens) {
    report.note('no isolated fixture: skipping destructive fixture-close scenario');
    return;
  }
  const { waitForValue } = require('./web_scenarios');
  const survivor = 'local:web-gate-survivor';
  for (const streamId of [fixture.streamId, survivor]) {
    const result = await ask(runtime, streamId);
    report.ok(`durable fixture question created for ${streamId}`, result.ok === true,
      { type: result.type, state: result.question?.state });
  }
  await session.eval(`(() => {
    const names = ['createPty', 'killPty', 'chatClose', 'chatKill', 'chatInterrupt',
      'chatSend', 'chatSendCorrelated', 'chatDismissQuestion', 'notificationResolve'];
    const saved = Object.fromEntries(names.map(name => [name, window.cc[name]]));
    window.__closedGate = { saved, releases: [], forbidden: [] };
    window.cc.createPty = async () => '%unused-chat-fixture';
    window.cc.killPty = async slot => { window.__closedGate.releases.push(slot); return saved.killPty(slot); };
    for (const name of names.slice(2)) window.cc[name] = (...args) => {
      window.__closedGate.forbidden.push(name); return saved[name](...args);
    };
    return true;
  })()`);
  try {
    for (const [slot, streamId] of [fixture.streamId, survivor].entries()) {
      await session.eval(`window.focusStreamId(${JSON.stringify(streamId)})`);
      await waitForValue(session, cdp, `!!document.querySelector('#header-${slot} [data-mode="chat"]')`, Boolean, { timeoutMs });
      await session.eval(`document.querySelector('#header-${slot} [data-mode="chat"]').click()`);
      await waitForValue(session, cdp, `!!document.querySelector('#cell-${slot} .slot-chat-question-open')`, Boolean,
        { timeoutMs, label: `question affordance in slot ${slot}` });
    }
    await session.eval(`(() => {
      const input = document.querySelector('#cell-1 .slot-chat-compose-input');
      input.value = 'Preserve survivor draft'; input.dispatchEvent(new Event('input', { bubbles: true }));
      document.querySelector('#cell-0 .slot-chat-question-open').click();
      document.querySelector('#cell-1 .slot-chat-question-open').click();
      return true;
    })()`);
    await waitForValue(session, cdp, 'document.querySelectorAll(".desktop-question-portal").length', n => n === 2,
      { timeoutMs, label: 'both durable question portals open' });
    const closed = await fixtureRequest(runtime, fixture.streamId,
      { type: 'close', host: fixture.host, session_name: fixture.sessionName });
    report.ok('only target fixture self-close is accepted', closed.type === 'close.ok' && closed.ok === true);
    await waitForValue(session, cdp,
      `window.cc.getChatStreamState().then(s => !s.sessions.some(row => row.stream_id === ${JSON.stringify(fixture.streamId)}))`, Boolean,
      { timeoutMs, label: 'real closure inventory reaches browser bridge' });
    await cdp.sleep(1800);
    const observed = await session.eval(`(() => ({
      targetOccupied: document.querySelector('#cell-0').classList.contains('occupied'),
      label: document.querySelector('#header-0 .cell-label').textContent,
      targetPortal: !!document.getElementById('desktop-question-portal-${fixture.streamId.replace(/[^a-zA-Z0-9_-]/g, '-')}'),
      survivorOccupied: document.querySelector('#cell-1').classList.contains('occupied'),
      survivorPortal: !!document.querySelector('#desktop-question-portal-local-web-gate-survivor .slot-chat-question'),
      draft: document.querySelector('#cell-1 .slot-chat-compose-input')?.value,
      releases: window.__closedGate.releases, forbidden: window.__closedGate.forbidden
    }))()`);
    report.ok('closed chat frees its slot and local question portal after grace',
      !observed.targetOccupied && observed.label === 'Slot 1' && !observed.targetPortal, observed);
    report.ok('survivor slot, durable question portal and draft survive target retirement',
      observed.survivorOccupied && observed.survivorPortal && observed.draft === 'Preserve survivor draft', observed);
    report.ok('retirement releases exactly its local attachment without remote mutation',
      JSON.stringify(observed.releases) === '[0]' && observed.forbidden.length === 0, observed);
  } finally {
    await session.eval(`(() => { Object.assign(window.cc, window.__closedGate.saved); delete window.__closedGate; return true; })()`);
  }
}
module.exports = { closedChatSlot };
