'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createHash, randomUUID } = require('node:crypto');
const Ajv = require('ajv');
const schema = require('./model_drop.schema.json');
const { makeMeshReader, round } = require('./model_mesh.cjs');
const readMesh = makeMeshReader(require('three'));

const DEFAULT_LIMITS = Object.freeze({ bytes: 32 * 1024 * 1024, triangles: 500_000 });
const DEFAULT_CAMERA = Object.freeze({ turn: -35, tilt: 25 });
const ID = /^[a-z0-9][a-z0-9-]{0,79}$/;
const DIGEST = /^[a-f0-9]{64}$/;
const validate = new Ajv({ allErrors: true, strictNumbers: true }).compile(schema);
const sha = data => createHash('sha256').update(data).digest('hex');
function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value !== null && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  }
  return JSON.stringify(value);
}
function regular(file) {
  if (!fs.lstatSync(file).isFile()) throw new Error('Use a regular file, not a symbolic link');
  return fs.readFileSync(file);
}
const json = file => JSON.parse(regular(file).toString('utf8'));
function located(root, rel) {
  let p = root;
  for (const [i, segment] of rel.entries()) {
    p = path.join(p, segment);
    const info = fs.lstatSync(p);
    if (!(i === rel.length - 1 ? info.isFile() : info.isDirectory())) throw new Error('Use only contained regular files and directories');
  }
  return p;
}
function safeDropFile(name) {
  return typeof name === 'string' && /\.glb$/i.test(name) && name.split('/').every(s =>
    /^[a-zA-Z0-9_][a-zA-Z0-9_. -]*$/.test(s) && !/[. ]$/.test(s) && !/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(s));
}
function validDate(date, now) {
  if (typeof date !== 'string' || !/^\d{4}-\d\d-\d\d$/.test(date) || date.startsWith('0000')) return false;
  const parsed = new Date(date + 'T00:00:00Z');
  return Number.isFinite(+parsed) && parsed.toISOString().slice(0, 10) === date && date <= now.toISOString().slice(0, 10);
}
function checkDrop(dir, { limits = DEFAULT_LIMITS, now = new Date() } = {}) {
  if (!Object.keys(DEFAULT_LIMITS).every(k => Number.isSafeInteger(limits[k]) && limits[k] > 0)) {
    throw new Error('Use positive whole-number byte and triangle ceilings');
  }
  const diagnostics = [], measurements = [], checked = [];
  const problem = (field, fix) => diagnostics.push({ field, fix });
  let sheet;
  try { sheet = json(path.join(dir, 'model.json')); }
  catch { problem('model.json', 'Provide a readable regular file containing valid JSON'); }
  if (sheet === undefined) return { ok: false, diagnostics, measurements };
  if (!validate(sheet)) {
    for (const e of validate.errors) {
      const leaf = e.params.missingProperty ?? e.params.additionalProperty;
      problem('model.json#' + e.instancePath + (leaf ? '/' + leaf.replace(/~/g, '~0').replace(/\//g, '~1') : ''),
        e.keyword === 'additionalProperties' ? 'Remove the unsupported field' :
          e.keyword === 'required' ? 'Supply the required field' : 'Correct this value: it ' + e.message);
    }
  }
  const nonblank = (value, field) => {
    if (typeof value === 'string' && !value.trim()) problem(field, 'Supply nonblank plain text');
  };
  const texts = [[sheet, '#']];
  while (texts.length) {
    const [value, field] = texts.pop();
    if (!value || typeof value !== 'object') continue;
    for (const [key, sub] of Object.entries(value)) {
      const location = field + '/' + key;
      if (typeof sub === 'string' && key !== 'material') nonblank(sub, location);
      else if (sub && typeof sub === 'object') texts.push([sub, location]);
    }
  }
  if (!validDate(sheet?.updated, now)) problem('#/updated', 'Use a real YYYY-MM-DD date no later than today UTC');
  for (const [i, fact] of (Array.isArray(sheet?.facts) ? sheet.facts : []).entries()) {
    if (fact?.url !== undefined) {
      try { if (!['http:', 'https:'].includes(new URL(fact.url).protocol) || !/^https?:\/\/[^\s]+$/.test(fact.url)) throw new Error(); }
      catch { problem('#/facts/' + i + '/url', 'Use an absolute HTTP or HTTPS URL'); }
    }
  }
  const views = Array.isArray(sheet?.views) ? sheet.views : [];
  const viewIds = new Set();
  for (const [i, v] of views.entries()) {
    if (!v || typeof v !== 'object') continue;
    const f = '#/views/' + i;
    if (viewIds.has(v.id)) problem(f + '/id', 'Give this view a unique ID');
    viewIds.add(v.id);
    const claims = new Set(), parts = new Set(), descriptions = new Set();
    const measuredParts = [];
    let mesh, bytes;
    if (!safeDropFile(v.file)) problem(f + '/file', 'Use a contained relative .glb path with safe filename segments');
    else {
      try { bytes = regular(located(dir, v.file.split('/'))); }
      catch { problem(f + '/file', 'Supply a contained, readable regular GLB at this path'); }
      if (bytes) {
        try { mesh = readMesh(bytes, limits.triangles, true); }
        catch (error) { problem(f + '/file', error instanceof SyntaxError ? 'Repair the GLB JSON chunk' : error.message); }
        const record = { view: v.id, bytes: bytes.length, triangles: mesh?.triangles ?? null };
        measurements.push(record);
        if (bytes.length > limits.bytes) problem(f + '/file', 'Reduce bytes to at most ' + limits.bytes);
        if (mesh?.triangles > limits.triangles) problem(f + '/file', 'Reduce triangles to at most ' + limits.triangles);
      }
    }
    for (const [j, part] of (Array.isArray(v.parts) ? v.parts : []).entries()) {
      if (!part || typeof part !== 'object') continue;
      const p = f + '/parts/' + j;
      if (parts.has(part.id)) problem(p + '/id', 'Give this part a unique ID within its view');
      parts.add(part.id);
      const about = typeof part.about === 'string' && part.about.trim().replace(/\s+/g, ' ').toLowerCase();
      if (!about) problem(p + '/about', 'Explain what this part does in one or two sentences');
      else {
        if (descriptions.has(about)) problem(p + '/about', 'Describe this part distinctly from other parts');
        if ((about.match(/[.!?]+(?=\s|$)/g) || []).length > 2) problem(p + '/about', 'Use at most two sentences');
        descriptions.add(about);
      }
      if (part.role === 'print' && !(typeof part.material === 'string' && part.material.trim())) {
        problem(p + '/material', 'Name the printing material');
      }
      let dimensions;
      const nodes = Array.isArray(part.nodes) ? part.nodes : [];
      for (const [k, name] of nodes.entries()) {
        const n = p + '/nodes/' + k;
        if (claims.has(name)) problem(n, 'Claim each mesh node exactly once');
        claims.add(name);
        if (mesh && !mesh.names.has(name)) problem(n, 'Use an exact mesh node name from this view');
        const size = mesh?.names.get(name);
        if (size && !dimensions) dimensions = size;
        else if (size && size.some((value, axis) => Math.abs(value - dimensions[axis]) > 0.100000001)) {
          problem(n, 'Use a matching copy, unbake its transform, or create a separate part');
        }
      }
      measuredParts.push({ ...part, dimensions: dimensions?.map(round), copy_count: nodes.length });
    }
    if (mesh) for (const name of mesh.names.keys()) {
      if (!claims.has(name)) problem(f + '/parts', 'Assign mesh node ' + JSON.stringify(name) + ' to exactly one part');
    }
    if (mesh && bytes) checked.push({ bytes, view: { ...v, camera: v.camera ?? DEFAULT_CAMERA, parts: measuredParts,
      extents: mesh.extents, bytes: bytes.length, triangles: mesh.triangles } });
  }
  const ok = diagnostics.length === 0;
  return { ok, diagnostics, measurements, ...(ok ? { sheet, checked } : {}) };
}

const absent = e => e.code === 'ENOENT';
function listDirs(dir, test) {
  try { return fs.readdirSync(dir, { withFileTypes: true }).filter(e => e.isDirectory() && test.test(e.name)).map(e => e.name).sort(); }
  catch (error) { if (absent(error)) return []; throw error; }
}

function createLibrary(root) {
  if (!root) throw new Error('Specify --root or PENTACLE_MODEL_LIBRARY_ROOT');
  root = path.resolve(root);
  const pointerPath = (p,m) => path.join(root,'models',p,m,'current.json');
  const manifestPath = (p,m,r) => path.join(root,'models',p,m,'revisions',r,'manifest.json');
  const assetPath = (digest, kind) => path.join(root,'objects',digest + (kind === 'mesh' ? '.glb' : '.png'));
  const readAsset = (digest, kind) => {
    if (!DIGEST.test(digest)) throw new Error('Invalid object digest');
    const bytes = regular(located(root,['objects', path.basename(assetPath(digest,kind))]));
    if (sha(bytes) !== digest) throw new Error('Object hash differs: ' + digest);
    return bytes;
  };
  function verifyRevision(p,m,r) {
    if (!DIGEST.test(r)) throw new Error('Invalid revision');
    const manifest = json(located(root,['models',p,m,'revisions',r,'manifest.json']));
    const { revision, published_at, ...content } = manifest;
    if (r !== revision || r !== sha(canonical({ ...content, published_at })) || !Number.isFinite(Date.parse(published_at))
      || content.sheet?.project?.id !== p || content.sheet?.id !== m || !validate(content.sheet)
      || !Array.isArray(content.views) || content.views.length !== content.sheet.views.length) throw new Error('Invalid manifest: ' + r);
    for (const [i,v] of content.views.entries()) {
      if (v.id !== content.sheet.views[i].id || readAsset(v.mesh,'mesh').length !== v.bytes) throw new Error('Invalid view inventory: ' + r);
      readAsset(v.picture,'picture');
    }
    return manifest;
  }
  const cache = new Map();
  function pointer(p,m) {
    const data = json(located(root,['models',p,m,'current.json']));
    if (data.schema !== 'model-selection.v1' || !(data.current === null || DIGEST.test(data.current))
      || !Array.isArray(data.previous) || data.previous.some(r => !DIGEST.test(r))) throw new Error('Invalid selection');
    return data;
  }
  const allModels = () => listDirs(path.join(root,'models'), ID).flatMap(p =>
    listDirs(path.join(root,'models',p), ID).map(m => [p,m]));
  function selected(p,m) {
    const key = p + '/' + m;
    try {
      const r = pointer(p,m).current;
      if (r === null) { cache.delete(key); return null; }
      const manifest = verifyRevision(p,m,r);
      cache.set(key, r);
      return manifest;
    } catch {
      const last = cache.get(key);
      if (last) try { return verifyRevision(p,m,last); } catch { /* only verified bytes */ }
      return null;
    }
  }
  const prefix = (p,m,r) => '/model-media/' + p + '/' + m + '/' + r + '/';
  const pictureUrl = (p,m,r,v) => prefix(p,m,r) + 'picture' + (v ? '/' + v : '');
  function catalog() {
    const projects = new Map();
    for (const [p,m] of allModels()) {
      const manifest = selected(p,m);
      if (!manifest) continue;
      const { revision, published_at, sheet, views } = manifest;
      if (!projects.has(p)) projects.set(p,{ id: p, title: sheet.project.title, models: [] });
      projects.get(p).models.push({ sheet, revision, published_at,
        views: views.map(({ mesh, picture, ...v }) => ({ ...v,
          mesh_url: prefix(p,m,revision) + 'mesh/' + v.id, picture_url: pictureUrl(p,m,revision,v.id) })) });
    }
    for (const project of projects.values()) {
      project.models.sort((a,b) => b.sheet.updated.localeCompare(a.sheet.updated) || a.sheet.id.localeCompare(b.sheet.id));
      project.title = project.models.reduce((a,b) => b.published_at > a.published_at ? b : a).sheet.project.title;
    }
    return { schema: 'model-catalog.v1', projects: [...projects.values()].sort((a,b) =>
      b.models[0].sheet.updated.localeCompare(a.models[0].sheet.updated) || a.id.localeCompare(b.id)) };
  }
  function media(p,m,r,kind,view) {
    try {
      // Do not expose unselected staging debris. Retained verified revisions
      // keep immutable URLs after an ordinary re-publish or removal.
      const pick = selected(p,m);
      const current = (() => { try { return pointer(p,m); } catch { return null; } })();
      const retained = (pick || current?.current === null) && current?.previous.includes(r);
      if (pick?.revision !== r && !retained) return null;
      const manifest = verifyRevision(p,m,r);
      const entry = view ? manifest.views.find(v => v.id === view) : manifest.views[0];
      return entry ? { bytes: readAsset(entry[kind], kind), digest: entry[kind] } : null;
    } catch { return null; }
  }
  function atomic(file, bytes) {
    fs.mkdirSync(root, { recursive:true });
    let dir = root;
    for (const segment of path.relative(root,path.dirname(file)).split(path.sep)) {
      dir = path.join(dir,segment);
      try { fs.mkdirSync(dir); } catch (error) { if (error.code !== 'EEXIST') throw error; }
      if (!fs.lstatSync(dir).isDirectory()) throw new Error('Library directories may not be symbolic links');
    }
    const tmp = file + '.' + randomUUID() + '.tmp';
    try {
      const fd = fs.openSync(tmp,'wx',0o600);
      try { fs.writeFileSync(fd, bytes); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
      fs.renameSync(tmp,file);
    } finally { fs.rmSync(tmp,{force:true}); }
  }
  const writePointer = (p,m,data) => atomic(pointerPath(p,m), canonical({schema:'model-selection.v1', ...data})+'\n');
  function prior(p,m) {
    try { return pointer(p,m); }
    catch (error) { if (!absent(error)) throw error; return { current:null, previous:[] }; }
  }
  function checkProject(sheet, rename) {
    const p = sheet.project.id;
    if (rename) return;
    const group = catalog().projects.find(x => x.id === p);
    const title = group?.title ?? allModels().filter(([project]) => project === p).flatMap(([,model]) => {
      const entry = prior(p,model);
      return [entry.current,...entry.previous].filter(Boolean).map(r=>verifyRevision(p,model,r));
    }).sort((a,b) => b.published_at.localeCompare(a.published_at))[0]?.sheet.project.title;
    if (title !== undefined && title !== sheet.project.title) throw new Error('Project title differs; match the current title or use --rename-project');
  }
  async function publish(drop, { capture, limits, rename = false, now = new Date() } = {}) {
    const checked = checkDrop(drop,{limits,now});
    if (!checked.ok) return checked;
    const { sheet } = checked, p = sheet.project.id, m = sheet.id;
    checkProject(sheet,rename);
    const old = prior(p,m);
    const pictures = capture || require('./model_picture.cjs').capturePictures;
    const pngs = await pictures(checked.checked, sheet);
    if (!Array.isArray(pngs) || pngs.length !== checked.checked.length || pngs.some(png =>
      !Buffer.isBuffer(png) || png.length < 33 || png.toString('hex',0,8) !== '89504e470d0a1a0a'
      || png.toString('ascii',12,16) !== 'IHDR' || png.readUInt32BE(16) !== 448 || png.readUInt32BE(20) !== 280)) {
      throw new Error('Publish needs one verified 448 × 280 PNG per view');
    }
    const views = checked.checked.map(({bytes,view},i) => ({ ...view, mesh: sha(bytes), picture: sha(pngs[i]) }));
    const content = { sheet, views };
    for (const r of [old.current,...old.previous].filter(Boolean)) {
      const verified = verifyRevision(p,m,r);
      const { revision, published_at, ...existing } = verified;
      if (canonical(existing) !== canonical(content)) continue;
      if (old.current !== revision) {
        writePointer(p,m,{ current:revision, previous:[...new Set([old.current,...old.previous].filter(x=>x && x!==revision))] });
      }
      return { ok:true, measurements:checked.measurements, revision, unchanged:old.current === revision, published_at };
    }
    const published_at = now.toISOString();
    const revision = sha(canonical({ ...content, published_at }));
    const output = { ok:true, measurements:checked.measurements, revision };
    const objects = new Map();
    for (const [i,v] of views.entries()) {
      objects.set(assetPath(v.mesh,'mesh'),checked.checked[i].bytes);
      objects.set(assetPath(v.picture,'picture'),pngs[i]);
    }
    // Validate all pre-existing objects before installing anything. New
    // files are verified against their digests before the selection changes.
    for (const [file,bytes] of objects) {
      try { if (!regular(file).equals(bytes)) throw new Error('Stored object is corrupt; restore a verified copy'); }
      catch (error) { if (!absent(error)) throw error; }
    }
    let verified;
    try { verified = verifyRevision(p,m,revision); }
    catch (error) {
      if (!absent(error)) throw error;
      const file = manifestPath(p,m,revision);
      if (fs.existsSync(file)) throw new Error('Existing revision is incomplete; restore it before publishing');
      const created = [];
      try {
        for (const [target,bytes] of objects) if (!fs.existsSync(target)) { atomic(target,bytes); created.push(target); }
        atomic(file, canonical({ ...content, revision, published_at }) + '\n');
        created.push(file);
        verified = verifyRevision(p,m,revision);
        writePointer(p,m,{ current:revision, previous:[...new Set([old.current,...old.previous].filter(r=>r && r!==revision))] });
      } catch (failure) {
        for (const target of created.reverse()) fs.rmSync(target,{force:true});
        throw failure;
      }
      return { ...output, unchanged:false, published_at:verified.published_at };
    }
    writePointer(p,m,{ current:revision, previous:[...new Set([old.current,...old.previous].filter(r=>r && r!==revision))] });
    return { ...output, unchanged:false, published_at:verified.published_at };
  }
  function verify() {
    let revisions = 0;
    for (const [p,m] of allModels()) {
      const state = pointer(p,m);
      const retained = [state.current,...state.previous].filter(Boolean);
      for (const r of new Set([...retained, ...listDirs(path.join(root,'models',p,m,'revisions'), DIGEST)])) {
        verifyRevision(p,m,r); revisions++;
      }
    }
    let objects = 0;
    const dir = path.join(root,'objects');
    let entries;
    try { entries = fs.readdirSync(dir); } catch (error) { if (!absent(error)) throw error; entries=[]; }
    for (const file of entries) {
      // An interrupted single writer may leave an unselected temporary file.
      if (file.endsWith('.tmp')) continue;
      const m = /^([a-f0-9]{64})\.(glb|png)$/.exec(file);
      if (!m) throw new Error('Unexpected stored object path');
      readAsset(m[1],m[2] === 'glb' ? 'mesh' : 'picture');
      objects++;
    }
    return { ok:true, revisions, objects };
  }
  function requireIds(p,m) {
    if (![p,m].every(x => typeof x === 'string' && ID.test(x))) throw new Error('Supply valid --project and --model IDs');
  }
  function remove(p,m) {
    requireIds(p,m);
    const state = pointer(p,m);
    if (state.current) {
      verifyRevision(p,m,state.current);
      writePointer(p,m,{ current:null, previous:[...new Set([state.current,...state.previous])] });
    }
    return { ok:true };
  }
  function rollback(p,m,r) {
    requireIds(p,m);
    const state = pointer(p,m);
    r ??= state.previous[0];
    if (!state.previous.includes(r)) throw new Error('Choose a retained earlier revision');
    const { published_at } = verifyRevision(p,m,r);
    writePointer(p,m,{ current:r, previous:[...new Set([state.current,...state.previous].filter(x=>x && x!==r))] });
    return { ok:true, revision:r, published_at };
  }
  function preview(p,m,v) {
    requireIds(p,m);
    const state = selected(p,m);
    if (!state) throw new Error('No verified current model');
    const view = v === undefined ? state.views[0] : state.views.find(x => x.id === v);
    if (!view) throw new Error('No current view with that ID');
    return { project_title:state.sheet.project.title, model_title:state.sheet.title,
      view_title:view.title, revision:state.revision, picture_path:pictureUrl(p,m,state.revision,v ?? view.id) };
  }
  return { publish, verify, remove, rollback, preview, catalog, media };
}

module.exports = { checkDrop, createLibrary, canonical, sha, ID, DIGEST, DEFAULT_LIMITS, DEFAULT_CAMERA };
