'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const store = require('../../main/review_store');
const dir = () => fs.mkdtempSync(path.join(os.tmpdir(), 'review-store-'));
const op = () => crypto.randomUUID();
const add = d => store.addItem(d, { kind: 'answer', title: 'An answer for review' }).item.id;
const ack = (d, id, kind, op_id = op()) => store.ackItem(d, { id, op: kind, op_id });

test('accepted no-op receipts replay original results after opposite transitions in both orders', () => {
  const d = dir(), id = add(d);
  const first = ack(d, id, 'ack');
  const noOpId = op(), noOp = ack(d, id, 'ack', noOpId);
  assert.equal(noOp.result.changed, false);
  const opposite = ack(d, id, 'unack');
  const before = fs.readFileSync(path.join(d, 'acks.json'));
  const replay = ack(d, id, 'ack', noOpId);
  assert.deepEqual(replay.result, noOp.result);
  assert.equal(replay.current.state, 'pending');
  assert.equal(replay.current.seq, opposite.current.seq);
  assert.deepEqual(fs.readFileSync(path.join(d, 'acks.json')), before);
  const secondNoOpId = op(), secondNoOp = ack(d, id, 'unack', secondNoOpId);
  const again = ack(d, id, 'ack');
  const replay2 = ack(d, id, 'unack', secondNoOpId);
  assert.deepEqual(replay2.result, secondNoOp.result);
  assert.equal(replay2.current.state, 'done');
  assert.equal(replay2.current.seq, again.current.seq);
  assert.equal(store.readAcks(d).value.entries[id].history.length, 3);
  assert.notEqual(again.current.acked_at, null);
  assert.equal(first.result.seq_seen, 1);
  assert.throws(() => ack(d, id, 'unack', noOpId), e => e.code === 'op_id_conflict');
  const another = add(d);
  assert.throws(() => ack(d, another, 'ack', noOpId), e => e.code === 'op_id_conflict');
});

test('resolve ordering preserves acknowledgement audit and refuses new ack', () => {
  const d = dir(), id = add(d);
  const accepted = ack(d, id, 'ack');
  const noOpId = op(), noOp = ack(d, id, 'ack', noOpId);
  const acksBefore = fs.readFileSync(path.join(d, 'acks.json'));
  store.resolveItem(d, id, 'Answered in chat');
  assert.deepEqual(fs.readFileSync(path.join(d, 'acks.json')), acksBefore);
  const feed = store.projection(d);
  assert.equal(feed.items.find(x => x.id === id).state, 'resolved');
  assert.equal(feed.items.find(x => x.id === id).acked_at, accepted.result.acked_at);
  assert.throws(() => ack(d, id, 'ack'), e => e.code === 'resolved');
  const replay = ack(d, id, 'ack', noOpId);
  assert.deepEqual(replay.result, noOp.result);
  assert.equal(replay.current.state, 'resolved');
  const second = add(d);
  store.resolveItem(d, second, 'Closed by Lews');
  assert.throws(() => ack(d, second, 'ack'), e => e.code === 'resolved');
});

test('invalid stores, symlinks, UTF-8 and lock timeout fail closed', () => {
  const d = dir(), id = add(d);
  fs.writeFileSync(path.join(d, 'acks.json'), Buffer.from([0xff]));
  assert.equal(store.projection(d).state, 'invalid');
  assert.throws(() => ack(d, id, 'ack'), e => e.code === 'store_invalid');
  fs.unlinkSync(path.join(d, 'acks.json'));
  fs.symlinkSync(path.join(d, 'items.json'), path.join(d, 'acks.json'));
  assert.equal(store.projection(d).state, 'invalid');
  fs.unlinkSync(path.join(d, 'acks.json'));
  fs.mkdirSync(path.join(d, '.txn.lock'));
  assert.throws(() => ack(d, id, 'ack'), e => e.code === 'lock_timeout');
  fs.rmdirSync(path.join(d, '.txn.lock'));
});

test('smoke remove refuses an entry or a receipt until cleanup; late no-op is safe', () => {
  const d = dir(), run = crypto.randomBytes(16).toString('hex');
  const id = store.addItem(d, { kind: 'review', title: `Review inbox smoke ${run}`, smoke_run: run }).item.id;
  ack(d, id, 'ack');
  assert.throws(() => store.smokeRemove(d, id, run), e => e.code === 'smoke_ack_exists');
  store.smokeCleanup(d, { id, run_id: run, op_id: op() });
  const lateNoOp = ack(d, id, 'unack');
  assert.equal(lateNoOp.result.changed, false);
  assert.throws(() => store.smokeRemove(d, id, run), e => e.code === 'smoke_ack_exists');
  store.smokeCleanup(d, { id, run_id: run, op_id: op() });
  store.smokeRemove(d, id, run);
  assert.equal(store.readItems(d).value.items.some(x => x.id === id), false);
  assert.equal(store.readAcks(d).value.receipts.some(x => x.id === id), false);
  assert.throws(() => store.smokeCleanup(d, { id, run_id: run, op_id: op() }), e => e.code === 'smoke_mismatch');
});

test('atomic backup contains the prior valid file and capacity refuses without mutation', () => {
  const d = dir(), id = add(d);
  const original = fs.readFileSync(path.join(d, 'items.json'));
  add(d);
  assert.deepEqual(fs.readFileSync(path.join(d, 'items.json.prev')), original);
  const first = ack(d, id, 'ack');
  const before = fs.readFileSync(path.join(d, 'acks.json'));
  assert.equal(first.result.changed, true);
  const acks = store.readAcks(d).value;
  acks.receipts = Array.from({ length: 20000 }, (_, n) => ({
    op_id: `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`,
    id, op: 'ack', accepted_at: new Date().toISOString(),
    result: { changed: false, seq_seen: 1, acked: true, acked_at: acks.entries[id].acked_at, state: 'done' },
  }));
  fs.writeFileSync(path.join(d, 'acks.json'), JSON.stringify(acks));
  const full = fs.readFileSync(path.join(d, 'acks.json'));
  assert.throws(() => ack(d, id, 'ack'), e => e.code === 'capacity');
  assert.deepEqual(fs.readFileSync(path.join(d, 'acks.json')), full);
  assert.notDeepEqual(before, full);
});

test('80 percent warning precedes eviction and only expired receipts are removed', () => {
  const d = dir(), id = add(d);
  ack(d, id, 'ack');
  const acks = store.readAcks(d).value;
  const recent = new Date().toISOString();
  acks.receipts = Array.from({ length: 15999 }, (_, n) => ({
    op_id: `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`,
    id, op: 'ack', accepted_at: recent,
    result: { changed: false, seq_seen: 1, acked: true, acked_at: acks.entries[id].acked_at, state: 'done' },
  }));
  fs.writeFileSync(path.join(d, 'acks.json'), JSON.stringify(acks));
  const firstWarning = ack(d, id, 'ack');
  assert.equal(firstWarning.warning, 'Inbox nearing capacity');
  assert.equal(store.readAcks(d).value.receipts.length, 16000);
  const after = store.readAcks(d).value;
  const expiredId = after.receipts[0].op_id;
  after.receipts[0].accepted_at = new Date(Date.now() - 25 * 3600 * 1000).toISOString();
  fs.writeFileSync(path.join(d, 'acks.json'), JSON.stringify(after));
  const next = ack(d, id, 'ack');
  assert.equal(next.warning, 'Inbox nearing capacity');
  const final = store.readAcks(d).value;
  assert.equal(final.receipts.some(x => x.op_id === expiredId), false);
  assert.equal(final.receipts.length, 16000);
  assert.equal(final.entries[id].history.length, 1);
});

test('projects are optional, strictly validated, and normalized only in the read model', () => {
  const d = dir();
  for (const project of store.PROJECTS) {
    const id = store.addItem(d, { kind: 'review', title: `Project ${project}`, project }).item.id;
    assert.equal(store.projection(d).items.find(item => item.id === id).project, project);
  }
  const missing = add(d);
  assert.equal(store.readItems(d).value.items.find(item => item.id === missing).project, undefined);
  assert.equal(store.projection(d).items.find(item => item.id === missing).project, 'Other');
  const before = fs.readFileSync(path.join(d, 'items.json'));
  for (const project of ['agenteach', 'Agenteach ', '', null, 42]) {
    assert.throws(() => store.addItem(d, { kind: 'review', title: 'Bad project', project }), e => e.code === 'invalid_project');
    assert.deepEqual(fs.readFileSync(path.join(d, 'items.json')), before);
  }
  const broken = store.readItems(d).value;
  broken.items[0].project = 'agenteach';
  fs.writeFileSync(path.join(d, 'items.json'), JSON.stringify(broken));
  assert.equal(store.projection(d).state, 'invalid');
});

test('seed project content is compared with unchanged bytes, marker, ids and keys', () => {
  const original = JSON.parse(fs.readFileSync(path.join(__dirname, '../fixtures/review_seed.json')));
  const d = dir(), plainDir = dir();
  const baseBytes = Buffer.from(JSON.stringify(original));
  store.seedItems(plainDir, baseBytes);
  const base = store.readItems(plainDir).value;
  const withProject = structuredClone(original);
  withProject.items[0].project = 'Agenteach';
  const bytes = Buffer.from(JSON.stringify(withProject));
  store.seedItems(d, bytes);
  const first = fs.readFileSync(path.join(d, 'items.json'));
  const firstStat = fs.statSync(path.join(d, 'items.json'));
  const seeded = store.readItems(d).value;
  assert.deepEqual(seeded.items.map(x => x.id), base.items.map(x => x.id));
  assert.deepEqual(seeded.seed_keys, base.seed_keys);
  assert.equal(seeded.items[0].project, 'Agenteach');
  assert.equal(store.seedItems(d, bytes).changed, false);
  assert.deepEqual(fs.readFileSync(path.join(d, 'items.json')), first);
  assert.equal(fs.statSync(path.join(d, 'items.json')).mtimeMs, firstStat.mtimeMs);
  const edited = structuredClone(seeded);
  edited.items[0].project = 'DUMBO';
  fs.writeFileSync(path.join(d, 'items.json'), JSON.stringify(edited));
  const editedBytes = fs.readFileSync(path.join(d, 'items.json'));
  assert.equal(edited.seed_marker, seeded.seed_marker);
  assert.deepEqual(edited.seed_keys, seeded.seed_keys);
  assert.throws(() => store.seedItems(d, bytes), e => e.code === 'seed_changed');
  assert.deepEqual(fs.readFileSync(path.join(d, 'items.json')), editedBytes);
  const changedInput = structuredClone(withProject);
  changedInput.items[0].project = 'Personal';
  assert.throws(() => store.seedItems(d, Buffer.from(JSON.stringify(changedInput))), e => e.code === 'seed_changed');
  const invalidInput = structuredClone(withProject);
  invalidInput.items[0].project = 'agenteach';
  const invalidDir = dir();
  assert.throws(() => store.seedItems(invalidDir, Buffer.from(JSON.stringify(invalidInput))), e => e.code === 'invalid_seed');
  assert.equal(fs.existsSync(path.join(invalidDir, 'items.json')), false);
});

test('FIFO item and ack files fail promptly without blocking regular reads', () => {
  const healthy = dir(); add(healthy);
  const child = `const s=require(${JSON.stringify(path.join(__dirname, '../../main/review_store'))}); console.log(JSON.stringify(s.projection(process.argv[1])))`;
  for (const name of ['items.json', 'acks.json']) {
    const d = dir();
    if (name === 'acks.json') add(d);
    assert.equal(spawnSync('/usr/bin/mkfifo', [path.join(d, name)]).status, 0);
    const started = Date.now();
    const result = spawnSync(process.execPath, ['-e', child, d], { encoding: 'utf8', timeout: 900 });
    assert.equal(result.error?.code, undefined, `${name} blocked for ${Date.now() - started} ms`);
    assert.equal(result.status, 0, result.stderr);
    const feed = JSON.parse(result.stdout);
    assert.ok(['invalid', 'unavailable'].includes(feed.state));
    assert.ok(['store_invalid', 'store_unreadable'].includes(feed.error_code));
    assert.equal(JSON.stringify(feed).includes(d), false);
    assert.equal(store.projection(healthy).state, 'ok');
  }
});

test('future and mtime-skewed item files refuse every mutation without changing targets or backups', () => {
  const seed = fs.readFileSync(path.join(__dirname, '../fixtures/review_seed.json'));
  for (const skew of ['future', 'mtime']) {
    for (const mutation of ['ack', 'add', 'resolve', 'seed', 'smoke-cleanup', 'smoke-remove']) {
      const d = dir();
      let id, run;
      if (mutation === 'seed') { store.seedItems(d, seed); id = store.readItems(d).value.items[0].id; }
      else if (mutation.startsWith('smoke')) {
        run = crypto.randomBytes(16).toString('hex');
        id = store.addItem(d, { kind: 'review', title: `Review inbox smoke ${run}`, smoke_run: run }).item.id;
      } else id = add(d);
      const file = path.join(d, 'items.json');
      const value = store.readItems(d).value;
      if (skew === 'future') value.updated_at = new Date(Date.now() + 3600000).toISOString();
      else value.updated_at = new Date(Date.now() - 3600000).toISOString();
      fs.writeFileSync(file, JSON.stringify(value));
      if (skew === 'mtime') {
        const old = new Date(Date.now() - 7200000);
        fs.utimesSync(file, old, old);
      }
      assert.equal(store.projection(d).state, 'invalid', `${skew}/${mutation}`);
      const names = ['items.json', 'items.json.prev', 'acks.json', 'acks.json.prev'];
      const before = names.map(name => fs.existsSync(path.join(d, name)) ? fs.readFileSync(path.join(d, name)) : null);
      const action = {
        ack: () => ack(d, id, 'ack'),
        add: () => add(d),
        resolve: () => store.resolveItem(d, id, 'Closed'),
        seed: () => store.seedItems(d, seed),
        'smoke-cleanup': () => store.smokeCleanup(d, { id, run_id: run, op_id: op() }),
        'smoke-remove': () => store.smokeRemove(d, id, run),
      }[mutation];
      assert.throws(action, e => e.code === 'store_invalid', `${skew}/${mutation}`);
      names.forEach((name, i) => assert.deepEqual(fs.existsSync(path.join(d, name)) ? fs.readFileSync(path.join(d, name)) : null, before[i], `${skew}/${mutation}/${name}`));
    }
  }
  const stale = dir(), id = add(stale), file = path.join(stale, 'items.json');
  const value = store.readItems(stale).value;
  value.updated_at = new Date(Date.now() - 26 * 3600000).toISOString();
  fs.writeFileSync(file, JSON.stringify(value));
  assert.equal(store.projection(stale).stale, true);
  assert.equal(ack(stale, id, 'ack').ok, true);
  assert.ok(add(stale));
});

test('source IDs require strings and an explicit seed schema must match exactly', () => {
  const d = dir(), id = add(d), file = path.join(d, 'items.json');
  const original = store.readItems(d).value;
  for (const bad of [123, null]) {
    const value = structuredClone(original);
    value.items[0].source.reply_to_message_id = bad;
    fs.writeFileSync(file, JSON.stringify(value));
    assert.equal(store.projection(d).state, 'invalid');
  }
  fs.writeFileSync(file, JSON.stringify(original));
  for (const key of ['dispatch_id', 'reply_to_message_id']) for (const bad of [123, null])
    assert.throws(() => store.addItem(d, { kind: 'review', title: 'Invalid source', [key]: bad }), e => e.code === 'invalid_input');
  assert.equal(store.projection(d).items[0].id, id);
  const input = { note: 'Fixture', prepared_at: new Date().toISOString(), items: [] };
  for (const schema of [null, '', false, 0]) {
    const empty = dir();
    assert.throws(() => store.seedItems(empty, JSON.stringify({ ...input, schema })), e => e.code === 'invalid_seed');
    assert.equal(fs.existsSync(path.join(empty, 'items.json')), false);
  }
  for (const bad of [123, null]) {
    const empty = dir();
    const row = { kind: 'review', title: 'Seed source', link: null, first_raised_at: null,
      source: 'dispatch example', reply_to_message_id: bad };
    assert.throws(() => store.seedItems(empty, JSON.stringify({ ...input, items: [row] })), e => e.code === 'invalid_seed');
    assert.equal(fs.existsSync(path.join(empty, 'items.json')), false);
  }
});
