// Cosmic-parity (workstream B) token-layer structural tests.
//
// Hard gate for the desktop cosmic token port:
//   1. The `--cosmic-*` CSS vars resolve on a `.cosmic`-scoped element (and do
//      NOT leak to a non-cosmic element), proving the scoped layer works.
//   2. The three font helper classes (.cosmic-display / .cosmic-mono /
//      .cosmic-myth) set the expected Rajdhani / JetBrains Mono / Cinzel family.
//   3. cosmic_tokens.ts exports the MACHINES / STATUS / SEV maps with the exact
//      hex values from mobile's constants/Colors.ts.
//   4. All 9 vendored TTFs exist with non-zero size, and every @font-face `src`
//      in cosmic_theme.css resolves to a real file relative to the css location.
//
// Run via `npm run test:cosmic-tokens`, which esbuild-bundles this TS (jsdom
// external) to a temp CJS file and runs it under `node --test`. The runner sets
// cwd to the repo root, so on-disk paths are resolved from process.cwd().

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

import {
  MACHINES,
  MACHINE_ORDER,
  STATUS,
  SEV,
  palette,
  Fonts,
  TypeScale,
  Spacing,
} from '../renderer/src/cosmic_tokens';

const repoRoot = process.cwd();
const rendererDir = path.join(repoRoot, 'renderer');
const cssPath = path.join(rendererDir, 'cosmic_theme.css');
const fontsDir = path.join(rendererDir, 'assets', 'fonts');

const cssText = fs.readFileSync(cssPath, 'utf8');

// Build a jsdom document with the cosmic stylesheet inlined and both a
// cosmic-scoped subtree and a non-cosmic control element.
function makeDom() {
  const dom = new JSDOM(
    `<!doctype html><html><head><style>${cssText}</style></head><body>` +
      `<div id="outside">control</div>` +
      `<div id="root" class="cosmic">` +
      `<span id="display" class="cosmic-display">A</span>` +
      `<span id="mono" class="cosmic-mono">B</span>` +
      `<span id="myth" class="cosmic-myth">C</span>` +
      `</div>` +
      `</body></html>`,
  );
  return dom.window;
}

// ── 1. Scoped CSS vars resolve on `.cosmic`, do not leak globally ──────
test('cosmic CSS vars resolve on a .cosmic-scoped element', () => {
  const win = makeDom();
  const root = win.document.getElementById('root')!;
  const cs = win.getComputedStyle(root);
  const v = (name: string) => cs.getPropertyValue(name).trim();

  // palette
  assert.equal(v('--cosmic-ink'), '#080b0a');
  assert.equal(v('--cosmic-panel'), '#0d1411');
  assert.equal(v('--cosmic-text'), '#e6fff2');
  assert.equal(v('--cosmic-dim'), '#9dc4b3');
  assert.equal(v('--cosmic-muted'), '#7fa896');
  assert.equal(v('--cosmic-line'), 'rgba(120, 255, 160, 0.16)');
  assert.equal(v('--cosmic-green'), '#3dff66');
  assert.equal(v('--cosmic-amber'), '#ffb53d');
  assert.equal(v('--cosmic-red'), '#ff2e3e');
  assert.equal(v('--cosmic-star'), '#bdffe0');
  assert.equal(v('--cosmic-codepanel'), '#04100a');

  // machine accents
  assert.equal(v('--cosmic-machine-djinni'), '#3dff66');
  assert.equal(v('--cosmic-machine-sun'), '#ff2e3e');
  assert.equal(v('--cosmic-machine-mage'), '#29d4ff');
  assert.equal(v('--cosmic-machine-flower'), '#b14dff');

  // status + severity
  assert.equal(v('--cosmic-status-working'), '#3dff66');
  assert.equal(v('--cosmic-status-idle'), '#7fa896');
  assert.equal(v('--cosmic-sev-info'), '#3dff66');
  assert.equal(v('--cosmic-sev-warning'), '#ffb53d');
  assert.equal(v('--cosmic-sev-critical'), '#ff2e3e');

  // type scale + spacing (px)
  assert.equal(v('--cosmic-type-title'), '19px');
  assert.equal(v('--cosmic-type-body'), '14.5px');
  assert.equal(v('--cosmic-type-tiny'), '9px');
  assert.equal(v('--cosmic-screen-pad'), '16px');
  assert.equal(v('--cosmic-top-inset'), '52px');
  assert.equal(v('--cosmic-bevel-card'), '12px');
  assert.equal(v('--cosmic-bevel-bubble'), '10px');
});

test('cosmic vars do NOT leak to a non-.cosmic element (scope discipline)', () => {
  const win = makeDom();
  const outside = win.document.getElementById('outside')!;
  const cs = win.getComputedStyle(outside);
  assert.equal(cs.getPropertyValue('--cosmic-ink').trim(), '');
  assert.equal(cs.getPropertyValue('--cosmic-green').trim(), '');
});

// ── 2. Font helper classes set the expected families ──────────────────
// The helper classes set `font-family: var(--cosmic-font-*)` (single source of
// truth, resolved by real browsers). jsdom's getComputedStyle does NOT
// substitute var() inside font-family, so we verify the full chain explicitly:
// the helper class points at the expected var, and that var resolves to the
// expected face on a .cosmic-scoped element.
test('font helper classes resolve to Rajdhani / JetBrains Mono / Cinzel', () => {
  const win = makeDom();
  const ff = (id: string) =>
    win.getComputedStyle(win.document.getElementById(id)!).fontFamily;
  const rootVar = (name: string) =>
    win.getComputedStyle(win.document.getElementById('root')!).getPropertyValue(name).trim();

  // class → var mapping
  assert.equal(ff('display'), 'var(--cosmic-font-display)');
  assert.equal(ff('mono'), 'var(--cosmic-font-mono)');
  assert.equal(ff('myth'), 'var(--cosmic-font-myth)');

  // var → face value (what a browser substitutes in)
  assert.match(rootVar('--cosmic-font-display'), /Rajdhani_600SemiBold/);
  assert.match(rootVar('--cosmic-font-mono'), /JetBrainsMono_400Regular/);
  assert.match(rootVar('--cosmic-font-myth'), /Cinzel_600SemiBold/);
});

// ── 3. JS token exports mirror constants/Colors.ts exactly ────────────
test('cosmic_tokens MACHINES map has correct kind/accent/epithet', () => {
  assert.deepEqual(MACHINE_ORDER, ['djinni', 'sun', 'mage', 'flower']);
  assert.deepEqual(MACHINES['djinni'], { kind: 'djinni', accent: '#3dff66', epithet: 'the djinni' });
  assert.deepEqual(MACHINES['sun'], { kind: 'sun', accent: '#ff2e3e', epithet: 'the flame' });
  assert.deepEqual(MACHINES['mage'], { kind: 'mage', accent: '#29d4ff', epithet: 'the mage' });
  assert.deepEqual(MACHINES['flower'], { kind: 'flower', accent: '#b14dff', epithet: 'the bloom' });
});

test('cosmic_tokens STATUS + SEV maps have correct hex values', () => {
  assert.equal(STATUS.working, '#3dff66');
  assert.equal(STATUS.idle, '#7fa896');
  assert.equal(STATUS.WORKING, '#3dff66');
  assert.equal(STATUS.IDLE, '#7fa896');

  assert.equal(SEV.info, '#3dff66');
  assert.equal(SEV.warning, '#ffb53d');
  assert.equal(SEV.critical, '#ff2e3e');
  assert.equal(SEV.INFO, '#3dff66');
  assert.equal(SEV.WARNING, '#ffb53d');
  assert.equal(SEV.CRITICAL, '#ff2e3e');
});

test('cosmic_tokens palette / TypeScale / Spacing mirror Colors.ts', () => {
  assert.equal(palette.ink, '#080b0a');
  assert.equal(palette.panel, '#0d1411');
  assert.equal(palette.text, '#e6fff2');
  assert.equal(palette.line, 'rgba(120,255,160,0.16)');
  assert.equal(palette.green, '#3dff66');
  assert.equal(palette.codePanel, '#04100a');

  assert.equal(TypeScale.title, 19);
  assert.equal(TypeScale.body, 14.5);
  assert.equal(TypeScale.tiny, 9);

  assert.equal(Spacing.screenPad, 16);
  assert.equal(Spacing.topInset, 52);
  assert.equal(Spacing.bevelCard, 12);
  assert.equal(Spacing.bevelBubble, 10);

  assert.equal(Fonts.rajdhani.semiBold, 'Rajdhani_600SemiBold');
  assert.equal(Fonts.jetBrainsMono.regular, 'JetBrainsMono_400Regular');
  assert.equal(Fonts.cinzel.semiBold, 'Cinzel_600SemiBold');
});

// ── 4. Vendored fonts exist and every @font-face src resolves ─────────
test('all 9 vendored TTFs exist with non-zero size', () => {
  const expected = [
    'Rajdhani_500Medium',
    'Rajdhani_600SemiBold',
    'Rajdhani_700Bold',
    'JetBrainsMono_400Regular',
    'JetBrainsMono_500Medium',
    'JetBrainsMono_700Bold',
    'Cinzel_500Medium',
    'Cinzel_600SemiBold',
    'Cinzel_700Bold',
  ];
  for (const name of expected) {
    const fp = path.join(fontsDir, `${name}.ttf`);
    assert.ok(fs.existsSync(fp), `missing font ${name}.ttf`);
    assert.ok(fs.statSync(fp).size > 0, `empty font ${name}.ttf`);
  }
});

test('every @font-face src in cosmic_theme.css resolves relative to the css', () => {
  const srcUrls = [...cssText.matchAll(/url\(['"]([^'"]+)['"]\)/g)].map((m) => m[1]);
  assert.ok(srcUrls.length === 9, `expected 9 @font-face src urls, got ${srcUrls.length}`);
  const cssDir = path.dirname(cssPath);
  for (const rel of srcUrls) {
    const resolved = path.resolve(cssDir, rel);
    assert.ok(fs.existsSync(resolved), `@font-face src does not resolve: ${rel}`);
    assert.ok(fs.statSync(resolved).size > 0, `@font-face src is empty: ${rel}`);
  }
});
