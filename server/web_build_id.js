'use strict';

// Deterministic web build identity for the update-available refresh icon
// (spec_pentacle__web_update_available_refresh_icon_2026_09).
//
// At startup the host FREEZES the served dist into memory and derives ONE
// SHA256 build id from a sorted manifest of the exact served web.html and its
// served bundle/style assets. Serving from the frozen snapshot guarantees the
// running host always returns exactly the bytes its build id was computed over,
// so concurrent requests can never mix an old page with a new id (or vice
// versa) even if the on-disk dist is replaced before the host is restarted.
// Because build:web emits stable (non-fingerprinted) filenames, a rebuild from
// identical source yields byte-identical files and therefore an identical id —
// a restart with unchanged assets never nags.

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

// The bytes that define the build identity: the served page plus its bundle and
// style assets. Source maps, fonts and icons are deliberately excluded so their
// incidental churn cannot force a spurious "update available".
function isManifestFile(rel) {
  const lower = rel.toLowerCase();
  return lower === 'web.html' || lower.endsWith('.js') || lower.endsWith('.css');
}

function walk(dir, base = dir, out = []) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const entry of entries.sort((a, b) => (a.name < b.name ? -1 : 1))) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, base, out);
    else if (entry.isFile()) out.push(path.relative(base, full).split(path.sep).join('/'));
  }
  return out;
}

// Hash the sorted manifest of {path, sha256(bytes)} for the manifest files. The
// id is stable under path/byte identity and changes when any served page/bundle/
// style byte changes. A 16-hex-char (64-bit) prefix is plenty for identity.
function computeBuildId(manifest) {
  if (!manifest.length) return null;
  const material = manifest.map((m) => `${m.path}\u0000${m.sha256}`).join('\n');
  return crypto.createHash('sha256').update(material).digest('hex').slice(0, 16);
}

function freezeWebDist(distDir) {
  const files = new Map();
  for (const rel of walk(distDir)) {
    try {
      files.set(rel, fs.readFileSync(path.join(distDir, rel)));
    } catch {
      /* skip an unreadable entry; a missing manifest file simply lowers coverage */
    }
  }
  const manifest = [...files.keys()]
    .filter(isManifestFile)
    .sort()
    .map((rel) => ({ path: rel, sha256: crypto.createHash('sha256').update(files.get(rel)).digest('hex') }));
  return { files, manifest, buildId: computeBuildId(manifest) };
}

module.exports = { freezeWebDist, computeBuildId, isManifestFile };
