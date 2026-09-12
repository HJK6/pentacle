'use strict';

// Integration tests for scripts/hooks/pre-push. Each test builds a throwaway
// "public" bare remote and a work clone, installs the hook via
// `-c core.hooksPath=…`, and drives real `git push` so the hook sees exactly the
// argv and stdin git gives it in the field.
//
// Proves (see the hook header): wrong-history REJECTED, clean public branch
// ACCEPTED, missing-refspec REJECTED, wrong-destination REJECTED.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const hooksDir = path.join(repoRoot, 'scripts', 'hooks');

function git(cwd, args, env = {}) {
  const res = execFileSync('git', args, {
    cwd,
    encoding: 'utf8',
    env: { ...process.env, ...env },
  });
  return res;
}

// Run a git command that may fail; capture status + both streams either way.
function tryGit(cwd, args, env = {}) {
  const r = spawnSync('git', args, {
    cwd,
    encoding: 'utf8',
    env: { ...process.env, ...env },
  });
  return {
    status: r.status == null ? 1 : r.status,
    stdout: r.stdout || '',
    stderr: r.stderr || '',
  };
}

// A fresh sandbox: a bare `public` remote with `main` seeded, and a work clone
// whose `public` remote points at it. Returns { dir, remote, work, allowEnv }.
function sandbox() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-prepush-'));
  const remote = path.join(dir, 'public.git');
  const work = path.join(dir, 'work');
  const identity = ['-c', 'user.name=Test', '-c', 'user.email=test@example.com'];

  execFileSync('git', ['init', '--bare', '-b', 'main', remote], { stdio: 'ignore' });
  execFileSync('git', ['init', '-b', 'main', work], { stdio: 'ignore' });
  fs.writeFileSync(path.join(work, 'README'), 'seed\n');
  git(work, [...identity, 'add', 'README']);
  git(work, [...identity, 'commit', '-m', 'seed main']);
  git(work, ['remote', 'add', 'public', remote]);
  // Seed the remote main without the hook, then have a tracking ref for it.
  git(work, [...identity, 'push', 'public', 'main:refs/heads/main']);
  git(work, ['fetch', 'public', 'main']);

  return {
    dir,
    remote,
    work,
    identity,
    // (a) allow-list override so a remote named `public` may point at this local
    // path in tests. The push commands opt in explicitly.
    allowEnv: { PENTACLE_ALLOWED_PUBLIC_REMOTES: remote },
  };
}

function withHook(sb, args) {
  return ['-c', `core.hooksPath=${hooksDir}`, ...sb.identity, ...args];
}

test('clean public branch is ACCEPTED (shared history, explicit refspec, valid destination)', () => {
  const sb = sandbox();
  git(sb.work, [...sb.identity, 'checkout', '-b', 'clean']);
  fs.writeFileSync(path.join(sb.work, 'feature.txt'), 'work\n');
  git(sb.work, [...sb.identity, 'add', 'feature.txt']);
  git(sb.work, [...sb.identity, 'commit', '-m', 'a real feature']);

  const r = tryGit(sb.work, withHook(sb, ['push', 'public', 'clean:refs/heads/clean']), sb.allowEnv);
  assert.equal(r.status, 0, `expected accept, got status ${r.status}\n${r.stderr}`);
  // The hook always prints the resolved destination.
  assert.match(r.stderr, /pushing to 'public' -> /);
});

test('foreign history (no merge-base with remote main) is REJECTED', () => {
  const sb = sandbox();
  git(sb.work, [...sb.identity, 'checkout', '--orphan', 'foreign']);
  git(sb.work, [...sb.identity, 'rm', '-rf', '.']);
  fs.writeFileSync(path.join(sb.work, 'OTHER'), 'a different repo\n');
  git(sb.work, [...sb.identity, 'add', 'OTHER']);
  git(sb.work, [...sb.identity, 'commit', '-m', 'foreign root']);

  const r = tryGit(sb.work, withHook(sb, ['push', 'public', 'foreign:refs/heads/foreign']), sb.allowEnv);
  assert.notEqual(r.status, 0, 'expected foreign history to be refused');
  assert.match(r.stderr, /no merge-base with public\/main/);
});

test('missing refspec (bare push) is REJECTED', () => {
  const sb = sandbox();
  // Something to push on main so git actually attempts the push and runs the hook.
  fs.writeFileSync(path.join(sb.work, 'more.txt'), 'more\n');
  git(sb.work, [...sb.identity, 'add', 'more.txt']);
  git(sb.work, [...sb.identity, 'commit', '-m', 'advance main']);

  // push.default=current so a bare `git push public` has a branch to push and
  // the hook runs (rather than git erroring on "no upstream" before the hook).
  const r = tryGit(
    sb.work,
    ['-c', 'push.default=current', ...withHook(sb, ['push', 'public'])],
    sb.allowEnv,
  );
  assert.notEqual(r.status, 0, 'expected a bare push with no refspec to be refused');
  assert.match(r.stderr, /no refspec/);
});

test('wrong destination for a remote named `public` is REJECTED', () => {
  const sb = sandbox();
  git(sb.work, [...sb.identity, 'checkout', '-b', 'clean2']);
  fs.writeFileSync(path.join(sb.work, 'x.txt'), 'x\n');
  git(sb.work, [...sb.identity, 'add', 'x.txt']);
  git(sb.work, [...sb.identity, 'commit', '-m', 'feature two']);

  // No allow-list override: the local path is not github.com/HJK6/pentacle, so
  // the destination check must refuse even though history + refspec are fine.
  const r = tryGit(sb.work, withHook(sb, ['push', 'public', 'clean2:refs/heads/clean2']));
  assert.notEqual(r.status, 0, 'expected a non-public destination to be refused');
  assert.match(r.stderr, /not the public HJK6\/pentacle/);
});
