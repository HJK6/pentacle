#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const library = require('../lib/design_library.cjs');

function parse(argv) {
  const [command, ...flags] = argv, options = {};
  for (let i = 0; i < flags.length; i += 2) {
    if (!/^--[a-z-]+$/.test(flags[i]) || !flags[i + 1] || flags[i + 1].startsWith('--')) throw new Error('expected --name value');
    options[flags[i].slice(2)] = flags[i + 1];
  }
  return { command, options };
}
async function run(argv) {
  const { command, options: o } = parse(argv);
  if (!o.library && ['publish', 'record', 'verify', 'rollback'].includes(command)) o.library = process.env.PENTACLE_DESIGN_LIBRARY_ROOT || path.join(os.homedir(), 'agent-workspace', 'design-library');
  const need = key => { if (!o[key]) throw new Error(`--${key} is required`); return path.resolve(o[key]); };
  if (command === 'import') {
    const result = library.importDrop({ drop: need('drop'), staging: need('staging'), dependencies: o.dependencies ? library.readJson(need('dependencies')) : {}, library: o.library ? need('library') : null });
    return { dir: result.dir, input_id: result.input_id };
  }
  if (command === 'prepare') return { state: (await require('../lib/design_prepare.cjs').prepareRevision(need('dir'))).state };
  if (command === 'record') {
    let dir;
    if (o.dir) dir = need('dir');
    else {
      const reader = library.createLibraryReader(need('library'));
      if (!o.project || !o.revision) throw new Error('--dir or --library --project --revision is required');
      // Re-record into an owned derivative; retained originals stay immutable.
      const revision = reader.revision(o.project, o.revision);
      const output = need('out'); fs.mkdirSync(output, { recursive: true });
      dir = fs.mkdtempSync(path.join(output, 'record-'));
      fs.cpSync(revision.dir, dir, { recursive: true });
      const manifest = revision.manifest; delete manifest.revision_id; delete manifest.files;
      manifest.state = 'prepared'; library.atomicJson(path.join(dir, 'manifest.json'), manifest);
    }
    const manifest = await require('../lib/design_record.cjs').recordRevision(dir, { out: o.out ? need('out') : dir });
    if (!o.dir) {
      const final = library.finalize(dir);
      return { dir, state: final.state, revision_id: final.revision_id };
    }
    return { dir, state: manifest.state };
  }
  if (command === 'verify') {
    if (o.dir) { const m = library.verifyRevision(need('dir')); return { verified: true, project_id: m.project_id, revision_id: m.revision_id }; }
    const reader = library.createLibraryReader(need('library'));
    if (!o.project) return reader.catalog();
    const r = o.revision ? reader.revision(o.project, o.revision) : reader.selected(o.project);
    if (!r.manifest || r.sync === 'pending') throw new Error('selected revision has not fully arrived/verified');
    return { verified: true, project_id: r.manifest.project_id, revision_id: r.manifest.revision_id, files: r.manifest.files.length };
  }
  if (command === 'publish') {
    const root = need('library');
    const imported = library.importDrop({ drop: need('drop'), staging: need('staging'), dependencies: o.dependencies ? library.readJson(need('dependencies')) : {}, library: root });
    imported.manifest.profile.capture = await require('../lib/design_record.cjs').captureProfile();
    imported.input_id = imported.manifest.input_id = library.inputIdentity(imported.manifest.provenance, imported.manifest.profile);
    library.atomicJson(path.join(imported.dir, 'manifest.json'), imported.manifest);
    // Idempotence requires verification, never just trusting input_id on disk.
    const reader = library.createLibraryReader(root), catalog = reader.catalog();
    for (const r of catalog.projects.find(p => p.id === imported.manifest.project_id)?.revisions || []) {
      const existing = reader.revision(imported.manifest.project_id, r.revision_id);
      if (existing.manifest.input_id === imported.input_id) {
        if (!existing.manifest.complete) return { revision_id: r.revision_id, current: false, incomplete: true, reused: true };
        if (reader.selected(imported.manifest.project_id).manifest?.revision_id !== r.revision_id) library.rollback({ library: root, project: imported.manifest.project_id, revision: r.revision_id });
        return { revision_id: r.revision_id, current: true, reused: true };
      }
    }
    await require('../lib/design_prepare.cjs').prepareRevision(imported.dir);
    await require('../lib/design_record.cjs').recordRevision(imported.dir, { out: o.out ? need('out') : imported.dir });
    library.finalize(imported.dir);
    return library.publishRevision({ dir: imported.dir, library: root });
  }
  if (command === 'rollback') return library.rollback({ library: need('library'), project: o.project, revision: o.revision });
  throw new Error('usage: design-library.cjs import|prepare|record|verify|publish|rollback --name value; see docs/design-library.md');
}
if (require.main === module) run(process.argv.slice(2)).then(result => console.log(JSON.stringify(result))).catch(error => { console.error(error.message); process.exitCode = 1; });
module.exports = { parse, run };
