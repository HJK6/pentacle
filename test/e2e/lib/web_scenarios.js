'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { closedChatSlot } = require('./closed_chat_scenario');
const { runGridSplit } = require('./grid_split_scenario');

// Named web-mode E2E scenario functions, driven over CDP against the served
// page (window.cc over the websocket) and a chat-stream-v2 daemon. web_gate.js
// runs them against a hermetic, SEEDED loopback daemon so every assertion is
// deterministic; the same functions can run against any daemon for a by-hand
// check (pass `fixture: null` to fall back to observational, non-fatal checks).
//
// Each scenario is `async (ctx) => {}` and throws on failure (ctx.report.ok
// throws). ctx carries: { session, report, cdp, url, timeoutMs, tmux, fixture,
// runtime }. `fixture` (when set) = { streamId, host, sessionName, transcript:
// [{kind,text}] } — the rows web_gate seeded into the daemon before boot.

// ── shared CDP polling helpers ──────────────────────────────────────────────
// cdp.js's waitFor only distinguishes truthy/falsy; these poll the evaluated
// value against a predicate (a non-empty list, a string containing a token).
async function waitForValue(session, cdp, expression, predicate,
  { timeoutMs = 30000, intervalMs = 300, label = expression } = {}) {
  const deadline = Date.now() + timeoutMs;
  let last;
  for (;;) {
    try { last = await session.eval(`(async () => (${expression}))()`); } catch { last = undefined; }
    if (predicate(last)) return last;
    if (Date.now() > deadline) {
      throw new Error(`timed out waiting for ${label} (last: ${JSON.stringify(last)?.slice(0, 300)})`);
    }
    await cdp.sleep(intervalMs);
  }
}

async function pollUntil(read, predicate, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  let last;
  for (;;) {
    last = read();
    if (predicate(last)) return last;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${label} (last: ${JSON.stringify(last)})`);
    await new Promise((r) => setTimeout(r, 200));
  }
}

// ── the scenarios ───────────────────────────────────────────────────────────

// The page installs the bridge and the host-injected config matches HTTP.
async function transportAndConfig(ctx) {
  const { session, report, cdp, url, timeoutMs } = ctx;
  await waitForValue(session, cdp, '!!(window.cc && window.HOST)', (v) => v === true,
    { timeoutMs, label: 'window.cc installed' });
  report.ok('window.cc and window.HOST install before the renderer runs', true);

  const injected = await session.eval('({ hostname: window.HOST.hostname, platform: window.HOST.platform, hasConfig: !!window.__PENTACLE_CONFIG__ })');
  report.ok('the host injected the computed config', injected.hasConfig && !!injected.hostname, injected);

  const viaCc = await session.eval('window.cc.getConfig().then(c => ({ appName: c.appName, hostIds: c.hostIds, localHostId: c.localHostId }))', { awaitPromise: true });
  const viaHttp = await fetch(`${url}api/config`).then((r) => r.json());
  report.ok('getConfig() over the websocket matches GET /api/config',
    viaCc.appName === viaHttp.appName && JSON.stringify(viaCc.hostIds) === JSON.stringify(viaHttp.hostIds),
    { viaCc, viaHttp });
}

// The sidebar renders from the daemon inventory. With a fixture the seeded
// session MUST appear (deterministic); without one, it is observational.
async function sidebarFromInventory(ctx) {
  const { session, report, cdp, timeoutMs, fixture } = ctx;
  await waitForValue(session, cdp, '!!document.getElementById("session-list")', (v) => v === true,
    { timeoutMs, label: 'sidebar mounted' });
  const streamState = await waitForValue(session, cdp,
    'window.cc.getChatStreamState()', (s) => s && s.connected === true,
    { timeoutMs, label: 'daemon connection over the websocket' });
  report.ok('the daemon connection is established over the websocket', streamState.connected === true,
    { sessions: (streamState.sessions || []).length });

  if (!fixture) {
    report.note('no fixture: skipping the deterministic sidebar-row assertion (observational run)');
    return;
  }

  // The hello snapshot can arrive marked connected before the daemon's inventory
  // refresh has run, so poll for the seeded session rather than asserting on the
  // first connected state (a CI-scheduling race).
  const inState = await waitForValue(session, cdp,
    `window.cc.getChatStreamState().then(s => (s.sessions||[]).some(x => x.stream_id === ${JSON.stringify(fixture.streamId)}))`,
    (v) => v === true,
    { timeoutMs, label: 'seeded session appears in the daemon inventory' });
  report.ok('the seeded session is present in the daemon inventory', inState === true,
    { want: fixture.streamId });

  const row = await waitForValue(session, cdp,
    `(() => { const el = document.querySelector('#session-list .session-item[data-stream-id="${fixture.streamId}"]'); return el ? { name: el.dataset.name, host: el.dataset.host, streamId: el.dataset.streamId } : null; })()`,
    (r) => r && r.streamId === fixture.streamId,
    { timeoutMs, label: 'seeded sidebar row rendered' });
  report.ok('the sidebar renders the seeded session row from the live daemon',
    row && row.streamId === fixture.streamId, { row });
}

// A local tmux slot: attach, type (round trip), resize, kill — session survives.
async function slotAttachTypeResizeKill(ctx) {
  const { session, report, cdp, timeoutMs, tmux, runtime } = ctx;
  const sessionName = `ptest-web-${process.pid}-${Date.now().toString(36)}`;
  runtime.tmuxSession = sessionName;
  tmux(['new-session', '-d', '-s', sessionName, 'sh', '-c', 'stty raw -echo; exec cat']);
  report.ok('local ptest tmux session created', tmux(['has-session', '-t', `=${sessionName}`]) === '');

  const known = await session.eval(`window.cc.checkSession(${JSON.stringify(sessionName)}, 'local')`, { awaitPromise: true });
  report.ok('checkSession sees the local session over the websocket', known === true, { known });

  await session.eval(`(() => {
    window.__gate = { data: '', exits: [] };
    window.cc.onPtyData((slot, data) => { if (slot === 0) window.__gate.data += data; });
    window.cc.onPtyExit((slot, code) => window.__gate.exits.push([slot, code]));
    return true;
  })()`);
  const paneId = await session.eval(`window.cc.createPty(0, ${JSON.stringify(sessionName)}, 'local', 80, 24)`, { awaitPromise: true });
  report.ok('createPty attaches the slot and returns a pane id', /^%\d+$/.test(String(paneId)), { paneId });

  await session.eval("window.cc.writePty(0, 'echo WEBOK\\r'), true");
  const buffer = await waitForValue(session, cdp, 'window.__gate.data', (d) => String(d).includes('WEBOK'),
    { timeoutMs, label: 'WEBOK echoed back to the browser' });
  report.ok('typing round-trips browser -> host -> tmux -> browser', String(buffer).includes('WEBOK'));

  const widthBefore = Number(tmux(['display', '-p', '-t', String(paneId), '#{pane_width}']));
  await session.eval('window.cc.resizePty(0, 120, 40), true');
  const widthAfter = await pollUntil(
    () => Number(tmux(['display', '-p', '-t', String(paneId), '#{pane_width}'])),
    (w) => w === 120, timeoutMs, 'tmux pane resized');
  report.ok('resizePty resizes the real tmux pane', widthAfter === 120 && widthBefore !== widthAfter, { widthBefore, widthAfter });

  const clientsBefore = tmux(['list-clients', '-t', `=${sessionName}`, '-F', 'x']).split('\n').filter(Boolean).length;
  await session.eval('window.cc.killPty(0)', { awaitPromise: true });
  const clientsAfter = await pollUntil(
    () => tmux(['list-clients', '-t', `=${sessionName}`, '-F', 'x']).split('\n').filter(Boolean).length,
    (n) => n === 0, timeoutMs, 'the tmux client to detach');
  const sessionAlive = (() => { try { tmux(['has-session', '-t', `=${sessionName}`]); return true; } catch { return false; } })();
  report.ok('killPty releases the attach without killing the tmux session',
    clientsBefore > 0 && clientsAfter === 0 && sessionAlive, { clientsBefore, clientsAfter, sessionAlive });
}

// The chat transcript loads and paints for the seeded session.
async function chatTranscriptPaint(ctx) {
  const { session, report, cdp, timeoutMs, fixture } = ctx;
  if (!fixture) { report.note('no fixture: skipping the transcript assertion (observational run)'); return; }

  const receipt = await session.eval(
    `window.cc.requestStreamEvents({ streamId: ${JSON.stringify(fixture.streamId)}, limit: 20 })`,
    { awaitPromise: true });
  report.ok('the seeded transcript events load over the websocket', receipt && receipt.ok && Number(receipt.count) > 0,
    { streamId: fixture.streamId, count: receipt && receipt.count });

  const wantText = (fixture.transcript && fixture.transcript[0] && fixture.transcript[0].text) || '';
  const painted = await waitForValue(session, cdp, `(() => {
    try {
      const container = document.getElementById('web-gate-transcript') || (() => {
        const el = document.createElement('div'); el.id = 'web-gate-transcript'; document.body.appendChild(el); return el;
      })();
      const detail = window.PentacleChatView.renderStreamTranscript(${JSON.stringify(fixture.streamId)}, container);
      return { items: (detail && detail.transcriptItems || []).length, text: (container.innerText || '').trim() };
    } catch (e) { return { error: String(e && e.message || e) }; }
  })()`, (v) => v && v.items > 0 && v.text.length > 0,
    { timeoutMs, label: 'chat transcript painted' });
  const textOk = !wantText || painted.text.includes(wantText);
  report.ok('the chat transcript paints the seeded events for the session',
    painted.items > 0 && painted.text.length > 0 && textOk,
    { items: painted.items, chars: painted.text.length, containsSeededText: textOk });
}

// A web host restart mid-session must restore full interaction WITHOUT a manual
// reload: the frozen (degraded) chat input re-enables and the reconnected socket
// serves the live daemon inventory. Regression guard for
// spec_pentacle__web_reconnect_input_frozen_2026_09.
//
// The failing journey (operator report 2026-09-12): a chat-stream drop latches
// the live app into connected:false (degraded / input frozen); the operator
// restarts the web host; the page's /cc websocket reconnects to a FRESH host
// process, but that host already completed its daemon handshake before the
// browser reconnected — so its connected:true frame was broadcast to nobody, and
// no snapshot is pushed on connect. Without a reconnect re-sync the browser
// never learns it is connected again: state.chatStream.connected stays false,
// chatControlTargetForSlot returns {error:'Chat stream offline.'}, and the input
// stays frozen until a manual reload (which re-pulls getChatStreamState).
//
// Distinct from reconnect_survival, which cycles the host↔daemon link via
// forceReconnect while the browser↔host websocket stays up (the host itself
// pushes connected:false→true). Here the browser↔host websocket is what drops,
// exactly as `pentacle-web-start stop && pentacle-web-start` does.
async function hostRestartRestoresInput(ctx) {
  const { session, report, cdp, timeoutMs, fixture, stopHost, startHostSamePort, killDaemon, startDaemonSamePort } = ctx;
  if (!fixture) { report.note('no fixture: skipping host-restart scenario (observational run)'); return; }
  if (![stopHost, startHostSamePort, killDaemon, startDaemonSamePort].every((f) => typeof f === 'function')) {
    report.note('host/daemon lifecycle primitives unavailable: skipping host-restart scenario'); return;
  }
  const degraded = 'document.body.classList.contains("chat-stream-degraded")';

  await waitForValue(session, cdp, 'window.cc.getChatStreamState().then((s) => s.connected === true)', (v) => v === true,
    { timeoutMs, label: 'connected before the drop' });
  await waitForValue(session, cdp, degraded, (v) => v === false, { timeoutMs, label: 'not degraded before the drop' });

  // A chat-stream drop latches the live app into connected:false — the frozen
  // input the operator reported.
  await killDaemon();
  await waitForValue(session, cdp, degraded, (v) => v === true, { timeoutMs, label: 'input frozen (degraded) after the chat-stream drop' });
  report.ok('the live app freezes (degraded) when the chat-stream connection drops', true);

  // Restart the web host PROCESS with the daemon brought up FIRST, so the fresh
  // host completes its daemon handshake and broadcasts connected:true BEFORE the
  // browser's websocket reconnects — i.e. the recovery frame reaches nobody. The
  // page stays loaded throughout; only its /cc websocket drops and reconnects.
  //
  // This makes the walk RED without the fix on the overwhelming majority of runs:
  // the daemon is already up, so host2's handshake broadcast fires within ms of
  // it listening, while the browser sits in a backoff grown near its 5s cap. A
  // narrow residual window remains (host2 accepts sockets between server.listen()
  // and the async handshake resolving), so a browser reconnect landing inside it
  // could catch the live broadcast and recover without the fix — a rare potential
  // false GREEN, never a false RED. The DETERMINISTIC guard for the underlying
  // logic is test/chat_stream_connection_state.test.js (version-reset RED/GREEN);
  // this e2e is the integration belt on top of it.
  await stopHost();
  await cdp.sleep(8000); // let the browser's reconnect backoff grow toward its 5s cap
  await startDaemonSamePort();
  const newHostPort = await startHostSamePort();
  report.note(`daemon-then-host back up on 127.0.0.1:${newHostPort}; browser has not reconnected yet`);

  // THE FIX: on websocket reconnect the browser re-pulls the chat-stream snapshot
  // (resetting its connection-state version baseline first, since the fresh
  // host's state_version namespace restarts), so it learns it is connected again
  // and degraded mode clears — WITHOUT a reload. Before the fix the input stays
  // frozen until a manual reload.
  const recovered = await waitForValue(session, cdp, degraded, (v) => v === false,
    { timeoutMs, label: 'input restored (not degraded) after host restart, NO reload' });
  report.ok('a host restart restores interaction without a manual reload', recovered === false);

  // And the reconnected socket serves the live daemon inventory again (re-sync).
  // Assert connected + a non-empty inventory rather than a specific stream_id:
  // earlier scenarios (e.g. closed-chat-slot) may retire the seeded fixture, so
  // the seeded session is not guaranteed to survive to this last walk — but the
  // daemon always has at least one session and the reconnected socket must serve
  // it.
  const invOk = await waitForValue(session, cdp,
    'window.cc.getChatStreamState().then((s) => s.connected === true && Array.isArray(s.sessions) && s.sessions.length >= 1)',
    (v) => v === true, { timeoutMs, label: 'daemon inventory re-synced over the reconnected socket' });
  report.ok('the reconnected socket re-syncs the daemon inventory', invOk === true);
}

// A browser<->host /cc websocket reconnect (the HOST stays up) must restore the
// WHOLE slot without a close/reopen: the terminal PTY attachment re-binds and an
// open chat transcript reloads. Regression guard for
// spec_pentacle__web_chat_view_stale_freeze_2026_09.
//
// The failing journey (operator report 2026-09-12): a chat/terminal slot left
// open freezes — terminal output stops and input dies (and a chat transcript
// blanks) — while the host stays up (NOT the degraded/host-restart path). The
// browser's /cc socket had dropped (a backgrounded tab's suspended socket) and
// reconnected; on reconnect the host had already torn down this connection's
// PTY attachments (ws_bridge removeSocket -> sender.destroy) and the renderer
// never re-attached, and the resync snapshot can wipe an open transcript. Only
// close/reopen recovered it. The fix re-attaches terminals and re-backfills chat
// on the web onReconnect. Distinct from host-restart-restores-input, where the
// host process itself restarts; here the host and daemon stay up and only the
// browser socket cycles.
async function slotSurvivesCcReconnect(ctx) {
  const { session, report, cdp, timeoutMs, tmux, runtime, fixture } = ctx;
  if (!fixture) { report.note('no fixture: skipping slot-survives-cc-reconnect (observational run)'); return; }
  const sid = fixture.streamId;               // local:web-gate-1
  const sessionName = fixture.sessionName || 'web-gate-1';
  const marker = (tag) => path.join(os.tmpdir(), `pentacle-gate-${process.pid}-${tag}-${Date.now().toString(36)}`);
  const preFile = marker('pre');
  const postFile = marker('post');
  let slot = -1;

  try {
    // Capture the /cc WebSocket so the test can drop it while the host stays up,
    // then reload so the hook is active for the real socket.
    await session.send('Page.addScriptToEvaluateOnNewDocument', { source: `
      (() => { const N = window.WebSocket;
        window.WebSocket = function(u, p){ const w = p===undefined ? new N(u) : new N(u, p);
          try { if (String(u).endsWith('/cc')) window.__ccSocket = w; } catch(e){} return w; };
        window.WebSocket.prototype = N.prototype;
        window.WebSocket.CONNECTING=N.CONNECTING; window.WebSocket.OPEN=N.OPEN; window.WebSocket.CLOSING=N.CLOSING; window.WebSocket.CLOSED=N.CLOSED;
      })();` });
    await session.send('Page.reload', {});
    await waitForValue(session, cdp, 'typeof window.focusStreamId === "function" && !!document.getElementById("session-list")',
      (v) => v === true, { timeoutMs, label: 'app ready after reload' });
    await waitForValue(session, cdp, 'window.cc.getChatStreamState().then((s)=>s.connected===true)', (v) => v === true,
      { timeoutMs, label: 'connected after reload' });
    await waitForValue(session, cdp, `window.cc.getChatStreamState().then(s => (s.sessions||[]).some(x => x.stream_id === ${JSON.stringify(sid)}))`,
      (v) => v === true, { timeoutMs, label: 'fixture in inventory' });
    await waitForValue(session, cdp, '!!window.__ccSocket', Boolean, { timeoutMs, label: '/cc socket captured' });

    // A real tmux session the fixture's terminal can attach to (createPty maps the
    // chat-stream session_name -> the local tmux session of the same name). A plain
    // shell runs a typed command.
    runtime.freezeTmux = sessionName;
    try { tmux(['kill-session', '-t', `=${sessionName}`], { stdio: 'ignore' }); } catch {}
    tmux(['new-session', '-d', '-s', sessionName, 'sh']);

    const clients = () => tmux(['list-clients', '-t', `=${sessionName}`, '-F', 'x']).split('\n').filter(Boolean).length;
    // Deterministic input-liveness probe independent of pane rendering: type a
    // command that touches a unique host file, then check the file exists. If the
    // slot's PTY attachment is live the shell runs it; if the host has no
    // attachment for this (reconnected) socket the write is dropped and no file
    // appears.
    const typeTouch = (s, file) => session.eval(`window.cc.writePty(${s}, ${JSON.stringify('touch ' + file + '\r')}), true`);
    const fileAppears = (file) => pollUntil(() => fs.existsSync(file), Boolean, timeoutMs, `host file ${path.basename(file)} created via typed input`).catch(() => false);

    // Attach the fixture to a slot through the app (defaults to the terminal view).
    await session.eval(`window.focusStreamId(${JSON.stringify(sid)})`);
    slot = await waitForValue(session, cdp,
      `(() => { for (let i=0;i<4;i++){ const c=document.getElementById('cell-'+i); if (c && c.querySelector('.xterm')) return i; } return -1; })()`,
      (v) => typeof v === 'number' && v >= 0, { timeoutMs, label: 'terminal attached to a slot' });
    const attachedClients = await pollUntil(clients, (n) => n >= 1, timeoutMs, 'host attached a tmux client');
    report.ok('a slot terminal attaches through the app', attachedClients >= 1, { slot, clients: attachedClients });

    // Baseline: typed input reaches the shell before the drop.
    await typeTouch(slot, preFile);
    report.ok('the slot terminal accepts input before the drop', (await fileAppears(preFile)) === true, { preFile });

    // Switch the SAME slot to its chat view and backfill the transcript, so the one
    // reconnect exercises both halves of the slot (a real slot has both a terminal
    // and a chat).
    await waitForValue(session, cdp, `!!document.querySelector('#header-${slot} [data-mode="chat"]')`, Boolean, { timeoutMs, label: 'chat toggle present' });
    await session.eval(`document.querySelector('#header-${slot} [data-mode="chat"]').click()`);
    await session.eval(`window.cc.requestStreamEvents({ streamId: ${JSON.stringify(sid)}, limit: 20 })`, { awaitPromise: true });
    const chatText = () => session.eval(`(document.querySelector('#cell-${slot} .slot-chat-list')?.innerText||'')`);
    // The composer's Send target: enabled (not disabled) means chatControlTargetForSlot
    // resolved the stream's session detail — i.e. the composer is usable, not the
    // "Waiting for websocket session detail." degraded state. Non-side-effecting read.
    const sendEnabled = () => session.eval(`(() => { const b = document.querySelector('#cell-${slot} .slot-chat-compose-send'); return !!b && b.disabled === false; })()`);
    await waitForValue(session, cdp, `(document.querySelector('#cell-${slot} .slot-chat-list')?.innerText||'').includes('fixture assistant reply')`,
      Boolean, { timeoutMs, label: 'chat transcript painted before the drop' });
    const sendBefore = await sendEnabled();
    report.ok('the slot chat transcript paints and the composer is usable before the drop', sendBefore === true, { sendBefore });

    // Model Bart's PRODUCTION summary daemon on the reconnect re-pull: the hello
    // snapshot carries no events (events_mode:'summary') AND the open stream is
    // momentarily ABSENT from the resync session inventory (a filtered/nested/remote
    // session, or a post-daemon-reconnect inventory gap). That is the exact shape
    // that evicts the open transcript. The gate's local daemon otherwise keeps its
    // ring + inventory populated, so shim just the get-state re-pull. Save the
    // original on a holder so cleanup can restore it deterministically.
    await session.eval(`(() => {
      window.__origGetChatStreamState = window.cc.getChatStreamState.bind(window.cc);
      window.cc.getChatStreamState = () => window.__origGetChatStreamState().then(s => ({
        ...s, events: [], sessions: (s.sessions||[]).filter(x => x.stream_id !== ${JSON.stringify(sid)}),
      }));
      return true;
    })()`);

    // Drop ONLY the browser<->host socket. The host and daemon stay up; the
    // transport reconnects and fires onReconnect.
    await session.eval('(() => { try { window.__ccSocket && window.__ccSocket.close(); } catch(e){} return true; })()');
    await waitForValue(session, cdp, '(() => { const s = window.__ccSocket; return !!s && s.readyState === 1; })()',
      (v) => v === true, { timeoutMs, label: '/cc socket reconnected' });

    // CHAT half — THE FIX: onReconnect carries the open slot's session forward into
    // the resync snapshot, so the reducer never evicts the transcript OR the
    // composer's session detail. Before the fix the summary snapshot drops the
    // stream and the transcript blanks to "Loading chat…" with a disabled send
    // (RED). Assert the transcript is still shown AND the composer stays usable
    // WITHOUT a close/reopen.
    await cdp.sleep(2500);
    const afterText = await chatText();
    report.ok('a /cc reconnect keeps the open chat transcript (no blank / close-reopen)',
      String(afterText).includes('fixture assistant reply') && !String(afterText).includes('Loading chat'),
      { sample: String(afterText).replace(/\s+/g, ' ').trim().slice(0, 90) });
    const sendAfter = await sendEnabled();
    report.ok('a /cc reconnect keeps the chat composer usable (session detail preserved)', sendAfter === true, { sendAfter });

    // TERMINAL half — THE FIX: onReconnect re-attaches the slot terminal. Before the
    // fix the host never re-attaches (client count stays 0) — the frozen terminal
    // the operator reported; after the fix the tmux client re-appears.
    const reattachedClients = await pollUntil(clients, (n) => n >= 1, timeoutMs, 'terminal re-attached after /cc reconnect');
    report.ok('a /cc reconnect re-attaches the slot terminal without close/reopen', reattachedClients >= 1, { clients: reattachedClients });

    // And typed input flows again over the reattached PTY (dead before the fix).
    await typeTouch(slot, postFile);
    report.ok('the reattached terminal accepts input after a /cc reconnect', (await fileAppears(postFile)) === true, { postFile });
  } finally {
    // Always run cleanup, even if an assertion above threw: restore the shimmed
    // get-state, detach the slot's client, remove the tmux session + marker files,
    // then reload so later scenarios start from a clean slot. A failed cleanup
    // reload is terminal (it can contaminate later scenarios), not a soft note.
    try { await session.eval('(() => { if (window.__origGetChatStreamState) { window.cc.getChatStreamState = window.__origGetChatStreamState; delete window.__origGetChatStreamState; } return true; })()'); } catch {}
    if (slot >= 0) { try { await session.eval(`window.cc.killPty(${slot})`, { awaitPromise: true }); } catch {} }
    try { tmux(['kill-session', '-t', `=${sessionName}`], { stdio: 'ignore' }); } catch {}
    try { fs.rmSync(preFile, { force: true }); fs.rmSync(postFile, { force: true }); } catch {}
    runtime.freezeTmux = null;
    let cleanupOk = true;
    try {
      await session.send('Page.reload', {});
      await waitForValue(session, cdp, '!!(window.cc && window.HOST)', (v) => v === true, { timeoutMs, label: 'app ready after cleanup reload' });
      await waitForValue(session, cdp, 'window.cc.getChatStreamState().then((s)=>s.connected===true)', (v) => v === true, { timeoutMs, label: 'reconnected after cleanup reload' });
    } catch (e) { cleanupOk = false; report.note('cleanup reload failed: ' + (e && e.message)); }
    report.ok('slot-survives-cc-reconnect cleanup restored a clean page for later scenarios', cleanupOk === true);
  }
}

// The ordered gate: names map to functions; web_gate runs them in this order.
// These four are fully deterministic against the seeded loopback daemon.
//
// A chat "send-turn round trip" scenario is intentionally NOT in this gate: a
// real chat send needs a live provider SPAWN, and the sanitized public repo
// omits the spawn/ingest fixture `tests/smoke/stub_cli.py` (with the available
// ingest_provider_stub the send reaches the provider and it writes the reply,
// but ingest does not associate that transcript back to the spawned stream, so
// the reply is not deterministically observable over the websocket). Per the
// Nexus (2026-09-12): the browser send path is covered by the web_cc/ws_bridge
// unit tests and the daemon send/ingest by the service's own python tests; see
// spec_pentacle__web_mode_e2e_gate_2026_09 § Findings.
const SCENARIOS = [
  ['transport-and-config', transportAndConfig],
  ['sidebar-from-inventory', sidebarFromInventory],
  ['slot-attach-type-resize-kill', slotAttachTypeResizeKill],
  ['chat-transcript-paint', chatTranscriptPaint],
  // Runs before closed-chat-slot (which retires the seeded fixture) and reloads
  // the page at both ends, so it neither depends on nor disturbs its neighbours.
  ['slot-survives-cc-reconnect', slotSurvivesCcReconnect],
  ['slot-column-split', runGridSplit],
  ['closed-chat-slot', closedChatSlot],
  // Host-restart walk runs LAST: it tears the host process (and briefly the
  // daemon) down, so it must not disturb the deterministic scenarios above.
  ['host-restart-restores-input', hostRestartRestoresInput],
];

module.exports = {
  waitForValue,
  pollUntil,
  transportAndConfig,
  sidebarFromInventory,
  slotAttachTypeResizeKill,
  chatTranscriptPaint,
  slotSurvivesCcReconnect,
  hostRestartRestoresInput,
  SCENARIOS,
};
