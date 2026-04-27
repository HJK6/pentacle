const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const chatUi = require('../renderer/chat_ui_state');

const fixtures = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'fixtures', 'slot_chat_state_subjects.json'), 'utf8'),
);

for (const subject of fixtures.render_event_cases) {
  test(`renderEventContent: ${subject.name}`, () => {
    const html = chatUi.renderEventContent(subject.event, false);
    for (const item of subject.expect_contains) {
      assert.match(html, new RegExp(escapeRegExp(item)));
    }
    for (const item of subject.expect_not_contains) {
      assert.doesNotMatch(html, new RegExp(escapeRegExp(item)));
    }
  });
}

for (const subject of fixtures.body_cases) {
  test(`renderChatBody: ${subject.name}`, () => {
    const html = chatUi.renderChatBody(subject.text, false);
    for (const item of subject.expect_contains) {
      assert.match(html, new RegExp(escapeRegExp(item)));
    }
    for (const item of subject.expect_not_contains) {
      assert.doesNotMatch(html, new RegExp(escapeRegExp(item)));
    }
  });
}

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

for (const subject of fixtures.status_cases || []) {
  test(`renderStatusBadges: ${subject.name}`, () => {
    const html = chatUi.renderStatusBadges(subject);
    if (subject.activity === 'working') {
      assert.match(html, /is-working/);
      if (subject.workingLabel) {
        assert.match(html, new RegExp(escapeRegExp(subject.workingLabel)));
      }
      assert.doesNotMatch(html, /is-pending/);
    }
    if (subject.activity === 'waiting') assert.match(html, /is-waiting/);
    if (subject.activity === 'idle') assert.match(html, /is-idle/);
    if (subject.pending && subject.activity !== 'working') assert.match(html, /is-pending/);
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

test('selectSessionDetailForDesktopSession renders mobile-style websocket transcript', () => {
  const state = {
    connected: true,
    drafts: {},
    sessions: [{
      stream_id: 'merlin:codex-1',
      host: 'merlin',
      provider: 'codex',
      session_name: 'codex-1',
      title: 'Desktop chat rebuild',
      last_event_at: '2026-04-27T10:01:00Z',
      last_text: 'Ran npm run test:chat-ui\n└ all good',
      last_kind: 'ASSIST',
      online: true,
      working: false,
    }],
    events: [
      {
        daemon_seq: 1,
        stream_id: 'merlin:codex-1',
        host: 'merlin',
        provider: 'codex',
        session_name: 'codex-1',
        timestamp: '2026-04-27T10:00:00Z',
        kind: 'USER',
        text: 'Make desktop match mobile',
      },
      {
        daemon_seq: 2,
        stream_id: 'merlin:codex-1',
        host: 'merlin',
        provider: 'codex',
        session_name: 'codex-1',
        timestamp: '2026-04-27T10:01:00Z',
        kind: 'ASSIST',
        text: 'Ran npm run test:chat-ui\n└ all good',
      },
    ],
  };

  const detail = chatUi.selectSessionDetailForDesktopSession(
    state,
    { name: 'codex-1' },
    'merlin',
    { includeDraft: false },
  );
  assert.equal(detail.title, 'Desktop chat rebuild');
  assert.equal(detail.transcriptItems.length, 2);
  assert.equal(detail.transcriptItems[1].displayRule, 'activity:command');

  const html = chatUi.renderTranscriptTimeline(detail, chatUi.hostChrome(detail.host));
  assert.match(html, /slot-chat-user-bubble/);
  assert.match(html, /slot-chat-activity/);
  assert.match(html, /#4da3ff/);
  assert.match(html, /Ran npm run test:chat-ui/);
});

test('selectSessionDetailForDesktopSession matches human desktop titles to websocket sessions', () => {
  const state = {
    connected: true,
    drafts: {},
    sessions: [{
      stream_id: 'bart:codex-20260426104452-annz',
      host: 'bart',
      provider: 'codex',
      session_name: 'codex-20260426104452-annz',
      display_name: 'Pentacle Chat Data Source',
      last_event_at: '2026-04-27T10:02:00Z',
      last_text: 'Ready',
      last_kind: 'ASSIST',
      online: true,
      working: false,
    }],
    events: [{
      daemon_seq: 10,
      stream_id: 'bart:codex-20260426104452-annz',
      host: 'bart',
      provider: 'codex',
      session_name: 'codex-20260426104452-annz',
      timestamp: '2026-04-27T10:02:00Z',
      kind: 'ASSIST',
      text: 'Ready',
    }],
  };

  const detail = chatUi.selectSessionDetailForDesktopSession(
    state,
    { name: 'Pentacle Chat Data Source', displayName: 'Pentacle Chat Data Source' },
    'bart',
    { includeDraft: false },
  );

  assert.ok(detail);
  assert.equal(detail.streamId, 'bart:codex-20260426104452-annz');
  assert.equal(detail.title, 'Pentacle Chat Data Source');
});

function escapeRegExp(str) {
  return String(str).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
