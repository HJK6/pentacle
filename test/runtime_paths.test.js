'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const { runtimeDirectory, desktopRuntimePath } = require('../main/runtime_paths');
const { withRuntimeDirectory } = require('./e2e/lib/runtime_directory');

function clientWithFilesystem(filesystem, env, now = '2026-01-02T03:04:05.000Z') {
  const filename = path.resolve(__dirname, '../main/chat_stream_client.js');
  const source = fs.readFileSync(filename, 'utf8');
  const realRequire = createRequire(filename);
  class FixedDate extends Date { constructor(...args) { super(...(args.length ? args : [now])); } }
  const context = { module: { exports: {} }, __dirname: path.dirname(filename), Buffer, TextDecoder, console,
    process: { env, pid: 42420 }, Date: FixedDate,
    setInterval: () => 'owned-fake-timer', clearInterval: () => {},
    setTimeout: () => 'owned-fake-timeout', clearTimeout: () => {},
    require(name) {
      if (name === 'fs') return filesystem;
      if (name === 'os') return { homedir: () => '/synthetic/home' };
      if (name === 'node:child_process') return { execFileSync: () => 'synthetic-build' };
      if (name === './runtime_paths') return {
        runtimeDirectory: () => runtimeDirectory(env, '/synthetic/home'),
        desktopRuntimePath: () => desktopRuntimePath(env, '/synthetic/home'),
      };
      return realRequire(name);
    } };
  vm.runInNewContext(source, context, { filename });
  const client = context.module.exports;
  client._ws = {}; client._handshakeState = 'hello_sent'; client._buildSha = 'synthetic-sha';
  return client;
}

test('unset and empty preserve exact historic path; override retains relative semantics', () => {
  for (const env of [{}, { PENTACLE_RUNTIME_DIR: '' }]) {
    assert.equal(runtimeDirectory(env, '/synthetic/home'), path.join('/synthetic/home', '.pentacle'));
    assert.equal(desktopRuntimePath(env, '/synthetic/home'), path.join('/synthetic/home', '.pentacle', 'desktop-runtime.json'));
  }
  assert.equal(desktopRuntimePath({ PENTACLE_RUNTIME_DIR: 'relative-runtime' }), path.join('relative-runtime', 'desktop-runtime.json'));
});

test('real handshake writes byte-identical legacy payload using late-resolved override', () => {
  for (const override of [undefined, '/synthetic/override']) {
    const calls = []; const env = {};
    const fake = { readFileSync: () => 'synthetic-build', mkdirSync: (...args) => calls.push(['mkdir', ...args]),
      writeFileSync: (...args) => calls.push(['write', ...args]) };
    const client = clientWithFilesystem(fake, env);
    if (override) env.PENTACLE_RUNTIME_DIR = override; // after module load
    client._completeHandshake(client._ws);
    const expectedDir = override || path.join('/synthetic/home', '.pentacle');
    assert.equal(calls[0][1], expectedDir);
    assert.equal(calls[0][2].recursive, true);
    assert.equal(calls[1][1], path.join(expectedDir, 'desktop-runtime.json'));
    const expected = JSON.stringify({ sha: 'synthetic-sha', pid: 42420, connected_at: '2026-01-02T03:04:05.000Z' }) + '\n';
    assert.deepEqual(Buffer.from(calls[1][2]), Buffer.from(expected));
  }
});

for (const failing of ['mkdirSync', 'writeFileSync']) {
  test('override ' + failing + ' EACCES is clear, preserves cause, never falls back', () => {
    const error = Object.assign(new Error('synthetic permission denied'), { code: 'EACCES' });
    const calls = [];
    const fake = { readFileSync: () => 'synthetic-build' };
    for (const op of ['mkdirSync', 'writeFileSync']) fake[op] = target => { calls.push(target); if (op === failing) throw error; };
    const client = clientWithFilesystem(fake, { PENTACLE_RUNTIME_DIR: '/synthetic/blocked' });
    assert.throws(() => client._completeHandshake(client._ws), e => e.code === 'EACCES' && e.cause === error && /PENTACLE_RUNTIME_DIR.*blocked.*EACCES/.test(e.message));
    assert.ok(calls.every(target => target.startsWith('/synthetic/blocked')));
  });
}

test('disposable real writer and resolver reader agree; file-as-directory fails deterministically', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'th-h3-writer-'));
  try {
    const env = { PENTACLE_RUNTIME_DIR: path.join(root, 'runtime') };
    const client = clientWithFilesystem(fs, env);
    client._completeHandshake(client._ws);
    assert.equal(JSON.parse(fs.readFileSync(desktopRuntimePath(env), 'utf8')).sha, 'synthetic-sha');
    const blocked = path.join(root, 'file');
    fs.writeFileSync(blocked, 'sentinel');
    const failing = clientWithFilesystem(fs, { PENTACLE_RUNTIME_DIR: blocked });
    assert.throws(() => failing._completeHandshake(failing._ws), /PENTACLE_RUNTIME_DIR.*(EEXIST|ENOTDIR)/);
    assert.equal(fs.readFileSync(blocked, 'utf8'), 'sentinel');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

for (const prior of [undefined, '', '/synthetic/prior']) {
  for (const fails of [false, true]) {
    test('runtime scope restores ' + String(prior) + ' after ' + (fails ? 'failure' : 'success'), async () => {
      const env = prior === undefined ? {} : { PENTACLE_RUNTIME_DIR: prior };
      let owned;
      const promise = withRuntimeDirectory(async dir => {
        owned = dir;
        assert.equal(env.PENTACLE_RUNTIME_DIR, dir);
        fs.writeFileSync(path.join(dir, 'desktop-runtime.json'), 'synthetic');
        if (fails) throw new Error('synthetic setup/scenario failure');
        return 42;
      }, { env });
      if (fails) await assert.rejects(promise, /synthetic setup/); else assert.equal(await promise, 42);
      assert.equal(fs.existsSync(owned), false);
      assert.equal(env.PENTACLE_RUNTIME_DIR, prior);
      assert.equal(Object.hasOwn(env, 'PENTACLE_RUNTIME_DIR'), prior !== undefined);
    });
  }
}
test('runtime cleanup error fails and still restores caller environment', async () => {
  const env = { PENTACLE_RUNTIME_DIR: '/synthetic/prior' };
  const fake = { mkdtempSync: () => '/synthetic/owned', rmSync: () => { throw new Error('synthetic cleanup failure'); } };
  await assert.rejects(withRuntimeDirectory(async () => 0, { env, filesystem: fake }), /cleanup failure/);
  assert.equal(env.PENTACLE_RUNTIME_DIR, '/synthetic/prior');
});

test('synthetic fixture manifest matches every authored input file', () => {
  const crypto = require('node:crypto');
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/runtime_paths/MANIFEST.json')));
  assert.deepEqual(manifest.files.map(row => row.path).sort(),
    ['test/runtime_paths.test.js', 'test/web_runtime_directory.test.js', 'test/fixtures/runner_home_probe.js'].sort());
  for (const row of manifest.files) {
    assert.equal(row.synthetic, true);
    assert.equal(row.contains_real_traffic, false);
    assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.resolve(__dirname, '..', row.path))).digest('hex'), row.sha256);
  }
});
