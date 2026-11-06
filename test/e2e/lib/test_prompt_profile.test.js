const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { selectTestPromptProfile } = require('./test_prompt_profile');

function context(tuple, hidden = false) {
  const changes = [];
  const values = { '#spawn-provider': ['codex', 'claude'],
                   '#spawn-model': hidden ? ['work-model'] : [tuple[0]],
                   '#spawn-effort': [tuple[1]] };
  const controls = Object.fromEntries(Object.entries(values).map(([key, options]) => [key, {
    options: options.map(value => ({ value })), value: options[0],
    dispatchEvent: event => changes.push([key, event.type]),
  }]));
  return { changes, controls, ctx: {
    eval: script => vm.runInNewContext(script, {
      window: { cc: { chatSpawnCatalog: async () => ({ ok: true, catalog: {
        profiles: { test_prompt: { codex: tuple } },
      } }) } },
      document: { querySelector: key => controls[key] },
      Event: class { constructor(type) { this.type = type; } },
    }),
    assert: (label, value) => assert.ok(value, label),
  } };
}

test('retained UI walk drives the configured model and effort through real select events', async () => {
  const fixture = context(['configured-cheap-model', 'configured-low-effort']);
  await selectTestPromptProfile(fixture.ctx, 'codex');
  assert.equal(fixture.controls['#spawn-model'].value, 'configured-cheap-model');
  assert.equal(fixture.controls['#spawn-effort'].value, 'configured-low-effort');
  assert.deepEqual(fixture.changes, [ ['#spawn-provider', 'change'], ['#spawn-model', 'change'], ['#spawn-effort', 'change'] ]);
});

test('hidden test model stops the walk before submission', async () => {
  const fixture = context(['configured-cheap-model', 'low'], true);
  await assert.rejects(selectTestPromptProfile(fixture.ctx, 'codex'), /configured test tuple/i);
  assert.equal(fixture.controls['#spawn-model'].value, 'work-model');
  assert.equal(fixture.changes.length, 1);
});
