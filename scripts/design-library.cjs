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
async function run(argv, hooks = {}) {
  const { command, options: o } = parse(argv);
  if (!o.library && ['publish', 'record', 'verify', 'rollback', 'link-preview', 'export', 'prune'].includes(command)) o.library = process.env.PENTACLE_DESIGN_LIBRARY_ROOT || path.join(os.homedir(), 'agent-workspace', 'design-library');
  const need = key => { if (!o[key]) throw new Error(`--${key} is required`); return path.resolve(o[key]); };
  if (command === 'import') {
    const result = library.importDrop({ drop: need('drop'), staging: need('staging'), dependencies: o.dependencies ? library.readJson(need('dependencies')) : {}, library: o.library ? need('library') : null });
    return { dir: result.dir, input_id: result.input_id };
  }
  if (command === 'board') {
    const drop = need('drop');
    const descriptor = library.readJson(library.contained(drop, 'drop.json'));
    library.validateDrop(descriptor, library.inventory(library.contained(drop, 'payload')));
    const designs = library.resolveDesigns(descriptor);
    return { designs, warnings: library.boardWarnings(descriptor, designs) };
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
  if (command === 'link-preview') {
    if (!o.project || !o.design) throw new Error('Design project and board IDs are required');
    for (const id of [o.project, o.design, ...(o.view ? [o.view] : [])]) library.assertId(id);
    const record = library.createLibraryReader(need('library')).selected(o.project);
    if (record.sync !== 'verified') throw new Error('No verified current revision for this Design project on this host');
    const { design, view, file } = library.designLinkPreview(record, o.design, o.view);
    if (file.size > 25 * 1024 * 1024) throw new Error('Recorded PNG exceeds the attachment limit');
    return {
      path: library.contained(record.dir, file.path),
      sha256: file.sha256,
      dashboard_link: {
        dashboard: 'design',
        target: { project_id: o.project, design_id: o.design, ...(o.view ? { view_id: o.view } : {}) },
        title: o.view ? view.title : design.title,
        subtitle: design.title,
        revision_id: record.manifest.revision_id,
      },
    };
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
    const keep = o.keep === undefined ? 3 : o.keep === 'all' ? null : Number(o.keep);
    if (keep !== null && (!Number.isInteger(keep) || keep < 2)) throw new Error('--keep must be "all" or a whole number of at least 2');
    const root = need('library'), recorder = require('../lib/design_record.cjs');
    const timings = {}; let lap = Date.now();
    const phase = name => { const now = Date.now(); timings[name] = now - lap; lap = now; };
    const imported = library.importDrop({ drop: need('drop'), staging: need('staging'), dependencies: o.dependencies ? library.readJson(need('dependencies')) : {}, library: root });
    const project = imported.manifest.project_id;
    imported.manifest.profile.capture = await (hooks.captureProfile || recorder.captureProfile)();
    imported.input_id = imported.manifest.input_id = library.inputIdentity(imported.manifest.provenance, imported.manifest.profile);
    library.atomicJson(path.join(imported.dir, 'manifest.json'), imported.manifest);
    phase('import');
    // Idempotence requires verification, never just trusting input_id on disk.
    const reader = library.createLibraryReader(root), catalog = reader.catalog(), retained = [];
    for (const r of catalog.projects.find(p => p.id === project)?.revisions || []) {
      const existing = reader.revision(project, r.revision_id);
      if (existing.manifest.input_id === imported.input_id) {
        if (!existing.manifest.complete) return { revision_id: r.revision_id, current: false, incomplete: true, reused: true };
        if (reader.selected(project).manifest?.revision_id !== r.revision_id) library.rollback({ library: root, project, revision: r.revision_id });
        return { revision_id: r.revision_id, current: true, reused: true };
      }
      if (existing.manifest.complete) retained.push(existing);
    }
    retained.sort((a, b) => b.manifest.imported_at.localeCompare(a.manifest.imported_at) || b.manifest.revision_id.localeCompare(a.manifest.revision_id));
    phase('verify');
    await require('../lib/design_prepare.cjs').prepareRevision(imported.dir);
    phase('prepare');
    const recorded = await recorder.recordRevision(imported.dir, { out: o.out ? need('out') : imported.dir, retained, capture: hooks.capture, profile: imported.manifest.profile.capture });
    phase('record');
    library.finalize(imported.dir, { selected: reader.selected(project), library: root });
    phase('finalize');
    const result = library.publishRevision({ dir: imported.dir, library: root });
    phase('install');
    const generated = recorded.views.map(view => view.generated).filter(Boolean);
    const outcome = { ...result, counts: { captured: generated.filter(g => !g.reused_from).length, reused: generated.filter(g => g.reused_from).length } };
    if (result.current && !result.reused && !result.incomplete && keep !== null) {
      // Pruning follows only a selection this process has just read back verified.
      try {
        if (library.createLibraryReader(root).selected(project).manifest?.revision_id !== result.revision_id) throw new Error('the new selection did not verify');
        const pruned = library.pruneProject({ library: root, project, keep, apply: true });
        outcome.pruned = { removed: pruned.remove.map(r => r.revision_id), bytes: pruned.bytes };
      } catch (error) { outcome.pruned = { refused: error.message }; }
      phase('prune');
    }
    return { ...outcome, timings_ms: timings };
  }
  if (command === 'export') {
    if (!o.project) throw new Error('--project is required');
    return library.exportDrop({ library: need('library'), project: o.project, out: need('out') });
  }
  if (command === 'reindex') return { drop: need('drop'), files: library.reindexDrop(need('drop')) };
  if (command === 'prune') {
    if (!o.project) throw new Error('--project is required');
    if (o.apply !== undefined && o.apply !== 'yes') throw new Error('--apply takes only yes');
    return library.pruneProject({ library: need('library'), project: o.project, keep: o.keep === undefined ? 3 : Number(o.keep), apply: o.apply === 'yes' });
  }
  if (command === 'rollback') return library.rollback({ library: need('library'), project: o.project, revision: o.revision });
  throw new Error('usage: design-library.cjs import|board|prepare|record|verify|publish|export|reindex|prune|rollback|link-preview --name value; see docs/design-library.md');
}
if (require.main === module) run(process.argv.slice(2)).then(result => console.log(JSON.stringify(result))).catch(error => { console.error(error.message); process.exitCode = 1; });
module.exports = { parse, run };
