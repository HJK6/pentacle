#!/usr/bin/env node
'use strict';

// Builds the browser bundle for Pentacle web mode (lane 1 of
// spec_pentacle__web_mode_2026_09).
//
//   node scripts/build-web.js            → renderer/dist/web/
//
// The page is DERIVED from renderer/index.html rather than hand-copied, so the
// desktop and web pages cannot drift: the same markup, with the <script src>
// tags replaced by the single bundle and the stylesheets rewritten to local
// copies. Electron's file:// load path is untouched.

const fs = require('fs');
const path = require('path');
const esbuild = require('esbuild');

const ROOT = path.join(__dirname, '..');
const RENDERER = path.join(ROOT, 'renderer');
const OUT = path.join(RENDERER, 'dist', 'web');

// Stylesheets index.html pulls from outside renderer/ are copied in beside the
// bundle; the rest are copied from renderer/ under the same relative name.
const EXTERNAL_CSS = {
  '../node_modules/@xterm/xterm/css/xterm.css': 'xterm.css',
};

// Scripts that stay <script src> tags instead of entering the bundle. They are
// already browser-ready IIFEs/UMDs that publish window globals, and a bundler's
// CommonJS wrapper is exactly what breaks them: a UMD sees `module.exports` and
// assigns there instead of to `window`, so `window.TriforceDashboards` and
// `window.PentacleChatCore` would silently never appear. Keeping them external
// also keeps them byte-identical to what Electron loads.
const EXTERNAL_SCRIPTS = {
  'confirm_dialog.js': 'confirm_dialog.js',
  'dashboards/registry.js': 'dashboards-registry.js',
  'dashboards/demo.js': 'dashboards-demo.js',
  'dist/chat_core.bundle.js': 'chat_core.bundle.js',
  'dist/cosmic_tokens.bundle.js': 'cosmic_tokens.bundle.js',
  'dist/cosmic_components.bundle.js': 'cosmic_components.bundle.js',
};

// The bundle takes this tag's place in the document, so everything it contains
// runs exactly where app.js ran.
const BUNDLE_ANCHOR = 'app.js';

// `../config-loader` reads the config off disk in the Electron preload; in a
// browser the host injects the computed config, so it maps to the shim. esbuild
// cannot infer that from the filesystem, hence the resolve hook.
function rendererResolvePlugin() {
  return {
    name: 'pentacle-renderer-resolve',
    setup(build) {
      build.onResolve({ filter: /(^|\/)config-loader$/ }, () => ({
        path: path.join(RENDERER, 'shims', 'config_loader.js'),
      }));
    },
  };
}

function readIndexHtml() {
  return fs.readFileSync(path.join(RENDERER, 'index.html'), 'utf8');
}

/** Every `<script src="…">` in index.html, in document order. */
function scriptSources(html) {
  return [...html.matchAll(/<script\s+src="([^"]+)"\s*>\s*<\/script>/g)].map((m) => m[1]);
}

/** Every `<link rel="stylesheet" href="…">` in index.html, in document order. */
function styleSources(html) {
  return [...html.matchAll(/<link\s+rel="stylesheet"\s+href="([^"]+)"\s*>/g)].map((m) => m[1]);
}

function buildHtml(html) {
  let out = html;
  for (const src of scriptSources(html)) {
    const tag = new RegExp(`[ \\t]*<script\\s+src="${src.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&')}"\\s*></script>\\n?`);
    if (EXTERNAL_SCRIPTS[src]) out = out.replace(`src="${src}"`, `src="${EXTERNAL_SCRIPTS[src]}"`);
    else if (src === BUNDLE_ANCHOR) out = out.replace(tag, '  <script src="bundle.js"></script>\n');
    else out = out.replace(tag, '');
  }
  for (const [href, local] of Object.entries(EXTERNAL_CSS)) {
    out = out.split(`href="${href}"`).join(`href="${local}"`);
  }
  return out.replace('</head>', '  <!--PENTACLE_CONFIG-->\n</head>');
}

function copyExternalScripts() {
  for (const [src, local] of Object.entries(EXTERNAL_SCRIPTS)) {
    fs.copyFileSync(path.resolve(RENDERER, src), path.join(OUT, local));
  }
}

// Icons referenced from <link rel="icon"> / apple-touch-icon are copied next to the page.
function copyIcons(html) {
  for (const m of html.matchAll(/<link[^>]+rel="(?:icon|apple-touch-icon)"[^>]+href="([^"]+)"/g)) {
    const from = path.resolve(RENDERER, m[1]);
    const to = path.join(OUT, m[1]);
    fs.mkdirSync(path.dirname(to), { recursive: true });
    fs.copyFileSync(from, to);
  }
}

function copyStyles(html) {
  for (const href of styleSources(html)) {
    const local = EXTERNAL_CSS[href] || href;
    const from = path.resolve(RENDERER, href);
    const to = path.join(OUT, local);
    fs.mkdirSync(path.dirname(to), { recursive: true });
    fs.copyFileSync(from, to);
  }
}

const PREREQ_BUNDLES = [
  ['renderer/src/chat_core_entry.ts', 'renderer/dist/chat_core.bundle.js'],
  ['renderer/src/cosmic_tokens_entry.ts', 'renderer/dist/cosmic_tokens.bundle.js'],
  ['renderer/src/cosmic_components_entry.ts', 'renderer/dist/cosmic_components.bundle.js'],
];

// index.html loads these as plain <script src> tags, so the web bundle can only
// inline them once they exist. Same invocation as the npm build:* scripts.
async function buildPrereqBundles() {
  for (const [entry, outfile] of PREREQ_BUNDLES) {
    await esbuild.build({
      entryPoints: [path.join(ROOT, entry)],
      bundle: true,
      format: 'iife',
      target: 'chrome134',
      outfile: path.join(ROOT, outfile),
      logLevel: 'warning',
    });
  }
}

async function build({ minify = false } = {}) {
  await buildPrereqBundles();
  fs.rmSync(OUT, { recursive: true, force: true });
  fs.mkdirSync(OUT, { recursive: true });

  await esbuild.build({
    entryPoints: [path.join(RENDERER, 'web_entry.js')],
    bundle: true,
    format: 'iife',
    target: 'chrome134',
    platform: 'browser',
    outfile: path.join(OUT, 'bundle.js'),
    sourcemap: true,
    minify,
    // The renderer's only Node dependencies. Everything else it requires is
    // already browser-safe (xterm, the renderer modules, main/mic-url).
    alias: {
      path: path.join(RENDERER, 'shims', 'path.js'),
    },
    plugins: [rendererResolvePlugin()],
    define: {
      // app.js does `path.join(__dirname, '..')` purely to locate the config
      // root; the shimmed loader ignores the argument.
      __dirname: '"/"',
      __filename: '"/web_entry.js"',
    },
    loader: { '.png': 'dataurl', '.svg': 'dataurl', '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl' },
    logLevel: 'info',
  });

  const html = readIndexHtml();
  fs.writeFileSync(path.join(OUT, 'web.html'), buildHtml(html));
  copyExternalScripts();
  copyStyles(html);
  copyIcons(html);

  // Fonts and images referenced from the copied stylesheets by relative URL.
  for (const dir of ['fonts', 'assets', 'img']) {
    const from = path.join(RENDERER, dir);
    if (fs.existsSync(from)) fs.cpSync(from, path.join(OUT, dir), { recursive: true });
  }

  console.log(`[build:web] wrote ${path.relative(ROOT, OUT)}`);
}

module.exports = { build, buildHtml, scriptSources, styleSources, EXTERNAL_CSS, EXTERNAL_SCRIPTS, BUNDLE_ANCHOR, OUT, RENDERER, PREREQ_BUNDLES };

if (require.main === module) {
  build({ minify: process.argv.includes('--minify') }).catch((e) => {
    console.error(e && e.stack ? e.stack : e);
    process.exit(1);
  });
}
