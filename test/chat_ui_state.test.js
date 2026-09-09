const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const chatUi = require('../renderer/chat_ui_state');

const fixtures = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'fixtures', 'slot_chat_state_subjects.json'), 'utf8'),
);

// NOTE (Phase 7 cutover, public_chat_ui): the transcript-render
// functions formerly tested here — renderEventContent / renderChatBody /
// selectSessionDetailForDesktopSession / renderTranscriptTimeline — were DELETED
// from renderer/chat_ui_state.js when the shared chat-core render path
// became the only path. Their coverage now lives in test:chat-view,
// test:parity, and test:harness-scenario (which exercise the shared
// PentacleChatView + selectSessionDetail). The fixtures.render_event_cases and
// fixtures.body_cases blocks were removed accordingly. This suite now covers
// only the RETAINED desktop-only helpers below.

for (const subject of fixtures.summary_cases) {
  test(`sanitizeSidebarDetail: ${subject.name}`, () => {
    assert.equal(chatUi.sanitizeSidebarDetail(subject.input), subject.expected);
  });
}

for (const subject of fixtures.composer_cases) {
  test(`deriveComposerInputValue: ${subject.name}`, () => {
    assert.equal(
      chatUi.deriveComposerInputValue(
        subject.local_draft,
        subject.remote_draft,
        subject.local_draft_touched,
      ),
      subject.expected,
    );
  });
}

for (const subject of fixtures.working_time_cases) {
  test(`extractWorkingTime: ${subject.name}`, () => {
    assert.equal(chatUi.extractWorkingTime(subject.input), subject.expected);
  });
}

// ── renderStatusCard (public_contract) ───────────
// Contract: render only the fields present (no placeholders); current step =
// active step, "n/n done" when complete; age from daemon-stamped updated_at;
// indicator slots (context_*, spec_issues) render whenever present, even
// without an agent-written card; absent everything -> empty string.

const CARD_NOW_MS = Date.parse('2026-07-09T12:10:00Z');

test('renderStatusCard: full card renders goal, active step, update, age, handoff', () => {
  const html = chatUi.renderStatusCard(
    {
      status_card: {
        goal: 'Ship the session status card end to end',
        plan: [
          { text: 'daemon', status: 'done' },
          { text: 'cli verb', status: 'active' },
          { text: 'ui', status: 'pending' },
        ],
        update: 'daemon slice merged',
        handoff_planned: true,
        updated_at: '2026-07-09T12:05:00Z',
      },
    },
    { nowMs: CARD_NOW_MS },
  );
  assert.match(html, /Ship the session status card end to end/);
  assert.match(html, /2\/3 · cli verb/);
  assert.match(html, /daemon slice merged/);
  assert.match(html, /5m ago/);
  assert.match(html, /is-handoff/);
});

test('renderStatusCard: partial card hides missing rows, no placeholders', () => {
  const html = chatUi.renderStatusCard(
    { status_card: { update: 'only an update', updated_at: '2026-07-09T12:09:30Z' } },
    { nowMs: CARD_NOW_MS },
  );
  assert.match(html, /only an update/);
  assert.match(html, /just now/);
  assert.doesNotMatch(html, /is-goal/);
  assert.doesNotMatch(html, /is-step/);
  assert.doesNotMatch(html, /is-handoff/);
});

test('renderStatusCard: all-done plan renders n/n done', () => {
  const html = chatUi.renderStatusCard(
    {
      status_card: {
        plan: [
          { text: 'a', status: 'done' },
          { text: 'b', status: 'done' },
        ],
        updated_at: '2026-07-09T12:00:00Z',
      },
    },
    { nowMs: CARD_NOW_MS },
  );
  assert.match(html, /2\/2 done/);
});

test('renderStatusCard: no card and no indicators renders nothing', () => {
  assert.equal(chatUi.renderStatusCard({}, { nowMs: CARD_NOW_MS }), '');
  assert.equal(chatUi.renderStatusCard(null, { nowMs: CARD_NOW_MS }), '');
});

test('renderStatusCard: context indicator renders without a card, with window pct and level', () => {
  const html = chatUi.renderStatusCard(
    { context_tokens: 251000, model_context_window: 500000, context_level: 'advisory' },
    { nowMs: CARD_NOW_MS },
  );
  assert.match(html, /ctx 251k/);
  assert.match(html, /50%</);            // window % is visible on the badge, not only the tooltip
  assert.doesNotMatch(html, /50% of window/);  // old tooltip-only form is gone
  assert.match(html, /is-ctx-advisory/);
  assert.doesNotMatch(html, /is-goal/);
});

test('renderStatusCard: context indicator without level or window renders plain', () => {
  const html = chatUi.renderStatusCard({ context_tokens: 97000 }, { nowMs: CARD_NOW_MS });
  assert.match(html, /ctx 97k/);
  assert.doesNotMatch(html, /is-ctx-/);
  assert.doesNotMatch(html, /%/);       // no window -> no percentage anywhere
});

test('renderStatusCard: spec issues show a count badge and list each entry visibly', () => {
  const single = chatUi.renderStatusCard(
    { spec_issues: [{ obligation_id: 'ob-1', detail: 'frontmatter drift' }] },
    { nowMs: CARD_NOW_MS },
  );
  assert.match(single, /1 spec issue</);
  assert.match(single, /is-spec-issue-entry/);   // entry listed as a visible row
  assert.match(single, /frontmatter drift/);
  const multiple = chatUi.renderStatusCard(
    { spec_issues: [{ detail: 'alpha' }, { detail: 'beta' }, { detail: 'gamma' }] },
    { nowMs: CARD_NOW_MS },
  );
  assert.match(multiple, /3 spec issues/);
  assert.match(multiple, /alpha/);
  assert.match(multiple, /beta/);
  assert.match(multiple, /gamma/);
});

test('renderStatusCard: escapes html in agent-written fields', () => {
  const html = chatUi.renderStatusCard(
    { status_card: { goal: '<script>alert(1)</script>', updated_at: '2026-07-09T12:00:00Z' } },
    { nowMs: CARD_NOW_MS },
  );
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
});

test('formatCardAge: unparseable stamp yields empty string', () => {
  assert.equal(chatUi.formatCardAge('not-a-timestamp', CARD_NOW_MS), '');
  assert.equal(chatUi.formatCardAge('', CARD_NOW_MS), '');
  assert.equal(chatUi.formatCardAge('2026-07-08T12:00:00Z', CARD_NOW_MS), '1d ago');
});

for (const subject of fixtures.status_cases || []) {
  test(`renderStatusBadges: ${subject.name}`, () => {
    const html = chatUi.renderStatusBadges(subject);
    if (subject.activity === 'working') {
      assert.match(html, /is-working/);
      assert.match(html, /activity-spinner/);
      // working always carries the (live) timer span, even when empty.
      assert.match(html, /slot-chat-status-timer/);
      if (subject.workingLabel) {
        assert.match(html, new RegExp(escapeRegExp(subject.workingLabel)));
      }
      assert.doesNotMatch(html, /is-pending/);
    } else {
      // idle / waiting render NO activity badge — no circle when not working.
      // (A genuine in-flight send may still add the separate pending badge.)
      assert.doesNotMatch(html, /is-working|is-waiting|is-idle/);
    }
    if (subject.pending && subject.activity !== 'working') assert.match(html, /is-pending/);
  });
}

test('renderStatusBadges: idle renders an empty status row (no dot/badge)', () => {
  const html = chatUi.renderStatusBadges({ activity: 'idle' });
  assert.doesNotMatch(html, /slot-chat-status-badge/);
  assert.doesNotMatch(html, /slot-chat-status-dot/);
  assert.match(html, /slot-chat-status-left/); // container still present
});

test('renderStatusBadges: working shows a timer span seeded with the label', () => {
  const html = chatUi.renderStatusBadges({ activity: 'working', workingLabel: '1m 04s' });
  assert.match(html, /activity-spinner slot-chat-status-dot/);
  assert.match(html, /class="slot-chat-status-timer">1m 04s</);
  assert.doesNotMatch(html, />Working</);
  assert.doesNotMatch(html, /title="Working"/);
  assert.doesNotMatch(html, /background terminal|\/ps to view/i);
});

test('renderStatusBadges: unresponsive names the tmux timeout without replacing chat content', () => {
  const html = chatUi.renderStatusBadges({ activity: 'unresponsive' });
  assert.match(html, /slot-chat-status-badge is-unresponsive/);
  assert.match(html, /Session unresponsive — tmux did not answer/);
  assert.doesNotMatch(html, /activity-spinner/);
});

for (const [ms, expected] of [[0, '0s'], [7000, '7s'], [59000, '59s'], [60000, '1m 00s'], [64000, '1m 04s'], [3725000, '62m 05s']]) {
  test(`formatElapsed(${ms}) -> ${expected}`, () => {
    assert.equal(chatUi.formatElapsed(ms), expected);
  });
}

for (const [label, expected] of [['Working (45s)', 45], ['1m 23s', 83], ['2h 3m', 7380], ['no time here', null], ['', null]]) {
  test(`parseWorkingSeconds(${JSON.stringify(label)}) -> ${expected}`, () => {
    assert.equal(chatUi.parseWorkingSeconds(label), expected);
  });
}

for (const subject of fixtures.draft_preview_cases || []) {
  test(`renderDraftPreview: ${subject.name}`, () => {
    const html = chatUi.renderDraftPreview(subject);
    assert.match(html, new RegExp(escapeRegExp(subject.remoteDraft)));
    if (subject.remotePending) {
      assert.match(html, /Queued for next tool call/);
      assert.match(html, /is-pending/);
    } else {
      assert.match(html, /Draft in progress/);
    }
    if (subject.activity === 'working') assert.match(html, /is-working/);
  });
}

// findStreamSessionForDesktopSession is RETAINED (the live app still uses it to
// map a desktop session to its websocket stream); the transcript selection that
// formerly built on it now lives in the shared chat-core selector
// (covered by test:chat-view / test:parity). This keeps coverage for the
// desktop-only stream-matching that stayed in chat_ui_state.js.
test('findStreamSessionForDesktopSession matches human desktop titles to websocket sessions', () => {
  const state = {
    connected: true,
    drafts: {},
    sessions: [{
      stream_id: 'hosta:provider_c-20260426104452-annz',
      host: 'hosta',
      provider: 'provider_c',
      session_name: 'provider_c-20260426104452-annz',
      display_name: 'Sample Chat Data Source',
      last_event_at: '2026-04-27T10:02:00Z',
      last_text: 'Ready',
      last_kind: 'ASSIST',
      online: true,
      working: false,
    }],
  };

  const streamSession = chatUi.findStreamSessionForDesktopSession(
    state,
    { name: 'Sample Chat Data Source', displayName: 'Sample Chat Data Source' },
    'hosta',
  );

  assert.ok(streamSession);
  assert.equal(streamSession.stream_id, 'hosta:provider_c-20260426104452-annz');
});

// Bug1 (chat_ui_hardening) new-chat-flash guard: the STRONG matcher must
// require BOTH host and id, so a transient id-only collision (shared/blank name,
// or the same name on a different host) can never masquerade as the bound stream
// for a slot. The loose matcher still resolves it (unchanged), but the strong
// matcher returns null until this session's OWN host+id stream is present.
test('findStrongStreamSessionForDesktopSession requires host AND id (no id-only fallback)', () => {
  const state = {
    connected: true,
    drafts: {},
    sessions: [{
      stream_id: 'hostc:provider_a-prev',
      host: 'hostc',
      provider: 'provider_a',
      session_name: 'Shared Title',
      display_name: 'Shared Title',
    }],
  };

  // Same (blank/shared) name, but on a DIFFERENT host => id-only collision.
  const desktopSession = { name: 'Shared Title', displayName: 'Shared Title' };

  // Loose matcher still falls back on id-only (legacy behavior, unchanged).
  const loose = chatUi.findStreamSessionForDesktopSession(state, desktopSession, 'hosta');
  assert.ok(loose, 'loose matcher still resolves via id-only fallback');
  assert.equal(loose.stream_id, 'hostc:provider_a-prev');

  // Strong matcher refuses the cross-host id-only match => null (no leak).
  const strong = chatUi.findStrongStreamSessionForDesktopSession(state, desktopSession, 'hosta');
  assert.equal(strong, null, 'strong matcher returns null on cross-host id-only collision');

  // When host matches too, the strong matcher resolves.
  const strongOk = chatUi.findStrongStreamSessionForDesktopSession(state, desktopSession, 'hostc');
  assert.ok(strongOk);
  assert.equal(strongOk.stream_id, 'hostc:provider_a-prev');
});

function escapeRegExp(str) {
  return String(str).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
