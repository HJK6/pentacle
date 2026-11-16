// Phase 6b (public_chat_ui) — AUTOMATED HARNESS SCENARIO.
//
// Spec deliverable: "QA needs no manual steps". With the dev/CI harness armed,
// this drives the REAL renderer path (ChatStoreController.applyFrame -> shared
// selectors -> window.PentacleChatView render in jsdom) over a scripted
// sequence of RAW daemon frames — a representative session — and ASSERTS QA
// OUTCOMES PURELY FROM TELEMETRY, with zero manual steps:
//
//   (a) every inbound event is either PERSISTED or DROPPED-WITH-A-KNOWN-REASON
//       (no unaccounted drops): inbound == persisted + sum(dropped reasons),
//       and 'unaccounted' never appears among the drop reasons.
//   (b) the rendered rows match the expected display rules for the scripted
//       events (read from CHAT_EVENT_RENDERED telemetry + the rendered DOM).
//   (c) a full USER send round-trip reaches `echoed` (optimistic insert ->
//       acked via send.result landed -> reconciled by the server USER echo),
//       proven from CHAT_COMPOSE_OPTIMISTIC_* telemetry + reducer state.
//
// Run via `npm run test:harness-scenario`.

import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';

import { TELEMETRY_EVENTS } from 'chat-core';
import { ChatStoreController } from '../renderer/src/chat_store_controller';
import {
  attachChatHarnessTelemetry,
  isHarnessArmed,
} from '../renderer/src/chat_harness_telemetry';
import {
  renderStreamTranscript,
  type ViewChrome,
} from '../renderer/src/shared_transcript_view';

const STREAM = 'hostc:codex-hostc-1';
const CHROME: ViewChrome = {
  header: '#102a4a', accent: '#4da3ff', surface: '#0c1827', border: '#2f6ca5', title: 'hostc',
};

function makeSession(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    stream_id: STREAM, host: 'hostc', provider: 'codex', session_name: 'codex-hostc-1',
    display_name: 'codex-hostc-1', last_event_at: '2026-05-25T12:00:00.000Z', last_text: '',
    last_kind: 'ASSIST', draft: '', pending: false, working: false, online: true, ...over,
  };
}

let seq = 1;
function ev(over: Record<string, unknown> = {}): Record<string, unknown> {
  seq += 1;
  return {
    daemon_seq: seq, host: 'hostc', provider: 'codex', session_id: 'sess-1',
    session_name: 'codex-hostc-1', stream_id: STREAM,
    timestamp: new Date().toISOString(), kind: 'ASSIST', text: 'x', ...over,
  };
}

function newDom() {
  const dom = new JSDOM('<!doctype html><html><body><div id="c"></div></body></html>');
  return dom.window.document.getElementById('c') as unknown as HTMLElement;
}

function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

// ── Gating sanity: the harness must NOT arm by default ──────────────
test('harness is gated OFF by default (no env, no CONFIG flag)', () => {
  assert.equal(isHarnessArmed({}, undefined), false, 'unarmed with no env + no config');
  assert.equal(isHarnessArmed({ PENTACLE_HARNESS: '0' }, { features: {} }), false, 'PENTACLE_HARNESS=0 stays off');
  assert.equal(isHarnessArmed({ PENTACLE_HARNESS: '1' }, undefined), true, 'PENTACLE_HARNESS=1 arms');
  assert.equal(isHarnessArmed({}, { features: { chatHarnessTelemetry: true } }), true, 'CONFIG flag arms');
});

test('attachChatHarnessTelemetry is INERT when not armed (installs nothing)', () => {
  const controller = new ChatStoreController();
  const handle = attachChatHarnessTelemetry(controller, { env: {}, config: { features: {} } });
  assert.equal(handle.armed, false, 'handle reports not armed');

  // Drive a frame: with no hook installed, nothing is captured and no flow
  // counts are recorded for this stream by THIS handle.
  controller.applyFrame({ type: 'snapshot', events: [], sessions: [makeSession()], drafts: {} });
  controller.applyFrame({ type: 'chat.event', event: ev({ kind: 'USER', text: 'inert' }) });
  assert.equal(handle.events.length, 0, 'no telemetry captured when inert');
  assert.equal(handle.flowCounts().size, 0, 'no flow counts when inert');
  handle.detach();
});

// ── The full scripted scenario ──────────────────────────────────────
test('scripted session: QA outcomes asserted PURELY from harness telemetry', async () => {
  const controller = new ChatStoreController();
  // Arm the harness deterministically (force) — no env mutation needed. This is
  // exactly what window-side arming does when PENTACLE_HARNESS=1.
  const harness = attachChatHarnessTelemetry(controller, { force: true, resetCounts: true });
  assert.equal(harness.armed, true, 'harness armed for the scenario');
  assert.equal(harness.eventsOfType(TELEMETRY_EVENTS.HARNESS_HARNESS_ARMED).length, 1, 'armed beacon emitted');

  const container = newDom();
  const renderStore = {
    selectSessionDetail: (streamId: string, options?: Record<string, unknown>) => controller.selectSessionDetail(streamId, {
      ...options,
      includeTools: true,
      includeSystem: true,
      emitRenderTelemetry: true,
    }),
  };
  const renderNow = () => renderStreamTranscript(STREAM, container, {
    store: renderStore as never,
    chrome: CHROME,
    showTurnDuration: true,
  });

  // 1) snapshot: seeds the session (no chat.event inbound from snapshot — the
  //    snapshot reducer applies events directly, so they are NOT counted as
  //    chat.event inbound; we keep the snapshot empty and stream events live to
  //    keep the inbound ledger clean and assertable).
  controller.applyFrame({ type: 'snapshot', events: [], sessions: [makeSession()], drafts: {} });

  // 2) A scripted chat.event stream (the representative session). Each entry
  //    declares the display-rule family we expect the desktop to render.
  type Scripted = { frameEvent: Record<string, unknown>; expectRow: boolean; family: string };
  const scripted: Scripted[] = [
    { frameEvent: ev({ kind: 'THINK', text: 'Planning the change' }), expectRow: true, family: 'activity' },
    { frameEvent: ev({ kind: 'TOOL', text: 'Bash npm test' }), expectRow: true, family: 'activity' },
    { frameEvent: ev({ kind: 'TOOL-OUT', text: '12 passing' }), expectRow: true, family: 'activity' },
    { frameEvent: ev({ kind: 'ASSIST', text: 'Here is the result of the run.' }), expectRow: true, family: 'bubble:assistant' },
    // working-status noise -> hidden:status (dropped, NO row)
    { frameEvent: ev({ kind: 'ASSIST', text: 'Working (5s • esc to interrupt)' }), expectRow: false, family: 'hidden' },
    // codex-helper suggestion on a USER event -> hidden:helper (dropped, NO row)
    { frameEvent: ev({ kind: 'USER', text: 'explore the repository structure' }), expectRow: false, family: 'hidden' },
  ];

  let inboundChatEvents = 0;
  for (const step of scripted) {
    controller.applyFrame({ type: 'chat.event', event: step.frameEvent });
    inboundChatEvents += 1;
    renderNow();
  }

  // 3) working.state frame (not a chat.event; updates the dock, not the ring).
  controller.applyFrame({ type: 'working.state', stream_id: STREAM, elapsed_ms: 4200, tokens_output: 128 });

  // 4) USER send round-trip: optimistic insert -> dispatch -> send.result
  //    landed (ack) -> server USER echo (reconcile = echoed).
  controller.setSendBridge(async () => ({ ok: true }));
  const sendText = 'ship it please';
  const optimisticId = controller.sendTurn(STREAM, sendText);
  assert.ok(optimisticId, 'optimistic send created');
  renderNow();
  await flush(); // let the dispatch promise settle (-> dispatched)

  const requestId = controller.getState().optimisticSends?.[optimisticId]?.request_id as string;
  assert.ok(requestId, 'renderer-owned request_id assigned');

  // send.result landed -> acked
  controller.applyFrame({ type: 'send.result', request_id: requestId, delivery: 'landed' });
  assert.equal(controller.getState().optimisticSends?.[optimisticId]?.status, 'acked', 'landed -> acked');

  // Server USER echo -> reconcile (optimistic pruned, server row kept).
  const echo = ev({ kind: 'USER', text: sendText });
  controller.applyFrame({ type: 'chat.event', event: echo });
  inboundChatEvents += 1; // the echo is an inbound chat.event
  renderNow();

  // (Converged shared-core behavior: a raw "Worked for" terminal-furniture line
  // is now hidden furniture, not a rendered terminal:divider row — asserted in
  // chat-core tests/peerAgentMessages.test.ts and the display-rule
  // parity fixture — so it is intentionally NOT part of this rendered-rows
  // scenario. Hidden-furniture/noise drop accounting is exercised by the
  // working-status noise_filter drop below.)

  // The optimistic USER send is also an inbound event the harness counts (it is
  // inserted via the reducer but NOT through applyFrame's chat.event path, so it
  // is NOT in inboundChatEvents). We assert the chat.event ledger only.

  // ============================================================
  // (a) NO UNACCOUNTED DROPS — inbound == persisted + dropped, and no
  //     'unaccounted' reason ever appears.
  // ============================================================
  const counts = harness.flowCounts().get(STREAM);
  assert.ok(counts, 'flow counts recorded for the stream');
  const droppedTotal = Object.values(counts!.dropped_count_by_reason).reduce((a, b) => a + b, 0);

  assert.equal(
    counts!.inbound_count,
    inboundChatEvents,
    `every scripted chat.event was counted inbound (expected ${inboundChatEvents})`,
  );
  assert.equal(
    counts!.persisted_count + droppedTotal,
    counts!.inbound_count,
    'inbound == persisted + dropped (full ledger balances)',
  );
  assert.equal(
    counts!.dropped_count_by_reason.unaccounted ?? 0,
    0,
    'NO unaccounted drops — every dropped event has a known reason',
  );
  // The two intentional drops carry the SHARED classifier reasons.
  assert.equal(counts!.dropped_count_by_reason.noise_filter, 1, 'working-status -> noise_filter drop');
  assert.equal(counts!.dropped_count_by_reason.helper_suggestion, 1, 'codex helper -> helper_suggestion drop');

  // ============================================================
  // (b) RENDERED ROWS MATCH EXPECTED DISPLAY RULES — read from
  //     CHAT_EVENT_RENDERED telemetry (display_rule per rendered row).
  // ============================================================
  const rendered = harness.eventsOfType(TELEMETRY_EVENTS.CHAT_EVENT_RENDERED);
  const renderedRules = new Set(rendered.map((p) => String(p.data.display_rule)));
  // Activity rows (think/tool/tool-out), an assistant bubble, and the reconciled
  // user bubble were all rendered…
  for (const expected of ['activity:thinking', 'activity:command', 'activity:tool-output', 'bubble:assistant', 'bubble:user']) {
    assert.ok(renderedRules.has(expected), `a row with display_rule ${expected} was rendered`);
  }
  // …and NO hidden:* rule ever reached a rendered row.
  for (const rule of renderedRules) {
    assert.ok(!rule.startsWith('hidden:'), `no hidden:* row rendered (saw ${rule})`);
  }
  // The dropped events never produced a CHAT_EVENT_RENDERED row for their text.
  const renderedKinds = rendered.map((p) => `${p.data.kind}:${p.data.display_rule}`);
  assert.ok(!renderedKinds.includes('USER:hidden:helper'), 'helper suggestion never rendered');

  // Cross-check against the live DOM: the final render shows the reconciled
  // user bubble + the assistant content, and NO working-status noise text.
  const dom = container.textContent || '';
  assert.ok(dom.includes(sendText), 'reconciled user message visible in the DOM');
  assert.ok(!/esc to interrupt/.test(dom), 'working-status noise not in the DOM');

  // ============================================================
  // (c) FULL SEND ROUND-TRIP REACHES `echoed` — from optimistic-lifecycle
  //     telemetry + reducer state.
  // ============================================================
  const inserts = harness.eventsOfType(TELEMETRY_EVENTS.CHAT_COMPOSE_OPTIMISTIC_INSERT);
  const reconciled = harness.eventsOfType(TELEMETRY_EVENTS.CHAT_COMPOSE_OPTIMISTIC_RECONCILED);
  assert.equal(inserts.length, 1, 'one optimistic insert telemetry event');
  assert.ok(
    inserts.some((p) => String(p.data.optimistic_id) === optimisticId),
    'insert telemetry carries our optimistic_id',
  );
  assert.equal(reconciled.length, 1, 'one optimistic reconcile telemetry event');
  assert.ok(
    reconciled.some((p) => String(p.data.optimistic_id) === optimisticId),
    'reconcile telemetry carries our optimistic_id (echoed)',
  );
  // Reducer state agrees: the optimistic was pruned (echoed -> removed) and a
  // SINGLE server USER row with the text remains.
  assert.equal(controller.getState().optimisticSends?.[optimisticId], undefined, 'optimistic pruned (echoed)');
  const userRows = controller.getState().events.filter(
    (e) => e.text === sendText && String(e.kind).toUpperCase() === 'USER',
  );
  assert.equal(userRows.length, 1, 'exactly one reconciled USER row (no duplicate)');

  harness.detach();
});

test('harness telemetry restores the default sink on detach (no leak across runs)', async () => {
  const controller = new ChatStoreController();
  const handle = attachChatHarnessTelemetry(controller, { force: true, resetCounts: true });
  controller.applyFrame({ type: 'snapshot', events: [], sessions: [makeSession()], drafts: {} });
  controller.applyFrame({ type: 'chat.event', event: ev({ kind: 'ASSIST', text: 'captured while armed' }) });
  const capturedWhileArmed = handle.events.length;
  assert.ok(capturedWhileArmed > 0, 'telemetry captured while armed');
  handle.detach();

  // After detach, applying more frames must NOT grow the captured buffer.
  controller.applyFrame({ type: 'chat.event', event: ev({ kind: 'ASSIST', text: 'after detach' }) });
  assert.equal(handle.events.length, capturedWhileArmed, 'no capture after detach (sink + hook removed)');
});
