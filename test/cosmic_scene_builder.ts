// Workstream E (public_chat_ui): cosmic visual-regression scene
// builder — the SINGLE source of truth for the desktop cosmic chat states.
//
// Both consumers read from here so the human-reviewable PNG artifact and the
// deterministic structural gate render the EXACT same DOM:
//   - test/generate_cosmic_chat_states.js  -> writes `.ui-review/desktop-chat/*.html`
//     (captured to baseline PNGs by scripts/capture-cosmic-baselines.js)
//   - test/cosmic_visual.test.ts            -> the hard structural assertions
//
// Fidelity bar: the transcript is built by the PRODUCTION renderer
// (`renderTranscriptTimelineHtml` over a REAL `ChatStoreController.selectSessionDetail`),
// the header ornaments by the PRODUCTION cosmic components (`arcaneRingFrame` /
// `providerTag` / `statusTag` / `starfield`), and the question docks by the
// PRODUCTION single-select markup + the real `renderMultiSelectQuestion`. The
// surface is opted into `.cosmic` exactly as app.js renderSlotChat does, so the
// scoped `cosmic_theme.css` + `cosmic_chat_surface.css` layers apply verbatim.
//
// Every factory relies on a jsdom `document`/`window` being installed on
// globalThis BEFORE these are called (the components call `document.createElementNS`
// at call time). The bundled test runner sets that up; see scripts/run-tests.js.

import { ChatStoreController } from '../renderer/src/chat_store_controller';
import { renderTranscriptTimelineHtml } from '../renderer/src/shared_transcript_view';
import {
  arcaneRingFrame,
  providerTag,
  statusTag,
  starfield,
  MACHINES,
} from '../renderer/src/cosmic_components';
// The multiSelect dock is the real live renderer (CJS module).
// eslint-disable-next-line @typescript-eslint/no-var-requires
const { renderMultiSelectQuestion } = require('../renderer/multiselect_question');

// ── This machine (mage the mage) drives the header chrome ────────────
const MACHINE = 'mage' as const;
const META = MACHINES[MACHINE];
const STREAM = 'mage:claude-mage-cosmic';
const stateStream = (name: StateName): string => `${STREAM}-${name}`;
const CHROME = {
  accent: META.accent,
  surface: '#101a16',
  border: '#356150',
  title: MACHINE,
} as const;

export const STATES = [
  'empty',
  'populated',
  'working',
  'question_single',
  'question_multiselect',
  'markdown_code',
  'markdown_table',
] as const;
export type StateName = (typeof STATES)[number];

// Human-facing titles for the gallery / manifest.
export const STATE_TITLES: Record<StateName, string> = {
  empty: 'Empty transcript',
  populated: 'Populated conversation',
  working: 'Working (no tool-row leak)',
  question_single: 'Question — single select',
  question_multiselect: 'Question — multiSelect',
  markdown_code: 'Markdown — code block',
  markdown_table: 'Markdown — table',
};

// ── Frame helpers (shaped like the daemon read-path frames the store consumes) ──

let seqCounter = 1000;

function userEvent(text: string): Record<string, unknown> {
  return event('USER', text);
}

function assistEvent(text: string): Record<string, unknown> {
  return event('ASSIST', text);
}

function event(kind: string, text: string, over: Record<string, unknown> = {}): Record<string, unknown> {
  seqCounter += 1;
  return {
    daemon_seq: seqCounter,
    host: 'mage',
    provider: 'claude',
    session_id: 'sess-cosmic',
    session_name: 'claude-mage-cosmic',
    stream_id: STREAM,
    timestamp: '2026-06-03T17:30:00.000Z',
    kind,
    text,
    ...over,
  };
}

function session(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    stream_id: STREAM,
    host: 'mage',
    provider: 'claude',
    session_name: 'claude-mage-cosmic',
    display_name: 'claude-mage-cosmic',
    last_event_at: '2026-06-03T17:30:00.000Z',
    // Default to no session-summary last_text so non-working baselines do not
    // grow a stray fallback row; states that need it (working repro, idle probe)
    // set last_text/last_kind explicitly.
    last_text: '',
    last_kind: '',
    draft: '',
    pending: false,
    working: false,
    online: true,
    ...over,
  };
}

// Seed a controller with a snapshot (events + session) and return it.
function seed(
  events: Record<string, unknown>[],
  over: Record<string, unknown> = {},
  streamId = STREAM,
): ChatStoreController {
  const controller = new ChatStoreController();
  const sessionName = streamId.split(':').pop() || 'claude-mage-cosmic';
  controller.applyFrame({
    type: 'snapshot',
    events: events.map((item) => ({
      ...item,
      stream_id: streamId,
      session_id: streamId,
      session_name: sessionName,
    })),
    sessions: [session({
      ...over,
      stream_id: streamId,
      session_id: streamId,
      session_name: sessionName,
      display_name: sessionName,
    })],
    drafts: {},
  });
  return controller;
}

// ── The user-bug gate: WORKING + a tool last_text must NOT leak a
//    session_summary_fallback tool/bash row from the default (tools-hidden)
//    selector; the fallback returns once the turn settles to idle. Ties to A's
//    fix (chat-core 3d93d9c: 60e2016 + 2426b63). ───────────────────────────────

const LEAKED_BASH_TEXT = 'Bash\nnpm test --silent';

export type FallbackProbe = {
  workingItems: Array<{ id?: string; text?: string }>;
  idleItems: Array<{ id?: string; text?: string }>;
  leakedText: string;
};

export function probeWorkingFallback(): FallbackProbe {
  // A WORKING session whose last_kind is a structured TOOL_USE and whose
  // last_text is a running Bash command (the exact user repro), with real
  // committed transcript content above it.
  const events = [userEvent('investigate the renderer'), assistEvent('Looking into it now.')];
  const working = seed(events, {
    working: true,
    last_kind: 'TOOL_USE',
    last_text: LEAKED_BASH_TEXT,
    last_event_at: '2026-06-03T17:30:05.000Z',
  });
  const workingDetail = working.selectSessionDetail(STREAM);

  // The same content, settled to idle — the fallback may now surface normally.
  const idle = seed(events, {
    working: false,
    last_kind: 'ASSIST',
    last_text: 'Worked for 4s · 8 msgs',
    last_event_at: '2026-06-03T17:30:09.000Z',
  });
  const idleDetail = idle.selectSessionDetail(STREAM);

  return {
    workingItems: (workingDetail?.transcriptItems ?? []) as Array<{ id?: string; text?: string }>,
    idleItems: (idleDetail?.transcriptItems ?? []) as Array<{ id?: string; text?: string }>,
    leakedText: LEAKED_BASH_TEXT,
  };
}

// ── Per-state transcript detail (drives the live selector) ───────────────

function detailFor(name: StateName) {
  const streamId = stateStream(name);
  switch (name) {
    case 'empty':
      // Truly empty: no events and an empty last_text/kind so the session-summary
      // fallback synthesizes nothing — the surface shows the empty-state node.
      return seed([], { last_text: '', last_kind: '' }, streamId).selectSessionDetail(streamId);
    case 'populated':
      return seed([
        userEvent('Can you theme the desktop chat surface?'),
        assistEvent('On it — porting the **cosmic** reskin to the renderer now.'),
        userEvent('Make the user bubbles JetBrains Mono.'),
        assistEvent('Done. User bubbles render in mono; assistant prose stays Rajdhani.'),
      ], {}, streamId).selectSessionDetail(streamId);
    case 'working': {
      // Render the committed content only — the working dock conveys live status,
      // and the tools-hidden selector hides the in-flight bash fallback row.
      return seed(
        [userEvent('run the test suite'), assistEvent('Running the suite now.')],
        { working: true, last_kind: 'TOOL_USE', last_text: LEAKED_BASH_TEXT },
        streamId,
      ).selectSessionDetail(streamId);
    }
    case 'markdown_code':
      // NOTE: the shared core collapses FENCED code blocks to "[code hidden]" for
      // ALL desktop transcript display (pentacleEventInterpreter.collapseCodeBlocks),
      // so a ```fence``` never paints a panel in the live chat. The real desktop
      // code affordance is INLINE code (`.slot-chat-md-icode`, mono on the cosmic
      // code-panel token). The baseline shows both: themed inline code plus the
      // honest "[code hidden]" collapse of a block — faithful to the product rule.
      return seed([
        userEvent('which helper builds the bevel path?'),
        assistEvent('Call the `bevelPath(w, h, b)` helper — it returns the SVG `d` string.\n\n```js\nfunction bevelPath(w, h, b) {\n  return "M0 0 H" + (w - b) + " V" + h + " Z";\n}\n```'),
      ], {}, streamId).selectSessionDetail(streamId);
    case 'markdown_table':
      return seed([
        userEvent('list the Public machines and sigils'),
        assistEvent('| Machine | Kind | Accent |\n| --- | --- | --- |\n| djinni | djinni | #3dff66 |\n| sun | sun | #ff2e3e |\n| mage | mage | #29d4ff |\n| flower | flower | #b14dff |'),
      ], {}, streamId).selectSessionDetail(streamId);
    case 'question_single':
    case 'question_multiselect':
      // Questions sit above a short conversation.
      return seed([
        userEvent('which workstream should I start?'),
        assistEvent('Let me ask which surfaces to prioritize.'),
      ], {}, streamId).selectSessionDetail(streamId);
    default:
      return seed([], {}, streamId).selectSessionDetail(streamId);
  }
}

// ── Question fixtures (shaped like the desktop PentacleQuestion) ───────────────

const SINGLE_QUESTION = {
  header: 'Pick a vehicle',
  prompt: 'Which screenshot vehicle should the gate use?',
  multiSelect: false,
  options: [
    { index: 1, label: 'Headless Electron capturePage', description: 'Offscreen render, no daemon.' },
    { index: 2, label: 'Full app over CDP', description: 'Highest fidelity; needs the local mock daemon.' },
    { index: 3, label: 'Type something.', meta: true },
  ],
};

const MULTI_QUESTION = {
  header: 'Capture states',
  prompt: 'Which states must the baseline cover?',
  multiSelect: true,
  options: [
    { index: 1, label: 'empty', description: 'No transcript rows.' },
    { index: 2, label: 'working', description: 'No tool-row leak.' },
    { index: 3, label: 'question[multiSelect]', description: 'Checkbox dock.' },
    { index: 4, label: 'markdown[table]', description: 'Table token.' },
  ],
};

// ── Header (mirrors app.js renderSlotChat hero, cosmic ornaments) ──────────────

function buildHero(doc: Document, activity: 'working' | 'idle'): HTMLElement {
  const hero = doc.createElement('div');
  hero.className = 'slot-chat-session-hero is-cosmic';
  hero.style.setProperty('--machine', CHROME.accent);
  hero.style.setProperty('--machine-surface', CHROME.surface);
  hero.style.setProperty('--machine-border', CHROME.border);

  const sigil = doc.createElement('div');
  sigil.className = 'slot-chat-session-sigil';
  sigil.appendChild(arcaneRingFrame({ machine: MACHINE, size: 44, sigilSize: 27 }));
  hero.appendChild(sigil);

  const head = doc.createElement('div');
  head.className = 'slot-chat-session-head';

  const kicker = doc.createElement('div');
  kicker.className = 'slot-chat-session-kicker';
  const machineName = doc.createElement('span');
  machineName.className = 'slot-chat-session-machine';
  machineName.textContent = MACHINE;
  const epithet = doc.createElement('span');
  epithet.className = 'slot-chat-session-epithet cosmic-myth';
  epithet.textContent = META.epithet;
  kicker.append(machineName, epithet);
  head.appendChild(kicker);

  const title = doc.createElement('div');
  title.className = 'slot-chat-session-title';
  title.textContent = 'claude-mage-cosmic';
  head.appendChild(title);

  const tags = doc.createElement('div');
  tags.className = 'slot-chat-session-tags';
  tags.appendChild(providerTag('claude', { color: META.accent }));
  tags.appendChild(statusTag(activity, { color: META.accent }));
  head.appendChild(tags);

  hero.appendChild(head);
  return hero;
}

// Working status row (the cosmic `.slot-chat-status-timer` surface).
function buildStatusRow(doc: Document): HTMLElement {
  const status = doc.createElement('div');
  status.className = 'slot-chat-status is-working';
  const badge = doc.createElement('span');
  badge.className = 'slot-chat-status-badge is-working';
  const dot = doc.createElement('span');
  dot.className = 'slot-chat-status-dot';
  const timer = doc.createElement('span');
  timer.className = 'slot-chat-status-timer';
  timer.textContent = '0m 04s';
  badge.append(dot, timer);
  status.appendChild(badge);
  return status;
}

// ── Question docks ─────────────────────────────────────────────────────────────

function buildSingleQuestion(doc: Document): HTMLElement {
  const dock = doc.createElement('div');
  dock.className = 'slot-chat-question';

  const prompt = doc.createElement('div');
  prompt.className = 'slot-chat-question-prompt';
  prompt.textContent = `${SINGLE_QUESTION.header}: ${SINGLE_QUESTION.prompt}`;
  dock.appendChild(prompt);

  const options = doc.createElement('div');
  options.className = 'slot-chat-question-options';
  const ordered = [...SINGLE_QUESTION.options].sort((a, b) => (a.meta === b.meta ? 0 : a.meta ? 1 : -1));
  ordered.forEach((opt, i) => {
    const btn = doc.createElement('button');
    btn.type = 'button';
    btn.className = 'slot-chat-question-option';
    if (opt.meta) btn.classList.add('is-meta');
    if (i === 0) btn.classList.add('is-selected');
    btn.dataset.option = String(opt.index);
    const label = doc.createElement('span');
    label.className = 'slot-chat-question-option-label';
    label.textContent = opt.label;
    btn.appendChild(label);
    if (opt.description) {
      const desc = doc.createElement('span');
      desc.className = 'slot-chat-question-option-desc';
      desc.textContent = opt.description;
      btn.appendChild(desc);
    }
    options.appendChild(btn);
  });
  dock.appendChild(options);
  return dock;
}

function buildMultiSelectQuestion(doc: Document): HTMLElement {
  const dock = doc.createElement('div');
  dock.className = 'slot-chat-question';
  // Pre-seed one selected option so the baseline shows the checked state.
  const drafts: Record<string, unknown> = {};
  renderMultiSelectQuestion({
    container: dock,
    doc,
    question: MULTI_QUESTION,
    streamId: STREAM,
    questionSig: 'cosmic-multiselect-sig',
    alreadyAnswered: false,
    drafts,
    answeredSig: {},
    onAnswer: () => {},
  });
  // Reflect a first-option selection in the static baseline.
  const first = dock.querySelector('.slot-chat-question-option');
  if (first) {
    first.classList.add('is-selected');
    first.setAttribute('aria-checked', 'true');
  }
  const submit = dock.querySelector('.slot-chat-question-submit') as HTMLButtonElement | null;
  if (submit) submit.disabled = false;
  return dock;
}

// ── Composer (mirrors app.js renderSlotChat composer) ──────────────────────────

function buildComposer(doc: Document): HTMLElement {
  const composer = doc.createElement('div');
  composer.className = 'slot-chat-composer';
  const input = doc.createElement('textarea');
  input.className = 'slot-chat-compose-input';
  input.setAttribute('placeholder', 'Type a message');
  const send = doc.createElement('button');
  send.type = 'button';
  send.className = 'slot-chat-compose-send';
  send.textContent = 'Send';
  composer.append(input, send);
  return composer;
}

// ── The surface assembler ──────────────────────────────────────────────────────

export function buildSurface(name: StateName, doc: Document = document): HTMLElement {
  const isWorking = name === 'working';

  const layer = doc.createElement('div');
  layer.className = 'slot-chat-layer cosmic';

  // Deterministic starfield behind the transcript (cosmic component).
  const field = starfield();
  field.classList.add('slot-chat-starfield');
  layer.appendChild(field);

  const shell = doc.createElement('div');
  shell.className = 'slot-chat-shell';

  shell.appendChild(buildHero(doc, isWorking ? 'working' : 'idle'));
  if (isWorking) shell.appendChild(buildStatusRow(doc));

  const list = doc.createElement('div');
  list.className = 'slot-chat-list';
  const detail = detailFor(name);
  const transcript = renderTranscriptTimelineHtml(detail, CHROME);
  if (transcript) {
    list.innerHTML = transcript;
  } else {
    const empty = doc.createElement('div');
    empty.className = 'slot-chat-empty';
    empty.textContent = 'No recent chat activity.';
    list.appendChild(empty);
  }
  shell.appendChild(list);

  if (name === 'question_single') shell.appendChild(buildSingleQuestion(doc));
  if (name === 'question_multiselect') shell.appendChild(buildMultiSelectQuestion(doc));

  shell.appendChild(buildComposer(doc));
  layer.appendChild(shell);
  return layer;
}
