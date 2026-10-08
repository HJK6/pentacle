'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const daemon = fs.readFileSync(path.join(__dirname, 'e2e/lib/web_gate_daemon.py'), 'utf8');
function probe(url) {
  assert.match(daemon, /    # BEGIN HOUSEHOLD FIXTURE:/);
  const block = daemon.split('    # BEGIN HOUSEHOLD FIXTURE:')[1].split('\n    # END HOUSEHOLD FIXTURE')[0].split('\n').slice(1).map(line => line.replace(/^    /, '')).join('\n');
  const script = `import os, sys, tempfile, types, json\nfrom pathlib import Path\nclass Household:\n def __init__(self, **kwargs): self.kwargs = kwargs\noriginal = Household.__init__\nsys.modules['household'] = types.SimpleNamespace(Household=Household)\nwith tempfile.TemporaryDirectory() as directory:\n scratch = Path(directory)\n try:\n  exec(${JSON.stringify(block)})\n  result = {'wrapped': Household.__init__ is not original, 'exists': (scratch / 'cosmo.token').exists()}\n  if result['exists']:\n   result.update(mode=oct((scratch / 'cosmo.token').stat().st_mode & 0o777), insecure=Household(allow_insecure=False).kwargs['allow_insecure'], self=os.environ['PENTACLE_COSMO_SELF'], partner=os.environ['PENTACLE_HOUSEHOLD_PARTNER_NAME'], url=os.environ['PENTACLE_COSMO_URL'], token_owned=Path(os.environ['COSMO_PENTACLE_TOKEN_FILE']).parent == scratch)\n except ValueError as error:\n  result = {'error': str(error), 'exists': (scratch / 'cosmo.token').exists()}\n print(json.dumps(result))\n`;
  return JSON.parse(execFileSync(process.env.PENTACLE_PYTHON || 'python3', ['-c', script], { encoding: 'utf8', env: { ...process.env, PENTACLE_WEB_GATE_COSMO_URL: url } }));
}
test('absent fixture env leaves Household initialization untouched', () => { assert.deepEqual(probe(''), { wrapped: false, exists: false }); });
test('fixture env permits only loopback and never writes a token on refusal', () => {
  for (const url of ['http://example.invalid', 'http://localhost', 'file:///tmp/fixture', 'http://user@127.0.0.1:1']) assert.deepEqual(probe(url), { error: 'household fixture URL must be loopback', exists: false });
});
test('fixture mode owns a 0600 synthetic token and wraps only insecure permission', () => {
  assert.deepEqual(probe('http://127.0.0.1:1'), { wrapped: true, exists: true, mode: '0o600', insecure: true, self: 'operator', partner: 'Partner Fixture', url: 'http://127.0.0.1:1', token_owned: true });
});
test('web gate starts and stops fake Cosmo only in hermetic path and passes env across restarts', () => {
  const source = fs.readFileSync(path.join(__dirname, 'e2e/web_gate.js'), 'utf8');
  assert.match(source, /if \(!profile\) \{\s+runtime\.fakeCosmo = await startFakeCosmo\(\);\s+daemon = await startDaemon/);
  assert.equal((source.match(/PENTACLE_WEB_GATE_COSMO_URL: runtime\.fakeCosmo\?\.url \|\| ''/g) || []).length, 2);
  assert.match(source, /if \(runtime\.fakeCosmo\) await runtime\.fakeCosmo\.close\(\)/);
});
test('real daemon Household translates synthetic fixture rows and all six routes', async t => {
  const { startFakeCosmo } = require('./e2e/lib/fake_cosmo_server');
  const { execFile } = require('node:child_process');
  const { promisify } = require('node:util');
  const fake = await startFakeCosmo({ today: '2099-06-15' }); t.after(() => fake.close());
  const script = `import sys, os, asyncio, tempfile, json\nfrom pathlib import Path\nfrom datetime import datetime, timezone\nsys.path[:0] = [os.path.join(os.getcwd(), 'services'), os.path.join(os.getcwd(), 'services/chat-stream-v2')]\nfrom household import Household\nfrom sessions import VerbError\nasync def run():\n with tempfile.TemporaryDirectory() as directory:\n  token = Path(directory) / 'token'\n  token.write_text('synthetic-test-only')\n  token.chmod(0o600)\n  h = Household(clock=lambda: datetime(2099, 6, 15, 12, tzinfo=timezone.utc).timestamp(), url=os.environ['FIXTURE_URL'], token_file=str(token), allow_insecure=True, self_person='operator', partner_name='Partner Fixture')\n  auth = {'_auth_context': {'operator_authenticated': True, 'operator_principal': 'operator:synthetic'}}\n  snap = await h.snapshot(auth)\n  assert all(len(rows) == 2 for rows in snap['lists'].values())\n  assert snap['lists']['grocery'][0]['created_by'] == 'assistant'\n  assert snap['lists']['grocery'][1]['created_by'] == 'partner_assistant'\n  assert any(row['who'] == 'partner' and row['scope'] == 'private' for row in snap['events'])\n  item = (await h.item_add({**auth, 'list': 'grocery', 'label': 'Synthetic daemon item'}))['item']\n  await h.item_done({**auth, 'item_id': item['id']})\n  await h.item_remove({**auth, 'item_id': item['id']})\n  event = (await h.event_add({**auth, 'date': snap['today'], 'time': None, 'title': 'Synthetic daemon event', 'who': 'partner'}))['event']\n  assert event['who'] == 'partner' and event['scope'] == 'private'\n  await h.event_remove({**auth, 'event_id': event['id']})\n  print(json.dumps({'counts': [len(rows) for rows in snap['lists'].values()], 'verbs': 6}))\nasyncio.run(run())\n`;
  const { stdout } = await promisify(execFile)(process.env.PENTACLE_PYTHON || 'python3', ['-c', script], { cwd: path.join(__dirname, '..'), env: { ...process.env, FIXTURE_URL: fake.url } });
  assert.deepEqual(JSON.parse(stdout), { counts: [2, 2, 2, 2, 2], verbs: 6 });
  assert.equal(fake.calls.length, 12); assert.ok(fake.calls.every(call => call.authPresent));
  assert.equal(fake.calls.filter(call => call.method === 'GET').length, 7);
});
test('real daemon reports one delayed fixture mutation as unknown and reconciles by reads', async t => {
  const { startFakeCosmo } = require('./e2e/lib/fake_cosmo_server');
  const { execFile } = require('node:child_process');
  const { promisify } = require('node:util');
  const fake = await startFakeCosmo({ today: '2099-06-15' }); t.after(() => fake.close());
  await fake.dispatch({ method: 'POST', url: '/__fixture/delay', body: { method: 'POST', path: '/events', ms: 6000 } });
  const script = `import sys, os, asyncio, tempfile, json\nfrom pathlib import Path\nfrom datetime import datetime, timezone\nsys.path[:0] = [os.path.join(os.getcwd(), 'services'), os.path.join(os.getcwd(), 'services/chat-stream-v2')]\nfrom household import Household\nfrom sessions import VerbError\nasync def run():\n with tempfile.TemporaryDirectory() as directory:\n  token = Path(directory) / 'token'\n  token.write_text('synthetic-test-only')\n  token.chmod(0o600)\n  h = Household(clock=lambda: datetime(2099, 6, 15, 12, tzinfo=timezone.utc).timestamp(), url=os.environ['FIXTURE_URL'], token_file=str(token), allow_insecure=True, self_person='operator', partner_name='Partner Fixture')\n  auth = {'_auth_context': {'operator_authenticated': True, 'operator_principal': 'operator:synthetic'}}\n  snap = await h.snapshot(auth)\n  try:\n   await h.event_add({**auth, 'date': snap['today'], 'time': None, 'title': 'Synthetic delayed proof', 'who': 'partner'})\n   raise AssertionError('expected unknown outcome')\n  except VerbError as error:\n   assert error.code == 'unknown_outcome'\n  first = await h.snapshot(auth)\n  assert not any(row['title'] == 'Synthetic delayed proof' for row in first['events'])\n  await asyncio.sleep(1.25)\n  second = await h.snapshot(auth)\n  assert sum(row['title'] == 'Synthetic delayed proof' for row in second['events']) == 1\n  print(json.dumps({'unknown': True, 'readback': True}))\nasyncio.run(run())\n`;
  const { stdout } = await promisify(execFile)(process.env.PENTACLE_PYTHON || 'python3', ['-c', script], { cwd: path.join(__dirname, '..'), env: { ...process.env, FIXTURE_URL: fake.url } });
  assert.deepEqual(JSON.parse(stdout), { unknown: true, readback: true });
  assert.equal(fake.calls.filter(call => call.method === 'POST' && call.path === '/events').length, 1);
  assert.equal(fake.calls.filter(call => call.method === 'GET').length, 21);
});

test('generic fixture integration retains the current catalog wiring', () => {
  const source = fs.readFileSync(path.join(__dirname, 'e2e/web_gate.js'), 'utf8');
  assert.match(source, /const catalogFixtures = require\('\.\/lib\/dashboard_catalog_fixture'\)/);
  assert.match(source, /catalogFixtures\.buildCatalogFixture\(scratch, \{ hostedUrl: runtime\.modelerFixture\.url \}\)/);
  assert.match(source, /catalogSpecId: catalog\.specId, catalogRoot: catalog\.root/);
  assert.match(source, /modelerFixtureUrl: runtime\.modelerFixture\?\.url, catalog \}/);
});

for (const [label, profile, closeFails] of [
  ['hermetic startup failure', null, false],
  ['fixture cleanup failure', null, true],
  ['external profile failure', '/synthetic/profile', false],
]) test(`actual web gate owns fixture lifecycle on ${label}`, async t => {
  const os = require('node:os');
  const vm = require('node:vm');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'generic-fixture-gate-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const observations = { starts: 0, closes: 0, runtimeCleanup: 0 };
  const context = {
    module: { exports: {} }, __dirname: path.join(root, 'e2e'),
    process: { env: {}, execPath: process.execPath },
    console: { log() {}, error() {} }, Buffer, Date, setTimeout, clearTimeout,
    require(name) {
      if (name === 'os') return { ...os, tmpdir: () => root };
      if (name === 'child_process') return {
        execFileSync() { throw new Error('synthetic daemon setup failure'); },
        spawn() { throw new Error('unexpected process launch'); },
      };
      if (name === './lib/fake_cosmo_server') return {
        async startFakeCosmo() {
          observations.starts++;
          return { url: 'http://127.0.0.1:1', async close() {
            observations.closes++;
            if (closeFails) throw new Error('synthetic fixture cleanup failure');
          } };
        },
      };
      if (name === './lib/runtime_directory') return {
        withRuntimeDirectory: run => run(root, () => { observations.runtimeCleanup++; }),
      };
      if (name === '../../server') return {
        async main() { throw new Error('synthetic external host setup failure'); },
      };
      if (name.startsWith('./lib/')) return {};
      return require(name);
    },
  };
  const filename = path.join(__dirname, 'e2e/web_gate.js');
  vm.runInNewContext(fs.readFileSync(filename, 'utf8'), context, { filename });
  assert.equal(await context.module.exports.run({ profile, keep: false, python: 'synthetic-python', timeoutMs: 100 }), 1);
  assert.deepEqual(observations, { starts: profile ? 0 : 1, closes: profile ? 0 : 1, runtimeCleanup: 1 });
  const reportRoot = path.join(root, 'e2e/runs');
  const verdict = JSON.parse(fs.readFileSync(path.join(reportRoot, fs.readdirSync(reportRoot)[0], 'web_gate/verdict.json')));
  assert.equal(verdict.status, 'FAIL');
  assert.equal(verdict.fixture_auth_cleanup.registry_removed, true);
  assert.equal(verdict.fixture_auth_cleanup.token_removed, true);
  if (closeFails) assert.match(verdict.cleanup_error, /synthetic fixture cleanup failure/);
  assert.ok(!fs.readdirSync(root).some(name => name.startsWith('pentacle-web-gate-')));
});
