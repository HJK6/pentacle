#!/usr/bin/env node
'use strict';

// Build the shippable classic-UMD artifact from src/, stamping VERSION with the
// source git short-sha (deterministic; the sync steps in each consumer copy
// dist/ + VERSION and compare the stamp to detect a stale vendored copy).

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const ROOT = __dirname;
const SRC = path.join(ROOT, 'src', 'dashboard-library.js');
const SRC_CSS = path.join(ROOT, 'src', 'dashboard-library.css');
const DIST_DIR = path.join(ROOT, 'dist');
const DIST = path.join(DIST_DIR, 'dashboard-library.js');
const DIST_CSS = path.join(DIST_DIR, 'dashboard-library.css');
const VERSION_FILE = path.join(ROOT, 'VERSION');

// VERSION is a DETERMINISTIC content hash of the source (the `__VERSION__`
// placeholder is hashed verbatim, never substituted, so the hash is stable).
// Idempotent: identical source ⇒ identical VERSION ⇒ identical dist, so a
// routine `npm run build` / `npm test` / `npm install` (prepare) never churns
// tracked files or breaks the consumers' stale-vendor guards. The version
// changes only when the source content changes — which is exactly when both
// consumers must re-sync. (Code-QA major #2, 2026-05-25.)
function computeBuild() {
  const rawSrc = fs.readFileSync(SRC, 'utf8');
  const rawCss = fs.existsSync(SRC_CSS) ? fs.readFileSync(SRC_CSS, 'utf8') : '';
  const version = crypto.createHash('sha256').update(rawSrc).update('\0').update(rawCss).digest('hex').slice(0, 12);

  return {
    js: rawSrc.replace(/__VERSION__/g, version),
    css: rawCss,
    version,
  };
}

function writeBuild(build) {
  fs.mkdirSync(DIST_DIR, { recursive: true });
  fs.writeFileSync(DIST, build.js);
  if (fs.existsSync(SRC_CSS)) fs.writeFileSync(DIST_CSS, build.css);
  fs.writeFileSync(VERSION_FILE, build.version + '\n');
}

if (require.main === module) {
  const build = computeBuild();
  writeBuild(build);
  console.log(`built dist/dashboard-library.js @ VERSION=${build.version}`);
}

module.exports = {
  computeBuild,
};
