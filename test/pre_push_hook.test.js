'use strict';

// Integration tests for scripts/hooks/pre-push. Each test builds throwaway
// remotes and a work clone, installs the hook via `-c core.hooksPath=…`, and
// drives real `git push` so the hook sees the argv and stdin git gives it.
//
// The child environment is isolated (host BASH_ENV/ENV and PENTACLE_* scrubbed)
// so the suite is deterministic regardless of the machine it runs on. A push to
// the public repo is recognized by URL; local test remotes are marked public
// only under the explicit test-mode override (PENTACLE_PREPUSH_TEST_MODE=1 +
// PENTACLE_ALLOWED_PUBLIC_REMOTES), mirroring the hook's contract.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const hooksDir = path.join(repoRoot, 'scripts', 'hooks');
const hookPath = path.join(hooksDir, 'pre-push');
const ID = ['-c', 'user.name=Test', '-c', 'user.email=test@example.com'];

// Deterministic child env: strip anything from the host that could change the
// hook's behavior, then layer only what a test explicitly asks for.
function childEnv(extra = {}) {
  const e = { ...process.env, ...extra };
  for (const k of ['BASH_ENV', 'ENV', 'PENTACLE_PREPUSH_TEST_MODE', 'PENTACLE_ALLOWED_PUBLIC_REMOTES']) {
    if (!(k in extra)) delete e[k];
  }
  return e;
}

function git(cwd, args, env = {}) {
  return execFileSync('git', args, { cwd, encoding: 'utf8', env: childEnv(env) });
}

function tryGit(cwd, args, env = {}) {
  const r = spawnSync('git', args, { cwd, encoding: 'utf8', env: childEnv(env) });
  return { status: r.status == null ? 1 : r.status, stdout: r.stdout || '', stderr: r.stderr || '' };
}

// Invoke the hook script directly (name, url) with a given stdin — for URL
// classification and empty-stdin behavior that does not need a real push.
// GIT_ALLOW_PROTOCOL=file makes any ssh/https `git ls-remote` fail instantly, so
// classifying a real github URL never touches the network (the classification
// line the tests assert on is printed before any remote resolution).
function runHookDirect(name, url, { stdin = '', env = {} } = {}) {
  const r = spawnSync('bash', [hookPath, name, url],
    { input: stdin, encoding: 'utf8', env: childEnv({ GIT_ALLOW_PROTOCOL: 'file', GIT_TERMINAL_PROMPT: '0', ...env }) });
  return { status: r.status == null ? 1 : r.status, stderr: r.stderr || '' };
}

function withHook(args) {
  return ['-c', `core.hooksPath=${hooksDir}`, ...ID, ...args];
}

// Mark a local remote path as "the public repo" for the hook (test-mode only).
function pub(remote, extra = {}) {
  return { PENTACLE_PREPUSH_TEST_MODE: '1', PENTACLE_ALLOWED_PUBLIC_REMOTES: remote, ...extra };
}

function commitFile(work, name, body, msg) {
  fs.writeFileSync(path.join(work, name), body);
  git(work, [...ID, 'add', name]);
  git(work, [...ID, 'commit', '-m', msg]);
}

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
  return { dir, remote, work };
}

function makeForeignBranch(work, name) {
  git(work, [...ID, 'checkout', '--orphan', name]);
  git(work, [...ID, 'rm', '-rf', '.']);
  commitFile(work, 'OTHER', 'a different repo\n', 'foreign root');
}

// ── core accept/reject ───────────────────────────────────────────────────────

test('(a)+(b)+(c) clean public branch is ACCEPTED', () => {
  const sb = sandbox();
  git(sb.work, [...ID, 'checkout', '-b', 'clean']);
  commitFile(sb.work, 'feature.txt', 'work\n', 'a real feature');
  const r = tryGit(sb.work, withHook(['push', 'public', 'clean:refs/heads/clean']), pub(sb.remote));
  assert.equal(r.status, 0, `expected accept, got ${r.status}\n${r.stderr}`);
  assert.match(r.stderr, /pushing to 'public' -> .* \(public repo/);
});

test('(b) a same-name branch refspec (git push public branch) is ACCEPTED', () => {
  const sb = sandbox();
  git(sb.work, [...ID, 'checkout', '-b', 'feat']);
  commitFile(sb.work, 'feat.txt', 'x\n', 'feat');
  const r = tryGit(sb.work, withHook(['push', 'public', 'feat']), pub(sb.remote));
  assert.equal(r.status, 0, `expected same-name branch accept, got ${r.status}\n${r.stderr}`);
});

test('(c) foreign orphan history (extra root) is REJECTED', () => {
  const sb = sandbox();
  makeForeignBranch(sb.work, 'foreign');
  const r = tryGit(sb.work, withHook(['push', 'public', 'foreign:refs/heads/foreign']), pub(sb.remote));
  assert.notEqual(r.status, 0, 'expected foreign history to be refused');
  assert.match(r.stderr, /not a root of public\/main/);
});

test('(c) a merge that pulls foreign history into a clean branch is REJECTED', () => {
  const sb = sandbox();
  git(sb.work, [...ID, 'checkout', '-b', 'merged', 'main']);
  commitFile(sb.work, 'clean.txt', 'clean\n', 'clean work');
  makeForeignBranch(sb.work, 'foreign');
  git(sb.work, [...ID, 'checkout', 'merged']);
  git(sb.work, [...ID, 'merge', '--allow-unrelated-histories', '--no-edit', 'foreign']);
  const r = tryGit(sb.work, withHook(['push', 'public', 'merged:refs/heads/merged']), pub(sb.remote));
  assert.notEqual(r.status, 0, 'expected a foreign-history merge to be refused');
  assert.match(r.stderr, /foreign history/);
});

test('(b) missing refspec (bare push) is REJECTED', () => {
  const sb = sandbox();
  commitFile(sb.work, 'more.txt', 'more\n', 'advance main');
  const r = tryGit(sb.work, ['-c', 'push.default=current', ...withHook(['push', 'public'])], pub(sb.remote));
  assert.notEqual(r.status, 0, 'expected a bare push to be refused');
  assert.match(r.stderr, /no refspec/);
});

test('(b) a matching-branch push (git push public :) is REJECTED', () => {
  const sb = sandbox();
  commitFile(sb.work, 'adv.txt', 'adv\n', 'advance main for matching push');
  const r = tryGit(sb.work, withHook(['push', 'public', ':']), pub(sb.remote));
  assert.notEqual(r.status, 0, 'expected a matching push to be refused');
  assert.match(r.stderr, /matching-branch push/);
});

test('(a) wrong destination for a remote named `public` is REJECTED', () => {
  const sb = sandbox();
  git(sb.work, [...ID, 'checkout', '-b', 'clean2']);
  commitFile(sb.work, 'x.txt', 'x\n', 'feature two');
  // No test-mode: the local path is not github.com/HJK6/pentacle.
  const r = tryGit(sb.work, withHook(['push', 'public', 'clean2:refs/heads/clean2']));
  assert.notEqual(r.status, 0, 'expected a non-public destination to be refused');
  assert.match(r.stderr, /not the public HJK6\/pentacle/);
});

test('(c) FAIL CLOSED: public destination whose main is unresolvable is REJECTED', () => {
  const sb = sandbox({ seedMain: false }); // remote has no `main`
  git(sb.work, [...ID, 'checkout', '-b', 'first']);
  commitFile(sb.work, 'first.txt', 'first\n', 'first work');
  const r = tryGit(sb.work, withHook(['push', 'public', 'first:refs/heads/first']), pub(sb.remote));
  assert.notEqual(r.status, 0, 'expected fail-closed refusal when public main is unresolvable');
  assert.match(r.stderr, /cannot resolve the public repo's live 'main'/);
});

// ── QA-review regressions ────────────────────────────────────────────────────

test('FAIL CLOSED: empty stdin (e.g. BASH_ENV consumed the update records) REJECTS a public push', () => {
  const sb = sandbox();
  makeForeignBranch(sb.work, 'foreign');
  // A BASH_ENV startup script that consumes stdin reproduces the reported
  // fail-open: the hook must then see no records and refuse, not accept.
  const consumer = path.join(sb.dir, 'consume.sh');
  fs.writeFileSync(consumer, 'cat >/dev/null 2>&1 || true\n');
  const r = tryGit(sb.work, withHook(['push', 'public', 'foreign:refs/heads/foreign']),
    pub(sb.remote, { BASH_ENV: consumer }));
  assert.notEqual(r.status, 0, 'expected a public push with consumed stdin to fail closed');
  assert.match(r.stderr, /no verifiable push records|foreign history|no refspec/);
});

test('does NOT trust a stale remote-tracking ref for the public repo', () => {
  const sb = sandbox();
  makeForeignBranch(sb.work, 'foreign');
  const foreignSha = git(sb.work, ['rev-parse', 'HEAD']).trim();
  // Poison the tracking ref so that trusting it would accept the foreign tip.
  git(sb.work, ['update-ref', 'refs/remotes/public/main', foreignSha]);
  const r = tryGit(sb.work, withHook(['push', 'public', 'foreign:refs/heads/foreign']), pub(sb.remote));
  assert.notEqual(r.status, 0, 'expected refusal despite a stale/poisoned tracking ref');
  assert.match(r.stderr, /not a root of public\/main/);
});

test('(b) a `-C <dir>` global option value equal to `push` does not fool the refspec check', () => {
  const sb = sandbox();
  fs.mkdirSync(path.join(sb.work, 'push'));
  commitFile(sb.work, 'z.txt', 'z\n', 'advance for -C push');
  // `git -C push push public`: the first `push` token is the -C value, not the
  // subcommand. A naive parser counts it as a positional and lets the bare push
  // through; the hook must still see no refspec.
  const r = tryGit(sb.work, ['-C', 'push', '-c', 'push.default=current', ...withHook(['push', 'public'])], pub(sb.remote));
  assert.notEqual(r.status, 0, 'expected the bare push to be refused despite `-C push`');
  assert.match(r.stderr, /no refspec/);
});

test('a deletion (push :refs/heads/x) is ALLOWED', () => {
  const sb = sandbox();
  git(sb.work, [...ID, 'checkout', '-b', 'todelete', 'main']);
  commitFile(sb.work, 'd.txt', 'd\n', 'to delete');
  assert.equal(tryGit(sb.work, withHook(['push', 'public', 'todelete:refs/heads/todelete']), pub(sb.remote)).status, 0);
  const del = tryGit(sb.work, withHook(['push', 'public', ':refs/heads/todelete']), pub(sb.remote));
  assert.equal(del.status, 0, `expected deletion to be allowed, got ${del.status}\n${del.stderr}`);
});

test('a multi-ref push with one foreign ref is REJECTED', () => {
  const sb = sandbox();
  git(sb.work, [...ID, 'checkout', '-b', 'good', 'main']);
  commitFile(sb.work, 'g.txt', 'g\n', 'good');
  makeForeignBranch(sb.work, 'bad');
  const r = tryGit(sb.work, withHook(['push', 'public', 'good:refs/heads/good', 'bad:refs/heads/bad']), pub(sb.remote));
  assert.notEqual(r.status, 0, 'expected a mixed clean+foreign push to be refused');
  assert.match(r.stderr, /not a root of public\/main/);
});

test('a foreign tag push is REJECTED', () => {
  const sb = sandbox();
  makeForeignBranch(sb.work, 'foreign');
  git(sb.work, [...ID, 'tag', 'ftag']);
  const r = tryGit(sb.work, withHook(['push', 'public', 'refs/tags/ftag:refs/tags/ftag']), pub(sb.remote));
  assert.notEqual(r.status, 0, 'expected a foreign tag to be refused');
  assert.match(r.stderr, /not a root of public\/main|foreign history/);
});

// ── URL classification (F5/F6) ───────────────────────────────────────────────

test('URL classification recognizes valid public forms and rejects lookalikes', () => {
  const publicForms = [
    'git@github.com:HJK6/pentacle.git',
    'https://github.com/HJK6/pentacle',
    'https://github.com/HJK6/pentacle.git',
    'ssh://git@github.com/HJK6/pentacle.git',
    'ssh://git@github.com:22/HJK6/pentacle.git',
    'https://user:token@github.com/HJK6/pentacle.git',
    'git@GITHUB.COM:HJK6/pentacle',
  ];
  for (const url of publicForms) {
    const r = runHookDirect('anyname', url, { stdin: '' });
    assert.match(r.stderr, /\(public repo/, `expected ${url} classified public`);
  }
  const nonPublic = [
    'git@github.com:HJK6/pentacle-private.git',
    'https://github.com/HJK6/pentacle-private',
    'git@github.com:HJK6/other.git',
    'git@example.com:HJK6/pentacle.git',
  ];
  for (const url of nonPublic) {
    const r = runHookDirect('public', url, { stdin: '' });
    assert.doesNotMatch(r.stderr, /\(public repo/, `expected ${url} NOT classified public`);
    assert.match(r.stderr, /not the public HJK6\/pentacle/, `expected ${url} refused as wrong destination`);
  }
});

test('the test-mode override never reclassifies a pentacle-private lookalike as public', () => {
  const privateUrl = 'file:///tmp/whatever/pentacle-private.git';
  const r = runHookDirect('public', privateUrl, {
    stdin: '',
    env: { PENTACLE_PREPUSH_TEST_MODE: '1', PENTACLE_ALLOWED_PUBLIC_REMOTES: privateUrl },
  });
  assert.doesNotMatch(r.stderr, /\(public repo/, 'pentacle-private must never be treated as public');
  assert.match(r.stderr, /not the public HJK6\/pentacle/);
});

// ── portability guard ────────────────────────────────────────────────────────

test('the hook uses no bash 4+ only features (macOS /bin/bash 3.2 compatibility)', () => {
  const src = fs.readFileSync(hookPath, 'utf8');
  const code = src.split('\n').filter((l) => !l.trimStart().startsWith('#')).join('\n');
  const forbidden = [
    [/\bdeclare\s+-A\b/, 'declare -A'],
    [/\blocal\s+-A\b/, 'local -A'],
    [/\bmapfile\b/, 'mapfile'],
    [/\breadarray\b/, 'readarray'],
    [/\$\{[A-Za-z_][A-Za-z0-9_]*\^\^/, '${var^^}'],
    [/\$\{[A-Za-z_][A-Za-z0-9_]*,,/, '${var,,}'],
  ];
  for (const [re, label] of forbidden) {
    assert.ok(!re.test(code), `hook must not use ${label} (bash 4+ only)`);
  }
});
