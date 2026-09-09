const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const { test } = require('node:test');

const ROOT = path.resolve(__dirname, '..');
const CHECKER = path.join(ROOT, 'tools', 'check_governance_tripwires.py');
const WINDOWS_SKIP_REASON = process.platform === 'win32'
  ? 'requires POSIX-only python3 governance checker (not available to Windows node)'
  : false;

function run(kind, body) {
  return spawnSync('python3', [CHECKER, '--kind', kind, '-'], {
    cwd: ROOT,
    encoding: 'utf8',
    input: body,
  });
}

test('missing ruling fields fail for a new process-lifecycle actor', { skip: WINDOWS_SKIP_REASON }, () => {
  const result = run('review', 'Governance scope: new process-lifecycle actor\n');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Governance ruling/);
});

test('a complete actor ruling passes', { skip: WINDOWS_SKIP_REASON }, () => {
  const result = run(
    'review',
    'Governance scope: new process-lifecycle actor\n' +
      'Governance ruling: necessity=bounded recovery; reused substrate=existing pane inventory; authority=maintainer policy\n',
  );
  assert.equal(result.status, 0, result.stderr);
});

test('angle-bracket ruling placeholders fail', { skip: WINDOWS_SKIP_REASON }, () => {
  const result = run(
    'review',
    'Governance scope: new APP_* knob\n' +
      'Governance ruling: necessity=<why>; reused substrate=<what>; authority=<who>\n',
  );
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /placeholder/);
});

test('unrelated and subtractive review artifacts are not blocked', { skip: WINDOWS_SKIP_REASON }, () => {
  assert.equal(run('review', '# unrelated public docs\n').status, 0);
  assert.equal(run('review', 'Governance scope: subtractive/compatibility-only\n').status, 0);
});

test('retro requires a measured ratio or explicit no-deletion value', { skip: WINDOWS_SKIP_REASON }, () => {
  assert.notEqual(run('retro', '## Retro\n').status, 0);
  assert.equal(run('retro', 'Deletion ratio: none (0 lines removed; no deletion work)\n').status, 0);
  assert.equal(run('retro', 'Deletion ratio: 42 removed / 100 added = 42%\n').status, 0);
});

test('retro rejects incorrect arithmetic without imposing a threshold', { skip: WINDOWS_SKIP_REASON }, () => {
  const wrong = run('retro', 'Deletion ratio: 1 removed / 3 added = 50%\n');
  assert.notEqual(wrong.status, 0);
  const high = run('retro', 'Deletion ratio: 200 removed / 100 added = 200%\n');
  assert.equal(high.status, 0, high.stderr);
});

test('retro rejects contradictory no-deletion evidence', { skip: WINDOWS_SKIP_REASON }, () => {
  const result = run('retro', 'Deletion ratio: none (1 lines removed; no deletion work)\n');
  assert.notEqual(result.status, 0);
});

test('v2-gate tag requires immutable exact-head evidence', { skip: WINDOWS_SKIP_REASON }, () => {
  const sha = 'a'.repeat(40);
  const tag = [
    'workflow_id: 7',
    'run_id: 123',
    'run_url: https://example.com/example-org/public-project/actions/runs/123',
    `headSha: ${sha}`,
    `old_main_sha: ${'b'.repeat(40)}`,
    `candidate_sha: ${sha}`,
    'timestamp: 2026-08-21T00:00:00Z',
  ].join('\n');
  assert.equal(run('v2-gate-tag', tag).status, 0);
  assert.notEqual(run('v2-gate-tag', tag.replace(`candidate_sha: ${sha}`, `candidate_sha: ${'c'.repeat(40)}`)).status, 0);
});

