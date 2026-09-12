import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { interpretPentacleEvent, initialPentacleStreamState, sendOptimisticMessage, applyPentacleEvent, applyFetchedStreamEvents, applyPentacleSnapshotMessage, selectSessionDetail } from 'pentacle-chat-core';
import { renderTranscriptItemHtml, renderTranscriptTimelineHtml, renderStreamTranscript } from '../renderer/src/shared_transcript_view';

function row(overrides: Record<string, unknown> = {}) {
  return {
    id: 'fixture', timestampLabel: '', label: '', tone: 'assistant', provider: 'claude',
    source: 'claude-jsonl', text: 'Hello', kind: 'ASSIST', isUser: false,
    eventCase: 'assistant-message', displayRule: 'bubble:assistant', ...overrides,
  } as never;
}

function rendered(item: ReturnType<typeof row>) {
  return new JSDOM(renderTranscriptItemHtml(item)).window.document;
}

test('tool disclosures retain the complete output behind an accessible collapsed preview', () => {
  const full = `Saved file\n${'Exact details\n'.repeat(30)}END <script>unsafe</script>`;
  const interpreted = interpretPentacleEvent({
    daemon_seq: 1, host: 'local', provider: 'claude', stream_id: 'local:fixture',
    session_id: 'fixture', session_name: 'fixture', timestamp: '2026-09-12T00:00:00Z',
    kind: 'TOOL_RESULT', text: full,
    raw: { source: 'claude-jsonl', tool_name: 'Write', tool_input: { file_path: '/tmp/example' } },
  } as never);
  const doc = rendered(row({ ...interpreted, eventCase: interpreted.caseId }));
  const disclosure = doc.querySelector('details');
  assert.ok(disclosure, 'full output is reachable through native keyboard-accessible disclosure');
  assert.equal(disclosure.open, false);
  assert.ok(disclosure.querySelector('summary')?.textContent?.includes('Saved file'));
  assert.ok(disclosure.textContent?.includes(full));
  assert.equal(doc.querySelector('script'), null);
});

test('subagent messages retain sender attribution and distinguish their expandable body', () => {
  const doc = rendered(row({ displayRule: 'bubble:agent', tone: 'agent', label: 'Review worker',
    text: 'Review complete\nDetails', disclosure: { mode: 'collapsed-preview', previewText: 'Review complete',
      previewTail: '… +1 line', expandedText: 'Review complete\nDetails', expandable: true } }));
  assert.match(doc.body.textContent || '', /Subagent.*Review worker/);
  assert.ok(doc.querySelector('details summary'));
  assert.equal(doc.querySelector('.slot-chat-assistant-card'), null);
});

test('file actions and code activity retain their exact body', () => {
  const file = rendered(row({ text: 'Edited example.js (2 lines)\n+ const preserved = true;' }));
  assert.ok(file.body.textContent?.includes('+ const preserved = true;'));
  const code = '  first line\n\n    second line';
  const doc = rendered(row({ text: code, displayRule: 'activity:code-block' }));
  assert.equal(doc.querySelector('pre code')?.textContent, code);
});

test('delivery receipt captions survive correlation and terminal failure wins over an old caption', () => {
  const user = { isUser: true, displayRule: 'bubble:user', queuedWhileWorking: true };
  const sent = rendered(row({ ...user, receiptCaption: 'sent' }));
  assert.match(sent.querySelector('.slot-chat-send-status')?.textContent || '', /Sent/);
  const failed = rendered(row({ ...user, sendState: 'failed', receiptCaption: 'sending', optimisticId: 'send-1' }));
  assert.match(failed.querySelector('.slot-chat-send-status')?.textContent || '', /Failed/);
  assert.ok(failed.querySelector('.slot-chat-send-retry[data-optimistic-id="send-1"]'));
});

test('question answer messages present labels and notes while copying the original answer', () => {
  const text = 'Answering your question:\n\nQ1 (Color): Green\nnote (Q1): Keep the accent';
  const doc = rendered(row({ isUser: true, displayRule: 'bubble:user', text }));
  const bubble = doc.querySelector('.slot-chat-user-bubble');
  assert.ok(bubble?.textContent?.includes('Color'));
  assert.ok(bubble?.textContent?.includes('Green'));
  assert.ok(bubble?.textContent?.includes('Keep the accent'));
  assert.equal(bubble?.textContent?.includes('Answering your question:'), false);
  assert.equal(doc.querySelector('[data-copy-text]')?.getAttribute('data-copy-text'), text);
});

test('photo-only rows omit empty text controls and photos precede a caption', () => {
  const attachments = [{ key: 'photo', mime: 'image/png', width: 100, height: 100 }];
  const doc = rendered(row({ isUser: true, displayRule: 'bubble:user', text: '', attachments }));
  assert.ok(doc.querySelector('img'));
  assert.equal(doc.querySelector('.slot-chat-user-bubble'), null);
  assert.equal(doc.querySelector('.slot-chat-message-copy'), null);
  const caption = rendered(row({ isUser: true, displayRule: 'bubble:user', text: 'Caption', attachments }));
  const children = [...caption.querySelector('article')!.children];
  assert.ok(children.findIndex(el => el.classList.contains('slot-chat-media-grid'))
    < children.findIndex(el => el.classList.contains('slot-chat-user-bubble')));
});


test('durable-only answers project once at resolution time and yield to a matching event', () => {
  const notification = {
    notification_id: 'durable-1', producer: 'agent_question.v1', state: 'answered',
    resolved_at: '2026-09-12T01:01:00Z', updated_at: '2026-09-12T04:00:00Z',
    question: { question_id: 'q1', state: 'answered', options: [{ value: 'green', label: 'Green' }],
      answer: { selections: ['green'], note: 'Keep the accent' } },
  };
  const before = row({ id: 'before', text: 'Before answer', timestamp: '2026-09-12T01:00:00Z' });
  const after = row({ id: 'after', text: 'After answer', timestamp: '2026-09-12T01:02:00Z' });
  const options = { resolvedQuestions: [notification] } as never;
  const html = renderTranscriptTimelineHtml({ transcriptItems: [before, after] } as never, undefined, options);
  const doc = new JSDOM(html).window.document;
  assert.match(doc.body.textContent || '', /Green/);
  assert.match(doc.body.textContent || '', /Keep the accent/);
  assert.ok(html.indexOf('Before answer') < html.indexOf('Green'));
  assert.ok(html.indexOf('Green') < html.indexOf('After answer'), 'consumption time is not resolution time');
  const echo = row({ id: 'echo', text: 'Answered: Green', eventCase: 'agent-question-answer',
    notificationId: 'durable-1', displayRule: 'activity:question' });
  const echoed = renderTranscriptTimelineHtml({ transcriptItems: [before, echo, after] } as never, undefined, options);
  assert.equal((new JSDOM(echoed).window.document.body.textContent?.match(/Green/g) || []).length, 1, 'matching authoritative answer suppresses projection');
});


test('preformatted diagrams retain box drawing, spacing and line breaks', () => {
  const text = '┌────┐\n│ X  │\n└────┘';
  const doc = rendered(row({ text }));
  assert.equal(doc.querySelector('pre code')?.textContent, text);
});


test('the core direct-correlated receipt reaches the rendered caption without inferring delivery from an RPC', () => {
  const streamId = 'local:receipt';
  const session = { stream_id: streamId, host: 'local', provider: 'codex', session_name: 'receipt', online: true,
    last_event_at: '2026-09-12T00:00:00Z', last_text: '', last_kind: '', draft: '', pending: false, working: false };
  const sending = sendOptimisticMessage({ ...initialPentacleStreamState, connected: true, sessions: [session] } as never,
    { streamId, text: 'Keep this change', optimisticId: 'receipt-1', requestId: 'request-1', createdAt: Date.parse(session.last_event_at), windowStartedAt: Date.parse(session.last_event_at) });
  const pending = selectSessionDetail(sending, streamId, { visibleCount: 'all' });
  assert.doesNotMatch(renderTranscriptTimelineHtml(pending), />Sent</);
  const echo = { daemon_seq: 20, host: 'local', provider: 'codex', stream_id: streamId, session_id: 'receipt', session_name: 'receipt',
    timestamp: '2026-09-12T00:00:01Z', kind: 'USER', text: 'Keep this change', optimistic_id: 'receipt-1', raw: { receipt_state: 'landed' } };
  const confirmed = selectSessionDetail(applyPentacleEvent(sending, echo as never), streamId, { visibleCount: 'all' });
  assert.equal(confirmed?.transcriptItems.length, 1);
  assert.equal(confirmed?.transcriptItems[0]?.receiptCaption, 'sent');
  assert.equal(confirmed?.transcriptItems[0]?.timestamp, echo.timestamp);
  assert.match(renderTranscriptTimelineHtml(confirmed), />Sent</);
});

test('disclosure expansion persists across refresh and remains isolated to its stream', () => {
  const doc = new JSDOM('<div id="transcript"></div>').window.document;
  const container = doc.getElementById('transcript')!;
  const item = row({ tone: 'tool', displayRule: 'activity:tool-output', disclosure: {
    mode: 'collapsed-preview', previewText: 'Summary', expandedText: 'Complete result', expandable: true } });
  const store = { selectSessionDetail: (streamId: string) => ({ streamId, transcriptItems: [item] }) } as never;
  renderStreamTranscript('local:first', container, { store });
  container.querySelector('details')!.open = true;
  renderStreamTranscript('local:first', container, { store });
  assert.equal(container.querySelector('details')!.open, true);
  renderStreamTranscript('local:second', container, { store });
  assert.equal(container.querySelector('details')!.open, false);
});


test('transcript rows and disclosures expose the locked accessible names and roles', () => {
  const disclosure = { mode: 'collapsed-preview', previewText: 'Preview', expandedText: 'Full result', expandable: true };
  const cases = [
    { item: row({ isUser: true, displayRule: 'bubble:user' }), name: 'User message' },
    { item: row({ tone: 'tool', displayRule: 'activity:tool-output', disclosure }), name: 'Tool result', summary: 'Show full tool result' },
    { item: row({ tone: 'agent', label: 'Researcher', displayRule: 'bubble:agent', disclosure }), name: 'Subagent · Researcher', summary: 'Show full Researcher output' },
    { item: row({ displayRule: 'activity:code-block' }), name: 'Code block' },
    { item: row({ displayRule: 'activity:tool-batch' }), name: 'Tool activity' },
    { item: row({ displayRule: 'activity:thinking', text: 'Thinking about the change' }), name: 'Thinking about the change' },
    { item: row({ displayRule: 'system:compacted' }), name: 'Compacted transcript' },
  ];
  for (const c of cases) {
    const doc = rendered(c.item);
    assert.equal(doc.querySelector('article')?.getAttribute('aria-label'), c.name);
    if (c.summary) assert.equal(doc.querySelector('summary')?.getAttribute('aria-label'), c.summary);
  }
  const file = rendered(row({ text: 'Edited example.js (1 line)\n    indented body' }));
  assert.equal(file.querySelector('.slot-chat-file-card')?.tagName, 'ARTICLE');
  assert.equal(file.querySelector('.slot-chat-file-card')?.getAttribute('aria-label'), 'File Edited example.js');
  assert.equal(file.querySelector('summary')?.getAttribute('aria-label'), 'Show file body');
  for (const displayRule of ['terminal:divider', 'activity:turn-summary']) {
    const doc = new JSDOM(renderTranscriptItemHtml(row({ displayRule }), undefined, { showTurnDuration: true })).window.document;
    assert.ok(doc.querySelector('[role="separator"]'));
  }
});


test('file body disclosure and copy preserve indentation and trailing whitespace', () => {
  const body = '    const value = 1;  \n  return value;\n';
  const doc = rendered(row({ text: 'Edited example.js (2 lines)\n' + body }));
  assert.equal(doc.querySelector('details pre code')?.textContent, body);
  assert.equal(doc.querySelector('details .slot-chat-code-copy')?.getAttribute('data-copy-text'), body);
});


test('attachment-only user messages survive fetched history and snapshots without admitting empty noise', () => {
  const streamId = 'local:photo-history';
  const session = { stream_id: streamId, host: 'local', provider: 'codex', session_id: 'photo-history', session_name: 'photo-history', online: true, last_text: '', last_kind: 'USER', last_event_at: '2026-09-12T00:00:00Z' };
  const photo = { daemon_seq: 1, host: 'local', provider: 'codex', stream_id: streamId, session_id: 'photo-history', session_name: 'photo-history', timestamp: session.last_event_at, kind: 'USER', text: '', attachments: [{ key: 'a'.repeat(64), mime: 'image/png', width: 1, height: 1 }] };
  const noise = ['ASSIST', 'DRAFT', 'WORKING'].map((kind, i) => ({ ...photo, kind, daemon_seq: i + 2, attachments: [] }));
  const initial = { ...initialPentacleStreamState, connected: true, sessions: [session] } as never;
  const fetched = applyFetchedStreamEvents(initial, [photo, ...noise] as never, { requestedStreamId: streamId });
  const snapshot = applyPentacleSnapshotMessage(initial, { sessions: [session], events: [photo, ...noise] } as never);
  for (const state of [fetched, snapshot]) {
    const detail = selectSessionDetail(state, streamId, { visibleCount: 'all' });
    assert.equal(detail?.transcriptItems.length, 1);
    assert.equal(detail?.transcriptItems[0].attachments?.[0]?.key, photo.attachments[0].key);
    const doc = new JSDOM(renderTranscriptTimelineHtml(detail)).window.document;
    assert.equal(doc.querySelectorAll('.slot-chat-media-button').length, 1);
    assert.equal(doc.querySelectorAll('.slot-chat-user-bubble, .slot-chat-message-copy').length, 0);
  }
});
