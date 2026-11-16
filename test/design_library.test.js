'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const L = require('../lib/design_library.cjs');
const P = require('../lib/design_prepare.cjs');
const fixture = require('./fixtures/design_library.cjs');
function scratch(t) { const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gallery-unit-')); t.after(() => fs.rmSync(root, { recursive: true, force: true })); return root; }

test('canonical hashing and path admission reject ambiguous filesystem names', () => {
  assert.equal(L.canonical({ b: 1, a: [2, 3] }), '{"a":[2,3],"b":1}');
  for (const p of ['../a', '/a', 'C:/a', 'a\\b', 'a//b', 'a/./b', 'a/../b', 'a\u0000b', 'a./b', 'a /b']) assert.throws(() => L.safePath(p), /invalid relative path/);
  assert.equal(L.safePath('views/My design.html'), 'views/My design.html');
});
test('drop verifies the exact bytes and rejects schema, duplicates, sizes and invented approval', t => {
  const root = scratch(t); const descriptor = fixture.drop(path.join(root, 'drop')); const actual = L.inventory(path.join(root, 'drop/payload'));
  assert.doesNotThrow(() => L.validateDrop(descriptor, actual));
  for (const mutate of [d => d.schema = 'other', d => d.files.push(d.files[0]), d => d.files[0].sha256 = '0'.repeat(64), d => d.files[0].size++, d => d.files[0].size = L.LIMITS.file + 1, d => d.views[0].approval_ref = true, d => d.views[0].entry = 'absent.html', d => d.missing_paths = ['absent.html'], d => d.files[0].path = 'run.exe']) {
    const copy = structuredClone(descriptor); mutate(copy); assert.throws(() => L.validateDrop(copy, actual));
  }
});
test('import preserves originals, produces stable input identity and refuses symlinks/case collisions', t => {
  const root = scratch(t), drop = path.join(root, 'drop'); fixture.drop(drop);
  const options = { drop, staging: path.join(root, 'staging') }, a = L.importDrop(options), b = L.importDrop(options);
  assert.equal(a.input_id, b.input_id); assert.deepEqual(L.inventory(path.join(a.dir, 'source')), L.inventory(path.join(drop, 'payload')));
  fs.symlinkSync('index.html', path.join(drop, 'payload/link.html')); assert.throws(() => L.importDrop(options), /symlink/); fs.unlinkSync(path.join(drop, 'payload/link.html'));
  // Descriptor case collisions are portable even on a case-insensitive host.
  const descriptor = L.readJson(path.join(drop, 'drop.json')); descriptor.files.push({ ...descriptor.files[0], path: 'INDEX.html' }); L.atomicJson(path.join(drop, 'drop.json'), descriptor);
  assert.throws(() => L.importDrop(options), /colliding/);
});
test('ZIP traversal and symlink entries fail before any payload is extracted', t => {
  const root = scratch(t);
  for (const [name, kind] of [['../escape.html', 'file'], ['index.html', 'link']]) {
    const zip = path.join(root, kind + '.zip'), dest = path.join(root, kind);
    execFileSync(process.env.PENTACLE_PYTHON || 'python3', ['-c', "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1],'w'); i=zipfile.ZipInfo(sys.argv[2]); i.external_attr=(0o120777<<16) if sys.argv[3]=='link' else 0; z.writestr(i,'target')", zip, name, kind]);
    assert.throws(() => L.extractZip(zip, dest)); assert.equal(fs.existsSync(dest), false);
  }
});
test('preparation inlines assets, compiles ordered JSX, installs CSP/storage and never changes source', async t => {
  const root = scratch(t), source = path.join(root, 'source'); fs.mkdirSync(source);
  fs.writeFileSync(path.join(source, 'picture.png'), fixture.PNG);
  const html = '<html><head><style>body{background-image:url(picture.png)}</style></head><body><img src="picture.png"><script>window.order=[1]</script><script type="text/babel">window.order.push(2);window.example=<div>example</div>;</script></body></html>';
  fs.writeFileSync(path.join(source, 'index.html'), html);
  const result = await P.prepareDocument({ sourceRoot: source, entry: 'index.html' });
  assert.match(result.html, /sandbox|Content-Security-Policy/); assert.match(result.html, /connect-src 'none'/); assert.match(result.html, /data:image\/png;base64/); assert.match(result.html, /React.createElement/); assert.ok(result.html.indexOf('order=[1]') < result.html.indexOf('order.push(2)'));
  assert.equal(fs.readFileSync(path.join(source, 'index.html'), 'utf8'), html);
});
test('unresolved assets, unmocked Claude calls and dynamic navigation fail preparation', async t => {
  const root = scratch(t); fs.mkdirSync(path.join(root, 'source'));
  for (const html of ['<img src="missing.png">', '<script>window.claude.complete({})</script>', '<script>window.open("https://example.invalid")</script>', '<script>location.href="https://example.invalid"</script>', '<script>fetch("https://example.invalid")</script>', '<script>import("module.js")</script>']) {
    fs.writeFileSync(path.join(root, 'source/index.html'), html); await assert.rejects(P.prepareDocument({ sourceRoot: path.join(root, 'source'), entry: 'index.html' }));
  }
});
test('pinned dependency bytes and adapters are required and hash-bound', async t => {
  const root = scratch(t), source = path.join(root, 'source'); fs.mkdirSync(source);
  fs.writeFileSync(path.join(source, 'index.html'), '<script src="https://example.invalid/library.js"></script>');
  const asset = path.join(root, 'library.js'); fs.writeFileSync(asset, 'window.fixture=1');
  const profile = { assets: { 'https://example.invalid/library.js': { path: asset, sha256: L.sha256(fs.readFileSync(asset)), version: '1', license: 'MIT' } } };
  assert.match((await P.prepareDocument({ sourceRoot: source, entry: 'index.html', profile })).html, /fixture=1/);
  fs.writeFileSync(asset, 'window.fixture=2'); await assert.rejects(P.prepareDocument({ sourceRoot: source, entry: 'index.html', profile }), /hash/);
});
test('publication, idempotent reuse and rollback preserve verified pointers', async t => {
  const root = scratch(t), library = path.join(root, 'library');
  const a = await fixture.revision(path.join(root, 'a')); const first = L.publishRevision({ dir: a.dir, library });
  assert.equal(first.current, true); assert.equal(L.publishRevision({ dir: a.dir, library }).reused, true);
  const b = await fixture.revision(path.join(root, 'b'), { html: fixture.HTML.replace('Fixture design', 'Edited design') }); L.publishRevision({ dir: b.dir, library });
  assert.notEqual(a.manifest.revision_id, b.manifest.revision_id);
  L.rollback({ library, project: 'example', revision: a.manifest.revision_id });
  assert.equal(L.createLibraryReader(library).selected('example').manifest.revision_id, a.manifest.revision_id);
});
test('partial transfers retain previous verified revision; incomplete drafts never replace current', async t => {
  const root = scratch(t), library = path.join(root, 'library'), a = await fixture.revision(path.join(root, 'a'));
  L.publishRevision({ dir: a.dir, library }); const reader = L.createLibraryReader(library); assert.equal(reader.selected('example').sync, 'verified');
  L.atomicJson(path.join(library, 'projects/example/current.json'), { schema: 'design-current.v1', project_id: 'example', revision_id: 'f'.repeat(64) });
  const pending = reader.selected('example'); assert.equal(pending.sync, 'pending'); assert.equal(pending.manifest.revision_id, a.manifest.revision_id);
  L.rollback({ library, project: 'example', revision: a.manifest.revision_id });
  const draft = await fixture.revision(path.join(root, 'draft'), { complete: false }); const result = L.publishRevision({ dir: draft.dir, library });
  assert.equal(result.current, false); assert.equal(reader.selected('example').manifest.revision_id, a.manifest.revision_id);
});
test('changed, missing or extra derived bytes and symlink substitutions refuse serving', async t => {
  const root = scratch(t), a = await fixture.revision(root), shot = path.join(a.dir, 'screenshots/home.png');
  fs.writeFileSync(shot, Buffer.from('corrupt')); assert.throws(() => L.verifyRevision(a.dir), /mismatch/);
  fs.writeFileSync(shot, fixture.PNG); assert.doesNotThrow(() => L.verifyRevision(a.dir));
  fs.writeFileSync(path.join(a.dir, 'unexpected.txt'), 'extra'); assert.throws(() => L.verifyRevision(a.dir), /unlisted/); fs.unlinkSync(path.join(a.dir, 'unexpected.txt'));
  fs.unlinkSync(shot); fs.symlinkSync(path.join(root, 'drop-example/payload/index.html'), shot); assert.throws(() => L.verifyRevision(a.dir), /symlink/);
});
test('configured real private drop validates without copying it into the repository', { skip: !process.env.PENTACLE_TEST_DESIGN_DROP }, t => {
  const root = scratch(t); const result = L.importDrop({ drop: process.env.PENTACLE_TEST_DESIGN_DROP, staging: root });
  assert.ok(result.manifest.views.length); assert.ok(result.manifest.provenance.files.length);
});
test('a source edit invalidates inherited approval evidence and cannot publish through a symlinked project parent', async t => {
  const root = scratch(t), library = path.join(root, 'library');
  const original = await fixture.revision(path.join(root, 'original'), { approvalRef: 'evidence:approved-original' }); L.publishRevision({ dir: original.dir, library });
  const edited = path.join(root, 'edited'); fixture.drop(edited, { html: fixture.HTML.replace('Fixture design', 'Edited design'), approvalRef: 'evidence:approved-original' });
  const candidate = L.importDrop({ drop: edited, staging: path.join(root, 'staging'), library });
  assert.equal(candidate.manifest.views[0].approval_ref, null); assert.equal(candidate.manifest.views[0].approval_stale_ref, 'evidence:approved-original'); assert.equal(candidate.manifest.approval_refs.length, 0);
  const unsafe = path.join(root, 'unsafe'), outside = path.join(root, 'outside'); fs.mkdirSync(unsafe); fs.mkdirSync(outside); fs.symlinkSync(outside, path.join(unsafe, 'projects'));
  assert.throws(() => L.publishRevision({ dir: original.dir, library: unsafe }), /unsafe publication/); assert.equal(fs.readdirSync(outside).length, 0);
});
test('nested transfer/conflict files do not hide canonical content and conflicts are visible', async t => {
  const root = scratch(t), library = path.join(root, 'library'), r = await fixture.revision(root);
  L.publishRevision({ dir: r.dir, library }); const reader = L.createLibraryReader(library); reader.selected('example');
  const source = path.join(library, 'projects/example/revisions', r.manifest.revision_id, 'source');
  fs.writeFileSync(path.join(source, '.syncthing.index.html.tmp'), 'partial');
  fs.writeFileSync(path.join(source, 'index.sync-conflict-20261108-123000-DEVICE.html'), 'conflicting copy');
  const catalog = reader.catalog(); assert.equal(catalog.projects[0].manifest?.revision_id, r.manifest.revision_id); assert.equal(catalog.projects[0].sync, 'conflict');
  fs.chmodSync(path.join(source, 'index.html'), 0o644); // owned corruption fixture only
  fs.writeFileSync(path.join(source, 'index.html'), 'canonical corruption'); assert.equal(reader.selected('example').manifest, null, 'canonical corruption must still refuse serving');
});
test('a declared missing image is indexed as an incomplete draft and never selects current', async t => {
  const root = scratch(t), library = path.join(root, 'library'), current = await fixture.revision(path.join(root, 'current')); L.publishRevision({ dir: current.dir, library });
  const source = path.join(root, 'missing'); const d = fixture.drop(source, { complete: false }); fs.unlinkSync(path.join(source, 'payload/index.html'));
  d.files = []; d.missing_paths = ['missing.png']; d.views[0].entry = 'missing.png'; L.atomicJson(path.join(source, 'drop.json'), d);
  const imported = L.importDrop({ drop: source, staging: path.join(root, 'staging') }); const prepared = await P.prepareRevision(imported.dir);
  assert.equal(prepared.views[0].compatibility.status, 'incomplete'); assert.match(prepared.views[0].compatibility.reason, /missing\.png/);
  L.finalize(imported.dir); assert.equal(L.publishRevision({ dir: imported.dir, library }).current, false); assert.equal(L.createLibraryReader(library).selected('example').manifest.revision_id, current.manifest.revision_id);
});
