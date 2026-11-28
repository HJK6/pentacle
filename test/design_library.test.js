'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const L = require('../lib/design_library.cjs');
const P = require('../lib/design_prepare.cjs');
const { DESIGN_FRAME_NAME } = require('../lib/design_frame.cjs');
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

test('boards resolve placed and loose screens while preserving the author’s step order', t => {
  const root = scratch(t), descriptor = fixture.drop(path.join(root, 'drop'));
  const view = descriptor.views[0];
  descriptor.views.push(
    { ...view, id: 'prototype', title: 'Prototype', variant: 'prototype' },
    { ...view, id: 'detail', title: 'Details', viewport: { width: 390, height: 844 } }
  );
  descriptor.designs = [{ id: 'journey', title: 'Journey', nodes: [
    { type: 'screen', view: 'home', x: -200, y: 50, title: 'Beginning' },
    { type: 'section', title: 'Start', x: -300, y: 0, w: 1000, h: 720 },
    { type: 'screen', view: 'detail' },
    { type: 'arrow', from: 'home', to: 'detail', label: 'Continue' },
    { type: 'note', text: 'Choose an option.', x: 0, y: 740, w: 400 },
    { type: 'screen', view: 'prototype' },
  ] }];
  const actual = L.inventory(path.join(root, 'drop/payload'));
  L.validateDrop(descriptor, actual);
  const [design] = L.resolveDesigns(descriptor);
  assert.equal(design.id, 'journey');
  const screens = design.nodes.filter(node => node.type === 'screen');
  assert.deepEqual(screens.map(node => node.view), ['home', 'detail', 'prototype']);
  assert.deepEqual(screens.map(({ x, y, w, h, title }) => ({ x, y, w, h, title })), [
    { x: -200, y: 50, w: 800, h: 600, title: 'Beginning' },
    { x: 0, y: 2460, w: 390, h: 844, title: 'Details' },
    { x: 0, y: 1480, w: 800, h: 600, title: 'Prototype' },
  ]);
  assert.deepEqual(design.nodes.find(node => node.type === 'arrow'), descriptor.designs[0].nodes[3]);
  assert.equal(descriptor.designs[0].nodes[2].x, undefined);
});

test('derived boards have stable IDs across legacy names and mixed explicit boards', t => {
  const root = scratch(t), descriptor = fixture.drop(path.join(root, 'drop'));
  const view = descriptor.views[0];
  const groups = ['Example', 'a b', 'a-b', '!!!', 'x'.repeat(120), 'É'];
  descriptor.views.push(...groups.map((group, index) => ({ ...view, id: 'v-' + index, group })));
  descriptor.views.push(
    { ...view, id: 'flow', group: 'a b', variant: 'flow-02' },
    { ...view, id: 'proto', group: 'a b', variant: 'prototype' },
    { ...view, id: 'other-flow', group: 'a b', variant: 'flow-01' }
  );
  const actual = L.inventory(path.join(root, 'drop/payload'));
  const initial = L.resolveDesigns(descriptor);
  assert.equal(initial.length, groups.length + 1);
  assert.equal(new Set(initial.map(design => design.id)).size, initial.length);
  assert.ok(initial.every(design => L.ID.test(design.id)));
  assert.equal(initial.filter(design => design.title === 'Example').length, 2);
  const space = initial.find(design => design.title === 'a b');
  assert.equal(space.id, 'group-' + L.sha256('a b'));
  assert.deepEqual(space.nodes.map(node => node.view), ['proto', 'other-flow', 'flow', 'v-1']);
  descriptor.designs = [{ id: 'explicit', title: 'Explicit', nodes: [{ type: 'screen', view: 'v-0' }] }];
  L.validateDrop(descriptor, actual);
  const mixed = L.resolveDesigns(descriptor);
  assert.ok(mixed.some(design => design.id === 'explicit'));
  assert.equal(mixed.find(design => design.title === 'a b').id, space.id);
  assert.equal(mixed.filter(design => design.title === 'Example').length, 1);
  descriptor.designs[0].id = 'group-' + L.sha256('Example');
  assert.throws(() => L.validateDrop(descriptor, actual), /group-.*Example/);
});

test('invalid boards identify the design and node', t => {
  const root = scratch(t), descriptor = fixture.drop(path.join(root, 'drop'));
  descriptor.designs = [{ id: 'journey', title: 'Journey', nodes: [{ type: 'screen', view: 'home' }] }];
  const actual = L.inventory(path.join(root, 'drop/payload'));
  for (const [node, message] of [
    [{ type: 'video' }, /journey.*node 2.*type/],
    [{ type: 'screen', view: 'flow-only' }, /journey.*node 2.*unknown view/],
    [{ type: 'screen', view: 'home' }, /journey.*node 2.*repeated view/],
    [{ type: 'note', text: 'x'.repeat(401), x: 0, y: 0, w: 10 }, /journey.*node 2.*text/],
    [{ type: 'section', title: 'Part', x: 100001, y: 0, w: 10, h: 10 }, /journey.*node 2.*position/],
    [{ type: 'arrow', from: 'home', to: 'unknown' }, /journey.*node 2.*ends/],
  ]) {
    const copy = structuredClone(descriptor);
    copy.designs[0].nodes.push(node);
    assert.throws(() => L.validateDrop(copy, actual), message);
  }
  const first = descriptor.designs[0].nodes[0];
  first.x = 1;
  assert.throws(() => L.validateDrop(descriptor, actual), /journey.*node 1.*both/);
  delete first.x;
  descriptor.designs.push({ ...descriptor.designs[0], nodes: [] });
  assert.throws(() => L.validateDrop(descriptor, actual), /journey.*duplicate design id/);
});

test('phone-only loose frames occupy rows of five', () => {
  const descriptor = { project_id: 'example', title: 'Example', views: Array.from({ length: 6 }, (_, index) => ({
    id: 'v-' + index, title: 'Screen ' + index, viewport: { width: 390, height: 844 }, surface: 'mobile',
  })) };
  const [design] = L.resolveDesigns(descriptor);
  assert.deepEqual(design.nodes.map(({ x, y }) => [x, y]), [[0, 0], [650, 0], [1300, 0], [1950, 0], [2600, 0], [0, 1164]]);
});

test('board command checks overlapping and mixed layout without publishing', async t => {
  const root = scratch(t), drop = path.join(root, 'drop'), descriptor = fixture.drop(drop);
  descriptor.views.push(...['second', 'third'].map(id => ({ ...descriptor.views[0], id })));
  descriptor.designs = [{ id: 'journey', title: 'Journey', nodes: [
    { type: 'screen', view: 'home', x: 0, y: 0 },
    { type: 'screen', view: 'second', x: 1, y: 1 },
    { type: 'screen', view: 'third' },
  ] }];
  L.atomicJson(path.join(drop, 'drop.json'), descriptor);
  const { run } = require('../scripts/design-library.cjs');
  const result = await run(['board', '--drop', drop]);
  assert.ok(result.warnings.some(message => /home.*second.*overlap/.test(message)));
  assert.ok(result.warnings.some(message => /third.*unplaced/.test(message)));
  assert.equal(result.designs[0].id, 'journey');
  delete descriptor.designs;
  L.atomicJson(path.join(drop, 'drop.json'), descriptor);
  assert.equal((await run(['board', '--drop', drop])).warnings.length, 0);
});

test('the prepared keys shim only forwards navigation outside editable controls', async t => {
  const root = scratch(t);
  fs.writeFileSync(path.join(root, 'index.html'), '<button id="canvas">Screen</button><input id="editor"><div id="note" contenteditable><span id="inside">Note</span></div>');
  const result = await P.prepareDocument({ sourceRoot: root, entry: 'index.html' });
  const { JSDOM } = require('jsdom');
  const posted = [];
  const dom = new JSDOM(result.html, {
    runScripts: 'dangerously',
    beforeParse(window) {
      window.name = DESIGN_FRAME_NAME;
      window.parent = { postMessage: (...args) => posted.push(args) };
    },
  });
  t.after(() => dom.window.close());
  const { window } = dom;
  function key(id, value, options = {}) {
    const event = new window.KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true, ...options });
    window.document.querySelector('#' + id).dispatchEvent(event);
    return event.defaultPrevented;
  }
  assert.equal(key('canvas', 'ArrowRight'), true);
  assert.equal(key('canvas', 'Escape'), true);
  assert.equal(key('editor', 'ArrowLeft'), false);
  assert.equal(key('inside', 'ArrowDown'), false);
  assert.equal(key('canvas', 'A'), false);
  assert.equal(key('canvas', 'Escape', { isComposing: true }), false);
  assert.equal(key('canvas', 'ArrowLeft', { altKey: true }), false);
  assert.deepEqual(posted, [['ArrowRight', '*'], ['Escape', '*']]);
});

test('stored revisions recheck board structure and board-only edits change the input identity', async t => {
  const root = scratch(t), drop = path.join(root, 'drop');
  const descriptor = fixture.drop(drop);
  descriptor.designs = [{ id: 'example-board', title: 'Board', nodes: [{ type: 'screen', view: 'home' }] }];
  L.atomicJson(path.join(drop, 'drop.json'), descriptor);
  const options = { drop, staging: path.join(root, 'staging') };
  const first = L.importDrop(options);
  descriptor.designs[0].nodes[0].x = 100;
  descriptor.designs[0].nodes[0].y = 200;
  L.atomicJson(path.join(drop, 'drop.json'), descriptor);
  const second = L.importDrop(options);
  assert.notEqual(first.input_id, second.input_id);
  const recorded = await fixture.revision(path.join(root, 'recorded'), {
    configure: (source) => { source.designs = descriptor.designs; },
  });
  const file = path.join(recorded.dir, 'manifest.json');
  const invalid = L.readJson(file);
  invalid.provenance.designs[0].nodes.push({ type: 'arrow', from: 'home', to: 'unknown' });
  invalid.revision_id = L.revisionIdentity(invalid);
  L.atomicJson(file, invalid);
  assert.throws(() => L.verifyRevision(recorded.dir), /example-board.*node 2.*ends/);
});

test('an unnamed legacy gallery frame keeps ordinary key behavior', async t => {
  const root = scratch(t);
  fs.writeFileSync(path.join(root, 'index.html'), '<main>Scrollable prototype</main>');
  const { html } = await P.prepareDocument({ sourceRoot: root, entry: 'index.html' });
  const { JSDOM } = require('jsdom');
  const forwarded = [];
  const { window } = new JSDOM(html, {
    runScripts: 'dangerously',
    beforeParse(frame) { frame.parent = { postMessage: (...args) => forwarded.push(args) }; },
  });
  t.after(() => window.close());
  const key = new window.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true });
  window.document.querySelector('main').dispatchEvent(key);
  assert.equal(key.defaultPrevented, false);
  assert.deepEqual(forwarded, []);
});

test('Design send preview resolves the current verified board or a named screen', async t => {
  const root = scratch(t), library = path.join(root, 'library');
  const record = await fixture.revision(root, { configure(descriptor) {
    descriptor.views.push({ ...descriptor.views[0], id: 'phone', title: 'Phone' });
    descriptor.designs = [{
      id: 'orders', title: 'Orders',
      nodes: [{ type: 'screen', view: 'phone' }, { type: 'screen', view: 'home' }],
    }];
  } });
  L.publishRevision({ dir: record.dir, library });
  const resolve = (...flags) => require('../scripts/design-library.cjs').run(
    ['link-preview', '--library', library, '--project', 'example', '--design', ...flags]);
  const board = await resolve('orders');
  assert.deepEqual(board.dashboard_link.target, { project_id: 'example', design_id: 'orders' });
  assert.equal(board.dashboard_link.title, 'Orders');
  assert.equal(board.dashboard_link.revision_id, record.manifest.revision_id);
  assert.equal(path.basename(board.path), 'phone.png');
  assert.equal(board.sha256, L.sha256(fixture.PNG));
  const screen = await resolve('orders', '--view', 'home');
  assert.equal(screen.dashboard_link.target.view_id, 'home');
  assert.equal(screen.dashboard_link.title, 'Home');
  assert.equal(path.basename(screen.path), 'home.png');
  await assert.rejects(resolve('unknown'), /Design board was not found/);
  await assert.rejects(resolve('orders', '--view', 'unavailable'), /Design screen was not found/);
  fs.writeFileSync(screen.path, 'changed fixture');
  await assert.rejects(resolve('orders'), /No verified current revision/);
});

// Incremental publish: `publish` runs for real with the browser work faked, and
// PENTACLE_CHROME names nothing so an unexpected browser launch fails the test.
const R = require('../lib/design_record.cjs');
const { run } = require('../scripts/design-library.cjs');
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
function lab(t) {
  const root = scratch(t), chrome = process.env.PENTACLE_CHROME;
  process.env.PENTACLE_CHROME = '/nonexistent/chrome';
  t.after(() => { if (chrome === undefined) delete process.env.PENTACLE_CHROME; else process.env.PENTACLE_CHROME = chrome; });
  const x = { root, library: path.join(root, 'library'), drop: path.join(root, 'drop'), fake: fixture.browser(), n: 0 };
  // Each call is a new publish; the pause keeps timestamps and history order distinct.
  x.publishDrop = async (drop, ...flags) => { await tick(); return run(['publish', '--drop', drop, '--staging', path.join(root, 'staging'), '--library', x.library, '--out', path.join(root, 'evidence-' + (++x.n)), ...flags], x.fake.hooks); };
  x.publish = (...flags) => x.publishDrop(x.drop, ...flags);
  x.write = (name, text) => { fs.writeFileSync(path.join(x.drop, 'payload', name), text); return run(['reindex', '--drop', x.drop]); };
  x.edit = change => { const file = path.join(x.drop, 'drop.json'), descriptor = L.readJson(file); change(descriptor); L.atomicJson(file, descriptor); };
  x.cli = (command, ...flags) => run([command, '--library', x.library, '--project', 'example', ...flags]);
  x.reader = () => L.createLibraryReader(x.library);
  x.revision = id => x.reader().revision('example', id);
  x.revisions = () => fs.readdirSync(path.join(x.library, 'projects/example/revisions')).filter(name => L.HASH.test(name));
  x.receipts = () => L.readJson(path.join(root, 'evidence-' + x.n, 'recording-receipt.json')).receipts;
  x.dates = () => Object.fromEntries(x.reader().catalog().projects[0].designs.map(design => [design.id, design.changed_at]));
  return x;
}
const png = (x, id, view) => fs.readFileSync(path.join(x.revision(id).dir, 'screenshots', view + '.png'));
const threeBoards = views => ({ views, designs: [{ id: 'main', title: 'Main', nodes: Object.keys(views).map((view, index) => ({ type: 'screen', view, x: index * 900, y: 0 })) }] });

test('a publish that changes only titles or board layout opens no page and reuses every picture', async t => {
  const x = lab(t); fixture.pages(x.drop, threeBoards({ a: fixture.page('A'), b: fixture.page('B'), c: fixture.page('C') }));
  const first = await x.publish();
  assert.deepEqual(first.counts, { captured: 3, reused: 0 }); assert.deepEqual(x.fake.captured.toSorted(), ['a', 'b', 'c']);
  x.edit(d => { d.views[0].title = 'Renamed screen'; d.designs[0].title = 'Renamed board'; });
  const second = await x.publish();
  assert.deepEqual(second.counts, { captured: 0, reused: 3 }); assert.equal(x.fake.captured.length, 3, 'no page was opened');
  assert.deepEqual(x.receipts().map(r => r.status), ['reused', 'reused', 'reused']);
  assert.notEqual(second.revision_id, first.revision_id);
  for (const view of ['a', 'b', 'c']) assert.deepEqual(png(x, second.revision_id, view), png(x, first.revision_id, view));
  const recorded = x.revision(second.revision_id).manifest.views[0];
  assert.equal(recorded.generated.reused_from, first.revision_id); assert.match(recorded.generated.capture_key, /^[a-f0-9]{64}$/);
  assert.equal(recorded.compatibility.status, 'offline-interactive'); assert.match(recorded.compatibility.reason, /capture passed/);
  x.edit(d => { d.designs[0].nodes[1].x = 1200; });
  const third = await x.publish();
  assert.deepEqual(third.counts, { captured: 0, reused: 3 }); assert.equal(x.revision(third.revision_id).manifest.views[1].generated.reused_from, second.revision_id, 'the newest picture is reused');
  assert.equal(x.fake.captured.length, 3);
});

test('a changed screen is captured alone, and a changed shared script captures every screen using it', async t => {
  const x = lab(t), shared = '<script src="shared.js"></script>';
  fixture.pages(x.drop, { views: { a: fixture.page('A') + shared, b: fixture.page('B') + shared, c: fixture.page('C') + shared, d: fixture.page('D') }, files: { 'shared.js': 'window.shared=1' } });
  assert.deepEqual((await x.publish()).counts, { captured: 4, reused: 0 });
  await x.write('d.html', fixture.page('D edited'));
  assert.deepEqual((await x.publish()).counts, { captured: 1, reused: 3 }); assert.deepEqual(x.fake.captured.slice(4), ['d']);
  await x.write('shared.js', 'window.shared=2');
  assert.deepEqual((await x.publish()).counts, { captured: 3, reused: 1 }); assert.deepEqual(x.fake.captured.slice(5).toSorted(), ['a', 'b', 'c']);
});

test('a changed viewport, ready selector, browser or launch hash forces a capture', async t => {
  const x = lab(t); fixture.pages(x.drop, { views: { a: fixture.page('A'), b: fixture.page('B') } });
  await x.publish();
  x.edit(d => { d.views[0].viewport = { width: 810, height: 600 }; });
  assert.deepEqual((await x.publish()).counts, { captured: 1, reused: 1 }); assert.equal(x.fake.captured.at(-1), 'a');
  x.edit(d => { d.views[1].ready_selector = 'h1'; });
  assert.deepEqual((await x.publish()).counts, { captured: 1, reused: 1 }); assert.equal(x.fake.captured.at(-1), 'b');
  for (const field of ['browser_sha256', 'launch_sha256']) {
    x.fake.profile[field] = field[0].repeat(64);
    assert.deepEqual((await x.publish()).counts, { captured: 2, reused: 0 }, field);
  }
  const profile = { recorder_sha256: '1'.repeat(64), playwright: '1.0.0', capture: { browser_sha256: '2'.repeat(64), launch_sha256: '3'.repeat(64) } };
  const view = { viewport: { width: 800, height: 600 } }, key = L.captureKey(profile, view, '4'.repeat(64));
  for (const changed of [{ ...profile, recorder_sha256: '5'.repeat(64) }, { ...profile, playwright: '1.0.1' }, { ...profile, capture: { ...profile.capture, browser_sha256: '6'.repeat(64) } }, { ...profile, capture: { ...profile.capture, launch_sha256: '7'.repeat(64) } }]) assert.notEqual(L.captureKey(changed, view, '4'.repeat(64)), key);
  assert.notEqual(L.captureKey(profile, view, '8'.repeat(64)), key);
  assert.notEqual(L.captureKey(profile, { ...view, ready_selector: 'main' }, '4'.repeat(64)), key);
  assert.equal(L.captureKey(profile, { ...view, ready_selector: 'body' }, '4'.repeat(64)), key);
});

test('a tampered capture key fails verification and a picture that no longer matches its manifest is not reused', async t => {
  const x = lab(t); fixture.pages(x.drop, { views: { a: fixture.page('A') } });
  const { revision_id } = await x.publish(), prior = x.revision(revision_id);
  const copy = path.join(x.root, 'copy'); fs.cpSync(prior.dir, copy, { recursive: true });
  assert.doesNotThrow(() => L.verifyRevision(copy));
  const manifest = L.readJson(path.join(copy, 'manifest.json'));
  manifest.views[0].generated.capture_key = 'f'.repeat(64); manifest.revision_id = L.revisionIdentity(manifest); manifest.views[0].generated.revision_id = manifest.revision_id;
  L.atomicJson(path.join(copy, 'manifest.json'), manifest);
  assert.throws(() => L.verifyRevision(copy), /stale capture key/);
  const record = async retained => {
    const imported = L.importDrop({ drop: x.drop, staging: path.join(x.root, 'again') }), fake = fixture.browser();
    await P.prepareRevision(imported.dir); await R.recordRevision(imported.dir, { retained, capture: fake.hooks.capture, profile: fake.profile });
    return fake.captured;
  };
  assert.deepEqual(await record([prior]), [], 'the intact picture is reused');
  const shot = path.join(prior.dir, 'screenshots/a.png'); fs.chmodSync(shot, 0o644); fs.writeFileSync(shot, 'substituted');
  assert.deepEqual(await record([prior]), ['a'], 'a substituted picture is captured again');
});

test('a predecessor without a capture key is never reused from and still verifies', async t => {
  const x = lab(t), legacy = await fixture.revision(path.join(x.root, 'legacy'));
  L.publishRevision({ dir: legacy.dir, library: x.library }); fixture.drop(x.drop);
  const result = await x.publish();
  assert.deepEqual(result.counts, { captured: 1, reused: 0 }); assert.notEqual(result.revision_id, legacy.manifest.revision_id);
  assert.doesNotThrow(() => x.revision(legacy.manifest.revision_id)); assert.equal(x.revision(legacy.manifest.revision_id).manifest.design_changes, undefined);
  assert.deepEqual((await x.publish('--keep', 'all')).reused, true);
});

test('the compile cache never changes prepared bytes and the script policy still applies on every use', async t => {
  const root = scratch(t), source = path.join(root, 'source'); fs.mkdirSync(source);
  const asset = path.join(root, 'library.js'), net = 'window.net=fetch("https://example.invalid/x")';
  fs.writeFileSync(asset, net); fs.writeFileSync(path.join(source, 'app.js'), 'window.app=1'); fs.writeFileSync(path.join(source, 'local.js'), net);
  fs.writeFileSync(path.join(source, 'index.html'), '<script src="https://example.invalid/library.js"></script><script src="app.js"></script><script type="text/babel">window.inline=<b>x</b></script>');
  fs.writeFileSync(path.join(source, 'untrusted.html'), '<script src="local.js"></script>');
  const profile = { assets: { 'https://example.invalid/library.js': { path: asset, sha256: L.sha256(fs.readFileSync(asset)), version: '1', license: 'MIT', content_type: 'application/javascript', trusted_script: true } }, adapters: [{ path: 'app.js', sha256: L.sha256('window.app=1'), replacement: 'window.adapted=1', reason: 'fixture', review_ref: 'review:fixture', mocked: false }] };
  const plain = await P.prepareDocument({ sourceRoot: source, entry: 'index.html', profile }), cache = new Map();
  const cached = await P.prepareDocument({ sourceRoot: source, entry: 'index.html', profile, compileCache: cache });
  const again = await P.prepareDocument({ sourceRoot: source, entry: 'index.html', profile, compileCache: cache });
  assert.equal(cached.html, plain.html); assert.equal(again.html, plain.html); assert.match(plain.html, /adapted=1/); assert.equal(cache.size, 3);
  // The cached trusted bundle's text is also an untrusted local file: still refused.
  await assert.rejects(P.prepareDocument({ sourceRoot: source, entry: 'untrusted.html', compileCache: cache }), /unsupported network/);
});

test('export, reindex and publish with dependencies reproduce an unedited project', async t => {
  const x = lab(t), asset = path.join(x.root, 'library.js'), url = 'https://example.invalid/library.js';
  fs.writeFileSync(asset, 'window.fixture=1');
  const dependencies = path.join(x.root, 'dependencies.json');
  L.atomicJson(dependencies, { assets: { [url]: { path: asset, sha256: L.sha256('window.fixture=1'), version: '1', license: 'MIT', content_type: 'application/javascript' } }, adapters: [{ path: 'app.js', sha256: L.sha256('window.app=1'), replacement: 'window.adapted=1', reason: 'fixture', review_ref: 'review:fixture', mocked: true }] });
  fixture.pages(x.drop, { views: { a: `<script src="${url}"></script><script src="app.js"></script>` }, files: { 'app.js': 'window.app=1' } });
  const first = await x.publish('--dependencies', dependencies), selected = x.revision(first.revision_id);
  const out = path.join(x.root, 'exported'), exported = await x.cli('export', '--out', out);
  assert.equal(exported.revision_id, first.revision_id); assert.deepEqual([exported.assets, exported.adapters], [1, 1]);
  assert.deepEqual(L.readJson(path.join(out, 'drop.json')), selected.manifest.provenance);
  assert.deepEqual(L.readJson(path.join(out, 'dependencies.json')), selected.manifest.profile.dependencies);
  assert.deepEqual(L.inventory(path.join(out, 'payload')), L.inventory(path.join(selected.dir, 'source')));
  assert.ok(fs.statSync(path.join(out, 'payload/a.html')).mode & 0o200, 'the editing copy is writable');
  await assert.rejects(x.cli('export', '--out', out), /already exists/);
  assert.deepEqual(await run(['reindex', '--drop', out]), { drop: out, files: 2 });
  const again = await x.publishDrop(out, '--dependencies', path.join(out, 'dependencies.json'));
  assert.deepEqual([again.reused, again.revision_id], [true, first.revision_id], 'the same input_id selects the existing revision');
  fs.rmSync(asset);
  await assert.rejects(x.cli('export', '--out', path.join(x.root, 'second')), /example\.invalid\/library\.js.*missing/);
  assert.equal(fs.existsSync(path.join(x.root, 'second')), false); assert.equal(fs.readdirSync(x.root).some(name => name.startsWith('second.tmp')), false);
  fs.writeFileSync(asset, 'window.fixture=2');
  await assert.rejects(x.cli('export', '--out', path.join(x.root, 'third')), /example\.invalid\/library\.js.*no longer matches/);
});

test('reindex rewrites only the file list from payload', async t => {
  const x = lab(t); fixture.pages(x.drop, { views: { a: fixture.page('A') } });
  const before = L.readJson(path.join(x.drop, 'drop.json'));
  await x.write('a.html', fixture.page('Edited'));
  const after = L.readJson(path.join(x.drop, 'drop.json'));
  assert.deepEqual(after.files, L.inventory(path.join(x.drop, 'payload'))); assert.notEqual(after.files[0].sha256, before.files[0].sha256);
  assert.deepEqual({ ...after, files: null }, { ...before, files: null });
});

test('board dates come from the selected revision and survive pruning, rollback and legacy ancestors', async t => {
  const x = lab(t);
  fixture.pages(x.drop, { views: { a: fixture.page('A'), b: fixture.page('B') }, designs: [{ id: 'one', title: 'One', nodes: [{ type: 'screen', view: 'a' }] }, { id: 'two', title: 'Two', nodes: [{ type: 'screen', view: 'b' }] }] });
  const first = await x.publish(), m1 = x.revision(first.revision_id).manifest;
  assert.deepEqual(Object.keys(m1.design_changes).sort(), ['one', 'two']); assert.equal(m1.design_changes.one.changed_at, m1.imported_at); assert.match(m1.design_changes.one.fingerprint, /^[a-f0-9]{64}$/);
  await x.write('b.html', fixture.page('B2'));
  const second = await x.publish(), m2 = x.revision(second.revision_id).manifest;
  assert.equal(m2.design_changes.one.changed_at, m1.imported_at); assert.equal(m2.design_changes.two.changed_at, m2.imported_at);
  await x.write('b.html', fixture.page('B3'));
  const third = await x.publish(), m3 = x.revision(third.revision_id).manifest;
  assert.deepEqual(x.dates(), { one: m1.imported_at, two: m3.imported_at });
  assert.equal((await x.cli('prune', '--keep', '2', '--apply', 'yes')).applied, true);
  assert.deepEqual(x.revisions().toSorted(), [second.revision_id, third.revision_id].toSorted());
  assert.deepEqual(x.dates(), { one: m1.imported_at, two: m3.imported_at }, 'dates do not move when the first revision is pruned');
  await x.cli('rollback', '--revision', second.revision_id);
  assert.deepEqual(x.dates(), { one: m1.imported_at, two: m2.imported_at }, 'a rollback shows the selected revision’s dates');
});

test('plain finalize writes no dates, a legacy selection yields derived dates, and a publish over it records them', async t => {
  const x = lab(t), early = '2026-01-01T00:00:00.000Z', late = '2026-02-01T00:00:00.000Z';
  const a = await fixture.revision(path.join(x.root, 'a'), { importedAt: early });
  assert.equal(a.manifest.design_changes, undefined);
  L.publishRevision({ dir: a.dir, library: x.library }); await tick();
  const b = await fixture.revision(path.join(x.root, 'b'), { importedAt: late, configure: d => { d.source_revision = 'fixture-v2'; } });
  L.publishRevision({ dir: b.dir, library: x.library });
  assert.deepEqual(Object.values(x.dates()), [early], 'the legacy derivation is unchanged');
  await assert.rejects(x.cli('prune'), /board change dates/);
  fixture.drop(x.drop);
  const n1 = await x.publish(), recorded = x.revision(n1.revision_id).manifest;
  assert.deepEqual(Object.values(recorded.design_changes).map(change => change.changed_at), [early], 'the legacy dates are recorded');
  await x.write('index.html', fixture.HTML.replace('Fixture design', 'Edited design'));
  const n2 = await x.publish();
  assert.deepEqual(n2.pruned.removed, [a.manifest.revision_id]);
  assert.doesNotThrow(() => x.revision(b.manifest.revision_id));
  assert.equal((await x.cli('rollback', '--revision', b.manifest.revision_id)).current, true);
  assert.equal((await x.cli('verify')).revision_id, b.manifest.revision_id);
  assert.deepEqual(Object.values(x.dates()), [late], 'a legacy rollback target derives its date from the ancestors still retained');
});

async function history(x, names) {
  const ids = {};
  for (const name of names) { await x.write('a.html', fixture.page(name)); ids[name] = (await x.publish('--keep', 'all')).revision_id; }
  return ids;
}

test('prune keeps the last N intact selections, and its dry run changes nothing', async t => {
  const x = lab(t); fixture.pages(x.drop, { views: { a: fixture.page('seed') } });
  const ids = await history(x, ['A', 'B', 'C']);
  await x.cli('rollback', '--revision', ids.A); Object.assign(ids, await history(x, ['D']));
  const listing = () => fs.readdirSync(path.join(x.library, 'projects/example/revisions')).sort(), pointers = () => fs.readdirSync(path.join(x.library, 'projects/example/history')).sort();
  const before = [listing(), pointers()], dry = await x.cli('prune');
  assert.deepEqual(dry.keep, [ids.D, ids.A, ids.C]); assert.deepEqual(dry.remove.map(r => r.revision_id), [ids.B]); assert.ok(dry.bytes > 0); assert.equal(dry.applied, false);
  assert.deepEqual([listing(), pointers()], before); assert.equal(fs.existsSync(path.join(x.root, '.design-library-pruned')), false);
  const applied = await x.cli('prune', '--apply', 'yes');
  assert.equal(applied.applied, true); assert.deepEqual(listing(), [ids.A, ids.C, ids.D].sort()); assert.deepEqual(pointers(), before[1], 'history pointers stay');
  assert.deepEqual(fs.readdirSync(path.join(x.root, '.design-library-pruned')), [], 'removed folders are not left in the holding folder');
  await assert.rejects(x.cli('rollback', '--revision', ids.B), /pruned/);
  assert.equal((await x.cli('rollback', '--revision', ids.C)).current, true);
  assert.equal((await x.cli('verify')).verified, true);
  await assert.rejects(x.cli('prune', '--keep', '1'), /at least 2/); await assert.rejects(x.cli('prune', '--keep', 'all'), /at least 2/); await assert.rejects(x.cli('prune', '--apply', 'now'), /only yes/);
});

test('prune skips revisions that do not verify, and leaves drafts and never-selected revisions alone', async t => {
  const x = lab(t); fixture.pages(x.drop, { views: { a: fixture.page('seed') } });
  const ids = await history(x, ['A', 'B', 'C', 'D']);
  const shot = path.join(x.revision(ids.C).dir, 'screenshots/a.png'); const picture = fs.readFileSync(shot); fs.chmodSync(shot, 0o644); fs.writeFileSync(shot, 'damaged');
  const unverified = await x.cli('prune', '--keep', '2');
  assert.deepEqual(unverified.keep, [ids.D, ids.B], 'a revision that does not verify does not count toward N'); assert.deepEqual(unverified.remove.map(r => r.revision_id), [ids.A]);
  fs.writeFileSync(shot, picture);
  const draft = await fixture.revision(path.join(x.root, 'draft'), { complete: false }); L.publishRevision({ dir: draft.dir, library: x.library });
  const extra = await fixture.revision(path.join(x.root, 'extra'), { configure: d => { d.source_revision = 'never-selected'; } });
  fs.cpSync(extra.dir, path.join(x.library, 'projects/example/revisions', extra.manifest.revision_id), { recursive: true });
  const applied = await x.cli('prune', '--keep', '2', '--apply', 'yes');
  assert.deepEqual(applied.remove.map(r => r.revision_id).sort(), [ids.A, ids.B].sort());
  assert.deepEqual(x.revisions().sort(), [ids.C, ids.D, draft.manifest.revision_id, extra.manifest.revision_id].sort());
});

test('prune refuses an unverified selection, a sync conflict and a legacy selection', async t => {
  const x = lab(t); fixture.pages(x.drop, { views: { a: fixture.page('seed') } });
  await history(x, ['A', 'B']);
  const conflict = path.join(x.library, 'projects/example/current.sync-conflict-20261108-123000-ABCDEFG.json');
  fs.writeFileSync(conflict, '{}'); await assert.rejects(x.cli('prune'), /sync conflict/); fs.unlinkSync(conflict);
  const selected = x.reader().selected('example'), shot = path.join(selected.dir, 'screenshots/a.png');
  fs.chmodSync(shot, 0o644); fs.writeFileSync(shot, 'damaged'); await assert.rejects(x.cli('prune'), /not verified/);
});

test('publish prunes by default only after selecting a new revision, and --keep controls it', async t => {
  const x = lab(t); fixture.pages(x.drop, { views: { a: fixture.page('seed') } });
  await assert.rejects(x.publish('--keep', '1'), /at least 2/); assert.equal(fs.existsSync(x.library), false, 'nothing was published');
  const ids = [];
  for (const name of ['A', 'B', 'C']) { await x.write('a.html', fixture.page(name)); ids.push(await x.publish()); }
  assert.deepEqual(ids.map(r => r.pruned), [{ removed: [], bytes: 0 }, { removed: [], bytes: 0 }, { removed: [], bytes: 0 }]);
  await x.write('a.html', fixture.page('D')); const d = await x.publish();
  assert.deepEqual(d.pruned.removed, [ids[0].revision_id]); assert.ok(d.pruned.bytes > 0); assert.equal(x.revisions().length, 3);
  await x.write('a.html', fixture.page('E')); const e = await x.publish('--keep', '2');
  assert.equal(x.revisions().length, 2); assert.equal(e.pruned.removed.length, 2);
  await x.write('a.html', fixture.page('F')); const f = await x.publish('--keep', 'all');
  assert.equal(x.revisions().length, 3); assert.equal(f.pruned, undefined);
  await x.write('a.html', fixture.page('G')); await x.publish('--keep', 'all');
  const reused = await x.publish();
  assert.equal(reused.reused, true); assert.equal(reused.pruned, undefined); assert.equal(x.revisions().length, 4, 'a reused publish does not prune');
  const draft = path.join(x.root, 'incomplete'); fixture.pages(draft, { id: 'example', views: { a: fixture.page('Incomplete') } });
  const descriptor = L.readJson(path.join(draft, 'drop.json')); descriptor.complete = false; descriptor.missing_paths = ['gone.png']; L.atomicJson(path.join(draft, 'drop.json'), descriptor);
  const incomplete = await x.publishDrop(draft);
  assert.equal(incomplete.incomplete, true); assert.equal(incomplete.pruned, undefined); assert.equal(x.revisions().length, 5);
});

test('plain finalize drops dates inherited from a copied revision', async t => {
  const x = lab(t); fixture.pages(x.drop, { views: { a: fixture.page('A') } });
  const { revision_id } = await x.publish(), copy = path.join(x.root, 'derivative'); fs.cpSync(x.revision(revision_id).dir, copy, { recursive: true });
  const manifest = L.readJson(path.join(copy, 'manifest.json')); assert.ok(manifest.design_changes); delete manifest.revision_id; delete manifest.files; L.atomicJson(path.join(copy, 'manifest.json'), manifest);
  assert.equal(L.finalize(copy).design_changes, undefined);
});
