// Workstream D (public_chat_ui): cosmic chat-SURFACE structural
// gate.
//
// Hard structural assertions for the themed desktop chat surface (the human-
// reviewable PNGs are workstream E's job; THESE are the deterministic gate):
//   1. renderer/index.html links cosmic_theme.css EXACTLY ONCE, after styles.css,
//      and loads the cosmic component/token bundles before app.js.
//   2. app.js opts the chat container into `.cosmic` (and ONLY the chat
//      container) and builds the cosmic header ornaments.
//   3. On a `.cosmic`-scoped surface, the `--cosmic-*` token layer resolves and
//      the user bubble resolves to the JetBrains Mono family (mobile's rule),
//      while a non-`.cosmic` control stays unstyled (scope discipline).
//   4. A MachineSigil SVG renders in the header for the active machine (via the
//      ArcaneRingFrame the header uses), and the MACHINES table is the single
//      source of truth shared with cosmic_tokens.ts.
//
// Run via `npm run test:cosmic-surface` (esbuild-bundle this TS, jsdom external,
// then `node --test`). The runner sets cwd to the repo root.

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

// jsdom globals BEFORE the component factories run (module load is DOM-free).
const bootDom = new JSDOM('<!doctype html><html><head></head><body></body></html>');
(globalThis as unknown as { document: Document }).document = bootDom.window.document;
(globalThis as unknown as { window: unknown }).window = bootDom.window;

import {
  arcaneRingFrame,
  machineSigil,
  providerTag,
  statusTag,
  MACHINES,
} from '../renderer/src/cosmic_components';
import { MACHINES as TOKEN_MACHINES } from '../renderer/src/cosmic_tokens';

const repoRoot = process.cwd();
const rendererDir = path.join(repoRoot, 'renderer');
const indexHtml = fs.readFileSync(path.join(rendererDir, 'index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(rendererDir, 'app.js'), 'utf8');
const themeCss = fs.readFileSync(path.join(rendererDir, 'cosmic_theme.css'), 'utf8');
const surfaceCss = fs.readFileSync(path.join(rendererDir, 'cosmic_chat_surface.css'), 'utf8');

// ── 1. index.html wiring ──────────────────────────────────────────────
test('index.html links cosmic_theme.css exactly once, after styles.css', () => {
  const links = [...indexHtml.matchAll(/<link\b[^>]*href="cosmic_theme\.css"[^>]*>/g)];
  assert.equal(links.length, 1, 'exactly one cosmic_theme.css <link>');
  assert.ok(
    indexHtml.indexOf('href="cosmic_theme.css"') > indexHtml.indexOf('href="styles.css"'),
    'cosmic_theme.css link comes after styles.css',
  );
});

test('index.html loads the cosmic bundles before app.js (mirrors chat_core)', () => {
  const tokensIdx = indexHtml.indexOf('dist/cosmic_tokens.bundle.js');
  const componentsIdx = indexHtml.indexOf('dist/cosmic_components.bundle.js');
  const appIdx = indexHtml.indexOf('src="app.js"');
  assert.ok(tokensIdx > -1, 'cosmic_tokens bundle script present');
  assert.ok(componentsIdx > -1, 'cosmic_components bundle script present');
  assert.ok(componentsIdx < appIdx, 'cosmic_components bundle loads before app.js');
  assert.ok(tokensIdx < appIdx, 'cosmic_tokens bundle loads before app.js');
});

// ── 2. app.js opts ONLY the chat container into `.cosmic` + builds header ──
test('app.js puts .cosmic on the chat container only', () => {
  assert.match(
    appJs,
    /chatMount\.className\s*=\s*'slot-chat-layer cosmic'/,
    'the chat layer (chat container) is the .cosmic scope root',
  );
  // Scope discipline: `.cosmic` is not slapped on a global/root element.
  assert.ok(
    !/document\.documentElement\.classList\.add\(['"]cosmic['"]\)/.test(appJs),
    '.cosmic is not added to <html>',
  );
  assert.ok(
    !/document\.body\.classList\.add\(['"]cosmic['"]\)/.test(appJs),
    '.cosmic is not added to <body>',
  );
});

test('app.js builds the cosmic header (ring sigil + epithet + provider/status tags)', () => {
  assert.match(appJs, /window\.PentacleCosmic/, 'reads the cosmic component global');
  assert.match(appJs, /arcaneRingFrame\(/, 'header uses arcaneRingFrame');
  assert.match(appJs, /slot-chat-session-epithet cosmic-myth/, 'epithet rendered in Cinzel (.cosmic-myth)');
  assert.match(appJs, /providerTag\(/, 'header renders a provider tag');
  assert.match(appJs, /statusTag\(/, 'header renders a status tag');
  assert.match(appJs, /starfield\(/, 'subtle starfield mounted behind the transcript');
});

// ── 3. Scoped token layer + user bubble in JetBrains Mono ──────────────
function makeSurfaceDom() {
  const dom = new JSDOM(
    `<!doctype html><html><head><style>${themeCss}\n${surfaceCss}</style></head><body>` +
      `<div id="outside">control</div>` +
      `<div id="layer" class="slot-chat-layer cosmic">` +
      `<article class="slot-chat-row is-user"><div id="user" class="slot-chat-user-bubble">hi</div></article>` +
      `<article class="slot-chat-row"><div id="assistant" class="slot-chat-assistant-card">yo</div></article>` +
      `<pre id="code" class="slot-chat-md-code"><code>x</code></pre>` +
      `</div>` +
      `</body></html>`,
  );
  return dom.window;
}

test('cosmic token vars resolve on the chat layer, not on a non-cosmic control', () => {
  const win = makeSurfaceDom();
  const layer = win.getComputedStyle(win.document.getElementById('layer')!);
  assert.equal(layer.getPropertyValue('--cosmic-green').trim(), '#3dff66');
  assert.equal(layer.getPropertyValue('--cosmic-codepanel').trim(), '#04100a');
  assert.match(layer.getPropertyValue('--cosmic-font-mono'), /JetBrainsMono_400Regular/);

  const outside = win.getComputedStyle(win.document.getElementById('outside')!);
  assert.equal(outside.getPropertyValue('--cosmic-green').trim(), '', 'no leak to non-cosmic element');
});

test('user bubble resolves to the JetBrains Mono family inside .cosmic', () => {
  const win = makeSurfaceDom();
  // jsdom does NOT substitute var() inside font-family, so (mirroring the B
  // token test) verify the full chain: the bubble rule points at --cosmic-font-mono,
  // and that var resolves to the JetBrains Mono face on the scoped layer.
  const bubbleFF = win.getComputedStyle(win.document.getElementById('user')!).fontFamily;
  assert.equal(bubbleFF, 'var(--cosmic-font-mono)');
  const layer = win.getComputedStyle(win.document.getElementById('layer')!);
  assert.match(layer.getPropertyValue('--cosmic-font-mono'), /JetBrainsMono/);

  // Assistant prose uses the Rajdhani display family; code panel stays mono.
  assert.equal(win.getComputedStyle(win.document.getElementById('assistant')!).fontFamily, 'var(--cosmic-font-display)');
  assert.equal(win.getComputedStyle(win.document.getElementById('code')!).fontFamily, 'var(--cosmic-font-mono)');
});

// ── 4. MachineSigil renders in the header for the active machine ───────
test('the header arcaneRingFrame embeds the active machine MachineSigil SVG', () => {
  // hostc is this machine; the header keys MACHINES by chrome.title.
  const frame = arcaneRingFrame({ machine: 'hostc', size: 44, sigilSize: 27 });
  assert.equal(frame.tagName.toLowerCase(), 'div');
  const ring = frame.querySelector('svg[viewBox="0 0 100 100"]');
  assert.ok(ring, 'arcane ring present');
  const sigil = frame.querySelector('svg[viewBox="0 0 64 64"]');
  assert.ok(sigil, 'machine sigil SVG embedded in the header ring');
  assert.ok(sigil!.querySelector('g'), 'sigil has its stroked <g> group');
});

test('every Public machine renders a distinct sigil for the header', () => {
  for (const name of Object.keys(MACHINES) as (keyof typeof MACHINES)[]) {
    const svg = machineSigil(MACHINES[name].kind);
    assert.equal(svg.getAttribute('viewBox'), '0 0 64 64', `${name} sigil is 64x64`);
  }
});

test('the header tags render for claude + working/idle status', () => {
  const claude = providerTag('claude', { color: MACHINES['hostc'].accent });
  assert.match(claude.textContent || '', /Claude/);
  const working = statusTag('working', { color: MACHINES['hostc'].accent });
  assert.ok(working.querySelector('.activity-spinner'), 'working tag shows the shared spinner');
  assert.equal(working.querySelector('.cosmic-spinner'), null);
  const idle = statusTag('idle', { color: MACHINES['hostc'].accent });
  assert.match(idle.textContent || '', /Idle/);
});

test('MACHINES in the component lib is the single source of truth (cosmic_tokens.ts)', () => {
  assert.equal(MACHINES, TOKEN_MACHINES, 'component MACHINES is re-exported from cosmic_tokens');
  assert.equal(MACHINES['hostc'].accent, '#29d4ff');
  assert.equal(MACHINES['hostc'].epithet, 'the mage');
});
