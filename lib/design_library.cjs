'use strict';

// Private design data never belongs in the repository. All paths below are
// relative to an explicitly configured library or an owned staging directory.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const os = require('node:os');
const { execFileSync } = require('node:child_process');

const ID = /^[a-z0-9][a-z0-9-]{0,79}$/;
const HASH = /^[a-f0-9]{64}$/;
const LIMITS = Object.freeze({ files: 10000, total: 512 * 1024 * 1024, file: 64 * 1024 * 1024, prepared: 32 * 1024 * 1024 });
const EXTENSIONS = new Set(['.html', '.htm', '.js', '.jsx', '.ts', '.tsx', '.css', '.json', '.md', '.txt', '.svg', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif', '.ico', '.woff', '.woff2', '.ttf', '.otf', '.mp4', '.webm', '.pdf', '.zip']);
const sha256 = value => crypto.createHash('sha256').update(value).digest('hex');
function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().filter(k => value[k] !== undefined).map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
function fail(message, file = '') { throw new Error(file ? `${file}: ${message}` : message); }
function safePath(value) {
  if (typeof value !== 'string' || !value || value.length > 1024 || /[\\\x00-\x1f\x7f:]/.test(value) || value.startsWith('/') || value.split('/').some(p => !p || p === '.' || p === '..' || /[. ]$/.test(p))) fail('invalid relative path', String(value));
  return value;
}
function assertId(value) { if (!ID.test(value)) fail('invalid id', String(value)); return value; }
function contained(root, relative) {
  safePath(relative);
  const realRoot = fs.realpathSync(root);
  let current = realRoot;
  for (const part of relative.split('/')) {
    current = path.join(current, part);
    if (fs.lstatSync(current).isSymbolicLink()) fail('symlink refused', relative);
  }
  const real = fs.realpathSync(current);
  if (!real.startsWith(realRoot + path.sep)) fail('path escaped root', relative);
  return real;
}
function readJson(file, limit = 16 * 1024 * 1024) {
  const s = fs.lstatSync(file);
  if (!s.isFile() || s.isSymbolicLink() || s.size > limit) fail('invalid JSON file', path.basename(file));
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}
function atomicJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temp = file + '.tmp-' + crypto.randomUUID();
  const fd = fs.openSync(temp, 'wx', 0o600);
  try { fs.writeFileSync(fd, canonical(value) + '\n'); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  fs.renameSync(temp, file);
}
function inventory(root) {
  const files = []; let total = 0;
  const seen = new Set();
  function walk(dir, prefix = '') {
    for (const name of fs.readdirSync(dir).sort()) {
      const rel = safePath(prefix + name), file = path.join(dir, name), s = fs.lstatSync(file);
      const key = rel.normalize('NFC').toLowerCase();
      if (seen.has(key)) fail('case-colliding path', rel); seen.add(key);
      if (s.isSymbolicLink()) fail('symlink refused', rel);
      if (s.isDirectory()) { walk(file, rel + '/'); continue; }
      if (!s.isFile()) fail('non-regular file', rel);
      if (s.size > LIMITS.file) fail('file exceeds 64 MiB', rel);
      total += s.size;
      if (total > LIMITS.total || files.length >= LIMITS.files) fail('expanded drop limit exceeded', rel);
      files.push({ path: rel, size: s.size, sha256: sha256(fs.readFileSync(file)) });
    }
  }
  walk(root); return files.sort((a, b) => a.path.localeCompare(b.path, 'en'));
}
function sourceExtension(file) {
  if (!EXTENSIONS.has(path.extname(file).toLowerCase()) && !['.thumbnail', '.gitignore'].includes(path.basename(file))) fail('unsupported source file', file);
  if (file.split('/').some(p => ['.git', 'node_modules'].includes(p))) fail('unsupported source directory', file);
}
function viewport(value = { width: 1440, height: 900 }) {
  if (!Number.isInteger(value.width) || !Number.isInteger(value.height) || value.width < 200 || value.width > 4096 || value.height < 200 || value.height > 4096) fail('invalid viewport');
  return { width: value.width, height: value.height };
}
function validateDrop(drop, actual) {
  if (!drop || drop.schema !== 'design-drop.v1') fail('unknown drop schema');
  assertId(drop.project_id);
  for (const k of ['title', 'source_revision', 'exported_at', 'exporter']) if (typeof drop[k] !== 'string' || !drop[k].trim()) fail(`missing ${k}`);
  if (!['claude-design', 'claude-artifact', 'local'].includes(drop.source_kind)) fail('unknown source_kind');
  if (typeof drop.complete !== 'boolean' || !Array.isArray(drop.missing_paths) || !Array.isArray(drop.files) || !Array.isArray(drop.views) || !Array.isArray(drop.flows)) fail('incomplete descriptor');
  if (!drop.source_id && !drop.source_url) fail('source identity required');
  const listed = new Map(), folded = new Set(); let total = 0;
  for (const f of drop.files) {
    safePath(f.path); sourceExtension(f.path);
    const key = f.path.normalize('NFC').toLowerCase();
    if (folded.has(key)) fail('duplicate/case-colliding path', f.path); folded.add(key);
    if (!HASH.test(f.sha256) || !Number.isInteger(f.size) || f.size < 0 || f.size > LIMITS.file) fail('invalid inventory', f.path);
    listed.set(f.path, f); total += f.size;
  }
  if (total > LIMITS.total || listed.size > LIMITS.files) fail('expanded drop limit exceeded');
  for (const f of actual) {
    const expected = listed.get(f.path);
    if (!expected || expected.sha256 !== f.sha256 || expected.size !== f.size) fail('digest/size mismatch or unlisted file', f.path);
  }
  for (const f of listed.values()) if (!actual.some(a => a.path === f.path)) fail('listed source missing', f.path);
  const missing = new Set(drop.missing_paths.map(safePath));
  if (drop.complete && missing.size) fail('complete drop declares missing paths');
  const ids = new Set();
  for (const view of drop.views) {
    assertId(view.id); if (ids.has(view.id)) fail('duplicate item id', view.id); ids.add(view.id);
    safePath(view.entry); viewport(view.viewport);
    if (typeof view.title !== 'string' || !view.title) fail('view title required', view.id);
    if (!listed.has(view.entry) && (drop.complete || !missing.has(view.entry))) fail('view entry missing', view.entry);
    if (view.approval_ref !== null && (typeof view.approval_ref !== 'string' || !view.approval_ref.trim())) fail('approval must be an evidence pointer or null', view.id);
  }
  for (const flow of drop.flows) {
    assertId(flow.id); if (ids.has(flow.id)) fail('duplicate item id', flow.id); ids.add(flow.id);
    if (!drop.views.some(v => v.entry === flow.entry) || !Array.isArray(flow.view_ids) || flow.view_ids.some(id => !drop.views.some(v => v.id === id))) fail('flow references unknown view', flow.id);
    if (!Array.isArray(flow.steps) || !flow.steps.length || typeof flow.title !== 'string') fail('explicit flow steps/title required', flow.id);
    viewport(flow.viewport);
    for (const step of flow.steps) {
      if (!['click', 'fill', 'assert', 'wait'].includes(step.action) || typeof step.name !== 'string' || !step.name) fail('invalid deterministic step', flow.id);
      if (step.action === 'wait') { if (!Number.isInteger(step.ms) || step.ms < 0 || step.ms > 5000) fail('invalid flow delay', flow.id); }
      else if (typeof step.selector !== 'string' || !step.selector) fail('selector required', flow.id);
      if (step.action === 'fill' && typeof step.value !== 'string') fail('fill value required', flow.id);
    }
  }
  return drop;
}

// Python's ZIP reader checks the complete central directory before extracting a
// byte; shell tools that write first and inspect later are deliberately avoided.
function extractZip(zip, destination, python = process.env.PENTACLE_PYTHON || 'python3') {
  execFileSync(python, ['-c', `import zipfile,stat,pathlib,sys
z=zipfile.ZipFile(sys.argv[1]); dest=pathlib.Path(sys.argv[2]); seen=set(); total=0
entries=z.infolist()
if len(entries)>20000: raise ValueError('too many ZIP entries')
for i in entries:
 p=i.filename.rstrip('/'); parts=p.split('/'); key=p.casefold()
 if not p or p.startswith('/') or any(c in p for c in ('\\\\',':','\\x00')) or any(x in ('','..','.') or x.endswith(('.',' ')) for x in parts): raise ValueError('invalid ZIP path: '+p)
 if key in seen: raise ValueError('duplicate ZIP path: '+p)
 seen.add(key); mode=i.external_attr>>16
 if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR)): raise ValueError('non-regular ZIP entry: '+p)
 if i.file_size>67108864: raise ValueError('oversized ZIP member: '+p)
 total+=i.file_size
 if total>536870912: raise ValueError('expanded ZIP limit exceeded: '+p)
if sum(not i.is_dir() for i in entries)>10000: raise ValueError('too many ZIP files')
dest.mkdir(parents=True,exist_ok=False)
for i in entries:
 p=dest/i.filename
 if i.is_dir(): p.mkdir(parents=True,exist_ok=True)
 else:
  p.parent.mkdir(parents=True,exist_ok=True)
  with z.open(i) as src,p.open('xb') as dst:
   count=0
   while True:
    b=src.read(1048576)
    if not b: break
    count+=len(b)
    if count>i.file_size: raise ValueError('ZIP size mismatch: '+i.filename)
    dst.write(b)
`, zip, destination], { stdio: 'pipe', timeout: 120000 });
  return destination;
}
function toolProfile(dependencies = {}) {
  return { preparation: 'offline-html.v1', preparer_sha256: sha256(fs.readFileSync(path.join(__dirname, 'design_prepare.cjs'))), recorder_sha256: sha256(fs.readFileSync(path.join(__dirname, 'design_record.cjs'))), esbuild: require('esbuild/package.json').version, playwright: require('playwright/package.json').version, dependencies };
}
function profileIdentity(profile) {
  const result = structuredClone(profile);
  // Cache locations are host-local; identical dependency bytes retain identity
  // when an editing copy moves between execution hosts.
  for (const asset of Object.values(result.dependencies?.assets || {})) delete asset.path;
  return result;
}
function inputIdentity(drop, profile) {
  return sha256(canonical({ files: [...drop.files].sort((a, b) => a.path.localeCompare(b.path, 'en')), views: drop.views, flows: drop.flows, profile: profileIdentity(profile), provenance: { title: drop.title, source_kind: drop.source_kind, source_id: drop.source_id, source_url: drop.source_url, source_revision: drop.source_revision, complete: drop.complete, missing_paths: drop.missing_paths } }));
}
function approvalState(drop, view) {
  return sha256(canonical({ files: drop.files, entry: view.entry, viewport: viewport(view.viewport), variant: view.variant || null, screen: view.screen || null }));
}
function importDrop({ drop: dropPath, staging, dependencies = {}, library }) {
  const stageRoot = path.resolve(staging || path.join(os.tmpdir(), 'design-library-staging'));
  fs.mkdirSync(stageRoot, { recursive: true });
  const sourceRoot = dropPath.endsWith('.zip') ? extractZip(dropPath, path.join(stageRoot, 'unzip-' + crypto.randomUUID())) : path.resolve(dropPath);
  const descriptor = readJson(contained(sourceRoot, 'drop.json'));
  const payload = contained(sourceRoot, 'payload');
  const actual = inventory(payload); validateDrop(descriptor, actual);
  const profile = toolProfile(dependencies), input_id = inputIdentity(descriptor, profile);
  const dir = fs.mkdtempSync(path.join(stageRoot, `${descriptor.project_id}-`));
  fs.mkdirSync(path.join(dir, 'source'));
  for (const f of actual) {
    const dest = path.join(dir, 'source', f.path); fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(contained(payload, f.path), dest, fs.constants.COPYFILE_EXCL);
    fs.chmodSync(dest, 0o444);
  }
  const manifest = { schema: 'design-library.v1', project_id: descriptor.project_id, title: descriptor.title, input_id, source_revision: descriptor.source_revision, provenance: descriptor, profile, imported_at: new Date().toISOString(), complete: descriptor.complete, missing_paths: descriptor.missing_paths, views: structuredClone(descriptor.views), flows: structuredClone(descriptor.flows), state: 'imported' };
  const previous = library ? createLibraryReader(library).selected(descriptor.project_id).manifest : null;
  manifest.approval_refs = [];
  for (const view of manifest.views) {
    const old = previous?.views.find(v => v.id === view.id || v.entry === view.entry);
    if (old?.approval_ref && old.approval_ref === view.approval_ref && approvalState(previous.provenance, old) !== approvalState(descriptor, view)) {
      view.approval_stale_ref = view.approval_ref; view.approval_ref = null;
    }
    if (view.approval_ref) manifest.approval_refs.push({ view_id: view.id, reference: view.approval_ref, source_state_sha256: approvalState(descriptor, view) });
  }
  atomicJson(path.join(dir, 'manifest.json'), manifest);
  return { dir, input_id, manifest };
}
function revisionIdentity(manifest) {
  const { revision_id, imported_at, generated_at, ...content } = manifest;
  // Bindings name the identity containing them, so their self-reference alone
  // is excluded. The tool/browser/flow/viewport hashes remain part of identity.
  const clone = structuredClone(content);
  clone.profile = profileIdentity(clone.profile);
  for (const item of [...clone.views, ...clone.flows]) if (item.generated) delete item.generated.revision_id;
  return sha256(canonical(clone));
}
function finalize(dir) {
  const file = path.join(dir, 'manifest.json'), m = readJson(file);
  m.files = inventory(dir).filter(f => f.path !== 'manifest.json');
  m.state = m.complete ? 'verified' : 'incomplete';
  m.revision_id = revisionIdentity(m);
  for (const item of [...m.views, ...m.flows]) if (item.generated) item.generated.revision_id = m.revision_id;
  atomicJson(file, m); verifyRevision(dir, m.revision_id); return m;
}
function verifyRevision(dir, expectedId) {
  const m = readJson(contained(dir, 'manifest.json'));
  if (m.schema !== 'design-library.v1' || !HASH.test(m.revision_id) || (expectedId && expectedId !== m.revision_id) || revisionIdentity(m) !== m.revision_id) fail('invalid revision identity');
  assertId(m.project_id);
  if (!Array.isArray(m.files) || m.files.length > LIMITS.files) fail('invalid manifest inventory');
  const listed = new Set();
  for (const f of m.files) {
    safePath(f.path);
    if (listed.has(f.path.toLowerCase())) fail('duplicate manifest path', f.path); listed.add(f.path.toLowerCase());
    const file = contained(dir, f.path), stat = fs.statSync(file);
    if (!stat.isFile() || stat.size !== f.size || sha256(fs.readFileSync(file)) !== f.sha256) fail('revision hash/size mismatch', f.path);
  }
  const actual = inventory(dir).filter(f => f.path !== 'manifest.json');
  if (actual.length !== m.files.length) fail('revision contains unlisted files');
  validateDrop(m.provenance, actual.filter(f => f.path.startsWith('source/')).map(f => ({ ...f, path: f.path.slice(7) })));
  if (m.input_id !== inputIdentity(m.provenance, m.profile)) fail('stale input identity');
  const has = rel => m.files.some(f => f.path === rel);
  function generated(item, size) {
    const g = item.generated;
    if (!g) return;
    if (g.revision_id !== m.revision_id || g.input_id !== m.input_id || g.preparation_hash !== sha256(canonical(profileIdentity(m.profile))) || g.viewport_hash !== sha256(canonical(viewport(size))) || g.flow_hash !== sha256(canonical(item.steps || [])) || m.profile.capture && g.browser_hash !== m.profile.capture.browser_sha256) fail('stale generated binding', item.id);
  }
  for (const view of m.views) {
    if (view.approval_ref && !m.approval_refs?.some(a => a.view_id === view.id && a.reference === view.approval_ref && a.source_state_sha256 === approvalState(m.provenance, view))) fail('stale approval evidence binding', view.id);
    if (!view.compatibility || !['offline-interactive', 'offline-with-mocks/limitations', 'screenshot/video-only', 'incomplete'].includes(view.compatibility.status)) fail('missing compatibility', view.id);
    if (!view.compatibility.reason) fail('compatibility reason required', view.id);
    if (view.prepared && (!has(view.prepared) || !view.prepared.startsWith('prepared/'))) fail('prepared file missing', view.id);
    if (!view.screenshots?.length && view.compatibility.status !== 'incomplete') fail('screenshot required', view.id);
    for (const shot of view.screenshots || []) if (!has(shot.path)) fail('screenshot missing', view.id);
    generated(view, view.viewport);
  }
  for (const flow of m.flows) {
    if (!flow.video || !has(flow.video) || !flow.recording || !has(flow.recording) || !flow.screenshots?.length || !flow.generated || flow.generated.revision_id !== m.revision_id) fail('flow capture missing/stale', flow.id);
    for (const shot of flow.screenshots) if (!has(shot.path)) fail('flow screenshot missing', flow.id);
    generated(flow, flow.viewport || m.views.find(v => v.entry === flow.entry)?.viewport);
  }
  return m;
}
function publishRevision({ dir, library }) {
  const m = verifyRevision(dir), root = path.resolve(library);
  if (dir === root || dir.startsWith(root + path.sep)) fail('build staging must be outside sync library');
  fs.mkdirSync(root, { recursive: true });
  let project = fs.realpathSync(root);
  for (const part of ['projects', m.project_id, 'revisions']) {
    project = path.join(project, part);
    if (fs.existsSync(project)) { if (fs.lstatSync(project).isSymbolicLink() || !fs.statSync(project).isDirectory()) fail('unsafe publication directory'); }
    else fs.mkdirSync(project);
  }
  project = path.dirname(project);
  const destination = path.join(project, 'revisions', m.revision_id);
  if (fs.existsSync(destination)) verifyRevision(destination, m.revision_id);
  else {
    const temp = path.join(project, 'revisions', '.tmp-' + crypto.randomUUID());
    fs.cpSync(dir, temp, { recursive: true, dereference: false, errorOnExist: true, force: false });
    verifyRevision(temp, m.revision_id); fs.renameSync(temp, destination);
  }
  // Incomplete revisions are retained/indexed drafts and never select current.
  if (!m.complete) return { revision_id: m.revision_id, current: false, incomplete: true };
  const pointer = path.join(project, 'current.json');
  if (fs.existsSync(pointer)) {
    const old = readJson(pointer);
    if (old.revision_id === m.revision_id) return { revision_id: m.revision_id, current: true, reused: true };
    atomicJson(path.join(project, 'history', `${Date.now()}-${crypto.randomUUID()}.json`), old);
  }
  atomicJson(pointer, { schema: 'design-current.v1', project_id: m.project_id, revision_id: m.revision_id });
  return { revision_id: m.revision_id, current: true };
}
function rollback({ library, project, revision }) {
  assertId(project); if (!HASH.test(revision)) fail('invalid revision');
  const dir = contained(library, `projects/${project}/revisions/${revision}`), m = verifyRevision(dir, revision);
  if (!m.complete) fail('cannot select incomplete revision');
  const pointer = path.join(library, 'projects', project, 'current.json');
  if (fs.existsSync(pointer)) atomicJson(path.join(library, 'projects', project, 'history', `${Date.now()}-${crypto.randomUUID()}.json`), readJson(pointer));
  atomicJson(pointer, { schema: 'design-current.v1', project_id: project, revision_id: revision });
  return { revision_id: revision, current: true };
}

// A per-process last VERIFIED selection survives a partial Syncthing update.
// No optimistic pointer is exposed while the selected revision is incomplete.
function createLibraryReader(library) {
  const previous = new Map();
  function revision(project, id) {
    assertId(project); if (!HASH.test(id)) fail('invalid revision');
    const dir = contained(library, `projects/${project}/revisions/${id}`);
    const manifest = verifyRevision(dir, id);
    if (manifest.project_id !== project) fail('project mismatch');
    return { dir, manifest };
  }
  function selected(project) {
    let error;
    try {
      const p = readJson(contained(library, `projects/${assertId(project)}/current.json`));
      if (p.schema !== 'design-current.v1' || p.project_id !== project) fail('invalid current pointer');
      const value = revision(project, p.revision_id);
      if (!value.manifest.complete) fail('incomplete selection');
      previous.set(project, p.revision_id); return { ...value, sync: 'verified' };
    } catch (e) { error = e.message; }
    const old = previous.get(project);
    if (old) { try { return { ...revision(project, old), sync: 'pending', error }; } catch {} }
    return { manifest: null, sync: 'pending', error };
  }
  function catalog() {
    if (!library || !fs.existsSync(path.join(library, 'projects'))) return { schema: 'design-catalog.v1', authority: 'Claude Design', projects: [] };
    const projects = [];
    for (const project of fs.readdirSync(path.join(library, 'projects')).sort().filter(p => ID.test(p))) {
      const current = selected(project), revisions = []; let conflicts = false;
      try {
        for (const id of fs.readdirSync(contained(library, `projects/${project}/revisions`))) {
          if (id.includes('sync-conflict')) conflicts = true;
          if (!HASH.test(id)) continue;
          try { const r = revision(project, id); revisions.push({ revision_id: id, source_revision: r.manifest.source_revision, complete: r.manifest.complete, imported_at: r.manifest.imported_at }); } catch { /* transfer still in progress */ }
        }
        conflicts ||= fs.readdirSync(contained(library, `projects/${project}`)).some(n => n.includes('sync-conflict'));
      } catch {}
      projects.push({ id: project, title: current.manifest?.title || project, sync: conflicts ? 'conflict' : current.sync, revisions, revision_id: current.manifest?.revision_id || null, manifest: current.manifest });
    }
    return { schema: 'design-catalog.v1', authority: 'Claude Design', projects };
  }
  return { selected, revision, catalog };
}
module.exports = { ID, HASH, LIMITS, sha256, canonical, safePath, assertId, contained, readJson, atomicJson, inventory, validateDrop, extractZip, toolProfile, profileIdentity, inputIdentity, approvalState, importDrop, revisionIdentity, finalize, verifyRevision, publishRevision, rollback, createLibraryReader, viewport };
