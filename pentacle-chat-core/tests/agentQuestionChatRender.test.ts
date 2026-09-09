import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyPentacleEvent,
  applyPentacleSnapshotMessage,
  initialPentacleStreamState,
  selectSessionDetail,
  splitProvenancedNotificationAnswerSummaryText,
  type PentacleEvent,
  type PentacleSessionSummary,
  type PentacleStreamState,
} from '../src/index.ts';

const STREAM_ID = 'host_c:claude-host_c-4bdb7e6d';

function session(): PentacleSessionSummary {
  return {
    stream_id: STREAM_ID,
    host: 'host_c',
    provider: 'claude',
    session_name: 'claude-host_c-4bdb7e6d',
    last_event_at: '2025-01-14T21:11:00.000Z',
    last_text: '',
    last_kind: '',
    draft: '',
    pending: false,
    working: false,
    online: true,
  };
}

function event(overrides: Partial<PentacleEvent>): PentacleEvent {
  return {
    daemon_seq: 1,
    host: 'host_c',
    provider: 'claude',
    session_id: 'sess-1',
    session_name: 'claude-host_c-4bdb7e6d',
    stream_id: STREAM_ID,
    timestamp: '2025-01-14T21:11:00.000Z',
    kind: 'USER',
    text: '',
    ...overrides,
  };
}

function detailFor(events: PentacleEvent[]) {
  let state: PentacleStreamState = {
    ...initialPentacleStreamState,
    sessions: [session()],
    eventContentVersionByStream: { [STREAM_ID]: 1 },
  };
  for (const item of events) {
    state = applyPentacleEvent(state, item);
  }
  return selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' });
}

test('notification.answer tell renders selected label as a human question row', () => {
  const payload = {
    type: 'notification.answer',
    answer: {
      notification_id: 'q-a1e684e0',
      action_kind: 'select',
      label: 'host_c',
      selections: ['host_c'],
      note: null,
    },
  };
  const detail = detailFor([
    event({
      text: `[from daemon:notifications]\n[tell:notification-answer-q-a1e684e0]${JSON.stringify(payload)}`,
    }),
  ]);

  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].displayRule, 'activity:question');
  assert.equal(detail?.transcriptItems[0].eventCase, 'agent-question-answer');
  assert.equal(detail?.transcriptItems[0].text, 'Operator answered: host_c');
});

test('notification.answer tell renders free-text and note without raw JSON', () => {
  const payload = {
    type: 'notification.answer',
    answer: {
      notification_id: 'q-a1e684e0',
      action_kind: 'answered',
      label: 'Answered',
      text: 'Use the host_b worktree.',
      selections: [],
      note: 'Run focused gates first.',
    },
  };
  const detail = detailFor([
    event({
      text: `[from daemon:notifications] [tell:notification-answer-q-a1e684e0]\n${JSON.stringify(payload)}`,
    }),
  ]);

  assert.equal(detail?.transcriptItems[0].displayRule, 'activity:question');
  assert.equal(detail?.transcriptItems[0].text, 'Operator answered: Use the host_b worktree.\nNote: Run focused gates first.');
  assert.equal(detail?.transcriptItems[0].text.includes('notification.answer'), false);
});

test('notification.answer tell renders CR-delimited operator ledger event', () => {
  const payload = {
    type: 'notification.answer',
    answer: {
      notification_id: '8d6901b5-98e0-4c44-83f9-bb52191eb278',
      action_id: 'a0',
      action_kind: 'yes_no',
      label: 'J confirmed: no disconnected banner — OK to close the wave',
      by: 'operator',
      at: '2026-07-09T05:03:58.193267Z',
      selections: ['j_ok'],
      note: null,
      choice: true,
      value: {
        schema_version: 1,
        question_id: 'q-682d72bf-a686-4fa5-b56e-d94b871a7129',
        answer: 'j_ok',
      },
    },
  };
  const detail = detailFor([
    event({
      text: `[from daemon:notifications] [tell:notification-answer-8d6901b5-98e0-4c44-83f9-bb52191eb278]\r${JSON.stringify(payload)}`,
    }),
  ]);

  assert.equal(detail?.transcriptItems[0].displayRule, 'activity:question');
  assert.equal(detail?.transcriptItems[0].eventCase, 'agent-question-answer');
  assert.equal(detail?.transcriptItems[0].text, 'Operator answered: J confirmed: no disconnected banner — OK to close the wave');
  assert.equal(detail?.transcriptItems[0].text.includes('notification.answer'), false);
});

test('notification.answer tell renders note-only answer without raw JSON', () => {
  const payload = {
    type: 'notification.answer',
    answer: {
      notification_id: 'q-note-only',
      action_kind: 'answered',
      label: 'answered',
      note: 'Use the release device build.',
    },
  };
  const detail = detailFor([
    event({
      text: `[from daemon:notifications] [tell:notification-answer-q-note-only]\r${JSON.stringify(payload)}`,
    }),
  ]);

  assert.equal(detail?.transcriptItems[0].displayRule, 'activity:question');
  assert.equal(detail?.transcriptItems[0].text, 'Operator answered with note:\nNote: Use the release device build.');
  assert.equal(detail?.transcriptItems[0].text.includes('notification.answer'), false);
});

test('prompt.ask.ok tool result renders one-line ask confirmation', () => {
  const payload = {
    type: 'prompt.ask.ok',
    ok: true,
    question: {
      question_id: 'q-a1e684e0',
      state: 'open',
      envelope: {
        title: 'Choose a host',
        question_id: 'q-a1e684e0',
      },
    },
  };
  const detail = detailFor([
    event({
      kind: 'TOOL_RESULT',
      text: JSON.stringify(payload),
      raw: { source: 'claude-jsonl', tool_name: 'Bash', is_error: false },
    }),
  ]);

  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].displayRule, 'activity:question');
  assert.equal(detail?.transcriptItems[0].eventCase, 'agent-question-ask');
  assert.equal(detail?.transcriptItems[0].text, 'Asked: Choose a host (q-a1e684e0)');
});

// --- Queued/redelivered peer tells (public-notification-answer-raw-json-bubble) ---
//
// Contract pin. The daemon stamps every peer tell with `_stamped_peer_text`
// (pentacle daemon transport):
//   f"[from {from_stream_id}]{anchor}{queued}\n{text}"
// where anchor is `_peer_tell_anchor` (daemon transport) => " [tell:<tell_id>]" and
// `queued` is " enqueued_at=<iso>" — present ONLY when the tell was queued rather than
// delivered directly (daemon transport queued / :1668,:1792,:1886
// redelivery pass it; the direct path at :1426-1434 does not). Those daemon sites are the
// source of truth for this shape; re-check them if the wire format changes.
function stampedPeerText(options: {
  from: string;
  tellId?: string;
  enqueuedAt?: string;
  body: string;
}) {
  const anchor = options.tellId ? ` [tell:${options.tellId}]` : '';
  const queued = options.enqueuedAt ? ` enqueued_at=${options.enqueuedAt}` : '';
  return `[from ${options.from}]${anchor}${queued}\n${options.body}`;
}

const QUEUED_AT = '2026-07-22T22:34:07.918233Z';

test('QUEUED notification.answer tell renders the answer card, never a raw JSON bubble', () => {
  const payload = {
    type: 'notification.answer',
    answer: {
      notification_id: 'q-022b16dd',
      action_id: 'a0',
      action_kind: 'other',
      label: 'Other (describe)',
      by: 'operator',
      at: QUEUED_AT,
      selections: [],
      note: 'App-side lanes must not block on the daemon.',
      value: { schema_version: 1, question_id: 'q-022b16dd', answer: 'Other (describe)' },
    },
  };
  const detail = detailFor([
    event({
      text: stampedPeerText({
        from: 'daemon:notifications',
        tellId: 'notification-answer-q-022b16dd',
        enqueuedAt: QUEUED_AT,
        body: JSON.stringify(payload),
      }),
    }),
  ]);

  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].eventCase, 'agent-question-answer');
  assert.equal(detail?.transcriptItems[0].displayRule, 'activity:question');
  for (const item of detail?.transcriptItems ?? []) {
    assert.equal(item.text.includes('notification.answer'), false);
    assert.equal(item.text.includes('"type":'), false);
    assert.equal(item.text.includes('enqueued_at='), false);
  }
});

test('QUEUED ordinary peer tell renders as attributed agent traffic, not a raw user bubble', () => {
  const detail = detailFor([
    event({
      text: stampedPeerText({
        from: 'host_c:claude-host_c-4bad59f5',
        tellId: 'peer-tell-7f31',
        enqueuedAt: QUEUED_AT,
        body: 'Lane update: both specs claimed.',
      }),
    }),
  ]);

  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].displayRule, 'bubble:agent');
  assert.equal(detail?.transcriptItems[0].text, 'Lane update: both specs claimed.');
});

test('notification.answer with only a yes_no choice never renders raw JSON', () => {
  // action_kind === label (so the label is suppressed at pentacleEventInterpreter.ts:290),
  // no text, no selections, no note, no value: the formatter used to bail and fall through
  // to the generic user bubble.
  const payload = {
    type: 'notification.answer',
    answer: {
      notification_id: 'q-choice-only',
      action_id: 'a0',
      action_kind: 'yes_no',
      label: 'yes_no',
      by: 'operator',
      at: QUEUED_AT,
      selections: [],
      note: null,
      choice: true,
    },
  };
  const detail = detailFor([
    event({
      text: stampedPeerText({
        from: 'daemon:notifications',
        tellId: 'notification-answer-q-choice-only',
        body: JSON.stringify(payload),
      }),
    }),
  ]);

  // The answer must still be SHOWN. Before the fix the formatter bailed and the peer-message
  // branch hid the row entirely, so the operator's answer silently vanished from the chat.
  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].eventCase, 'agent-question-answer');
  assert.equal(detail?.transcriptItems[0].displayRule, 'activity:question');
  assert.equal(detail?.transcriptItems[0].text, 'Operator answered: Yes');
  for (const item of detail?.transcriptItems ?? []) {
    assert.equal(item.text.includes('notification.answer'), false);
    assert.equal(item.text.includes('"type":'), false);
    assert.equal(item.displayRule === 'bubble:user', false);
  }
});

test('notification.answer with a TOP-LEVEL notification_id never renders raw JSON', () => {
  // Shape transcribed in the operator screenshot: notification_id alongside `answer`
  // rather than nested inside it. The app must defend against the envelope regardless of
  // which variant the daemon emits (operator ruling 2026-07-23).
  const payload = {
    type: 'notification.answer',
    notification_id: 'd8f7e596-1c2b-4a55-9f31-0b7a5c2e44aa',
    answer: {
      action_kind: 'other',
      label: 'Other (describe)',
      note: 'Fix the app against the expected contract.',
    },
  };
  const detail = detailFor([
    event({
      text: stampedPeerText({
        from: 'daemon:notifications',
        tellId: 'notification-answer-d8f7e596-1c2b-4a55-9f31-0b7a5c2e44aa',
        enqueuedAt: QUEUED_AT,
        body: JSON.stringify(payload),
      }),
    }),
  ]);

  for (const item of detail?.transcriptItems ?? []) {
    assert.equal(item.text.includes('notification.answer'), false);
    assert.equal(item.text.includes('"type":'), false);
    assert.equal(item.displayRule === 'bubble:user', false);
  }
});

test('human text that merely resembles a stamped header stays a visible user bubble', () => {
  // Regression for the false positive a permissive `key=value` suffix introduced: this is
  // ordinary prose, not agent-orch traffic, and hiding it would be worse than the bug being
  // fixed. Only the daemon's literal `enqueued_at` stamp may be tolerated.
  const detail = detailFor([
    event({ text: '[from John] status=ready\nShipping the build tonight.' }),
  ]);

  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].displayRule, 'bubble:user');
  assert.equal(detail?.transcriptItems[0].text.includes('Shipping the build tonight.'), true);
});

test('notification.answer row carries the notification id for client-side dedupe', () => {
  const payload = {
    type: 'notification.answer',
    answer: {
      notification_id: 'q-dedupe-1',
      action_kind: 'select',
      label: 'host_c',
      selections: ['host_c'],
    },
  };
  const detail = detailFor([
    event({
      text: stampedPeerText({
        from: 'daemon:notifications',
        tellId: 'notification-answer-q-dedupe-1',
        enqueuedAt: QUEUED_AT,
        body: JSON.stringify(payload),
      }),
    }),
  ]);

  assert.equal(detail?.transcriptItems[0].eventCase, 'agent-question-answer');
  assert.equal(detail?.transcriptItems[0].notificationId, 'q-dedupe-1');
});

test('notification.answer id is also read from the top level of the envelope', () => {
  const payload = {
    type: 'notification.answer',
    notification_id: 'q-dedupe-top',
    answer: { action_kind: 'other', label: 'Other (describe)' },
  };
  const detail = detailFor([
    event({
      text: stampedPeerText({
        from: 'daemon:notifications',
        tellId: 'notification-answer-q-dedupe-top',
        body: JSON.stringify(payload),
      }),
    }),
  ]);

  assert.equal(detail?.transcriptItems[0].notificationId, 'q-dedupe-top');
});

test('a sparse notification.answer renders a complete sentence, not a dangling label', () => {
  const payload = { type: 'notification.answer', answer: {} };
  const detail = detailFor([
    event({
      text: stampedPeerText({
        from: 'daemon:notifications',
        tellId: 'notification-answer-q-sparse',
        enqueuedAt: QUEUED_AT,
        body: JSON.stringify(payload),
      }),
    }),
  ]);

  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].text, 'Operator answered.');
  assert.equal(detail?.transcriptItems[0].text.includes('notification.answer'), false);
});

test('REDELIVERED notification.answer keeps its original enqueue stamp and still renders', () => {
  // Redelivery (daemon transport) re-stamps from the STORED queue row —
  // `enqueued_at=str(row.get("enqueued_at") or "")` — so the timestamp is the original enqueue
  // time, not "now", and can be far older than the surrounding transcript. Same tell_id, same
  // envelope. This shape is load-bearing: it is the one an operator sees after a peer-queue drain.
  const payload = {
    type: 'notification.answer',
    answer: {
      notification_id: 'q-redelivered',
      action_kind: 'select',
      label: 'hosta',
      selections: ['hosta'],
    },
  };
  const detail = detailFor([
    event({
      text: stampedPeerText({
        from: 'daemon:notifications',
        tellId: 'notification-answer-q-redelivered',
        enqueuedAt: '2026-07-21T09:02:44.010000Z',
        body: JSON.stringify(payload),
      }),
    }),
  ]);

  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].eventCase, 'agent-question-answer');
  assert.equal(detail?.transcriptItems[0].text, 'Operator answered: hosta');
  assert.equal(detail?.transcriptItems[0].notificationId, 'q-redelivered');
  assert.equal(detail?.transcriptItems[0].text.includes('enqueued_at='), false);
});

test('a redelivery whose stored enqueue time is blank carries no stamp and still renders', () => {
  // `str(row.get("enqueued_at") or "")` yields '' for a missing value, and `_stamped_peer_text`
  // omits the segment entirely when it is falsy — so the unstamped shape must keep working too.
  const payload = {
    type: 'notification.answer',
    answer: { notification_id: 'q-blank-stamp', action_kind: 'answered', text: 'Proceed.' },
  };
  const detail = detailFor([
    event({
      text: stampedPeerText({
        from: 'daemon:notifications',
        tellId: 'notification-answer-q-blank-stamp',
        body: JSON.stringify(payload),
      }),
    }),
  ]);

  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].text, 'Operator answered: Proceed.');
});

test('human lookalikes of the enqueued_at stamp stay visible user bubbles', () => {
  // The stamp grammar is the daemon's ISO-8601 UTC timestamp. Prose that merely reuses the key
  // must not be swallowed — this pattern hides what it matches, so a false positive destroys a
  // real message.
  const lookalikes = [
    '[from John] enqueued_at=tomorrow\nCan you take this one?',
    '[from John] enqueued_at=\nEmpty value, still just prose.',
    '[from John] enqueued_at=2026-13-45\nNot a real timestamp.',
    '[from Ops] status=ready\nDeploy is green.',
  ];
  for (const text of lookalikes) {
    const detail = detailFor([event({ text })]);
    assert.equal(detail?.transcriptItems.length, 1, `hidden: ${text}`);
    assert.equal(detail?.transcriptItems[0].displayRule, 'bubble:user', `not a bubble: ${text}`);
  }
});

test('adversarial stamp lookalikes stay visible user bubbles', () => {
  // The grammar is the daemon's exact lexical form. Anything else is a human typing.
  const lookalikes = [
    // Whitespace: the daemon interpolates exactly one ASCII space.
    '[from John]  enqueued_at=2026-07-22T22:34:07.918233Z\nTwo spaces — I typed this.',
    '[from John]\tenqueued_at=2026-07-22T22:34:07.918233Z\nTab, not a daemon stamp.',
    // Right shape, impossible instant.
    '[from John] enqueued_at=2026-13-45T99:99:99Z\nMonth 13, day 45.',
    '[from John] enqueued_at=2026-00-00T00:00:00Z\nZero month and day.',
    '[from John] enqueued_at=2026-07-22T24:00:00Z\nHour 24.',
    // Fractions the daemon never emits (Python gives exactly six digits, or none).
    '[from John] enqueued_at=2026-07-22T22:34:07.9Z\nOne fractional digit.',
    '[from John] enqueued_at=2026-07-22T22:34:07.918233123Z\nNine fractional digits.',
    // Not UTC-suffixed.
    '[from John] enqueued_at=2026-07-22T22:34:07\nNo trailing Z.',
    '[from John] enqueued_at=2026-07-22T22:34:07+00:00\nOffset instead of Z.',
    // Key present, value prose.
    '[from John] enqueued_at=tomorrow\nCan you take this one?',
    '[from John] enqueued_at=\nEmpty value, still just prose.',
    '[from Ops] status=ready\nDeploy is green.',
  ];
  for (const text of lookalikes) {
    const detail = detailFor([event({ text })]);
    assert.equal(detail?.transcriptItems.length, 1, `hidden: ${text}`);
    assert.equal(detail?.transcriptItems[0].displayRule, 'bubble:user', `not a bubble: ${text}`);
  }
});

test('every real daemon stamp form still parses as peer traffic', () => {
  // Both forms Python's isoformat() produces: with six-digit microseconds, and without any
  // fraction at all when microsecond == 0. A regression here reinstates the raw-JSON bug.
  const realStamps = [
    '2026-07-22T22:34:07.918233Z',
    '2026-07-22T22:34:07Z',
    '2026-01-01T00:00:00Z',
    '2026-12-31T23:59:59.000000Z',
  ];
  for (const stamp of realStamps) {
    const payload = {
      type: 'notification.answer',
      answer: { notification_id: 'q-grammar', action_kind: 'answered', text: 'Ship it.' },
    };
    const detail = detailFor([
      event({
        text: stampedPeerText({
          from: 'daemon:notifications',
          tellId: 'notification-answer-q-grammar',
          enqueuedAt: stamp,
          body: JSON.stringify(payload),
        }),
      }),
    ]);
    assert.equal(detail?.transcriptItems.length, 1, `stamp rejected: ${stamp}`);
    assert.equal(detail?.transcriptItems[0].text, 'Operator answered: Ship it.', `stamp rejected: ${stamp}`);
  }
});

test('non-ASCII-space header separators are human typing, not daemon output', () => {
  // The daemon emits exactly one ASCII space around the header tokens
  // (daemon transport). Anything else — NBSP, tab, doubled space, a newline right
  // after `[from` — is a person, and hiding it would destroy a real message.
  const STAMP = 'enqueued_at=2026-07-22T22:34:07.918233Z';
  const lookalikes = [
    `[from John] ${STAMP}\nNon-breaking space after from.`,
    `[from\tJohn] ${STAMP}\nTab after from.`,
    `[from  John] ${STAMP}\nTwo spaces after from.`,
    `[from\nJohn] ${STAMP}\nNewline after from.`,
    `[from John] [tell:x] ${STAMP}\nNon-breaking space before the anchor.`,
    `[from John]\t[tell:x] ${STAMP}\nTab before the anchor.`,
    `[from John]  [tell:x] ${STAMP}\nTwo spaces before the anchor.`,
  ];
  for (const text of lookalikes) {
    const detail = detailFor([event({ text })]);
    assert.equal(detail?.transcriptItems.length, 1, `hidden: ${JSON.stringify(text)}`);
    assert.equal(detail?.transcriptItems[0].displayRule, 'bubble:user', `not a bubble: ${JSON.stringify(text)}`);
  }
});

test('calendar-invalid stamps are matched by component range — a documented, accepted limit', () => {
  // The grammar bounds each FIELD (month 01-12, day 01-31, ...); it does not run a calendar.
  // `2026-02-31` and year `0000` therefore still read as stamps and are hidden. This is
  // characterization, not an endorsement: closing it would need calendar math that shuts a
  // strictly SMALLER hole than the byte-exact-wire-form collision already accepted above (a human
  // typing a real timestamp is far likelier than one typing an impossible-but-well-formed date).
  // The honest fix is daemon-set provenance, not more regex. Asserted so the behaviour is pinned
  // and any future change to it is deliberate.
  const payload = { type: 'notification.answer', answer: { notification_id: 'q-cal', text: 'ok' } };
  for (const stamp of ['2026-02-31T00:00:00Z', '0000-01-01T00:00:00Z']) {
    const detail = detailFor([
      event({
        text: `[from daemon:notifications] [tell:notification-answer-q-cal] enqueued_at=${stamp}\n${JSON.stringify(payload)}`,
      }),
    ]);
    assert.equal(detail?.transcriptItems.length, 1, stamp);
    assert.equal(detail?.transcriptItems[0].eventCase, 'agent-question-answer', stamp);
  }
});

test('pane-wrapped peer headers stay attributed agent traffic on every transport shape', () => {
  // The Codex pane fallback captures with tmux `capture-pane` WITHOUT -J, so a header that wraps
  // visually arrives with a NEWLINE where the daemon emitted a space, and rstrip can drop the
  // trailing space (daemon transport daemon transport).
  // Ordinary peer traffic must keep parsing in every shape so it never surfaces as the
  // operator's own bubble.
  const STAMP = 'enqueued_at=2026-07-22T22:34:07.918233Z';
  const shapes = [
    `[from host_c:codex-host_c-x] [tell:abc]\nLane update: both specs claimed.`,
    `[from host_c:codex-host_c-x] [tell:abc] ${STAMP}\nLane update: both specs claimed.`,
    `[from host_c:codex-host_c-x]\n[tell:abc]\nLane update: both specs claimed.`,
    `[from host_c:codex-host_c-x]\n[tell:abc] ${STAMP}\nLane update: both specs claimed.`,
    `[from host_c:codex-host_c-x] [tell:abc]\n${STAMP}\nLane update: both specs claimed.`,
    `[from host_c:codex-host_c-x]\nLane update: both specs claimed.`,
  ];
  for (const text of shapes) {
    const detail = detailFor([event({ text })]);
    assert.equal(detail?.transcriptItems.length, 1, `missing: ${JSON.stringify(text)}`);
    assert.equal(detail?.transcriptItems[0].displayRule, 'bubble:agent', JSON.stringify(text));
  }
});

test('a pane-wrapped queued notification.answer still renders its card, never raw JSON', () => {
  const payload = {
    type: 'notification.answer',
    answer: { notification_id: 'q-wrapped', action_kind: 'answered', text: 'Ship it.' },
  };
  const body = JSON.stringify(payload);
  const STAMP = 'enqueued_at=2026-07-22T22:34:07.918233Z';
  // Only shapes whose stamp stays on the header line. A wrap that pushes the stamp INTO the body
  // leaves nothing parseable; recovering from that needs provenance the wire text cannot supply,
  // so it belongs to public-tmux-capture-line-wrap-transport, not to more app-side
  // guessing. Those shapes are covered by the suppression test below instead.
  const shapes = [
    `[from daemon:notifications] [tell:notification-answer-q-wrapped] ${STAMP}\n${body}`,
    `[from daemon:notifications]\n[tell:notification-answer-q-wrapped]\n${body}`,
  ];
  for (const text of shapes) {
    const detail = detailFor([event({ text })]);
    // The card must be SHOWN, not merely "not raw JSON": a wrap that pushed the stamp into the
    // body used to leave nothing parseable, so the operator's answer vanished from the chat.
    assert.equal(detail?.transcriptItems.length, 1, `no card: ${JSON.stringify(text)}`);
    assert.equal(detail?.transcriptItems[0].eventCase, 'agent-question-answer', JSON.stringify(text));
    assert.equal(detail?.transcriptItems[0].text, 'Operator answered: Ship it.', JSON.stringify(text));
    for (const item of detail?.transcriptItems ?? []) {
      assert.equal(item.text.includes('notification.answer'), false, `raw JSON: ${JSON.stringify(text)}`);
      assert.equal(item.displayRule === 'bubble:user', false, `raw bubble: ${JSON.stringify(text)}`);
    }
  }
});




test('a stamp pushed into the body still suppresses malformed notification protocol JSON', () => {
  // The card is lost (the transport mangled the envelope beyond parsing), but the raw-JSON floor
  // still holds: nothing protocol-shaped reaches the transcript. Recovering the card here would
  // require inferring provenance from wire text, which QA r7 showed is spoofable by a user pasting
  // the same text — a failure mode strictly worse than the missing card.
  const payload = {
    type: 'notification.answer',
    answer: { notification_id: 'q-lost', action_kind: 'answered', text: 'Ship it.' },
  };
  const body = JSON.stringify(payload);
  const STAMP = 'enqueued_at=2026-07-22T22:34:07.918233Z';
  for (const text of [
    `[from daemon:notifications]\n[tell:notification-answer-q-lost] ${STAMP}\n${body}`,
    `[from daemon:notifications] [tell:notification-answer-q-lost]\n${STAMP}\n${body}`,
  ]) {
    const detail = detailFor([event({ text })]);
    for (const item of detail?.transcriptItems ?? []) {
      assert.equal(item.text.includes('notification.answer'), false, JSON.stringify(text));
      assert.equal(item.displayRule === 'bubble:user', false, JSON.stringify(text));
    }
  }
});

test('a provenanced concatenated summary splits the answer and preserves the peer suffix', () => {
  const payload = JSON.stringify({
    type: 'notification.answer',
    answer: { notification_id: 'q-summary', label: 'Lane B', selections: ['lane_b'] },
  });
  const suffix = '[from host_c:codex-next] [tell:next]\nnext message\r\nwith bytes intact';
  const summary = {
    ...session(),
    last_kind: 'USER',
    last_text: payload + suffix,
    last_text_provenance: {
      schema_version: 1 as const,
      kind: 'notification.answer' as const,
      tell_id: 'notification-answer-q-summary',
      injected_text: payload,
    },
  };
  const state = applyPentacleSnapshotMessage(initialPentacleStreamState, {
    sessions: [summary],
    events: [],
  });

  const split = splitProvenancedNotificationAnswerSummaryText(summary);
  assert.equal(split?.suffixText, suffix);
  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' })?.transcriptItems ?? [];
  assert.equal(items.length, 2);
  const answer = items.find((item) => item.eventCase === 'agent-question-answer');
  const peer = items.find((item) => item.eventCase === 'peer-agent-message');
  assert.match(answer?.text || '', /Operator answered: Lane B/);
  assert.equal(answer?.text.includes('notification.answer'), false);
  assert.equal(peer?.text, 'next message\nwith bytes intact');
});

test('a concatenated answer lookalike without daemon provenance stays raw and unsplit', () => {
  const payload = JSON.stringify({
    type: 'notification.answer',
    answer: { notification_id: 'q-spoof', selections: ['lane_b'] },
  });
  const state: PentacleStreamState = {
    ...initialPentacleStreamState,
    sessions: [{
      ...session(),
      last_kind: 'USER',
      last_text: `${payload}[from host_c:codex-next] [tell:next]\nquoted message`,
    }],
  };

  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' })?.transcriptItems ?? [];
  assert.equal(items.length, 1);
  assert.equal(items[0].displayRule, 'bubble:user');
  assert.equal(items[0].text.includes('notification.answer'), true);
  assert.equal(items[0].text.includes('quoted message'), true);
});

test('a provenanced answer whose captured prefix was mangled stays raw and unsplit', () => {
  const payload = JSON.stringify({
    type: 'notification.answer',
    answer: { notification_id: 'q-mangled', selections: ['lane_b'] },
  });
  const summary: PentacleSessionSummary = {
    ...session(),
    last_kind: 'USER',
    last_text: `${payload.replace('notification.answer', 'notification.\\nanswer')}suffix`,
    last_text_provenance: {
      schema_version: 1,
      kind: 'notification.answer',
      tell_id: 'notification-answer-q-mangled',
      injected_text: payload,
    },
  };
  const state: PentacleStreamState = {
    ...initialPentacleStreamState,
    sessions: [summary],
  };

  assert.equal(splitProvenancedNotificationAnswerSummaryText(summary), null);
  const items = selectSessionDetail(state, STREAM_ID, { visibleCount: 'all' })?.transcriptItems ?? [];
  assert.equal(items.length, 1);
  assert.equal(items[0].displayRule, 'bubble:user');
  assert.equal(items[0].text.includes('suffix'), true);
});

test('a human message quoting a peer header plus an envelope stays the human message', () => {
  // QA r7's exploit against the removed recovery: the gate was `[from ` appearing ANYWHERE, so
  // quoting a log line and an envelope replaced the operator's own words with an answer card.
  // Pinned so no future "helpful" recovery reintroduces it.
  const envelope = JSON.stringify({ type: 'notification.answer', answer: { text: 'nope' } });
  const detail = detailFor([
    event({ text: `Saw this in the log: [from their-service] and then ${envelope} — is that expected?` }),
  ]);
  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].displayRule, 'bubble:user');
  assert.equal(detail?.transcriptItems[0].text.includes('is that expected?'), true);
});

test('a pasted line beginning with a valid stamp stays the human message', () => {
  // QA r7's second exploit: body-side stamp stripping ran on every USER event, so a pasted stamp
  // followed by an envelope became a card. The stripping is gone; this pins that it stays gone.
  const envelope = JSON.stringify({ type: 'notification.answer', answer: { text: 'nope' } });
  const detail = detailFor([
    event({ text: `enqueued_at=2026-07-22T22:34:07.918233Z ${envelope}` }),
  ]);
  assert.equal(detail?.transcriptItems.length, 1);
  assert.equal(detail?.transcriptItems[0].displayRule, 'bubble:user');
});
