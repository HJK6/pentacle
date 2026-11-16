const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const root = path.join(__dirname, '..');
const app = fs.readFileSync(path.join(root, 'renderer', 'app.js'), 'utf8');

function loadModelLabel() {
  const src = app.match(/function modelLabel\(model\) \{[\s\S]*?\n\}/);
  assert.ok(src, 'modelLabel function found in renderer/app.js');
  return new Function(`${src[0]}; return modelLabel;`)();
}

function catalogModelIds() {
  const script = 'import json, sys; sys.path.insert(0, sys.argv[1]); import spawn_profiles; '
    + 'print(json.dumps(sorted(m for models in spawn_profiles.catalog()["models"].values() for m in models)))';
  const result = spawnSync('python3', ['-c', script, path.join(root, 'services', '_shared')], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

test('every daemon catalog model renders a label, not its raw id', {
  skip: process.platform === 'win32' ? 'requires POSIX python3 for the daemon catalog' : false,
}, () => {
  const modelLabel = loadModelLabel();
  const ids = catalogModelIds();
  assert.ok(ids.length > 0, 'catalog lists models');
  const raw = ids.filter((id) => modelLabel(id) === id);
  assert.deepEqual(raw, [], `catalog models rendered as raw ids: ${raw.join(', ')}`);
});

test('modelLabel holds no provider model-id list', () => {
  const src = app.match(/function modelLabel\(model\) \{[\s\S]*?\n\}/)[0];
  assert.doesNotMatch(src, /'(claude|gpt)-[^']*'\s*:/, 'modelLabel enumerates model ids');
});
