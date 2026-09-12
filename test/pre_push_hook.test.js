'use strict';

// Integration tests for scripts/hooks/pre-push. Each test builds throwaway
// remotes and a work clone, installs the hook via `-c core.hooksPath=…`, and
// drives real `git push` so the hook sees exactly the argv and stdin git gives
// it in the field.
//
// Coverage — see the hook header for the contract:
//   (a) destination name  — remote named `public` with a non-public URL REJECTED
//   (b) explicit refspec  — bare push REJECTED; same-name branch ACCEPTED
//   (c) foreign ancestry  — orphan root REJECTED; foreign *merge* REJECTED;
//                           fail-closed when a public main is unresolvable
//   (4) destination by URL — public rules apply under remote name `origin` and
//                            for a raw-URL push, not only for a `public` remote

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const hooksDir = path.join(repoRoot, 'scripts', 'hooks');
const ID = ['-c', 'user.name=Test', '-c', 'user.email=test@example.com'];

function git(cwd, args, env = {}) {
  return execFileSync('git', args, { cwd, encoding: 'utf8', env: { ...process.env, ...env } });
}

// Run a git command that may fail; capture status + both streams either way.
function tryGit(cwd, args, env = {}) {
  const r = spawnSync('git', args, { cwd, encoding: 'utf8', env: { ...process.env, ...env } });
  return { status: r.status == null ? 1 : r.status, stdout: r.stdout || '', stderr: r.stderr || '' };
}

function withHook(args) {
  return ['-c', `core.hooksPath=${hooksDir}`, ...ID, ...args];
}

function commitFile(work, name, body, msg) {
  fs.writeFileSync(path.join(work, name), body);
  git(work, [...ID, 'add', name]);
  git(work, [...ID, 'commit', '-m', msg]);
}

// A bare remote with `main` seeded, plus a work clone whose `public` remote
// points at it. `allowEnv` marks that local path as "the public repo" so the
// URL-based rules apply in tests. seedMain=false leaves the remote without main.
function sandbox({ seedMain = true } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-prepush-'));
  const remote = path.join(dir, 'public.git');
  const work = path.join(dir, 'work');
  execFileSync('git', ['init', '--bare', '-b', 'main', remote], { stdio: 'ignore' });
  execFileSync('git', ['init', '-b', 'main', work], { stdio: 'ignore' });
  commitFile(work, 'README', 'seed\n', 'seed main');
  git(work, ['remote', 'add', 'public', remote]);
  if (seedMain) {
    git(work, [...ID, 'push', 'public', 'main:refs/heads/main']);
    git(work, ['fetch', 'public', 'main']);
  }
  return { dir, remote, work, allowEnv: { PENTACLE_ALLOWED_PUBLIC_REMOTES: remote } };
}

// Build an orphan branch with a root unrelated to main.
function makeForeignBranch(work, name) {
  git(work, [...ID, 'checkout', '--orphan', name]);
  git(work, [...ID, 'rm', '-rf', '.']);
  commitFile(work, 'OTHER', 'a different repo\n', 'foreign root');
}

test('(a)+(b)+(c) clean public branch is ACCEPTED', () => {
  const sb = sandbox();
  git(sb.work, [...ID, 'checkout', '-b', 'clean']);
  commitFile(sb.work, 'feature.txt', 'work\n', 'a real feature');
  const r = tryGit(sb.work, withHook(['push', 'public', 'clean:refs/heads/clean']), sb.allowEnv);
  assert.equal(r.status, 0, `expected accept, got ${r.status}\n${r.stderr}`);
  assert.match(r.stderr, /pushing to 'public' -> .* \(public repo/);
});

test('(b) a same-name branch refspec (git push public branch) is ACCEPTED', () => {
  const sb = sandbox();
  git(sb.work, [...ID, 'checkout', '-b', 'feat']);
  commitFile(sb.work, 'feat.txt', 'x\n', 'feat');
  // No colon: `git push public feat` maps to the same branch name — allowed.
  const r = tryGit(sb.work, withHook(['push', 'public', 'feat']), sb.allowEnv);
  assert.equal(r.status, 0, `expected same-name branch accept, got ${r.status}\n${r.stderr}`);
});

test('(c) foreign orphan history (extra root) is REJECTED', () => {
  const sb = sandbox();
  makeForeignBranch(sb.work, 'foreign');
  const r = tryGit(sb.work, withHook(['push', 'public', 'foreign:refs/heads/foreign']), sb.allowEnv);
  assert.notEqual(r.status, 0, 'expected foreign history to be refused');
  assert.match(r.stderr, /not a root of public\/main/);
});

test('(c) a merge that pulls foreign history into a clean branch is REJECTED', () => {
  const sb = sandbox();
  // clean branch off main
  git(sb.work, [...ID, 'checkout', '-b', 'merged', 'main']);
  commitFile(sb.work, 'clean.txt', 'clean\n', 'clean work');
  // an unrelated-root branch…
  makeForeignBranch(sb.work, 'foreign');
  // …merged into the clean branch => two roots reachable from the tip.
  git(sb.work, [...ID, 'checkout', 'merged']);
  git(sb.work, [...ID, 'merge', '--allow-unrelated-histories', '--no-edit', 'foreign']);
  const r = tryGit(sb.work, withHook(['push', 'public', 'merged:refs/heads/merged']), sb.allowEnv);
  assert.notEqual(r.status, 0, 'expected a foreign-history merge to be refused');
  assert.match(r.stderr, /foreign history/);
});

test('(b) missing refspec (bare push) is REJECTED', () => {
  const sb = sandbox();
  commitFile(sb.work, 'more.txt', 'more\n', 'advance main');
  const r = tryGit(sb.work, ['-c', 'push.default=current', ...withHook(['push', 'public'])], sb.allowEnv);
  assert.notEqual(r.status, 0, 'expected a bare push to be refused');
  assert.match(r.stderr, /no refspec/);
});

test('(a) wrong destination for a remote named `public` is REJECTED', () => {
  const sb = sandbox();
  git(sb.work, [...ID, 'checkout', '-b', 'clean2']);
  commitFile(sb.work, 'x.txt', 'x\n', 'feature two');
  // No allow-list override: the local path is not github.com/HJK6/pentacle.
  const r = tryGit(sb.work, withHook(['push', 'public', 'clean2:refs/heads/clean2']));
  assert.notEqual(r.status, 0, 'expected a non-public destination to be refused');
  assert.match(r.stderr, /not the public HJK6\/pentacle/);
});

test('(c) FAIL CLOSED: public destination whose main is unresolvable is REJECTED', () => {
  const sb = sandbox({ seedMain: false }); // remote has no `main`
  git(sb.work, [...ID, 'checkout', '-b', 'first']);
  commitFile(sb.work, 'first.txt', 'first\n', 'first work');
  const r = tryGit(sb.work, withHook(['push', 'public', 'first:refs/heads/first']), sb.allowEnv);
  assert.notEqual(r.status, 0, 'expected fail-closed refusal when public main is unresolvable');
  assert.match(r.stderr, /cannot resolve the public repo's 'main'/);
});

test('(4) destination by URL applies under remote name `origin` (not just `public`)', () => {
  const sb = sandbox();
  git(sb.work, ['remote', 'add', 'origin', sb.remote]);
  git(sb.work, ['fetch', 'origin', 'main']);
  // clean via origin => accepted
  git(sb.work, [...ID, 'checkout', '-b', 'okbranch', 'main']);
  commitFile(sb.work, 'ok.txt', 'ok\n', 'ok');
  const good = tryGit(sb.work, withHook(['push', 'origin', 'okbranch:refs/heads/okbranch']), sb.allowEnv);
  assert.equal(good.status, 0, `expected accept via origin, got ${good.status}\n${good.stderr}`);
  assert.match(good.stderr, /\(public repo/); // rules recognized by URL, name is origin
  // foreign via origin => rejected by the URL-matched public rules
  makeForeignBranch(sb.work, 'foreign');
  const bad = tryGit(sb.work, withHook(['push', 'origin', 'foreign:refs/heads/foreign']), sb.allowEnv);
  assert.notEqual(bad.status, 0, 'expected foreign push via origin to be refused');
  assert.match(bad.stderr, /not a root of origin\/main/);
});

test('(4) destination by URL applies to a raw-URL push', () => {
  const sb = sandbox();
  makeForeignBranch(sb.work, 'foreign');
  // push straight at the path (no named remote); URL match still applies rules.
  const r = tryGit(sb.work, withHook(['push', sb.remote, 'foreign:refs/heads/foreign']), sb.allowEnv);
  assert.notEqual(r.status, 0, 'expected foreign raw-URL push to be refused');
  assert.match(r.stderr, /foreign history|not a root of/);
});
