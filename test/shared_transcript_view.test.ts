// Phase 4 (public_behavior_contract) shared-core transcript VIEW tests.
//
// Feeds representative RAW daemon frames into the renderer store controller,
// then renders a stream via the NEW shared-core view (renderer/src/
// shared_transcript_view.ts) into a jsdom container and asserts the produced
// DOM per displayRule. Proves XSS escaping and multi-slot routing.
//
// Run via `npm run test:chat-view`, which esbuild-bundles this TS to a temp CJS
// file (resolving the bare 'pentacle-chat-core' specifier like the renderer
// bundle does) and runs it under `node --test`. jsdom is a devDependency.

import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';

import { ChatStoreController } from '../renderer/src/chat_store_controller';
import {
  renderStreamTranscript,
  renderTranscriptTimelineHtml,
  renderTranscriptItemHtml,
  mountSlotTranscript,
  type ViewChrome,
} from '../renderer/src/shared_transcript_view';

const CHROME: ViewChrome = {
  header: '#102a4a',
  accent: '#4da3ff',
  surface: '#0c1827',
  border: '#2f6ca5',
  title: 'hostc',
};

function makeEvent(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    daemon_seq: 1,
    host: 'hostc',
    provider: 'codex',
    session_id: 'sess-1',
    session_name: 'codex-hostc-1',
    stream_id: 'hostc:codex-hostc-1',
    timestamp: '2026-05-25T12:00:00.000Z',
    kind: 'ASSIST',
    text: 'hello from the model',
    ...over,
  };
}

function makeSession(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    stream_id: 'hostc:codex-hostc-1',
    host: 'hostc',
    provider: 'codex',
    session_name: 'codex-hostc-1',
    display_name: 'codex-hostc-1',
    last_event_at: '2026-05-25T12:00:00.000Z',
    last_text: '',
    last_kind: 'ASSIST',
    draft: '',
    pending: false,
    working: false,
    online: true,
    ...over,
  };
}

const STREAM = 'hostc:codex-hostc-1';

function newDom() {
  const dom = new JSDOM('<!doctype html><html><body><div id="c"></div></body></html>');
  return { dom, container: dom.window.document.getElementById('c') as unknown as HTMLElement };
}

// A controller seeded so the stream resolves, with the given events applied.
function controllerWithEvents(events: Record<string, unknown>[], sessionOver: Record<string, unknown> = {}) {
  const controller = new ChatStoreController();
  controller.applyFrame({ type: 'snapshot', events: [], sessions: [makeSession(sessionOver)], drafts: {} });
  for (const ev of events) controller.applyFrame({ type: 'chat.event', event: ev });
  return controller;
}

test('bubble:user renders an escaped user bubble (XSS payload neutralized)', () => {
  const xss = '<script>alert("pwn")</script> & <img src=x onerror=alert(1)>';
  const controller = controllerWithEvents([
    makeEvent({ daemon_seq: 10, kind: 'USER', text: xss }),
  ]);
  const { container } = newDom();
  renderStreamTranscript(STREAM, container, { store: controller as never, chrome: CHROME });

  const bubble = container.querySelector('.slot-chat-row.is-user .slot-chat-user-bubble');
  assert.ok(bubble, 'user bubble rendered with slot-chat-user-bubble class');
  // No live <script> / <img> element was injected.
  assert.equal(container.querySelector('script'), null, 'no <script> element injected');
  assert.equal(container.querySelector('img'), null, 'no <img> element injected');
  // The raw markup contains the escaped entities, not live tags.
  assert.ok(container.innerHTML.includes('&lt;script&gt;'), 'script tag escaped to entities');
  assert.ok(container.innerHTML.includes('onerror=alert(1)'), 'attribute text preserved as escaped text');
  // textContent round-trips the original payload (proves it is TEXT, not DOM).
  assert.equal((bubble as Element).textContent, xss, 'bubble text content equals the original payload verbatim');
});

// ── B1 (chat_send_turn_lifecycle_batch2): user bubble send-state affordance ──

function userItem(over: Record<string, unknown> = {}) {
  return {
    id: 'u1', timestampLabel: '', label: '', tone: 'user', provider: 'codex', source: '',
    text: 'hello', kind: 'USER', isUser: true, eventCase: 'user-message',
    displayRule: 'bubble:user', optimisticId: 'opt1',
    ...over,
  } as never;
}

test('B1: a sending optimistic row renders a "sending…" affordance + is-sending class', () => {
  const html = renderTranscriptItemHtml(userItem({ sendState: 'sending' }), CHROME);
  assert.ok(/slot-chat-row is-user is-sending/.test(html), 'row carries is-sending');
  assert.ok(/slot-chat-send-status is-sending/.test(html), 'status node present');
  assert.ok(/sending…/.test(html), 'shows sending… label (no fabricated timer)');
  assert.ok(!/\d+s/.test(html), 'no timer/seconds rendered in the sending affordance');
});

test('B1: failed and cancelled rows render their affordances', () => {
  const failed = renderTranscriptItemHtml(userItem({ sendState: 'failed' }), CHROME);
  assert.ok(/is-user is-failed/.test(failed) && /Failed to send/.test(failed), 'failed affordance');
  const cancelled = renderTranscriptItemHtml(userItem({ sendState: 'cancelled' }), CHROME);
  assert.ok(/is-user is-cancelled/.test(cancelled) && /Cancelled/.test(cancelled), 'cancelled affordance');
});

test('B4: a queued row renders a "queued" affordance + is-queued class', () => {
  const html = renderTranscriptItemHtml(userItem({ sendState: 'queued' }), CHROME);
  assert.ok(/is-user is-queued/.test(html), 'row carries is-queued');
  assert.ok(/slot-chat-send-status is-queued/.test(html) && />queued</.test(html), 'queued label');
});

test('native queue: mountSlotTranscript shows mid-turn sends as sending immediately', async () => {
  const controller = new ChatStoreController();
  controller.setSendBridge(async () => ({ ok: true }));
  controller.applyFrame({ type: 'snapshot', events: [], sessions: [makeSession()], drafts: {} });
  const a = newDom();
  mountSlotTranscript(a.container, STREAM, { store: controller as never, chrome: CHROME });

  controller.sendTurn(STREAM, 'first'); // begins the turn
  controller.sendTurn(STREAM, 'follow up');
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(a.container.querySelector('.slot-chat-send-status.is-queued'), null, 'no queued affordance for native-queued send');
  assert.ok(a.container.querySelector('.slot-chat-send-status.is-sending'), 'mid-turn send renders as sending immediately');
});

test('B1: a confirmed user row (no sendState) renders an ordinary bubble (no status node)', () => {
  const html = renderTranscriptItemHtml(userItem({ sendState: undefined }), CHROME);
  assert.ok(/slot-chat-row is-user/.test(html), 'ordinary user row');
  assert.ok(!/slot-chat-send-status/.test(html), 'no status node for a confirmed bubble');
  assert.ok(!/ is-sending| is-failed| is-cancelled/.test(html), 'no send-state class');
});

test('G1: user image attachments render inline media bubbles with local viewer src', () => {
  const html = renderTranscriptItemHtml(userItem({
    text: 'caption',
    attachments: [{
      key: 'a'.repeat(64),
      mime: 'image/png',
      width: 640,
      height: 480,
      bytes: 1234,
      uri: 'blob:local-preview',
      name: 'photo.png',
    }],
  }), CHROME);
  const { container } = newDom();
  container.innerHTML = html;
  const media = container.querySelector('.slot-chat-media-button') as HTMLElement;
  assert.ok(media, 'media button rendered');
  assert.equal(media.dataset.viewerSrc, 'blob:local-preview');
  assert.equal(media.dataset.attachmentKey, 'a'.repeat(64));
  assert.ok(container.querySelector('.slot-chat-media-img[src="blob:local-preview"]'), 'image uses local preview src');
});

test('G1: server-keyed image attachments render a lazy blob placeholder', () => {
  const html = renderTranscriptItemHtml(userItem({
    text: '',
    attachments: [{
      key: 'b'.repeat(64),
      mime: 'image/jpeg',
      width: 10,
      height: 20,
    }],
  }), CHROME);
  const { container } = newDom();
  container.innerHTML = html;
  const media = container.querySelector('.slot-chat-media-button') as HTMLElement;
  const img = container.querySelector('.slot-chat-media-img') as HTMLElement;
  assert.equal(media.dataset.attachmentKey, 'b'.repeat(64));
  assert.equal(media.dataset.attachmentMime, 'image/jpeg');
  assert.equal(img.getAttribute('data-needs-blob'), '1');
  assert.ok(container.textContent?.includes('Loading image'), 'placeholder label rendered before fetch');
});

test('bubble:assistant renders an assistant card with escaped text', () => {
  const controller = controllerWithEvents([
    makeEvent({ daemon_seq: 11, kind: 'ASSIST', text: 'Here is a plan <b>bold</b>' }),
  ]);
  const { container } = newDom();
  renderStreamTranscript(STREAM, container, { store: controller as never, chrome: CHROME });

  const card = container.querySelector('.slot-chat-row .slot-chat-assistant-card');
  assert.ok(card, 'assistant card rendered');
  assert.equal(container.querySelector('b'), null, 'inline <b> from assistant text not injected as element');
  assert.ok(container.innerHTML.includes('&lt;b&gt;bold&lt;/b&gt;'), 'assistant inline tag escaped');
});

test('activity:* renders an activity pill (dot + title + detail)', () => {
  // A THINK event -> activity:thinking. Include tools/system so it survives the
  // selector filter, then render. Use a two-line text to exercise title+detail.
  const controller = controllerWithEvents([
    makeEvent({ daemon_seq: 12, kind: 'THINK', text: 'Planning the change\nWill edit two files' }),
  ]);
  const detail = controller.selectSessionDetail(STREAM, { includeTools: true, includeSystem: true, visibleCount: 120, includeDraft: false });
  const activityItem = detail!.transcriptItems.find((it) => String(it.displayRule).startsWith('activity:'));
  assert.ok(activityItem, 'an activity item is present');

  const html = renderTranscriptItemHtml(activityItem!, CHROME);
  const { dom, container } = newDom();
  container.innerHTML = html;
  void dom;
  assert.ok(container.querySelector('.slot-chat-activity'), 'activity container present');
  assert.ok(container.querySelector('.slot-chat-activity-dot'), 'activity dot present');
  const body = container.querySelector('.slot-chat-activity-body');
  assert.ok(body, 'activity body present');
  assert.ok(body!.querySelector('b'), 'activity title <b> present');
  assert.ok(body!.querySelector('p'), 'activity detail <p> present for multi-line text');
});

test('agent-orch notification.answer renders as a compact desktop question row', () => {
  const payload = {
    type: 'notification.answer',
    answer: {
      notification_id: 'q-19bccfa3',
      action_kind: 'select',
      label: 'hostc',
      selections: ['hostc'],
      note: 'Use the orchestrator.',
    },
  };
  const controller = controllerWithEvents([
    makeEvent({
      daemon_seq: 121,
      kind: 'USER',
      text: `[from daemon:notifications]\n[tell:notification-answer-q-19bccfa3]${JSON.stringify(payload)}`,
    }),
  ]);
  const { container } = newDom();
  renderStreamTranscript(STREAM, container, { store: controller as never, chrome: CHROME });

  assert.ok(container.querySelector('.slot-chat-activity'), 'question activity row rendered');
  assert.ok(container.textContent?.includes('Operator answered: hostc'), 'selected label rendered');
  assert.ok(container.textContent?.includes('Note: Use the orchestrator.'), 'note rendered');
  assert.equal(container.textContent?.includes('notification.answer'), false, 'raw JSON suppressed');
});

test('agent-orch prompt.ask.ok renders as a compact desktop ask confirmation', () => {
  const payload = {
    type: 'prompt.ask.ok',
    ok: true,
    question: {
      question_id: 'q-19bccfa3',
      state: 'open',
      envelope: {
        title: 'Choose a host',
        question_id: 'q-19bccfa3',
      },
    },
  };
  const controller = controllerWithEvents([
    makeEvent({
      daemon_seq: 122,
      kind: 'TOOL_RESULT',
      text: JSON.stringify(payload),
      raw: { source: 'claude-jsonl', tool_name: 'Bash', is_error: false },
    }),
  ]);
  const { container } = newDom();
  renderStreamTranscript(STREAM, container, { store: controller as never, chrome: CHROME });

  assert.ok(container.querySelector('.slot-chat-activity'), 'ask activity row rendered');
  assert.ok(container.textContent?.includes('Asked: Choose a host (q-19bccfa3)'), 'question title/id rendered');
  assert.equal(container.textContent?.includes('prompt.ask.ok'), false, 'raw ask envelope suppressed');
});

test('terminal:divider renders a terminal divider row when showTurnDuration is enabled', () => {
  // Converged parity behavior (shared pentacle-chat-core): a raw "Worked for"
  // terminal-furniture line is classified as hidden furniture by
  // isTerminalFurnitureText and is NOT surfaced as a durable transcript row —
  // the same suppression mobile applies. So it must not appear as a
  // terminal:divider item in the selected detail.
  const controller = controllerWithEvents([
    makeEvent({ daemon_seq: 13, kind: 'ASSIST', text: '──────── Worked for 2m 30s ────────' }),
  ]);
  const detail = controller.selectSessionDetail(STREAM, { includeSystem: true, visibleCount: 120, includeDraft: false });
  const surfacedFurniture = detail!.transcriptItems.find((it) => it.displayRule === 'terminal:divider');
  assert.ok(!surfacedFurniture, 'raw "Worked for" terminal furniture stays hidden, not surfaced as a transcript row');

  // The showTurnDuration render path still renders a terminal:divider row when a
  // divider item IS present, so the desktop turn-duration toggle keeps working
  // for any surfaced divider.
  const dividerItem = {
    id: 'divider-13', timestampLabel: '12:00', label: '', tone: 'assistant', provider: 'codex', source: '',
    text: 'Worked for 2m 30s', kind: 'ASSIST', isUser: false, eventCase: 'terminal-divider',
    displayRule: 'terminal:divider',
  } as never;
  const { container } = newDom();
  container.innerHTML = renderTranscriptItemHtml(dividerItem, CHROME, { showTurnDuration: true });
  assert.ok(container.querySelector('.slot-chat-terminal-divider'), 'terminal divider rendered');
  assert.ok(/Worked for/.test(container.textContent || ''), 'divider label present');
});

test('showTurnDuration defaults off and suppresses terminal:divider rows', () => {
  const dividerItem = {
    id: 'divider-1', timestampLabel: '12:00', label: '', tone: 'assistant', provider: 'codex', source: '',
    text: 'Worked for 2m 30s', kind: 'ASSIST', isUser: false, eventCase: 'terminal-divider',
    displayRule: 'terminal:divider',
  } as never;

  assert.equal(renderTranscriptItemHtml(dividerItem, CHROME), '', 'direct item render suppresses divider by default');
  const html = renderTranscriptTimelineHtml({ transcriptItems: [dividerItem] } as never, CHROME);
  assert.equal(html, '', 'timeline suppresses divider and its timestamp by default');
});

test('showTurnDuration defaults off and suppresses only activity:turn-summary', () => {
  const turnSummary = {
    id: 'summary-1', timestampLabel: '12:00', label: '', tone: 'assistant', provider: 'claude', source: '',
    text: 'Turn complete', kind: 'SYSTEM', isUser: false, eventCase: 'turn-summary',
    displayRule: 'activity:turn-summary',
  } as never;
  const thinking = {
    id: 'thinking-1', timestampLabel: '12:01', label: '', tone: 'assistant', provider: 'claude', source: '',
    text: 'Thinking\nKeeping this activity row', kind: 'THINK', isUser: false, eventCase: 'thinking',
    displayRule: 'activity:thinking',
  } as never;

  const html = renderTranscriptTimelineHtml({ transcriptItems: [turnSummary, thinking] } as never, CHROME);
  const { container } = newDom();
  container.innerHTML = html;
  assert.equal(container.textContent?.includes('Turn complete'), false, 'turn-summary activity is hidden by default');
  assert.ok(container.querySelector('.slot-chat-activity'), 'other activity rows still render');
  assert.ok(container.textContent?.includes('Thinking'), 'non-summary activity text remains visible');
});

test('showTurnDuration defaults off and suppresses inline timing annotations', () => {
  const item = {
    id: 'assist-1', timestampLabel: '', label: '', tone: 'assistant', provider: 'codex', source: '',
    text: 'Finished the change\nWorked for 1m 12s', kind: 'ASSIST', isUser: false, eventCase: 'assistant',
    displayRule: 'bubble:assistant',
  } as never;
  const hidden = renderTranscriptTimelineHtml({ transcriptItems: [item] } as never, CHROME);
  const shown = renderTranscriptTimelineHtml({ transcriptItems: [item] } as never, CHROME, { showTurnDuration: true });
  const { container } = newDom();
  container.innerHTML = hidden;
  assert.ok(container.querySelector('.slot-chat-assistant-card'), 'assistant card still renders');
  assert.equal(container.querySelector('.slot-chat-annotation.is-timing'), null, 'inline timing annotation hidden by default');
  assert.ok(container.textContent?.includes('Finished the change'), 'assistant prose remains visible');

  container.innerHTML = shown;
  assert.ok(container.querySelector('.slot-chat-annotation.is-timing'), 'inline timing annotation renders when enabled');
});

test('system:compacted renders a compacted marker row', () => {
  const controller = controllerWithEvents([
    makeEvent({ daemon_seq: 14, kind: 'ASSIST', text: 'Context Compacted to save tokens' }),
  ]);
  const detail = controller.selectSessionDetail(STREAM, { includeSystem: true, visibleCount: 120, includeDraft: false });
  const compactedItem = detail!.transcriptItems.find((it) => it.displayRule === 'system:compacted');
  assert.ok(compactedItem, 'system:compacted item present');

  const { container } = newDom();
  container.innerHTML = renderTranscriptItemHtml(compactedItem!, CHROME);
  assert.ok(container.querySelector('.slot-chat-compacted'), 'compacted marker rendered');
});

test('hidden:* items produce NO row', () => {
  // Working-status text -> hidden:status; the selector drops it from
  // transcriptItems. Even if a hidden item were forced through, the view
  // renders nothing for hidden:*.
  const controller = controllerWithEvents([
    makeEvent({ daemon_seq: 15, kind: 'ASSIST', text: 'Working (5s • esc to interrupt)' }),
  ]);
  const detail = controller.selectSessionDetail(STREAM, { includeTools: true, includeSystem: true, visibleCount: 120, includeDraft: false });
  assert.equal(
    detail!.transcriptItems.filter((it) => String(it.displayRule).startsWith('hidden:')).length,
    0,
    'selector drops hidden:* items entirely',
  );

  // Direct view guard: a synthetic hidden:* item renders to empty string.
  const synthetic = {
    id: 'x', timestampLabel: '', label: '', tone: 'assistant', provider: '', source: '',
    text: 'should not render', kind: 'ASSIST', isUser: false, eventCase: 'working-status',
    displayRule: 'hidden:status',
  } as never;
  assert.equal(renderTranscriptItemHtml(synthetic, CHROME), '', 'view renders nothing for hidden:* item');
});

test('multi-slot routing: stream A content appears only in container A, B only in B', () => {
  const STREAM_A = 'hostc:codex-hostc-1';
  const STREAM_B = 'hostb:claude-1';
  const controller = new ChatStoreController();
  controller.applyFrame({
    type: 'snapshot',
    events: [],
    sessions: [
      makeSession(),
      makeSession({ stream_id: STREAM_B, host: 'hostb', provider: 'claude', session_name: 'claude-1', display_name: 'claude-1' }),
    ],
    drafts: {},
  });
  controller.applyFrame({ type: 'chat.event', event: makeEvent({ daemon_seq: 20, kind: 'USER', text: 'ALPHA-ONLY-MESSAGE', stream_id: STREAM_A }) });
  controller.applyFrame({ type: 'chat.event', event: makeEvent({ daemon_seq: 21, kind: 'USER', text: 'BRAVO-ONLY-MESSAGE', stream_id: STREAM_B, host: 'hostb', provider: 'claude', session_name: 'claude-1' }) });

  const a = newDom();
  const b = newDom();
  renderStreamTranscript(STREAM_A, a.container, { store: controller as never, chrome: CHROME });
  renderStreamTranscript(STREAM_B, b.container, { store: controller as never, chrome: CHROME });

  assert.ok(a.container.textContent!.includes('ALPHA-ONLY-MESSAGE'), 'A contains its own message');
  assert.ok(!a.container.textContent!.includes('BRAVO-ONLY-MESSAGE'), "A does NOT contain B's message");
  assert.ok(b.container.textContent!.includes('BRAVO-ONLY-MESSAGE'), 'B contains its own message');
  assert.ok(!b.container.textContent!.includes('ALPHA-ONLY-MESSAGE'), "B does NOT contain A's message");
});

test('mountSlotTranscript re-renders only when THIS stream content-version changes', () => {
  const STREAM_A = 'hostc:codex-hostc-1';
  const STREAM_B = 'hostb:claude-1';
  const controller = new ChatStoreController();
  controller.applyFrame({
    type: 'snapshot',
    events: [],
    sessions: [
      makeSession(),
      makeSession({ stream_id: STREAM_B, host: 'hostb', provider: 'claude', session_name: 'claude-1', display_name: 'claude-1' }),
    ],
    drafts: {},
  });
  controller.applyFrame({ type: 'chat.event', event: makeEvent({ daemon_seq: 30, kind: 'USER', text: 'first A', stream_id: STREAM_A }) });

  const a = newDom();
  const mount = mountSlotTranscript(a.container, STREAM_A, { store: controller as never, chrome: CHROME });
  assert.ok(a.container.textContent!.includes('first A'), 'initial paint shows A content');

  // A frame for STREAM_B bumps B's version, not A's. A's DOM must be untouched.
  const beforeHtml = a.container.innerHTML;
  controller.applyFrame({ type: 'chat.event', event: makeEvent({ daemon_seq: 31, kind: 'USER', text: 'first B', stream_id: STREAM_B, host: 'hostb', provider: 'claude', session_name: 'claude-1' }) });
  assert.equal(a.container.innerHTML, beforeHtml, "B's frame did not re-render slot A");

  // A frame for STREAM_A bumps A's version -> A re-renders with the new content.
  controller.applyFrame({ type: 'chat.event', event: makeEvent({ daemon_seq: 32, kind: 'USER', text: 'second A', stream_id: STREAM_A }) });
  assert.ok(a.container.textContent!.includes('second A'), 'A re-rendered after its own frame');

  mount.unmount();
  // After unmount, further A frames do not mutate the container.
  const afterUnmount = a.container.innerHTML;
  controller.applyFrame({ type: 'chat.event', event: makeEvent({ daemon_seq: 33, kind: 'USER', text: 'third A', stream_id: STREAM_A }) });
  assert.equal(a.container.innerHTML, afterUnmount, 'no re-render after unmount');
});

test('B1: mountSlotTranscript re-renders on an optimistic send STATUS change (not just content-version)', () => {
  const controller = new ChatStoreController();
  controller.applyFrame({ type: 'snapshot', events: [], sessions: [makeSession()], drafts: {} });

  const a = newDom();
  mountSlotTranscript(a.container, STREAM, { store: controller as never, chrome: CHROME });

  // Optimistic send → a 'sending' row appears (status change only, the event
  // content-version bump is incidental; the gate must also catch the status).
  const optimisticId = controller.sendTurn(STREAM, 'queued msg');
  assert.ok(a.container.textContent!.includes('sending…'), 'mount shows sending… after optimistic send');

  // Daemon marks it failed (status-only transition — no new event). The mount
  // MUST repaint the row to its failed affordance.
  const requestId = controller.getState().optimisticSends?.[optimisticId]?.request_id as string;
  controller.applyFrame({ type: 'send.result', request_id: requestId, delivery: 'not_landed', reason: 'boom' });
  assert.ok(!a.container.textContent!.includes('sending…'), 'sending… cleared after status change');
  assert.ok(a.container.textContent!.includes('Failed to send'), 'mount repainted to failed affordance');
});

// --- Markdown rendering in assistant bubbles (chat UI hardening batch 1) ---

function assistantHtml(text: string): string {
  return renderTranscriptItemHtml(
    { displayRule: 'bubble:assistant', text, isUser: false } as never,
    CHROME,
  );
}

test('markdown: ## heading renders a heading element, not literal text', () => {
  const html = assistantHtml('## Done');
  assert.ok(html.includes('slot-chat-md-heading'), 'heading class present');
  assert.ok(html.includes('slot-chat-md-h2'), 'level-2 class present');
  assert.ok(!html.includes('<div class="slot-chat-line">## Done</div>'), 'raw "## Done" not rendered literally');
});

test('markdown: **bold** -> <strong>, *italic* -> <em>, `code` -> <code>', () => {
  const html = assistantHtml('a **b** _c_ `d`');
  assert.ok(html.includes('<strong>b</strong>'), 'bold');
  assert.ok(html.includes('<em>c</em>'), 'italic');
  assert.ok(html.includes('<code class="slot-chat-md-icode">d</code>'), 'inline code');
});

test('markdown: lists + fenced code render structurally', () => {
  const ul = assistantHtml('- one\n- two');
  assert.ok(/<ul class="slot-chat-md-list"><li>one<\/li><li>two<\/li><\/ul>/.test(ul), ul);
  const code = assistantHtml('```js\nconst x = 1;\n```');
  assert.ok(code.includes('<pre class="slot-chat-md-code"'), 'code block');
  assert.ok(code.includes('data-lang="js"'), 'language preserved');
  assert.ok(code.includes('const x = 1;'), 'code text preserved');
});

test('copy affordances: message and code buttons carry source text, not rendered HTML', () => {
  const source = 'Here is **source** text.\n```js\nconst x = 1;\n```';
  const html = assistantHtml(source);
  const { container } = newDom();
  container.innerHTML = html;
  const messageCopy = container.querySelector('.slot-chat-message-copy') as HTMLButtonElement | null;
  const codeCopy = container.querySelector('.slot-chat-code-copy') as HTMLButtonElement | null;
  assert.ok(messageCopy, 'message copy button present');
  assert.ok(codeCopy, 'code copy button present');
  assert.equal(messageCopy!.dataset.copyText, source, 'message button copies raw message source');
  assert.equal(codeCopy!.dataset.copyText, 'const x = 1;', 'code button copies raw code text');
  assert.equal(messageCopy!.getAttribute('aria-label'), 'Copy message');
  assert.equal(codeCopy!.getAttribute('aria-label'), 'Copy code block');
});

test('markdown: GFM table renders as escaped table DOM with per-column alignment', () => {
  const html = assistantHtml([
    '| Name | Score | Note |',
    '| :--- | ---: | :---: |',
    '| **Ada** | 42 | `<ok>` |',
  ].join('\n'));
  assert.ok(html.includes('<table class="slot-chat-md-table">'), html);
  assert.ok(html.includes('<th style="text-align:left">Name</th>'), html);
  assert.ok(html.includes('<th style="text-align:right">Score</th>'), html);
  assert.ok(html.includes('<th style="text-align:center">Note</th>'), html);
  assert.ok(html.includes('<td style="text-align:left"><strong>Ada</strong></td>'), html);
  assert.ok(html.includes('<td style="text-align:center"><code class="slot-chat-md-icode">&lt;ok&gt;</code></td>'), html);
});

test('markdown escape-first: HTML payload is escaped, never injected', () => {
  const html = assistantHtml('Look: <script>alert(1)</script> and **<b>x</b>**');
  assert.ok(!/<script>/.test(html), 'no raw <script> tag');
  assert.ok(html.includes('&lt;script&gt;'), 'script escaped');
  // bold marker still works but its inner HTML is escaped
  assert.ok(html.includes('<strong>&lt;b&gt;x&lt;/b&gt;</strong>'), 'escaped inside strong');
});

test('markdown link: safe href -> anchor with noopener; unsafe -> text only', () => {
  const safe = assistantHtml('[docs](https://example.com)');
  assert.ok(safe.includes('<a class="slot-chat-md-link" href="https://example.com" target="_blank" rel="noopener noreferrer">docs</a>'), safe);
  const unsafe = assistantHtml('[x](javascript:alert(1))');
  assert.ok(!unsafe.includes('<a '), 'no anchor for javascript: href');
  assert.ok(unsafe.includes('x'), 'link text preserved');
});

test('markdown preserves non-prose handling: tool label + tree log unchanged', () => {
  const html = assistantHtml('Explored\n└ src/app.js\n\nFixed the **bug**.');
  assert.ok(html.includes('slot-chat-annotation is-label'), 'tool label preserved');
  assert.ok(html.includes('slot-chat-loglist'), 'tree-char log list preserved');
  assert.ok(html.includes('<strong>bug</strong>'), 'prose markdown still rendered');
});

