'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const store = require('../../main/review_store');
const { install } = require('../../scripts/install-review-item');
const CLI = path.join(__dirname, '../../scripts/review-item.js');
const fixture = path.join(__dirname, '../fixtures/review_seed.json');
function run(dir, ...args) { return spawnSync(process.execPath, [CLI, '--dir', dir, ...args], { encoding: 'utf8' }); }
function tmp() { return fs.mkdtempSync(path.join(os.tmpdir(), 'review-cli-')); }

test('CLI validates kind/title/source/link, lists and resolves without editing acks', () => {
  const dir = tmp();
  assert.match(run(dir, '--help').stdout, /No status updates, progress notes, FYIs or acknowledgements/);
  for (const kind of ['status', 'fyi', 'progress']) assert.equal(run(dir, 'add', '--kind', kind, '--title', 'No').status, 2);
  assert.equal(run(dir, 'add', '--kind', 'answer', '--title', '<script>').status, 2);
  assert.equal(run(dir, 'add', '--kind', 'answer', '--title', 'Safe', '--link', 'javascript:alert(1)').status, 2);
  const added = run(dir, 'add', '--kind', 'answer', '--title', "A's answer: ($5), accepted", '--reply-to-message-id', 'optimistic_local_a', '--json');
  assert.equal(added.status, 0, added.stderr);
  const id = JSON.parse(added.stdout).item.id;
  assert.match(id, /^ri_[0-9a-f]{32}$/);
  const list = JSON.parse(run(dir, 'list', '--state', 'pending', '--json').stdout);
  assert.equal(list.items[0].title, "A's answer: ($5), accepted");
  assert.equal(list.items[0].source.reply_to_message_id, 'optimistic_local_a');
  assert.equal(run(dir, 'list', '--state', 'wrong').status, 2);
  const acksBefore = fs.existsSync(path.join(dir, 'acks.json')) ? fs.readFileSync(path.join(dir, 'acks.json')) : null;
  assert.equal(run(dir, 'resolve', id, '--reason', 'Answered in chat').status, 0);
  assert.equal(run(dir, 'resolve', id, '--reason', 'Answered in chat').status, 0);
  assert.equal(run(dir, 'resolve', id, '--reason', 'Different reason').status, 3);
  assert.equal(run(dir, 'resolve', 'ri_00000000000000000000000000000000', '--reason', 'Unknown').status, 3);
  assert.equal(run(dir, 'validate').status, 0);
  assert.equal(JSON.parse(run(dir, 'list', '--state', 'resolved', '--json').stdout).items.length, 1);
  assert.deepEqual(fs.existsSync(path.join(dir, 'acks.json')) ? fs.readFileSync(path.join(dir, 'acks.json')) : null, acksBefore);
});

test('seed fixture preserves all fourteen titles, one timestamp, provenance and strict rerun', () => {
  const dir = tmp(), seed = fs.readFileSync(fixture), parsed = JSON.parse(seed);
  assert.equal(crypto.createHash('sha256').update(seed).digest('hex'), '798bdd7ae5a2c9c25d1f6b7c20ebaf869a07a9c778c7ea068f0c3676cd6d1dcd');
  const first = run(dir, 'seed', fixture); assert.equal(first.status, 0, first.stderr);
  const before = fs.readFileSync(path.join(dir, 'items.json'));
  const items = JSON.parse(before).items;
  assert.equal(items.length, 14);
  assert.deepEqual(items.map(x => x.title), parsed.items.map(x => x.title));
  assert.equal(new Set(items.map(x => x.created_at)).size, 1);
  for (let i = 0; i < 14; i++) {
    assert.equal(items[i].first_raised_at, parsed.items[i].first_raised_at);
    if (parsed.items[i].source.startsWith('dispatch ')) assert.equal(items[i].source.dispatch_id, parsed.items[i].source.slice(9));
    else assert.deepEqual(items[i].source.reply_to_message_id ? { reply_to_message_id: items[i].source.reply_to_message_id } : items[i].source,
      parsed.items[i].reply_to_message_id ? { reply_to_message_id: parsed.items[i].reply_to_message_id } : {});
  }
  assert.match(run(dir, 'seed', fixture).stdout, /seed unchanged/);
  assert.deepEqual(fs.readFileSync(path.join(dir, 'items.json')), before);
  assert.equal(fs.existsSync(path.join(dir, 'acks.json')), false);
  const different = path.join(dir, 'different.json');
  fs.writeFileSync(different, JSON.stringify({ ...parsed, note: `${parsed.note} changed` }));
  assert.equal(run(dir, 'seed', different).status, 3);
  const subset = path.join(dir, 'subset.json');
  fs.writeFileSync(subset, JSON.stringify({ ...parsed, items: parsed.items.slice(1) }));
  assert.equal(run(dir, 'seed', subset).status, 3);
  const add = run(dir, 'add', '--kind', 'review', '--title', 'New review after seed');
  assert.equal(add.status, 0);
  assert.equal(run(dir, 'seed', fixture).status, 3);
});

test('smoke-only remove requires cleanup, and installed wrapper pins version and directory', () => {
  const root = tmp(), dir = path.join(root, 'inbox'), bin = path.join(root, 'bin');
  const candidate = 'a'.repeat(40);
  const { wrapper, versionDir } = install({ binDir: bin, inboxDir: dir, candidate });
  try {
    assert.equal(fs.statSync(versionDir).mode & 0o222, 0);
    const version = spawnSync(wrapper, ['--version'], { encoding: 'utf8' });
    assert.equal(version.status, 0, version.stderr || version.error?.message);
    assert.equal(version.stdout.trim(), candidate);
    assert.equal(spawnSync(wrapper, ['--dir', path.join(root, 'elsewhere'), 'list'], { encoding: 'utf8' }).status, 2);
    const titleWithOptionText = spawnSync(wrapper, ['add', '--kind', 'review', '--title', 'Review --dir guidance', '--json'], { encoding: 'utf8' });
    assert.equal(titleWithOptionText.status, 0, titleWithOptionText.stderr);
    assert.equal(JSON.parse(titleWithOptionText.stdout).item.title, 'Review --dir guidance');
    const runId = crypto.randomBytes(16).toString('hex');
    const added = spawnSync(wrapper, ['add', '--kind', 'review', '--title', `Review inbox smoke ${runId}`, '--smoke-run', runId, '--json'], { encoding: 'utf8' });
    assert.equal(added.status, 0, added.stderr);
    const id = JSON.parse(added.stdout).item.id;
    const ackId = crypto.randomUUID();
    store.ackItem(dir, { id, op: 'unack', op_id: ackId });
    assert.equal(spawnSync(wrapper, ['smoke-remove', id, '--smoke-run', runId], { encoding: 'utf8' }).status, 3);
    store.smokeCleanup(dir, { id, run_id: runId, op_id: crypto.randomUUID() });
    assert.equal(spawnSync(wrapper, ['smoke-remove', id, '--smoke-run', runId], { encoding: 'utf8' }).status, 0);
    assert.equal(run(dir, 'add', '--kind', 'answer', '--title', 'Normal item').status, 0);
    const normal = JSON.parse(run(dir, 'list', '--state', 'all', '--json').stdout).items[0].id;
    assert.equal(spawnSync(wrapper, ['smoke-remove', normal, '--smoke-run', runId], { encoding: 'utf8' }).status, 3);
  } finally {
    for (const sub of ['scripts', 'main']) fs.chmodSync(path.join(versionDir, sub), 0o700);
    fs.chmodSync(versionDir, 0o700);
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('CLI projection refuses FIFO item and ack files promptly', () => {
  for (const name of ['items.json', 'acks.json']) {
    const dir = tmp();
    if (name === 'acks.json') store.addItem(dir, { kind: 'review', title: 'Regular file control' });
    assert.equal(spawnSync('/usr/bin/mkfifo', [path.join(dir, name)]).status, 0);
    const result = spawnSync(process.execPath, [CLI, '--dir', dir, 'list'], { encoding: 'utf8', timeout: 900 });
    assert.equal(result.error?.code, undefined, `${name} blocked the CLI`);
    assert.equal(result.status, 4);
    assert.match(result.stderr, /^store_(invalid|unreadable)\n$/);
  }
});

test('CLI project flag, default list column and smoke restriction', () => {
  const dir = tmp();
  assert.match(run(dir, '--help').stdout, /Agenteach\|DUMBO\|Pentacle\|Personal\|Other/);
  for (const project of store.PROJECTS) {
    const result = run(dir, 'add', '--kind', 'review', '--title', `Review ${project}`, '--project', project);
    assert.equal(result.status, 0, result.stderr);
  }
  assert.equal(run(dir, 'add', '--kind', 'answer', '--title', 'No project').status, 0);
  const stored = store.readItems(dir).value.items;
  assert.equal(stored.at(-1).project, undefined);
  const human = run(dir, 'list', '--state', 'all');
  assert.equal(human.status, 0, human.stderr);
  assert.match(human.stdout, /PROJECT\s+STATE\s+KIND\s+TITLE/);
  assert.match(human.stdout, /Other\s+pending\s+answer\s+No project/);
  const listed = JSON.parse(run(dir, 'list', '--state', 'all', '--json').stdout).items;
  assert.deepEqual(new Set(listed.map(item => item.project)), new Set(store.PROJECTS));
  assert.equal(listed.find(item => item.title === 'No project').project, 'Other');
  const before = fs.readFileSync(path.join(dir, 'items.json'));
  for (const project of ['agenteach', 'Agenteach ', '', 'wrong']) {
    const bad = run(dir, 'add', '--kind', 'review', '--title', 'Bad project', '--project', project);
    assert.equal(bad.status, 2);
    for (const name of store.PROJECTS) assert.match(bad.stderr, new RegExp(name));
    assert.deepEqual(fs.readFileSync(path.join(dir, 'items.json')), before);
  }
  const runId = crypto.randomBytes(16).toString('hex');
  const smoke = run(dir, 'add', '--kind', 'review', '--title', `Review inbox smoke ${runId}`,
    '--smoke-run', runId, '--project', 'Other');
  assert.equal(smoke.status, 2);
  assert.deepEqual(fs.readFileSync(path.join(dir, 'items.json')), before);
});
