import test from 'node:test';
import assert from 'node:assert/strict';
import { renderTranscriptItemHtml } from '../renderer/src/shared_transcript_view';

// Canonical Bart chat shows only actionable status under a user message:
// progress and latency chrome is redundant with the slot working indicator.
const REMOVED = ['queued', 'routing', 'awaiting_reply', 'acknowledged', 'answered', 'reply_received'];
const RETAINED: Record<string, string> = {
  waiting_for_operator: 'Waiting for you',
  waiting_for_dependency: 'Waiting on an earlier reply',
  uncertain: 'Delivery unconfirmed',
  failed: 'Could not get a reply',
  cancelled: 'Cancelled',
};

function userItem(responseState: string) {
  return {
    id: `user-${responseState}`, messageId: `message-${responseState}`, isUser: true, displayRule: 'bubble:user',
    text: 'Hello Bart', timestamp: '2026-09-27T04:00:00Z',
    assistantActivity: {
      message_id: `message-${responseState}`, accepted_at: '2026-09-27T04:00:00Z',
      first_visible_at: '2026-09-27T04:00:03Z', final_visible_at: '2026-09-27T04:00:09Z',
      reply_latency_ms: 3000, final_latency_ms: 9000, response_state: responseState, work_state: 'discussion',
    },
  } as any;
}

test('progress and latency labels are not rendered under Bart user messages', () => {
  for (const responseState of REMOVED) {
    const html = renderTranscriptItemHtml(userItem(responseState), undefined, { allowReplies: false });
    assert.match(html, /Hello Bart/, responseState);
    assert.doesNotMatch(html, /slot-chat-assistant-activity|data-assistant-waiting-at/, responseState);
    assert.doesNotMatch(html, /Waiting for Bart|First reply|Answer complete|Follow-up pending|Reply received/, responseState);
  }
});

test('pending operator action and failure labels remain', () => {
  for (const [responseState, label] of Object.entries(RETAINED)) {
    const html = renderTranscriptItemHtml(userItem(responseState), undefined, { allowReplies: false });
    assert.match(html, /class="slot-chat-assistant-activity" role="status"/, responseState);
    assert.ok(html.includes(label), `${responseState} keeps ${label}`);
    assert.doesNotMatch(html, /First reply|Answer complete/, responseState);
  }
});

test('Bart assistant messages have no Reply control; user messages keep theirs', () => {
  const assistant = { id: 'a1', messageId: 'assistant-1', displayRule: 'bubble:assistant', text: 'Done.', replyToQuestionId: 'q1' } as any;
  const user = { id: 'u1', messageId: 'user-1', isUser: true, displayRule: 'bubble:user', text: 'Thanks' } as any;
  assert.doesNotMatch(renderTranscriptItemHtml(assistant, undefined, { allowReplies: true }), /slot-chat-reply-btn|>Reply</);
  assert.match(renderTranscriptItemHtml(user, undefined, { allowReplies: true }), /class="slot-chat-reply-btn" data-reply-message-id="user-1"/);
  assert.doesNotMatch(renderTranscriptItemHtml(user, undefined, { allowReplies: false }), /slot-chat-reply-btn/);
});
