import assert from 'node:assert/strict';
import test from 'node:test';

import {
  initialPentacleStreamState,
  interpretPentacleEvent,
  isDaemonNotificationText,
  isOrchestrationReceiptAck,
  isPeerAgentMessage,
  isTerminalFurnitureText,
  isTerminalToolEchoText,
  parsePeerAgentMessage,
  selectSessionDetail,
  type PentacleEvent,
  type PentacleSessionSummary,
} from '../src/index.ts';

const STREAM_ID = 'host_c:codex-host_c-host';
const PEER_STREAM_ID = 'host_c:claude-host_c-1b7d6cb6';

function session(overrides: Partial<PentacleSessionSummary> = {}): PentacleSessionSummary {
  return {
    stream_id: STREAM_ID,
    host: 'host_c',
    provider: 'codex',
    session_name: 'codex-host_c-host',
    last_event_at: '2026-06-18T12:00:00.000Z',
    last_text: '',
    last_kind: '',
    draft: '',
    pending: false,
    working: false,
    online: true,
    ...overrides,
  };
}

function event(overrides: Partial<PentacleEvent> = {}): PentacleEvent {
  return {
    daemon_seq: 1,
    host: 'host_c',
    provider: 'codex',
    session_id: STREAM_ID,
    session_name: 'codex-host_c-host',
    stream_id: STREAM_ID,
    timestamp: '2026-06-18T12:00:00.000Z',
    kind: 'USER',
    text: 'plain user message',
    ...overrides,
  };
}

test('parsePeerAgentMessage strips agent-orch peer delivery envelopes', () => {
  const parsed = parsePeerAgentMessage(`[from ${PEER_STREAM_ID}] [tell:abc-123]\nbody\nsecond line`);

  assert.deepEqual(parsed, {
    fromStreamId: PEER_STREAM_ID,
    anchorId: 'tell:abc-123',
    body: 'body\nsecond line',
  });
  assert.equal(isPeerAgentMessage(`[from ${PEER_STREAM_ID}] [send:msg-7]\nbody`), true);
});

test('daemon-liveness nudges (direct + queued) render as hidden collapsed peer tells, not raw JSON bubbles', () => {
  // Regression guard for public-daemon-liveness-tell-rendering.
  // These are the EXACT wire shapes the daemon emits for child-liveness nudges:
  //   header  = `[from <daemon-liveness>] {anchor}{queued}\n{body}`
  //             (daemon transport _stamped_peer_text)
  //   anchor  = ` [tell:child-liveness-<32hex>-<16hex>]` (daemon transport)
  //   queued  = ` enqueued_at=<iso-utc>` for QUEUED/redelivered tells only (…:183)
  //   body    = `[daemon-liveness] {json} suggested_action=… accept_action=…`
  //             (daemon transport) — kinds child_idle_unreported /
  //             child_report_ready share this identical wire format.
  // The `<daemon-liveness>` from-id trips the daemonNotification hidden path, and
  // the optional ` enqueued_at=` stamp is tolerated by PEER_AGENT_MESSAGE, so both
  // shapes collapse to the same hidden `bubble:agent` tell as an ordinary peer
  // delivery — never a raw-JSON `bubble:user` row.
  const tellId = `child-liveness-${'a'.repeat(32)}-${'b'.repeat(16)}`;
  const jsonBody = '{"child_stream_id":"host_c:codex-host_c-abc","idle_s":42,"kind":"child_idle_unreported"}';
  const body = `[daemon-liveness] ${jsonBody} suggested_action=agent-orch inspect host_c:codex-host_c-abc accept_action=agent-orch park host_c:codex-host_c-abc --reason 'accepted idle'`;
  const direct = `[from <daemon-liveness>] [tell:${tellId}]\n${body}`;
  const queued = `[from <daemon-liveness>] [tell:${tellId}] enqueued_at=2026-07-29T07:00:00.000000Z\n${body}`;

  for (const [label, wire] of [['direct', direct], ['queued', queued]] as const) {
    const parsed = parsePeerAgentMessage(wire);
    assert.ok(parsed, `${label}: parses as a peer-agent-message envelope`);
    assert.equal(parsed!.fromStreamId, '<daemon-liveness>', `${label}: from-id preserved`);
    const interp = interpretPentacleEvent(event({ text: wire }));
    assert.equal(interp.displayRule, 'bubble:agent', `${label}: collapsed tell display rule`);
    assert.equal(interp.hidden, true, `${label}: hidden from the visible transcript by default`);
    assert.ok(!/^\{/.test(interp.text.trim()), `${label}: does not surface raw JSON as the row text`);
  }

  // Negative guard: an ordinary user line that merely contains a bracket (no
  // `[from X]` + newline envelope) stays a visible user bubble.
  const human = interpretPentacleEvent(event({ text: '[from 3:30pm] standup notes, see you there' }));
  assert.equal(human.displayRule, 'bubble:user', 'human bracketed prose stays a user bubble');
  assert.equal(human.hidden, false, 'human bracketed prose is NOT hidden');
});

test('parsePeerAgentMessage now catches non-tell/send anchor variants that used to leak', () => {
  // Any anchor word (handoff/reply/inbox/peer/...) — previously only tell|send
  // matched, so these fell through to the operator's own bubble.
  assert.equal(isPeerAgentMessage(`[from ${PEER_STREAM_ID}] [handoff:x]\nbody`), true);
  assert.equal(isPeerAgentMessage(`[from ${PEER_STREAM_ID}] [inbox:9]\nbody`), true);
  // No anchor at all (with the newline body separator).
  assert.equal(isPeerAgentMessage(`[from ${PEER_STREAM_ID}]\njust a body`), true);
  // Non-stream-id markers (e.g. <daemon-liveness>) are still recognized — the
  // from-target stays permissive, so these are not regressed into user bubbles.
  assert.equal(isPeerAgentMessage('[from <daemon-liveness>] [tell:child-liveness-7d9]\n{"kind":"x"}'), true);
});

test('the trailing newline keeps human text safe (no false-positive hiding)', () => {
  // Ordinary prose that merely starts with a bracketed token and has NO newline
  // body separator must stay a normal user message — never silently hidden.
  assert.equal(isPeerAgentMessage('[from 3:30pm] meeting moved'), false);
  assert.equal(isPeerAgentMessage('[from John] hey can you check this'), false);
  assert.equal(isPeerAgentMessage('[from the desk of example] hello'), false);
  assert.equal(isPeerAgentMessage('plain user message'), false);
});

test('interpretPentacleEvent renders peer-agent USER events as visible agent rows', () => {
  const result = interpretPentacleEvent(event({
    text: `[from ${PEER_STREAM_ID}] [tell:abc-123]\nplease review the mobile row`,
  }));

  assert.equal(result.caseId, 'peer-agent-message');
  assert.equal(result.displayRule, 'bubble:agent');
  assert.equal(result.tone, 'agent');
  assert.equal(result.label, 'claude-host_c-1b7d6cb6');
  assert.equal(result.text, 'please review the mobile row');
  assert.equal(result.hidden, false);
});

test('typed TELL from a synthetic peer renders as a visible collapsed agent row', () => {
  // Contract change (public-agent-message-collapsed-row):
  // the daemon now stamps trusted peer tell/send traffic so both normalizers emit
  // kind:TELL with raw.sender/raw.peer_payload. A synthetic peer delivery is
  // operationally relevant transcript context and must be VISIBLE — the same
  // compact agent card the `[from …]` USER-prefix path already produces — not
  // hidden as typed peer traffic used to be.
  const result = interpretPentacleEvent(event({
    kind: 'TELL',
    text: 'typed body',
    raw: {
      source: 'structured',
      transport: 'claude-jsonl',
      sender: PEER_STREAM_ID,
      tell_id: 'abc-123',
      peer_payload: 'typed body',
    },
  }));
  assert.equal(result.caseId, 'peer-agent-message');
  assert.equal(result.displayRule, 'bubble:agent');
  assert.equal(result.tone, 'agent');
  assert.equal(result.label, 'claude-host_c-1b7d6cb6');
  assert.equal(result.text, 'typed body');
  assert.equal(result.hidden, false);
});

test('typed TELL housekeeping (daemon sender or protocol payload) stays hidden', () => {
  const daemon = interpretPentacleEvent(event({
    kind: 'TELL',
    text: '{"kind":"child_report_ready"}',
    raw: {
      source: 'structured', transport: 'claude-jsonl',
      sender: '<daemon-liveness>', tell_id: 'child-liveness-1',
      peer_payload: '{"kind":"child_report_ready"}',
    },
  }));
  assert.equal(daemon.caseId, 'peer-agent-message');
  assert.equal(daemon.displayRule, 'bubble:agent');
  assert.equal(daemon.hidden, true);

  const protocolBody = '{"type":"notification.answer","answer":{"choice":true}}';
  const protocol = interpretPentacleEvent(event({
    kind: 'TELL',
    text: protocolBody,
    raw: {
      source: 'structured', transport: 'claude-jsonl',
      sender: PEER_STREAM_ID, tell_id: 'abc-9', peer_payload: protocolBody,
    },
  }));
  assert.equal(protocol.hidden, true);
});

test('scrollback fallback is hidden by default and explicitly revealable', () => {
  const fallback = event({
    kind: 'ASSIST_TEXT',
    text: 'fallback assistant text',
    raw: { source: 'scrollback_fallback', transport: 'codex-pane' },
  });
  assert.equal(interpretPentacleEvent(fallback).hidden, true);
  const revealed = interpretPentacleEvent(fallback, 'Agent', { revealScrollbackFallback: true });
  assert.equal(revealed.caseId, 'assistant-message');
  assert.equal(revealed.hidden, false);
  assert.equal(revealed.text, 'fallback assistant text');
});

test('ordinary USER messages remain visible user bubbles', () => {
  const result = interpretPentacleEvent(event({ text: 'plain user message' }));
  assert.equal(result.caseId, 'user-message');
  assert.equal(result.displayRule, 'bubble:user');
  assert.equal(result.tone, 'user');
  assert.equal(result.hidden, false);
  assert.equal(result.text, 'plain user message');
});

test('selectSessionDetail keeps peer-agent rows visible in chronological order', () => {
  const peerEvent = event({
    text: `[from ${PEER_STREAM_ID}] [tell:abc-123]\nplease review the mobile row`,
  });
  const state = {
    ...initialPentacleStreamState,
    connected: true,
    hasHydrated: true,
    sessions: [session()],
    events: [
      event({ daemon_seq: 1, text: 'before peer delivery' }),
      { ...peerEvent, daemon_seq: 2 },
      event({ daemon_seq: 3, text: 'after peer delivery' }),
    ],
    eventContentVersionByStream: { [STREAM_ID]: 1 },
  };

  const detail = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' });

  const texts = (detail?.transcriptItems ?? []).map((item) => item.text);
  assert.deepEqual(texts, [
    'before peer delivery',
    'please review the mobile row',
    'after peer delivery',
  ]);
});

test('curated transcript drops receipts, daemon notifications, and exact terminal furniture', () => {
  const hidden = [
    event({ kind: 'USER', text: '   ' }),
    event({ kind: 'SYSTEM', text: '──── Worked for 4s' }),
    event({ kind: 'ASSIST', text: '✻ Brewed for 12m 36s' }),
    event({ kind: 'SYSTEM', text: '2,048 weighted tokens left' }),
    event({ kind: 'SYSTEM', text: '⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt' }),
    event({ kind: 'SYSTEM', text: 'How is Claude doing this session? (optional)' }),
    event({ text: '[from daemon:status-sweep] [tell:nudge]\nstatus check' }),
    event({ text: `[from ${PEER_STREAM_ID}] [tell:done]\n[child_report_ready]\nstatus=done` }),
    event({ kind: 'TOOL_RESULT', text: '{"type":"tell.ok","delivery_status":"submitting"}', raw: { source: 'claude-jsonl', tool_name: 'Bash' } }),
  ];
  for (const item of hidden) assert.equal(interpretPentacleEvent(item).hidden, true, item.text);

  assert.equal(isTerminalFurnitureText('87% context left', 'SYSTEM'), true);
  assert.equal(isDaemonNotificationText('[child_report_ready]\nstatus=done'), true);
  assert.equal(isOrchestrationReceiptAck('{"type":"spawn.ok"}'), true);
});

test('curated transcript preserves prose and maps terminal tool echoes to compact activity', () => {
  const operator = interpretPentacleEvent(event({ text: '❯ ship the fix' }));
  assert.equal(operator.hidden, false);
  assert.equal(operator.text, 'ship the fix');

  for (const text of [
    'We reduced token usage safely.',
    'Please remove the Claude feedback prompt.',
    'Permission mode behavior changed in this release.',
  ]) {
    assert.equal(interpretPentacleEvent(event({ kind: 'ASSIST', text })).hidden, false, text);
  }

  const toolEcho = 'Ran npm test\n└ 24 tests passed';
  assert.equal(isTerminalToolEchoText(toolEcho), true);
  const tool = interpretPentacleEvent(event({ kind: 'ASSIST', text: toolEcho }));
  assert.equal(tool.hidden, false);
  assert.equal(tool.displayRule, 'activity:tool-output');
  assert.deepEqual(tool.disclosure, {
    mode: 'collapsed-preview',
    previewText: 'Ran npm test',
    previewTail: '… +1 line',
    expandable: true,
    expandedText: toolEcho,
  });

  const errorReceipt = interpretPentacleEvent(event({
    kind: 'TOOL_RESULT',
    text: '{"type":"tell.error","error":"offline"}',
    raw: { source: 'claude-jsonl', tool_name: 'Bash', is_error: true },
  }));
  assert.equal(errorReceipt.hidden, false);
  assert.equal(errorReceipt.displayRule, 'activity:tool-output');
  assert.equal(errorReceipt.disclosure?.expandable, false);
});

test('core owns disclosure previews and exact furniture negatives', () => {
  const peer = interpretPentacleEvent(event({
    text: `[from ${PEER_STREAM_ID}] [tell:preview]\nfirst line\nsecond line`,
  }));
  assert.deepEqual(peer.disclosure, {
    mode: 'collapsed-preview',
    previewText: 'first line',
    previewTail: '… +1 line',
    expandable: true,
    expandedText: 'first line\nsecond line',
  });

  const rawTellBody = '⎿ raw marker\n  exact indentation';
  const rawTell = interpretPentacleEvent(event({
    text: `[from ${PEER_STREAM_ID}] [tell:raw]\n${rawTellBody}`,
  }));
  assert.equal(rawTell.disclosure?.expandedText, rawTellBody);

  const rawWriteResult = 'Saved file\n  exact tool payload';
  const writeResult = interpretPentacleEvent(event({
    kind: 'TOOL_RESULT',
    text: rawWriteResult,
    raw: { source: 'claude-jsonl', tool_name: 'Write', tool_input: { file_path: '/tmp/result.txt' } },
  }));
  assert.equal(writeResult.disclosure?.expandedText, rawWriteResult);
  assert.equal(writeResult.disclosure?.expandable, true);

  for (const kind of ['USER', 'ASSIST_TEXT']) {
    for (const text of [
      'How is Claude doing this session? We should improve the prompt.',
      '2,048 weighted tokens left in the budget, which is enough.',
      '87% context left after the optimization.',
      'Permission mode: the documentation changed.',
      'esc to interrupt is a useful terminal shortcut.',
      'Worked for months on this reliability improvement.',
    ]) {
      assert.equal(interpretPentacleEvent(event({ kind, text })).hidden, false, `${kind}: ${text}`);
    }
  }
});
