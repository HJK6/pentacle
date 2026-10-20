'use strict';

// agent-chat-steer-controls: an agent chat that is working offers `Queue` |
// `Steer now` (default Queue) and a persistent Stop beside Send; the assistant
// composite chat offers neither. The hermetic daemon cannot make a seeded seat
// work, so a turn is entered through the page's own store: a send whose bridge
// never answers leaves the turn in flight, the state the controls key on. The
// real Send / Steer now / Stop controls then drive fake send and cancel bridges,
// which records exactly what the renderer would put on the wire. The Escape
// reaching a pane is covered by the live seat journey, not this hermetic gate.
const { WORKSPACE_KEY, waitFor, reloadWith } = require('./workspace_scenario');

const json = JSON.stringify;
// Composer controls of one grid cell, as the operator sees them.
const probe = cell => `(() => {
  const root = document.getElementById(${json(cell)});
  const visible = selector => { const el = root?.querySelector(selector); return !!el && !el.hidden && getComputedStyle(el).display !== 'none'; };
  const mode = root?.querySelector('.slot-chat-send-mode');
  return {
    compose: !!root?.querySelector('.slot-chat-compose'),
    mode: visible('.slot-chat-send-mode'), stop: visible('.slot-chat-compose-stop'), send: !!root?.querySelector('.slot-chat-compose-send'),
    sendDisabled: root?.querySelector('.slot-chat-compose-send')?.disabled ?? null,
    pressed: mode ? [...mode.querySelectorAll('[data-send-mode]')].filter(b => b.getAttribute('aria-pressed') === 'true').map(b => b.dataset.sendMode) : [],
    labels: mode ? [...mode.querySelectorAll('[data-send-mode]')].map(b => b.textContent.trim()) : [],
  };
})()`;

async function agentChatSteerControls(ctx) {
  const { session, report, cdp, fixture } = ctx;
  if (!fixture) { report.note('agent-chat-steer-controls: hermetic-only scenario skipped'); return; }
  const agent = fixture.streamId;
  const composite = 'local:web-gate-assistant';

  // A known four-pane workspace with nothing open, so cell numbers are deterministic.
  await reloadWith(ctx, { [WORKSPACE_KEY]: json({ paneCount: 4, activeSlot: 0 }) });
  await waitFor(ctx, 'typeof window.focusStreamId === "function" && !!window.PentacleChatStore', 'app and chat store ready');

  const openChat = async (streamId, cell) => {
    await waitFor(ctx, `window.focusStreamId(${json(streamId)}) === true`, `focus ${streamId}`);
    await waitFor(ctx, `document.getElementById(${json(cell)})?.classList.contains('occupied') === true`, `${streamId} occupies ${cell}`);
    await waitFor(ctx, `!!document.querySelector('#header-${cell.slice(5)} [data-mode="chat"]')`, 'chat toggle');
    await session.eval(`document.querySelector('#header-${cell.slice(5)} [data-mode="chat"]').click()`);
    await waitFor(ctx, `!!document.querySelector(${json('#' + cell + ' .slot-chat-compose-send')})`, `${cell} composer`);
  };

  await openChat(agent, 'cell-0');
  const idle = await session.eval(probe('cell-0'));
  report.ok('an idle agent chat shows neither the mode control nor Stop', idle.compose && idle.send && !idle.mode && !idle.stop, idle);

  await session.eval(`(() => { window.PentacleChatStore.setSendBridge(() => new Promise(() => {}));
    window.PentacleChatStore.sendTurn(${json(agent)}, 'working probe'); return true; })()`);
  await waitFor(ctx, `window.PentacleChatStore.getTurnPhase(${json(agent)}) !== 'idle'`, 'agent turn in flight');
  await session.eval(`(() => { const input = document.querySelector('#cell-0 .slot-chat-compose-input');
    input.value = 'unsent steer probe'; input.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
  await waitFor(ctx, `document.querySelector('#cell-0 .slot-chat-send-mode') && !document.querySelector('#cell-0 .slot-chat-send-mode').hidden`, 'mode control shown');
  const working = await session.eval(probe('cell-0'));
  report.ok('a working agent chat shows Queue | Steer now with Queue selected', working.mode && working.labels.join('|') === 'Queue|Steer now' && working.pressed.join() === 'queue', working);
  report.ok('Stop shows beside Send while a draft exists, and Send stays enabled', working.stop && working.send && working.sendDisabled === false, working);

  await session.eval(`document.querySelector('#cell-0 [data-send-mode="steer"]').click()`);
  const steer = await session.eval(probe('cell-0'));
  report.ok('choosing Steer now selects it', steer.pressed.join() === 'steer', steer);
  await session.eval(`document.querySelector('#cell-0 [data-send-mode="queue"]').click()`);
  const queue = await session.eval(probe('cell-0'));
  report.ok('choosing Queue selects it again', queue.pressed.join() === 'queue', queue);

  // The assistant composite never offers either control, even while a send is in flight.
  await session.eval(`window.focusStreamId(${json(composite)}), true`);
  const cell = await session.eval(`(() => { const c = [...document.querySelectorAll('.grid-cell')].find(c => c.querySelector('.cell-label')?.textContent.trim() === 'Assistant Fixture'); return c ? c.id : ''; })()`);
  report.ok('the composite chat opens in a pane', /^cell-\d$/.test(cell), { cell });
  await waitFor(ctx, `!!document.querySelector(${json('#' + cell + ' .slot-chat-compose-send')})`, 'composite composer');
  await session.eval(`window.PentacleChatStore.sendTurn(${json(composite)}, 'working probe'), true`);
  await cdp.sleep(500);
  const bart = await session.eval(probe(cell));
  report.ok('the composite chat shows neither the mode control nor Stop', bart.compose && !bart.mode && !bart.stop, bart);

  await controlsJourney(ctx, agent);

  // Drop the probe sends and stubbed bridges; later scenarios expect a clean page.
  await reloadWith(ctx, { [WORKSPACE_KEY]: json({ paneCount: 4, activeSlot: 0 }) });
}

// Real controls against fake bridges: Queue, Steer now, the shared Escape budget, Stop.
async function controlsJourney(ctx, agent) {
  const { session, report } = ctx;
  const cell = '#cell-0';
  await reloadWith(ctx, { [WORKSPACE_KEY]: json({ paneCount: 4, activeSlot: 0 }) });
  await waitFor(ctx, 'typeof window.focusStreamId === "function" && !!window.PentacleChatStore', 'app and chat store ready');
  await waitFor(ctx, `window.focusStreamId(${json(agent)}) === true`, 'focus agent');
  await waitFor(ctx, `document.getElementById('cell-0')?.classList.contains('occupied') === true`, 'agent in cell-0');
  await session.eval(`document.querySelector('#header-0 [data-mode="chat"]').click()`);
  await waitFor(ctx, `!!document.querySelector('${cell} .slot-chat-compose-send')`, 'composer');
  await session.eval(`(() => {
    window.__steerSends = []; window.__steerStops = [];
    const store = window.PentacleChatStore;
    store.setSendBridge(async args => { window.__steerSends.push({ text: args.text, urgent: args.urgent === true, hasUrgentKey: 'urgent' in args }); return { ok: true, provider_queued: args.text.startsWith('queued') }; });
    store.setCancelBridge(async args => { window.__steerStops.push(args.streamId); return { ok: true, interrupted: true, confirm: 'interrupted' }; });
    store.sendTurn(${json(agent)}, 'working probe');
    return true;
  })()`);
  await waitFor(ctx, `window.__steerSends.length === 1 && window.PentacleChatStore.getTurnPhase(${json(agent)}) !== 'idle'`, 'probe turn in flight');
  const draft = text => session.eval(`(() => { const input = document.querySelector('${cell} .slot-chat-compose-input');
    input.value = ${json(text)}; input.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
  const click = selector => session.eval(`document.querySelector(${json(cell + ' ' + selector)}).click(), true`);
  const sends = () => session.eval('window.__steerSends.slice(1)');
  const notice = () => session.eval(`(() => { const el = document.querySelector('${cell} .slot-chat-compose-notice'); return el && !el.hidden ? el.textContent : ''; })()`);

  await draft('queued first');
  await click('.slot-chat-compose-send');
  await waitFor(ctx, 'window.__steerSends.length === 2', 'queue send dispatched');
  report.ok('Queue sends exactly as before, without urgent', (await sends())[0].hasUrgentKey === false, await sends());

  await draft('steer now please');
  await click('[data-send-mode="steer"]');
  await click('.slot-chat-compose-send');
  await waitFor(ctx, 'window.__steerSends.length === 3', 'steer send dispatched');
  const afterSteer = await sends();
  report.ok('Steer now sends urgent: true', afterSteer[1].urgent === true, afterSteer);
  report.ok('the mode resets to Queue after each send', (await session.eval(probe(cell.slice(1)))).pressed.join() === 'queue', null);

  await draft('steer again');
  await session.eval(`document.querySelector('${cell} .slot-chat-compose-input').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', altKey: true, bubbles: true, cancelable: true })), true`);
  await waitFor(ctx, 'window.__steerSends.length === 4', 'Alt+Enter send dispatched');
  const second = await sends();
  report.ok('a second Steer now in the same turn goes out without urgent', second[2].hasUrgentKey === false, second);
  report.ok('and the composer says the steer did not take', (await notice()) === 'Steer did not take; queued instead', await notice());

  await draft('draft survives stop');
  await click('.slot-chat-compose-stop');
  await session.eval('new Promise(resolve => setTimeout(resolve, 300))', { awaitPromise: true });
  report.ok('Stop after Steer now spent the turn\'s Escape is a no-op', (await session.eval('window.__steerStops.length')) === 0, null);

  // Fresh turn: Stop with a draft interrupts, keeps the draft, and keeps queued rows queued.
  await reloadWith(ctx, { [WORKSPACE_KEY]: json({ paneCount: 4, activeSlot: 0 }) });
  await waitFor(ctx, `window.focusStreamId(${json(agent)}) === true`, 'refocus agent');
  await waitFor(ctx, `document.getElementById('cell-0')?.classList.contains('occupied') === true`, 'agent in cell-0 again');
  await session.eval(`document.querySelector('#header-0 [data-mode="chat"]').click()`);
  await waitFor(ctx, `!!document.querySelector('${cell} .slot-chat-compose-send')`, 'composer again');
  await session.eval(`(() => {
    window.__steerStops = [];
    const store = window.PentacleChatStore;
    store.setSendBridge(async args => ({ ok: true, provider_queued: args.text.startsWith('queued') }));
    store.setCancelBridge(async args => { window.__steerStops.push(args.streamId); return { ok: true, interrupted: true, confirm: 'interrupted' }; });
    store.sendTurn(${json(agent)}, 'working probe');
    store.sendTurn(${json(agent)}, 'queued behind the turn');
    return true;
  })()`);
  await waitFor(ctx, `window.PentacleChatStore.getTurnPhase(${json(agent)}) !== 'idle'`, 'second probe turn in flight');
  await draft('draft survives stop');
  await click('.slot-chat-compose-stop');
  await waitFor(ctx, 'window.__steerStops.length === 1', 'Stop sent the interrupt');
  const kept = await session.eval(`document.querySelector('${cell} .slot-chat-compose-input').value`);
  report.ok('Stop with a draft interrupts once and keeps the draft', kept === 'draft survives stop', kept);
  await waitFor(ctx, `(() => { const el = document.querySelector('${cell} .slot-chat-compose-notice'); return !!el && !el.hidden && el.textContent === 'Stopped. Queued messages run next'; })()`, 'stopped notice');
  const queuedRows = await session.eval(`[...document.querySelectorAll('${cell} .slot-chat-send-status.is-queued')].map(el => el.textContent.trim())`);
  report.ok('Stop says queued messages run next and the queued row stays queued', queuedRows.length === 1, { notice: await notice(), queuedRows });
}

module.exports = { agentChatSteerControls };
