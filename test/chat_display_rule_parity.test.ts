// Phase 6b (public_chat_ui) — DISPLAY-RULE PARITY.
//
// The guarantee: desktop and mobile make IDENTICAL display-rule decisions
// because they run the SAME interpreter (chat-core's
// interpretPentacleEvent). This test proves the DESKTOP VIEW faithfully follows
// the shared interpreter's decision for EVERY event case:
//
//   for each fixture event:
//     rule  = interpretPentacleEvent(event).displayRule   (the shared decision)
//     row   = render the event through the store + window.PentacleChatView
//     assert: the produced DOM matches `rule`:
//       hidden:* / draft:*   -> NO row
//       bubble:user          -> .slot-chat-row.is-user > .slot-chat-user-bubble
//       bubble:assistant     -> .slot-chat-row > .slot-chat-assistant-card
//       activity:*           -> .slot-chat-activity pill (dot + body)
//       terminal:divider     -> .slot-chat-terminal-divider
//       system:compacted     -> .slot-chat-compacted
//
// We render through the REAL renderer path (ChatStoreController.applyFrame ->
// selectSessionDetail -> renderTranscriptTimelineHtml) into a jsdom container,
// so this is the actual desktop output, not a mock.
//
// Run via `npm run test:parity`.

import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';

import {
  interpretPentacleEvent,
  type PentacleEvent,
  type PentacleDisplayRule,
} from 'chat-core';
import { ChatStoreController } from '../renderer/src/chat_store_controller';
import {
  renderStreamTranscript,
  renderTranscriptItemHtml,
  type ViewChrome,
} from '../renderer/src/shared_transcript_view';

const CHROME: ViewChrome = {
  header: '#102a4a',
  accent: '#4da3ff',
  surface: '#0c1827',
  border: '#2f6ca5',
  title: 'hostc',
};
const SHOW_TURN_DURATION = { showTurnDuration: true };

const STREAM = 'hostc:codex-hostc-1';

let seq = 1000;
function makeEvent(over: Partial<PentacleEvent> & Record<string, unknown> = {}): PentacleEvent {
  seq += 1;
  return {
    daemon_seq: seq,
    host: 'hostc',
    provider: 'codex',
    session_id: 'sess-1',
    session_name: 'codex-hostc-1',
    stream_id: STREAM,
    timestamp: '2026-05-25T12:00:00.000Z',
    kind: 'ASSIST',
    text: 'hello from the model',
    ...over,
  } as PentacleEvent;
}

function makeSession(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    stream_id: STREAM,
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

function newDom() {
  const dom = new JSDOM('<!doctype html><html><body><div id="c"></div></body></html>');
  return dom.window.document.getElementById('c') as unknown as HTMLElement;
}

// The fixture matrix: one entry per event case/kind the interpreter handles.
// `expectedRulePrefix` documents the shared interpreter family we assert the
// DESKTOP DOM follows. The test recomputes the EXACT rule from the interpreter
// (not the prefix) so it stays honest if the interpreter is refined.
type Fixture = {
  name: string;
  event: PentacleEvent;
  // The display-rule family the DOM assertion is keyed to. Used both to assert
  // the interpreter's decision falls in this family AND to pick the DOM check.
  expectedFamily:
    | 'bubble:user'
    | 'bubble:assistant'
    | 'activity'
    | 'terminal:divider'
    | 'system:compacted'
    | 'hidden'
    | 'draft';
};

const FIXTURES: Fixture[] = [
  // ── USER ──
  {
    name: 'USER message -> bubble:user',
    event: makeEvent({ kind: 'USER', text: 'please do the thing' }),
    expectedFamily: 'bubble:user',
  },
  {
    name: 'USER (claude-jsonl) -> bubble:user',
    event: makeEvent({ kind: 'USER', text: 'structured user', raw: { source: 'claude-jsonl' } as never }),
    expectedFamily: 'bubble:user',
  },
  {
    name: 'USER image attachment -> bubble:user media',
    event: makeEvent({
      kind: 'USER',
      text: 'caption',
      attachments: [{ key: 'd'.repeat(64), mime: 'image/png', width: 24, height: 24 }],
    } as never),
    expectedFamily: 'bubble:user',
  },
  {
    name: 'USER codex-helper suggestion -> hidden:helper (NO row)',
    event: makeEvent({ kind: 'USER', text: 'explore the repository structure' }),
    expectedFamily: 'hidden',
  },
  // ── ASSIST / ASSIST_TEXT ──
  {
    name: 'ASSIST prose -> bubble:assistant',
    event: makeEvent({ kind: 'ASSIST', text: 'Here is the final answer to your question.' }),
    expectedFamily: 'bubble:assistant',
  },
  {
    name: 'ASSIST_TEXT (claude-jsonl) -> bubble:assistant',
    event: makeEvent({ kind: 'ASSIST_TEXT', text: 'Structured assistant text.', raw: { source: 'claude-jsonl' } as never }),
    expectedFamily: 'bubble:assistant',
  },
  {
    name: 'ASSIST file edit -> activity:file-change',
    event: makeEvent({ kind: 'ASSIST', text: 'Edited renderer/app.js (+12 -3)' }),
    expectedFamily: 'activity',
  },
  {
    name: 'ASSIST explore -> activity:explored',
    event: makeEvent({ kind: 'ASSIST', text: 'Explored the services directory' }),
    expectedFamily: 'activity',
  },
  {
    name: 'ASSIST working-status noise -> hidden:status (NO row)',
    event: makeEvent({ kind: 'ASSIST', text: 'Working (5s • esc to interrupt)' }),
    expectedFamily: 'hidden',
  },
  // ── THINK / THINKING ──
  {
    name: 'THINK -> activity:thinking',
    event: makeEvent({ kind: 'THINK', text: 'Planning the change\nWill touch two files' }),
    expectedFamily: 'activity',
  },
  {
    name: 'THINKING (claude-jsonl) -> activity:thinking',
    event: makeEvent({ kind: 'THINKING', text: 'Considering options', raw: { source: 'claude-jsonl' } as never }),
    expectedFamily: 'activity',
  },
  // ── TOOL / TOOL_USE / TOOL_RESULT / TOOL_BATCH_SUMMARY ──
  {
    name: 'TOOL command -> activity:command',
    event: makeEvent({ kind: 'TOOL', text: 'Bash npm test' }),
    expectedFamily: 'activity',
  },
  {
    name: 'TOOL-OUT -> activity:tool-output',
    event: makeEvent({ kind: 'TOOL-OUT', text: '12 passing' }),
    expectedFamily: 'activity',
  },
  {
    name: 'TOOL_USE (claude-jsonl) -> activity (command)',
    event: makeEvent({ kind: 'TOOL_USE', text: 'ls -la', raw: { source: 'claude-jsonl', tool_name: 'Bash' } as never }),
    expectedFamily: 'activity',
  },
  {
    name: 'TOOL_RESULT (claude-jsonl) -> activity:tool-output',
    event: makeEvent({ kind: 'TOOL_RESULT', text: 'ok', raw: { source: 'claude-jsonl', tool_name: 'Edit', is_error: false, tool_input: { old_string: 'a', new_string: 'a\nb' } } as never }),
    expectedFamily: 'activity',
  },
  {
    name: 'TOOL_BATCH_SUMMARY (claude-jsonl) -> activity:tool-batch',
    event: makeEvent({ kind: 'TOOL_BATCH_SUMMARY', text: 'Ran 3 tools', raw: { source: 'claude-jsonl' } as never }),
    expectedFamily: 'activity',
  },
  // ── SYSTEM / turn-summary / compacted ──
  {
    name: 'SYSTEM -> activity:system',
    event: makeEvent({ kind: 'SYSTEM', text: 'Session resumed' }),
    expectedFamily: 'activity',
  },
  {
    name: 'SYSTEM turn-summary (claude-jsonl) -> activity:turn-summary',
    event: makeEvent({ kind: 'SYSTEM', text: 'Turn complete', raw: { source: 'claude-jsonl', subtype: 'turn-summary' } as never }),
    expectedFamily: 'activity',
  },
  {
    name: 'Context Compacted -> system:compacted',
    event: makeEvent({ kind: 'ASSIST', text: 'Context Compacted to save tokens' }),
    expectedFamily: 'system:compacted',
  },
  // ── WORKING (claude-jsonl structured) ──
  {
    name: 'WORKING (claude-jsonl) -> hidden:status (NO row)',
    event: makeEvent({ kind: 'WORKING', text: 'Working…', raw: { source: 'claude-jsonl' } as never }),
    expectedFamily: 'hidden',
  },
  // ── terminal furniture ("Worked for" duration line) ──
  // Converged shared-core behavior: a raw "Worked for" terminal-furniture line
  // (ASSIST/SYSTEM Codex pane scrape) is classified as hidden furniture by
  // isTerminalFurnitureText — the same suppression mobile applies (asserted in
  // chat-core tests/peerAgentMessages.test.ts). The interpreter no
  // longer emits `terminal:divider` from these events; that displayRule is now
  // render-only and its DOM is covered by shared_transcript_view.test.ts.
  {
    name: 'terminal furniture ("Worked for") -> hidden',
    event: makeEvent({ kind: 'ASSIST', text: '──────── Worked for 2m 30s ────────' }),
    expectedFamily: 'hidden',
  },
  // ── transient noise ──
  {
    name: 'transient noise (blank-after-clean) -> hidden:noise (NO row)',
    event: makeEvent({ kind: 'TOOL', text: '⎿ (No output)' }),
    expectedFamily: 'hidden',
  },
  // ── DRAFT ──
  {
    name: 'DRAFT -> draft:composer (NO transcript row)',
    event: makeEvent({ kind: 'DRAFT', text: 'a half-typed message' }),
    expectedFamily: 'draft',
  },
];

// Map an interpreter displayRule to the assertion family.
function familyOf(rule: PentacleDisplayRule | string): Fixture['expectedFamily'] {
  if (rule === 'bubble:user') return 'bubble:user';
  if (rule === 'bubble:assistant') return 'bubble:assistant';
  if (rule === 'terminal:divider') return 'terminal:divider';
  if (rule === 'system:compacted') return 'system:compacted';
  if (rule.startsWith('activity:')) return 'activity';
  if (rule.startsWith('hidden:')) return 'hidden';
  if (rule.startsWith('draft:')) return 'draft';
  throw new Error(`unmapped displayRule: ${rule}`);
}

// Assert the rendered DOM for a single transcript-item HTML string matches the
// expected family. Used for present rows (NO-row families are checked at the
// full-transcript level so we prove the row is truly absent end-to-end).
function assertRowDom(container: HTMLElement, family: Fixture['expectedFamily'], label: string) {
  switch (family) {
    case 'bubble:user':
      assert.ok(
        container.querySelector('.slot-chat-row.is-user .slot-chat-user-bubble'),
        `${label}: expected a user bubble`,
      );
      break;
    case 'bubble:assistant':
      assert.ok(
        container.querySelector('.slot-chat-row .slot-chat-assistant-card'),
        `${label}: expected an assistant card`,
      );
      assert.equal(
        container.querySelector('.slot-chat-user-bubble'),
        null,
        `${label}: assistant must NOT be a user bubble`,
      );
      break;
    case 'activity':
      assert.ok(
        container.querySelector('.slot-chat-activity'),
        `${label}: expected an activity pill`,
      );
      assert.ok(
        container.querySelector('.slot-chat-activity-dot'),
        `${label}: activity pill has a dot`,
      );
      break;
    case 'terminal:divider':
      assert.ok(
        container.querySelector('.slot-chat-terminal-divider'),
        `${label}: expected a terminal divider`,
      );
      break;
    case 'system:compacted':
      assert.ok(
        container.querySelector('.slot-chat-compacted'),
        `${label}: expected a compacted marker`,
      );
      break;
    default:
      throw new Error(`assertRowDom called with NO-row family ${family}`);
  }
}

test('the fixture matrix covers every interpreter displayRule family', () => {
  const families = new Set(FIXTURES.map((f) => f.expectedFamily));
  // `terminal:divider` is intentionally NOT required here: the converged shared
  // interpreter no longer PRODUCES it from real events (raw "Worked for" lines
  // are hidden furniture). It survives only as a render-only displayRule, whose
  // DOM is covered by shared_transcript_view.test.ts.
  for (const fam of ['bubble:user', 'bubble:assistant', 'activity', 'system:compacted', 'hidden', 'draft'] as const) {
    assert.ok(families.has(fam), `fixture matrix must cover the "${fam}" family`);
  }
});

for (const fixture of FIXTURES) {
  test(`display-rule parity: ${fixture.name}`, () => {
    // 1) The SHARED interpreter's decision for this event.
    const interpreted = interpretPentacleEvent(fixture.event, 'hostc');
    const rule = interpreted.displayRule;
    const family = familyOf(rule);
    assert.equal(
      family,
      fixture.expectedFamily,
      `interpreter decided ${rule} (family ${family}) but fixture expected family ${fixture.expectedFamily}`,
    );

    // 2) Render the event through the REAL desktop path (store + view).
    const controller = new ChatStoreController();
    controller.applyFrame({ type: 'snapshot', events: [], sessions: [makeSession()], drafts: {} });
    if (fixture.event.kind === 'DRAFT') {
      // Drafts arrive in the snapshot drafts map / draft channel, not as a
      // committed chat.event. Seed it as a draft so the path is faithful.
      controller.applyFrame({ type: 'snapshot', events: [], sessions: [makeSession()], drafts: { [STREAM]: fixture.event } });
    } else {
      controller.applyFrame({ type: 'chat.event', event: fixture.event });
    }

    const container = newDom();
    // Render with tools + system included so activity/system rows survive the
    // selector's tool/system filter (the desktop slot view uses the same
    // selector; this is the "show everything" parity surface).
    const detail = controller.selectSessionDetail(STREAM, {
      includeTools: true,
      includeSystem: true,
      visibleCount: 120,
      includeDraft: false,
    });
    // renderStreamTranscript writes the timeline HTML into `container` itself
    // (and returns the detail). We inject a store stub that returns the same
    // detail we asserted on, so the rendered DOM is the real view output for
    // exactly this session's transcript.
    renderStreamTranscript(STREAM, container, {
      store: {
        selectSessionDetail: () => detail,
      } as never,
      chrome: CHROME,
    });

    // 3) Assert the DOM matches the shared decision.
    if (family === 'hidden' || family === 'draft') {
      // hidden:* and draft:composer must produce NO transcript row at all.
      assert.equal(
        container.querySelector('.slot-chat-row, .slot-chat-terminal-divider, .slot-chat-compacted, .slot-chat-activity'),
        null,
        `${fixture.name}: ${rule} must render NO row (container should be empty of transcript rows)`,
      );
      // The selector itself must also have dropped it from transcriptItems.
      const leaked = (detail?.transcriptItems || []).filter(
        (it) => it.displayRule === rule,
      );
      assert.equal(leaked.length, 0, `${fixture.name}: selector must drop ${rule} from transcriptItems`);
    } else {
      const renderOptions = rule === 'terminal:divider' || rule === 'activity:turn-summary'
        ? SHOW_TURN_DURATION
        : {};
      container.innerHTML = '';
      renderStreamTranscript(STREAM, container, {
        store: {
          selectSessionDetail: () => detail,
        } as never,
        chrome: CHROME,
        ...renderOptions,
      });
      assertRowDom(container, family, fixture.name);
      if (fixture.name.includes('image attachment')) {
        assert.ok(container.querySelector('.slot-chat-media-button[data-attachment-key]'), 'attachment media bubble rendered');
      }
      // And the transcript item the selector produced for this event carries
      // EXACTLY the interpreter's displayRule (no view-side reclassification).
      const item = (detail?.transcriptItems || []).find((it) => it.displayRule === rule)
        || (detail?.transcriptItems || [])[0];
      assert.ok(item, `${fixture.name}: a transcript item was produced`);
      assert.equal(
        familyOf(item!.displayRule),
        family,
        `${fixture.name}: the rendered item's displayRule family (${item!.displayRule}) must match the interpreter family ${family}`,
      );
    }
  });
}

// A direct view-level guarantee: feed the view a synthetic item for EACH
// displayRule and assert the displayRule -> DOM contract holds in isolation
// (independent of selector filtering). This pins the view's rule->DOM map.
test('renderTranscriptItemHtml honors the displayRule -> DOM contract for every rule', () => {
  const base = {
    id: 'x', timestampLabel: '', label: '', tone: 'assistant', provider: '', source: '',
    kind: 'ASSIST', isUser: false, eventCase: 'unknown',
  };
  const cases: Array<{ rule: string; text: string; check: (c: HTMLElement) => void; isUser?: boolean; showTurnDuration?: boolean }> = [
    { rule: 'bubble:user', text: 'hi', isUser: true, check: (c) => assert.ok(c.querySelector('.slot-chat-user-bubble')) },
    { rule: 'bubble:assistant', text: 'plan', check: (c) => assert.ok(c.querySelector('.slot-chat-assistant-card')) },
    { rule: 'activity:thinking', text: 'thinking', check: (c) => assert.ok(c.querySelector('.slot-chat-activity')) },
    { rule: 'activity:turn-summary', text: 'Turn complete', showTurnDuration: true, check: (c) => assert.ok(c.querySelector('.slot-chat-activity')) },
    { rule: 'activity:command', text: 'Ran cmd', check: (c) => assert.ok(c.querySelector('.slot-chat-activity')) },
    { rule: 'activity:tool-output', text: 'out', check: (c) => assert.ok(c.querySelector('.slot-chat-activity')) },
    { rule: 'terminal:divider', text: 'Worked for 1m', showTurnDuration: true, check: (c) => assert.ok(c.querySelector('.slot-chat-terminal-divider')) },
    { rule: 'system:compacted', text: 'Context', check: (c) => assert.ok(c.querySelector('.slot-chat-compacted')) },
    { rule: 'hidden:status', text: 'noise', check: (c) => assert.equal(c.innerHTML, '') },
    { rule: 'hidden:noise', text: 'noise', check: (c) => assert.equal(c.innerHTML, '') },
    { rule: 'hidden:helper', text: 'helper', check: (c) => assert.equal(c.innerHTML, '') },
    { rule: 'draft:composer', text: 'draft', check: (c) => assert.equal(c.innerHTML, '') },
  ];
  for (const cse of cases) {
    const container = newDom();
    const item = { ...base, text: cse.text, isUser: Boolean(cse.isUser), displayRule: cse.rule } as never;
    container.innerHTML = renderTranscriptItemHtml(item, CHROME, { showTurnDuration: cse.showTurnDuration });
    cse.check(container);
  }
});
