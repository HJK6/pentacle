// Workstream E (public_chat_ui): cosmic visual-regression
// STRUCTURAL gate.
//
// The baseline PNGs under `.ui-review/desktop-chat/` are the human-reviewable
// artifact; THIS suite is the deterministic hard gate. For each defined cosmic
// chat state (empty / populated / working / question[single+multi] /
// markdown[code+table]) it builds the SAME live DOM the PNG generator
// renders (test/cosmic_scene_builder.ts), drops it into a jsdom document that
// has the scoped `cosmic_theme.css` + `cosmic_chat_surface.css` layers applied,
// and asserts the cosmic styling is ACTUALLY in effect:
//   - cosmic `--cosmic-*` token vars resolve on the `.cosmic` surface (and do not
//     leak to a non-cosmic control);
//   - fonts resolve (user bubble -> JetBrains Mono, assistant prose -> Rajdhani,
//     code -> mono) via the computed font-family chain;
//   - the active machine's MachineSigil SVG + arcane ring render in the header;
//   - bubble / bevel (clip-path hero) classes are applied;
//   - the WORKING state surfaces NO `session_summary_fallback` tool/bash row from
//     the real `selectSessionDetail` while working (ties to A's user-bug fix),
//     and the fallback returns once idle.
//
// Run via `npm run test:cosmic-visual` (esbuild-bundles this TS with jsdom kept
// external, then `node --test`). The runner sets cwd to the repo root so the
// on-disk CSS reads resolve from process.cwd().

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

// Install jsdom globals BEFORE importing the scene builder (its cosmic component
// factories call document.createElementNS at call time; module load is DOM-free).
const bootDom = new JSDOM('<!doctype html><html><head></head><body></body></html>');
(globalThis as unknown as { document: Document }).document = bootDom.window.document;
(globalThis as unknown as { window: unknown }).window = bootDom.window;

import { STATES, StateName, buildSurface, probeWorkingFallback } from './cosmic_scene_builder';

const repoRoot = process.cwd();
const rendererDir = path.join(repoRoot, 'renderer');
const themeCss = fs.readFileSync(path.join(rendererDir, 'cosmic_theme.css'), 'utf8');
const surfaceCss = fs.readFileSync(path.join(rendererDir, 'cosmic_chat_surface.css'), 'utf8');
const baseCss = fs.readFileSync(path.join(rendererDir, 'styles.css'), 'utf8');

// Build a state's surface inside a fresh jsdom that has the cosmic CSS applied,
// plus a non-cosmic control sibling to prove scope discipline. Returns the
// window so callers can read computed styles.
function mountState(name: StateName) {
  const dom = new JSDOM(
    `<!doctype html><html><head><style>${baseCss}\n${themeCss}\n${surfaceCss}</style></head>` +
      `<body><div id="control">control</div></body></html>`,
  );
  // Point the scene builder's global document at THIS window so the components it
  // creates belong to the styled document.
  (globalThis as unknown as { document: Document }).document = dom.window.document;
  (globalThis as unknown as { window: unknown }).window = dom.window;
  const surface = buildSurface(name, dom.window.document);
  dom.window.document.body.appendChild(surface);
  return { win: dom.window, surface };
}

function cssVar(win: Window, el: Element, name: string): string {
  return win.getComputedStyle(el).getPropertyValue(name).trim();
}

// ── Per-state: cosmic surface is opted-in and the token layer resolves ─────────
for (const name of STATES) {
  test(`[${name}] surface is .cosmic-scoped and the cosmic token layer resolves`, () => {
    const { win, surface } = mountState(name);
    assert.ok(surface.classList.contains('cosmic'), 'surface carries the .cosmic scope class');
    assert.ok(surface.classList.contains('slot-chat-layer'), 'surface is the chat layer');

    // Cosmic tokens resolve on the scoped surface, mirrored from mobile Colors.ts.
    assert.equal(cssVar(win, surface, '--cosmic-green'), '#3dff66', 'cosmic green token resolves');
    assert.equal(cssVar(win, surface, '--cosmic-codepanel'), '#04100a', 'cosmic code-panel token resolves');
    assert.equal(cssVar(win, surface, '--cosmic-ink'), '#080b0a', 'cosmic ink token resolves');
    assert.match(cssVar(win, surface, '--cosmic-font-mono'), /JetBrainsMono/, 'mono font token resolves');
    assert.match(cssVar(win, surface, '--cosmic-font-display'), /Rajdhani/, 'display font token resolves');

    // Scope discipline: the non-cosmic control gets none of it.
    const control = win.document.getElementById('control')!;
    assert.equal(cssVar(win, control, '--cosmic-green'), '', 'tokens do not leak to a non-cosmic element');
  });

  test(`[${name}] the header renders the active machine sigil + arcane ring`, () => {
    const { surface } = mountState(name);
    const hero = surface.querySelector('.slot-chat-session-hero.is-cosmic');
    assert.ok(hero, 'cosmic hero present (bevel via clip-path scoped to .is-cosmic)');
    const ring = surface.querySelector('.slot-chat-session-sigil svg[viewBox="0 0 100 100"]');
    assert.ok(ring, 'arcane ring frame SVG present in the header');
    const sigil = surface.querySelector('.slot-chat-session-sigil svg[viewBox="0 0 64 64"]');
    assert.ok(sigil, 'machine sigil SVG (64x64) embedded for the active machine');
    assert.ok(sigil!.querySelector('g'), 'sigil has its stroked <g> group');
    // The mythical epithet renders in the Cinzel display layer.
    assert.ok(surface.querySelector('.slot-chat-session-epithet.cosmic-myth'), 'epithet in the .cosmic-myth layer');
    // Header status + provider tags render.
    assert.ok(surface.querySelector('.cosmic-status-tag'), 'status tag rendered');
    assert.ok(surface.querySelector('.cosmic-provider-tag'), 'provider tag rendered');
    // Decorative deterministic starfield mounted behind the transcript.
    assert.ok(surface.querySelector('.slot-chat-starfield .cosmic-star'), 'starfield dots mounted');
  });
}

// ── empty ──────────────────────────────────────────────────────────────────────
test('[empty] shows the empty state and no bubbles', () => {
  const { surface } = mountState('empty');
  assert.ok(surface.querySelector('.slot-chat-empty'), 'empty-state node present');
  assert.equal(surface.querySelectorAll('.slot-chat-user-bubble').length, 0, 'no user bubbles');
  assert.equal(surface.querySelectorAll('.slot-chat-assistant-card').length, 0, 'no assistant cards');
});

// ── populated ───────────────────────────────────────────────────────────────────
test('[populated] renders user + assistant bubbles, user bubble in JetBrains Mono', () => {
  const { win, surface } = mountState('populated');
  const userBubbles = surface.querySelectorAll('.slot-chat-user-bubble');
  const assistantCards = surface.querySelectorAll('.slot-chat-assistant-card');
  assert.ok(userBubbles.length >= 1, 'at least one user bubble');
  assert.ok(assistantCards.length >= 1, 'at least one assistant card');

  // Mobile's rule: user bubbles in mono, assistant prose in the Rajdhani display
  // family. jsdom does not substitute var() inside font-family, so assert the
  // rule points at the token and the token resolves to the expected face.
  assert.equal(win.getComputedStyle(userBubbles[0]).fontFamily, 'var(--cosmic-font-mono)', 'user bubble -> mono token');
  assert.equal(win.getComputedStyle(assistantCards[0]).fontFamily, 'var(--cosmic-font-display)', 'assistant -> display token');
  assert.match(cssVar(win, surface, '--cosmic-font-mono'), /JetBrainsMono/);
  assert.match(cssVar(win, surface, '--cosmic-font-display'), /Rajdhani/);
});

// ── working (the user-bug gate) ─────────────────────────────────────────────
test('[working] surfaces the cosmic working timer and NO leaked tool/bash row in the DOM', () => {
  const { win, surface } = mountState('working');
  const timer = surface.querySelector('.slot-chat-status-badge.is-working .slot-chat-status-timer');
  assert.ok(timer, 'working status badge + timer present');
  const statusLabels = [...surface.querySelectorAll('.cosmic-status-tag-label')]
    .map((el) => (el.textContent || '').trim());
  assert.equal(statusLabels.includes('Working'), false, 'cosmic working header has no visible Working label');
  // The cosmic timer resolves to the working accent (token applied).
  assert.equal(cssVar(win, surface, '--cosmic-status-working'), '#3dff66', 'working status token resolves');
  // No activity/tool row leaking the running Bash command into the transcript.
  const leaked = [...surface.querySelectorAll('.slot-chat-activity, .slot-chat-user-bubble, .slot-chat-assistant-card')]
    .some((el) => /npm test --silent/.test(el.textContent || ''));
  assert.equal(leaked, false, 'no transcript row leaks the in-flight Bash command');
});

test('[working] the real selectSessionDetail emits NO session_summary_fallback row while working, but does once idle', () => {
  const { workingItems, idleItems, leakedText } = probeWorkingFallback();

  // While WORKING + last_kind=TOOL_USE (Bash …), the default tools-hidden
  // selector must NOT synthesize the `fallback:<stream>` session-summary row
  // (chat-core 3d93d9c: 60e2016 catches structured tool kinds; 2426b63 gates the
  // fallback on `includeTools || !session.working`).
  const workingFallback = workingItems.filter((i) => String(i.id || '').startsWith('fallback:'));
  assert.equal(workingFallback.length, 0, 'no session_summary_fallback row synthesized while working');
  const leakedWhileWorking = workingItems.some((i) => String(i.text || '').includes(leakedText));
  assert.equal(leakedWhileWorking, false, 'the live Bash last_text never leaks into a transcript row');

  // Sanity: the committed content is still present (we did not hide everything).
  assert.ok(workingItems.length >= 1, 'committed transcript rows still render while working');

  // Once settled to idle the normal session-summary fallback may surface again —
  // proves we suppressed the WORKING leak, not the mechanism itself.
  const idleFallback = idleItems.filter((i) => String(i.id || '').startsWith('fallback:'));
  assert.ok(idleFallback.length >= 1, 'the session-summary fallback returns once the turn settles to idle');
});

// ── question[single] ─────────────────────────────────────────────────────────────
test('[question_single] renders a single-select (radio-style) option dock, not checkboxes', () => {
  const { surface } = mountState('question_single');
  const dock = surface.querySelector('.slot-chat-question');
  assert.ok(dock, 'question dock present');
  assert.ok(surface.querySelector('.slot-chat-question-prompt'), 'question prompt present');
  const options = surface.querySelectorAll('.slot-chat-question-option');
  assert.ok(options.length >= 2, 'multiple options rendered');
  assert.equal(surface.querySelectorAll('.slot-chat-question-option.is-checkbox').length, 0, 'single-select has no checkbox options');
  assert.equal(surface.querySelectorAll('.slot-chat-question-submit').length, 0, 'single-select has no Submit (immediate click)');
  assert.ok(surface.querySelector('.slot-chat-question-option.is-selected'), 'a selected option is shown');
});

// ── question[multiSelect] ─────────────────────────────────────────────────────────
test('[question_multiselect] renders checkbox-style options + a Submit button', () => {
  const { surface } = mountState('question_multiselect');
  assert.ok(surface.querySelector('.slot-chat-question'), 'question dock present');
  const checkboxes = surface.querySelectorAll('.slot-chat-question-option.is-checkbox[role="checkbox"]');
  assert.ok(checkboxes.length >= 2, 'multiple checkbox options rendered');
  assert.ok(surface.querySelector('.slot-chat-question-options.is-multiselect'), 'options container marked multiselect');
  assert.ok(surface.querySelector('.slot-chat-question-submit'), 'a Submit button batches the multi-answer');
  assert.ok(surface.querySelector('.slot-chat-question-option.is-selected[aria-checked="true"]'), 'a checked option is shown');
});

// ── markdown[code] ────────────────────────────────────────────────────────────────
test('[markdown_code] themes inline code in mono on the cosmic code-panel; block code stays collapsed', () => {
  const { win, surface } = mountState('markdown_code');
  // The desktop renders inline code (`.slot-chat-md-icode`) — the live code
  // affordance — which cosmic themes in mono on the code-panel token. Fenced
  // blocks are collapsed to "[code hidden]" by the shared core for all transcript
  // display, so we assert the real affordance, not a panel the app never paints.
  const icode = surface.querySelector('.slot-chat-md-icode');
  assert.ok(icode, 'inline code renders to .slot-chat-md-icode');
  assert.equal(win.getComputedStyle(icode!).fontFamily, 'var(--cosmic-font-mono)', 'inline code -> mono token');
  assert.match(cssVar(win, surface, '--cosmic-font-mono'), /JetBrainsMono/);
  assert.equal(cssVar(win, surface, '--cosmic-codepanel'), '#04100a', 'code-panel token resolves for the inline-code bg');
  // The fenced block is collapsed by core (product rule), not leaked verbatim.
  assert.ok(/\[code hidden\]/.test(surface.textContent || ''), 'block code is collapsed to [code hidden]');
});

// ── markdown[table] ───────────────────────────────────────────────────────────────
test('[markdown_table] renders a markdown table themed in the cosmic mono family', () => {
  const { win, surface } = mountState('markdown_table');
  const table = surface.querySelector('table.slot-chat-md-table');
  assert.ok(table, 'markdown table renders to .slot-chat-md-table');
  assert.ok(table!.querySelectorAll('thead th').length >= 2, 'table header cells present');
  assert.ok(table!.querySelectorAll('tbody tr').length >= 2, 'table body rows present');
  assert.equal(win.getComputedStyle(table!).fontFamily, 'var(--cosmic-font-mono)', 'table -> mono token');
});

// ── coverage guard: every defined state is exercised ────────────────────────────
test('STATES covers the seven required cosmic chat states', () => {
  assert.deepEqual(
    [...STATES].sort(),
    ['empty', 'markdown_code', 'markdown_table', 'populated', 'question_multiselect', 'question_single', 'working'],
    'the seven baseline states are present',
  );
});
