const test = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const { renderStatusCard, renderStatusView, renderSessionStatusCardView, statusCardAttentionState } = require('../renderer/chat_ui_state');

test('capacity status renders in every existing status surface without authored card', () => {
  for (const render of [renderStatusCard, renderStatusView, renderSessionStatusCardView]) {
    const session = { capacity_retry: { state: 'waiting', attempts_used: 1, next_retry_at: 1790000000 } };
    let dom = new JSDOM(render(session));
    assert.match(dom.window.document.body.textContent, /Codex capacity.*waiting.*1\/3 attempts.*after 2026/);
    assert.equal(statusCardAttentionState(session).hasContent, true);
    session.capacity_retry.state = 'exhausted'; session.capacity_retry.attempts_used = 3;
    dom = new JSDOM(render(session));
    assert.match(dom.window.document.body.textContent, /exhausted.*manual action needed.*3\/3/);
    session.capacity_retry = null;
    assert.doesNotMatch(new JSDOM(render(session)).window.document.body.textContent, /Codex capacity/);
  }
});
