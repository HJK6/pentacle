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

function escapeRegExp(str) {
  return String(str).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
