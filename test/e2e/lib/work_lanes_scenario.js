'use strict';
const fixtureContract = require('../../../pentacle-chat-core/tests/fixtures/work-lanes-inventory.json');

// Web "work lanes" scenario (spec_pentacle__first_class_work_lanes_2026_10, M4).
//
// The real web bundle in real headless Chrome with the real renderer CSS. The lane
// frames are the daemon's frozen wire fixture (pentacle-chat-core/tests/fixtures/
// work-lanes-inventory.json), delivered through the renderer's own chat-stream
// frame listener; the seeded gate daemon serves the one open lane chat. A seeded
// daemon owns no closed chat, so the history tap proves the request shape and the
// honest failure state rather than a transcript (the transcript path is covered by
// test/work_lanes_web.test.js against fetched rows).
//
// Self-contained: it reloads the page at both ends so neither neighbour sees its state.
async function webWorkLanes(ctx) {
  const { session, report, cdp, timeoutMs, fixture } = ctx;
  if (!fixture) { report.note('no fixture: skipping the work-lanes scenario (observational run)'); return; }
  const { waitForValue } = require('./web_scenarios');
  const hostLocal = fixture.streamId; // local:web-gate-1, an open default-visibility session

  const hook = await session.send('Page.addScriptToEvaluateOnNewDocument', { source: `
    (() => { let cc;
      Object.defineProperty(window, 'cc', { configurable: true, get: () => cc, set: (value) => {
        const original = value.onChatStreamFrame;
        value.onChatStreamFrame = (callback) => { window.__laneFrameSink = callback; return original.call(value, callback); };
        const read = value.requestStreamEvents;
        window.__laneReads = [];
        value.requestStreamEvents = (args) => { window.__laneReads.push(args); return read.call(value, args); };
        cc = value; } });
    })();` });
  // Reload and wait for the NEW document: the old one answers readiness probes until it unloads.
  const reloadFresh = async () => {
    await session.eval('window.__staleDocument = true, true');
    await session.send('Page.reload', {});
    await waitForValue(session, cdp, 'window.__staleDocument === undefined && !!window.cc && typeof window.focusStreamId === "function" && !!document.getElementById("session-list")',
      (v) => v === true, { timeoutMs, label: 'fresh document' });
  };
  const reload = async () => {
    await reloadFresh();
    await waitForValue(session, cdp, 'typeof window.focusStreamId === "function" && !!document.getElementById("session-list") && typeof window.__laneFrameSink === "function"',
      (v) => v === true, { timeoutMs, label: 'app ready with the lane frame sink' });
    await waitForValue(session, cdp, 'window.cc.getChatStreamState().then((s) => s.connected === true)', (v) => v === true,
      { timeoutMs, label: 'daemon connected' });
    await waitForValue(session, cdp, `window.cc.getChatStreamState().then((s) => (s.sessions || []).some((x) => x.stream_id === ${JSON.stringify(hostLocal)}))`,
      (v) => v === true, { timeoutMs, label: 'fixture session in inventory' });
  };

  // Frames reach the renderer stamped with the host's connection state version and are ordered by it,
  // so injected frames carry the live version (read fresh each time; real daemon frames also bump it).
  const inject = (frame, delta = 0) => session.eval(`window.cc.getChatStreamState().then((s) =>
    window.__laneFrameSink(Object.assign(${JSON.stringify(frame)}, { state_version: s.state_version + (${delta}) })), true)`);

  const frame = JSON.parse(JSON.stringify(fixtureContract.inventory_frame));
  frame.lanes.find((lane) => lane.lane_id === 'wl-blocked-0001').visible_chat.stream_id = hostLocal;

  let viewHook = null;
  try {
    // Default: the sidebar lanes view is off. A lane inventory changes neither the header nor the session list.
    await reload();
    const sidebarShape = `(() => ({ stats: document.getElementById('stats').textContent,
      rows: [...document.querySelectorAll('#session-list .session-item')].map((el) => el.dataset.name),
      labels: [...document.querySelectorAll('#session-list .sidebar-group-label')].map((el) => el.textContent),
      lanes: document.querySelectorAll('#session-list .lane-row, #session-list .lanes-label, #session-list .lanes-truncated').length }))()`;
    const offBefore = await waitForValue(session, cdp, sidebarShape, (v) => /^\d+ sessions \|/.test(v.stats) && v.rows.length > 0,
      { timeoutMs, label: 'session-only header with the lanes view off' });
    await inject(frame);
    await cdp.sleep(500);
    const offAfter = await session.eval(sidebarShape);
    report.ok('by default a lane inventory adds no lane count, no "Lanes (N)" section and no lane rows',
      offAfter.lanes === 0 && !/lanes/i.test(offAfter.stats) && !offAfter.labels.some((label) => /lanes/i.test(label)), { offBefore, offAfter });
    report.ok('by default the session rows and tier labels are unchanged by a lane inventory',
      JSON.stringify(offAfter.rows) === JSON.stringify(offBefore.rows) && JSON.stringify(offAfter.labels) === JSON.stringify(offBefore.labels),
      { offBefore, offAfter });

    // The rest covers the view switched on, as the later rebuild will find it.
    viewHook = await session.send('Page.addScriptToEvaluateOnNewDocument', { source: 'window.__PENTACLE_WORK_LANES_SIDEBAR__ = true;' });
    await reload();
    // Pane attach is irrelevant here; keep the ordinary-session open path off the real tmux.
    await session.eval(`(() => { window.cc.createPty = async () => '%unused-work-lanes-fixture'; window.cc.killPty = async () => true; return true; })()`);

    // The seeded gate daemon is lane-aware (work_lanes_v1) and owns no lanes, so before any injected frame the
    // header already leads with the daemon's own empty inventory: "0 lanes | N sessions …", and no lane rows.
    const before = await waitForValue(session, cdp, `document.getElementById('stats').textContent`, (v) => /^0 lanes \| \d+ sessions/.test(v),
      { timeoutMs, label: 'header leads with the daemon\'s empty lane inventory' });
    const rowsBefore = await session.eval(`document.querySelectorAll('#session-list .lane-row').length`);
    report.ok('before any injected frame the header shows the daemon\'s own empty inventory (0 lanes) and no lane rows',
      /^0 lanes \| \d+ sessions/.test(before) && rowsBefore === 0, { before, rowsBefore });

    await inject(frame);
    const stats = await waitForValue(session, cdp, `document.getElementById('stats').textContent`, (v) => /^4 lanes \|/.test(v),
      { timeoutMs, label: 'stats line leads with the open-lane count' });
    report.ok('the header leads with counts.open (4 lanes), not the session count', /^4 lanes \| \d+ sessions/.test(stats), { stats });

    const rows = await session.eval(`[...document.querySelectorAll('#session-list .lane-row')].map((el) => ({
      id: el.dataset.laneId, state: el.dataset.laneState, action: el.dataset.tapAction,
      stale: !!el.querySelector('.lane-eta.is-stale'), badge: el.querySelector('.lane-state').textContent }))`);
    report.ok('the Lanes panel lists lanes in the daemon order', JSON.stringify(rows.map((r) => r.id)) === JSON.stringify(fixtureContract.expected.order), { rows });
    report.ok('only ACTIVE/PAUSED/BLOCKED badges are shown and no done lane appears',
      rows.every((r) => ['ACTIVE', 'PAUSED', 'BLOCKED'].includes(r.badge)), { rows });
    report.ok('stale ETAs read "ETA stale" for exactly the daemon-flagged lanes',
      rows.every((r) => r.stale === fixtureContract.expected.eta_stale[r.id]), { rows });
    const labelFirst = await session.eval(`(() => { const list = document.getElementById('session-list');
      const label = list.querySelector('.lanes-label'); return !!label && list.firstElementChild === label && label.textContent; })()`);
    report.ok('the "Lanes (N)" section sits above the session tiers', labelFirst === 'Lanes (4)', { labelFirst });
    const visible = await session.eval(`(() => { const row = document.querySelector('#session-list .lane-row');
      const box = row.getBoundingClientRect(); return { width: box.width, height: box.height, color: getComputedStyle(row.querySelector('.lane-state')).color }; })()`);
    report.ok('lane rows are laid out with the real stylesheet', visible.width > 100 && visible.height > 10, visible);

    // open: the seeded session opens in a slot.
    await session.eval(`document.querySelector('#session-list .lane-row[data-lane-id="wl-blocked-0001"]').click(), true`);
    const opened = await waitForValue(session, cdp, `(() => { const label = document.querySelector('#cell-0 .cell-label');
      return { occupied: document.getElementById('cell-0').classList.contains('occupied'), label: label && label.textContent,
        lane: document.getElementById('cell-0').classList.contains('lane-history') }; })()`,
      (v) => v.occupied, { timeoutMs, label: 'open lane chat attached' });
    report.ok('tapping an open lane attaches its chat, not a lane-history slot', opened.occupied && !opened.lane, opened);

    // history: read-only slot, request carries the lane generation, composer hidden by the real CSS.
    await session.eval(`document.querySelector('#session-list .lane-row[data-lane-id="wl-paused-0003"]').click(), true`);
    const history = await waitForValue(session, cdp, `(() => { const cell = document.querySelector('.grid-cell.lane-history');
      if (!cell) return null; const compose = cell.querySelector('.slot-chat-compose');
      return { banner: (cell.querySelector('.lane-history-banner') || {}).textContent || '', composeDisplay: compose ? getComputedStyle(compose).display : 'absent',
        reads: window.__laneReads.map((r) => ({ streamId: r.streamId, generation: r.generation })) }; })()`,
      (v) => v && v.reads.length > 0, { timeoutMs, label: 'history slot requested its rows' });
    report.ok('a history lane opens a read-only slot', /Read-only history/.test(history.banner), history);
    const laneReads = history.reads.filter((r) => r.streamId === 'fixture-host:v2-lead0003');
    report.ok('the history request names the lane generation',
      laneReads.length > 0 && laneReads.every((r) => r.generation === 'gen-lead-0003'), history);
    report.ok('the open lane chat is read without a generation (it is a live chat)',
      history.reads.filter((r) => r.streamId === hostLocal).every((r) => r.generation === undefined), history);
    report.ok('the composer is not shown in a history slot', history.composeDisplay === 'none' || history.composeDisplay === 'absent', history);

    // unavailable: explicit card, nothing else opened, no read.
    await session.eval(`document.querySelector('#session-list .lane-row[data-lane-id="wl-paused-0004"]').click(), true`);
    const unavailable = await waitForValue(session, cdp, `(() => { const banner = document.querySelector('.lane-history-banner.is-unavailable');
      return banner ? { text: banner.textContent, reads: window.__laneReads.filter((r) => r.streamId === 'fixture-host:v2-lead0004').length,
        stillListed: !!document.querySelector('#session-list .lane-row[data-lane-id="wl-paused-0004"]') } : null; })()`,
      Boolean, { timeoutMs, label: 'unavailable card' });
    report.ok('an unavailable lane shows "Chat unavailable", reads nothing and stays listed',
      /Chat unavailable/.test(unavailable.text) && unavailable.reads === 0 && unavailable.stillListed, unavailable);

    // done: a completed lane leaves the header on the next frame.
    const done = JSON.parse(JSON.stringify(frame));
    done.lanes = done.lanes.filter((lane) => lane.lane_id !== 'wl-active-0002');
    done.counts = { open: 3, active: 0, paused: 2, blocked: 1 };
    await inject(done, -1);
    await cdp.sleep(500);
    const ignored = await session.eval(`document.getElementById('stats').textContent`);
    report.ok('a lane push older than the connection state version is ignored', /^4 lanes \|/.test(ignored), { ignored });
    await inject(done);
    const after = await waitForValue(session, cdp, `document.getElementById('stats').textContent`, (v) => /^3 lanes \|/.test(v),
      { timeoutMs, label: 'completed lane leaves the header' });
    report.ok('a completed lane leaves the header count and panel',
      /^3 lanes/.test(after) && await session.eval(`!document.querySelector('#session-list .lane-row[data-lane-id="wl-active-0002"]')`), { after });

    // lane_update cards in Bart's timeline (composite stream), rendered by the real view.
    const cards = await session.eval(`(() => {
      const store = window.PentacleChatStore;
      store.applyFrame({ type: 'snapshot', connected: true, events: [], sessions: [{ stream_id: 'bart:assistant', host: 'bart',
        provider: 'composite', session_name: 'assistant', session_kind: 'assistant_composite', visibility: 'visible',
        last_event_at: '2026-10-07T19:00:00.000Z' }] });
      for (const wire of ${JSON.stringify(fixtureContract.lane_update_events)}) store.applyFrame(wire);
      const container = document.createElement('div'); document.body.appendChild(container);
      try {
        window.PentacleChatView.renderStreamTranscript('bart:assistant', container);
        return { kinds: [...container.querySelectorAll('.slot-chat-lane-update')].map((el) => el.dataset.updateKind).sort(),
          prose: container.querySelectorAll('.slot-chat-assistant-card').length };
      } finally { container.remove(); }
    })()`);
    report.ok('all six lane_update kinds render as typed cards in Bart\'s timeline, none as prose',
      JSON.stringify(cards.kinds) === JSON.stringify(['lane_blocked', 'lane_completed', 'lane_started', 'lane_unblocked', 'major_decision', 'milestone']) && cards.prose === 0, cards);
  } finally {
    await session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier: hook.identifier }).catch(() => {});
    if (viewHook) await session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier: viewHook.identifier }).catch(() => {});
    await reloadFresh();
    await waitForValue(session, cdp, 'window.cc.getChatStreamState().then((s) => s.connected === true)', (v) => v === true,
      { timeoutMs, label: 'daemon connected after work-lanes cleanup' });
  }
}

module.exports = { webWorkLanes };
