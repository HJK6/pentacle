#!/usr/bin/env node
'use strict';

// Cosmic visual-state example generator.
// artifact generator.
//
// Extends the existing `generate_chat_ui_mockups.js` prior art into a COSMIC
// variant: it renders the defined cosmic chat states (empty / populated /
// working / question[single+multi] / markdown[code+table]) with the REAL cosmic
// surface — `cosmic_theme.css` + `cosmic_chat_surface.css` + the shared
// transcript/component renderers (via test/cosmic_scene_builder.ts) — and writes
// one self-contained HTML page per state into `artifacts/desktop-chat/`, plus a
// gallery `index.html` and a `public.uiReview.v1` `manifest.json` so the
// artifact is discoverable by the same artifact viewer the mobile
// `artifacts/mobile-screens/` gate uses (public artifact viewer).
//
// The HTML pages are the screenshot SOURCE: scripts/capture-cosmic-baselines.js
// loads each one in a headless Electron window and captures the baseline PNG.
//
// Regenerate with:  npm run mockups:cosmic        (HTML states + manifest)
//                   npm run mockups:cosmic:png     (HTML + baseline PNGs)
//
// Build vehicle: the scene builder is TypeScript importing the renderer sources
// + the bare `chat-core` specifier, so (like the test runners) we
// esbuild-bundle it to a temp CJS module, run it under a jsdom `document`, and
// snapshot each state's `.slot-chat-layer.cosmic` outerHTML.

const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const repoRoot = path.resolve(__dirname, '..');
const outDir = path.join(repoRoot, 'artifacts', 'desktop-chat');
const cacheRoot = path.join(repoRoot, 'node_modules', '.cache');

// CSS the pages link, in cascade order (base app tokens -> cosmic token layer ->
// cosmic chat-surface map). Relative to the page's location in
// `artifacts/desktop-chat/`, the renderer dir is two levels up.
const REL_RENDERER = '../../renderer';

function bundleSceneBuilder() {
  fs.mkdirSync(cacheRoot, { recursive: true });
  const dir = fs.mkdtempSync(path.join(cacheRoot, 'pentacle-cosmic-gen-'));
  const outFile = path.join(dir, 'scene_builder.cjs');
  const esbuild = path.join(repoRoot, 'node_modules', '.bin', 'esbuild');
  execFileSync(
    esbuild,
    [
      path.join(repoRoot, 'test', 'cosmic_scene_builder.ts'),
      '--bundle',
      '--platform=node',
      '--format=cjs',
      '--target=node18',
      '--external:node:*',
      '--external:jsdom',
      `--outfile=${outFile}`,
    ],
    { stdio: 'inherit', cwd: repoRoot },
  );
  return { dir, outFile };
}

function pageHtml(title, surfaceHtml) {
  // A self-contained page: link the real cosmic CSS so the @font-face faces +
  // every `--cosmic-*` rule resolve exactly as in the app, set the cosmic ink
  // background, and drop the surface in. The cosmic spinner injects its own
  // keyframes <style> at component-build time (already inlined in surfaceHtml's
  // sibling head is not needed — the factory appends to document.head at runtime;
  // for the static page we add the keyframes here so the working spinner spins).
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Cosmic chat — ${escapeHtml(title)}</title>
  <link rel="stylesheet" href="${REL_RENDERER}/styles.css">
  <link rel="stylesheet" href="${REL_RENDERER}/cosmic_theme.css">
  <link rel="stylesheet" href="${REL_RENDERER}/cosmic_chat_surface.css">
  <style>
    @keyframes cosmic-spin { to { transform: rotate(360deg); } }
    html, body { margin: 0; background: #080b0a; }
    body { padding: 22px; }
    .cosmic-stage {
      max-width: 720px;
      margin: 0 auto;
    }
    /* Give the scoped chat layer a concrete frame for the screenshot. */
    .cosmic-stage .slot-chat-layer.cosmic {
      display: block;
      border: 1px solid var(--cosmic-line, rgba(120,255,160,0.16));
      min-height: 320px;
      padding: 16px;
      position: relative;
      overflow: hidden;
    }
    .cosmic-stage .slot-chat-shell { display: block; }
    .cosmic-stage .slot-chat-list { display: block; }
    .cosmic-stage .slot-chat-composer { display: flex; gap: 8px; margin-top: 14px; }
    .cosmic-stage .slot-chat-compose-input { flex: 1; min-height: 44px; padding: 10px; }
    .cosmic-stage .slot-chat-question { margin-top: 14px; padding-top: 12px; }
  </style>
</head>
<body>
  <div class="cosmic-stage">
    ${surfaceHtml}
  </div>
</body>
</html>`;
}

function escapeHtml(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function galleryHtml(states) {
  const cards = states
    .map(
      (s) => `<li>
        <a href="${escapeHtml(s.file)}">${escapeHtml(s.title)}</a>
        <span class="state-id">${escapeHtml(s.state)}</span>
      </li>`,
    )
    .join('\n');
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Desktop Cosmic Chat — visual states</title>
  <style>
    body { margin: 0; padding: 28px; background: #080b0a; color: #e6fff2; font-family: system-ui, sans-serif; }
    h1 { font-size: 20px; letter-spacing: 0.5px; }
    p { color: #9dc4b3; max-width: 680px; line-height: 1.5; }
    ul { list-style: none; padding: 0; display: grid; gap: 10px; max-width: 520px; }
    li { display: flex; align-items: baseline; justify-content: space-between; padding: 12px 14px; border: 1px solid rgba(120,255,160,0.16); }
    a { color: #3dff66; text-decoration: none; font-size: 15px; }
    .state-id { color: #7fa896; font-family: monospace; font-size: 12px; }
  </style>
</head>
<body>
  <h1>Desktop Cosmic Chat — visual state examples</h1>
  <p>Baseline states of the cosmic-themed desktop chat surface. Each links a
  self-contained page rendered with the shared cosmic CSS + renderers; the
  PNG baselines under this directory are captured from these pages. The
  structural hard gate is <code>npm run test:cosmic-visual</code>.</p>
  <ul>
    ${cards}
  </ul>
</body>
</html>`;
}

function main() {
  const { dir, outFile } = bundleSceneBuilder();
  try {
    // jsdom globals for the component factories, then snapshot each state.
    const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>');
    global.document = dom.window.document;
    global.window = dom.window;

    // eslint-disable-next-line import/no-dynamic-require, global-require
    const builder = require(outFile);
    const { STATES, STATE_TITLES, buildSurface } = builder;

    fs.mkdirSync(outDir, { recursive: true });

    const states = [];
    for (const state of STATES) {
      const surface = buildSurface(state, dom.window.document);
      const file = `${state}.html`;
      fs.writeFileSync(path.join(outDir, file), pageHtml(STATE_TITLES[state], surface.outerHTML), 'utf8');
      states.push({ state, title: STATE_TITLES[state], file, png: `${state}.png` });
      // Reset body between states so factory-appended head nodes don't accumulate.
      dom.window.document.body.innerHTML = '';
    }

    fs.writeFileSync(path.join(outDir, 'index.html'), galleryHtml(states), 'utf8');

    // Public visual-state manifest for the generated pages.
    // createdAt is fixed (no Date.now in a deterministic generator) and bumped by
    // hand when the baseline set materially changes.
    const manifest = {
      schema: 'public.uiReview.v1',
      id: 'desktop-chat',
      repo: 'public-app',
      title: 'Desktop Cosmic Chat (visual state examples)',
      summary:
        'Cosmic-themed desktop chat surface rendered with the shared cosmic CSS + renderers — one page (and PNG baseline) per state. Hard gate: npm run test:cosmic-visual.',
      entry: 'index.html',
      createdAt: '2026-06-03T00:00:00Z',
      tags: ['desktop', 'chat', 'cosmic', 'visual-example'],
      states: states.map((s) => ({ state: s.state, title: s.title, page: s.file, png: s.png })),
    };
    fs.writeFileSync(path.join(outDir, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');

    process.stdout.write(`${path.join(outDir, 'index.html')}\n`);
    for (const s of states) process.stdout.write(`  ${s.state} -> ${path.join(outDir, s.file)}\n`);
  } finally {
    try {
      fs.rmSync(dir, { recursive: true, force: true });
    } catch (_) {
      // best-effort temp cleanup
    }
  }
}

main();
