'use strict';

// Integration tests for scripts/hooks/pre-push (POSIX sh). Each test builds
// throwaway remotes and a work clone, installs the hook via
// `-c core.hooksPath=…`, and drives real `git push` so the hook sees the argv
// and stdin git gives it. The child environment is isolated (host BASH_ENV/ENV
// and PENTACLE_* scrubbed) so the suite is deterministic on any host; individual
// tests re-add exactly what they exercise.

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
function runHookDirect(name, url, { stdin = '', env = {} } = {}) {
  const r = spawnSync('sh', [hookPath, name, url],
    { input: stdin, encoding: 'utf8', env: childEnv({ GIT_ALLOW_PROTOCOL: 'file', GIT_TERMINAL_PROMPT: '0', ...env }) });
  return { status: r.status == null ? 1 : r.status, stderr: r.stderr || '' };
}
function withHook(args) { return ['-c', `core.hooksPath=${hooksDir}`, ...ID, ...args]; }
function pub(remote, extra = {}) {
  return { PENTACLE_PREPUSH_TEST_MODE: '1', PENTACLE_ALLOWED_PUBLIC_REMOTES: remote, ...extra };
}
function commitFile(work, name, body, msg) {
  fs.writeFileSync(path.join(work, name), body);
  git(work, [...ID, 'add', name]);
  git(work, [...ID, 'commit', '-m', msg]);
}
function sandbox({ seedMain = true, objectFormat = null } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-prepush-'));
  const remote = path.join(dir, 'public.git');
  const work = path.join(dir, 'work');
  const fmt = objectFormat ? [`--object-format=${objectFormat}`] : [];
  execFileSync('git', ['init', '--bare', '-b', 'main', ...fmt, remote], { stdio: 'ignore' });
  execFileSync('git', ['init', '-b', 'main', ...fmt, work], { stdio: 'ignore' });
  commitFile(work, 'README', 'seed\n', 'seed main');
  git(work, ['remote', 'add', 'public', remote]);
  if (seedMain) { git(work, [...ID, 'push', 'public', 'main:refs/heads/main']); git(work, ['fetch', 'public', 'main']); }
  return { dir, remote, work };
}
function makeForeignBranch(work, name) {
  git(work, [...ID, 'checkout', '--orphan', name]);
  git(work, [...ID, 'rm', '-rf', '.']);
  commitFile(work, 'OTHER', 'a different repo\n', 'foreign root');
}

// ── core accept/reject ───────────────────────────────────────────────────────
test('clean public branch is ACCEPTED', () => {
  const sb = sandbox();
  git(sb.work, [...ID, 'checkout', '-b', 'clean']);
  commitFile(sb.work, 'f.txt', 'work\n', 'a real feature');
  const r = tryGit(sb.work, withHook(['push', 'public', 'clean:refs/heads/clean']), pub(sb.remote));
  assert.equal(r.status, 0, `expected accept, got ${r.status}\n${r.stderr}`);
  assert.match(r.stderr, /pushing to 'public' -> .* \(public repo/);
});
test('a same-name branch refspec is ACCEPTED', () => {
  const sb = sandbox();
  git(sb.work, [...ID, 'checkout', '-b', 'feat']);
  commitFile(sb.work, 'a.txt', 'x\n', 'feat');
  assert.equal(tryGit(sb.work, withHook(['push', 'public', 'feat']), pub(sb.remote)).status, 0);
});
test('foreign orphan history is REJECTED', () => {
  const sb = sandbox();
  makeForeignBranch(sb.work, 'foreign');
  const r = tryGit(sb.work, withHook(['push', 'public', 'foreign:refs/heads/foreign']), pub(sb.remote));
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /not a root of public\/main/);
});
test('a merge that pulls foreign history into a clean branch is REJECTED', () => {
  const sb = sandbox();
  git(sb.work, [...ID, 'checkout', '-b', 'merged', 'main']);
  commitFile(sb.work, 'c.txt', 'c\n', 'clean');
  makeForeignBranch(sb.work, 'foreign');
  git(sb.work, [...ID, 'checkout', 'merged']);
  git(sb.work, [...ID, 'merge', '--allow-unrelated-histories', '--no-edit', 'foreign']);
  const r = tryGit(sb.work, withHook(['push', 'public', 'merged:refs/heads/merged']), pub(sb.remote));
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /foreign history/);
});
test('missing refspec (bare push) is REJECTED', () => {
  const sb = sandbox();
  commitFile(sb.work, 'm.txt', 'm\n', 'advance');
  const r = tryGit(sb.work, ['-c', 'push.default=current', ...withHook(['push', 'public'])], pub(sb.remote));
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /no refspec/);
});
test("a matching-branch push (':') is REJECTED", () => {
  const sb = sandbox();
  commitFile(sb.work, 'adv.txt', 'a\n', 'advance');
  const r = tryGit(sb.work, withHook(['push', 'public', ':']), pub(sb.remote));
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /matching-branch push/);
});
test("a force matching push ('+:') is REJECTED", () => {
  const sb = sandbox();
  commitFile(sb.work, 'adv2.txt', 'a\n', 'advance');
  const r = tryGit(sb.work, withHook(['push', 'public', '+:']), pub(sb.remote));
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /matching-branch push/);
});
test('wrong destination for a remote named `public` is REJECTED', () => {
  const sb = sandbox();
  git(sb.work, [...ID, 'checkout', '-b', 'c2']);
  commitFile(sb.work, 'x.txt', 'x\n', 'two');
  const r = tryGit(sb.work, withHook(['push', 'public', 'c2:refs/heads/c2']));
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /not the public HJK6\/pentacle/);
});
test('FAIL CLOSED: public destination whose main is unresolvable is REJECTED', () => {
  const sb = sandbox({ seedMain: false });
  git(sb.work, [...ID, 'checkout', '-b', 'first']);
  commitFile(sb.work, 'first.txt', 'f\n', 'first');
  const r = tryGit(sb.work, withHook(['push', 'public', 'first:refs/heads/first']), pub(sb.remote));
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /cannot resolve the public repo's live 'main'/);
});

// ── env-driven silent-skip immunity (finding 1) ──────────────────────────────
test('immune to a BASH_ENV that consumes stdin (foreign push still REJECTED)', () => {
  const sb = sandbox();
  makeForeignBranch(sb.work, 'foreign');
  const consumer = path.join(sb.dir, 'consume.sh');
  fs.writeFileSync(consumer, 'read _x\n');
  const r = tryGit(sb.work, withHook(['push', 'public', 'foreign:refs/heads/foreign']), pub(sb.remote, { BASH_ENV: consumer }));
  assert.notEqual(r.status, 0, `sh must not read BASH_ENV; got ${r.status}\n${r.stderr}`);
  assert.match(r.stderr, /not a root of public\/main|no verifiable push records/);
});
test('immune to an ENV that consumes stdin (foreign push still REJECTED)', () => {
  const sb = sandbox();
  makeForeignBranch(sb.work, 'foreign');
  const consumer = path.join(sb.dir, 'consume.sh');
  fs.writeFileSync(consumer, 'read _x\n');
  const r = tryGit(sb.work, withHook(['push', 'public', 'foreign:refs/heads/foreign']), pub(sb.remote, { ENV: consumer }));
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /not a root of public\/main|no verifiable push records/);
});
test('immune to a BASH_ENV that exits 0 (no silent skip; foreign push REJECTED)', () => {
  const sb = sandbox();
  makeForeignBranch(sb.work, 'foreign');
  const exiter = path.join(sb.dir, 'exit0.sh');
  fs.writeFileSync(exiter, 'exit 0\n');
  const r = tryGit(sb.work, withHook(['push', 'public', 'foreign:refs/heads/foreign']), pub(sb.remote, { BASH_ENV: exiter }));
  assert.notEqual(r.status, 0, `env exit 0 must not skip the hook; got ${r.status}\n${r.stderr}`);
  assert.match(r.stderr, /not a root of public\/main/);
});

// ── ls-remote exact ref (finding 2) ──────────────────────────────────────────
test('resolves exactly refs/heads/main, not a feature/main pattern match', () => {
  const sb = sandbox();
  // Put a foreign-rooted refs/heads/feature/main on the remote (bypass the hook).
  makeForeignBranch(sb.work, 'foreign');
  git(sb.work, [...ID, 'push', '--no-verify', 'public', 'foreign:refs/heads/feature/main']);
  // Now push the foreign tip: the hook must compare against the real main root,
  // not feature/main, and reject.
  const r = tryGit(sb.work, withHook(['push', 'public', 'foreign:refs/heads/foreign']), pub(sb.remote));
  assert.notEqual(r.status, 0, `must not be fooled by feature/main; got ${r.status}\n${r.stderr}`);
  assert.match(r.stderr, /not a root of public\/main/);
});

// ── stale tracking ref (finding from round 1) ────────────────────────────────
test('does NOT trust a stale/poisoned remote-tracking ref for the public repo', () => {
  const sb = sandbox();
  makeForeignBranch(sb.work, 'foreign');
  git(sb.work, ['update-ref', 'refs/remotes/public/main', git(sb.work, ['rev-parse', 'HEAD']).trim()]);
  const r = tryGit(sb.work, withHook(['push', 'public', 'foreign:refs/heads/foreign']), pub(sb.remote));
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /not a root of public\/main/);
});

// ── argv parsing (findings 4/5) ──────────────────────────────────────────────
test('a `-C <dir>` value equal to `push` does not fool the refspec check', () => {
  const sb = sandbox();
  fs.mkdirSync(path.join(sb.work, 'push'));
  commitFile(sb.work, 'z.txt', 'z\n', 'advance');
  const r = tryGit(sb.work, ['-C', 'push', '-c', 'push.default=current', ...withHook(['push', 'public'])], pub(sb.remote));
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /no refspec/);
});
test('a `-C "<dir with spaces and push>"` value does not fool the refspec check', () => {
  const sb = sandbox();
  fs.mkdirSync(path.join(sb.work, 'dir with push'));
  commitFile(sb.work, 'z2.txt', 'z\n', 'advance');
  const r = tryGit(sb.work, ['-C', 'dir with push', '-c', 'push.default=current', ...withHook(['push', 'public'])], pub(sb.remote));
  assert.notEqual(r.status, 0, `spaces in a -C path must not create a phantom refspec; got ${r.status}\n${r.stderr}`);
  assert.match(r.stderr, /no refspec|could not read\/parse/);
});

// ── record/sha validation (finding 3) ────────────────────────────────────────
test('a malformed / non-full-hex tip sha is fail-closed for a public push', () => {
  // Drive the hook directly with a crafted stdin record (git would never emit a
  // bogus sha, so exercise the validation path directly). A real github URL makes
  // is_public true without touching the network for classification.
  const r = runHookDirect('public', 'git@github.com:HJK6/pentacle.git', {
    stdin: 'refs/heads/x deadbeef refs/heads/x 0000000000000000000000000000000000000000\n',
  });
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /not a full object id|no verifiable push records|cannot resolve the public repo/);
});

// ── strict record validation (round-3 findings) ──────────────────────────────
const HEX40 = 'a'.repeat(40);
test('a public record with extra fields is fail-closed', () => {
  const r = runHookDirect('public', 'git@github.com:HJK6/pentacle.git',
    { stdin: `refs/heads/x ${HEX40} refs/heads/x ${HEX40} EXTRA\n` });
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /malformed push record|no verifiable push records|cannot resolve the public repo/);
});
test('a public record with an invalid remote sha is fail-closed', () => {
  const r = runHookDirect('public', 'git@github.com:HJK6/pentacle.git',
    { stdin: `refs/heads/x ${HEX40} refs/heads/x not-a-sha\n` });
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /malformed push record|cannot resolve the public repo/);
});
test('a public record with a non-refs/* ref is fail-closed', () => {
  const r = runHookDirect('public', 'git@github.com:HJK6/pentacle.git',
    { stdin: `badref ${HEX40} refs/heads/x ${HEX40}\n` });
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /malformed local ref|malformed push record|cannot resolve the public repo/);
});

// ── deletions / multi-ref / tags ─────────────────────────────────────────────
test('a deletion (push :refs/heads/x) is ALLOWED', () => {
  const sb = sandbox();
  git(sb.work, [...ID, 'checkout', '-b', 'todelete', 'main']);
  commitFile(sb.work, 'd.txt', 'd\n', 'to delete');
  assert.equal(tryGit(sb.work, withHook(['push', 'public', 'todelete:refs/heads/todelete']), pub(sb.remote)).status, 0);
  const del = tryGit(sb.work, withHook(['push', 'public', ':refs/heads/todelete']), pub(sb.remote));
  assert.equal(del.status, 0, `expected deletion allowed, got ${del.status}\n${del.stderr}`);
});
test('a SHA-256 deletion (64-zero old id) is ALLOWED, not false-rejected', () => {
  const sb = sandbox({ objectFormat: 'sha256' });
  git(sb.work, [...ID, 'checkout', '-b', 'todelete', 'main']);
  commitFile(sb.work, 'd.txt', 'd\n', 'to delete');
  assert.equal(tryGit(sb.work, withHook(['push', 'public', 'todelete:refs/heads/todelete']), pub(sb.remote)).status, 0);
  const del = tryGit(sb.work, withHook(['push', 'public', ':refs/heads/todelete']), pub(sb.remote));
  assert.equal(del.status, 0, `sha256 deletion must be allowed, got ${del.status}\n${del.stderr}`);
});

test('a multi-ref push with one foreign ref is REJECTED', () => {
  const sb = sandbox();
  git(sb.work, [...ID, 'checkout', '-b', 'good', 'main']);
  commitFile(sb.work, 'g.txt', 'g\n', 'good');
  makeForeignBranch(sb.work, 'bad');
  const r = tryGit(sb.work, withHook(['push', 'public', 'good:refs/heads/good', 'bad:refs/heads/bad']), pub(sb.remote));
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /not a root of public\/main/);
});
test('a foreign tag push is REJECTED', () => {
  const sb = sandbox();
  makeForeignBranch(sb.work, 'foreign');
  git(sb.work, [...ID, 'tag', 'ftag']);
  const r = tryGit(sb.work, withHook(['push', 'public', 'refs/tags/ftag:refs/tags/ftag']), pub(sb.remote));
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /not a root of public\/main|foreign history/);
});

// ── URL classification (findings 6/7) ────────────────────────────────────────
test('URL classification recognizes public forms (case-insensitive) and rejects lookalikes', () => {
  const publicForms = [
    'git@github.com:HJK6/pentacle.git',
    'https://github.com/HJK6/pentacle',
    'ssh://git@github.com:22/HJK6/pentacle.git',
    'https://user:token@github.com/HJK6/pentacle.git',
    'git@GITHUB.COM:HJK6/pentacle',
    'https://github.com/hjk6/pentacle.git',      // lowercase owner/repo
    'https://github.com/HJK6/PENTACLE.git',      // uppercase repo
  ];
  for (const url of publicForms) {
    assert.match(runHookDirect('anyname', url).stderr, /\(public repo/, `expected public: ${url}`);
  }
  const nonPublic = [
    'git@github.com:HJK6/pentacle-private.git',
    'git@github.com:HJK6/PENTACLE-PRIVATE.git',
    'https://github.com/HJK6/other',
    'git@example.com:HJK6/pentacle.git',
  ];
  for (const url of nonPublic) {
    const r = runHookDirect('public', url);
    assert.doesNotMatch(r.stderr, /\(public repo/, `expected NOT public: ${url}`);
    assert.match(r.stderr, /not the public HJK6\/pentacle/, `expected refused: ${url}`);
  }
});
test('the test-mode override never reclassifies a pentacle-private lookalike (any case)', () => {
  for (const u of ['file:///tmp/x/pentacle-private.git', 'file:///tmp/x/PENTACLE-PRIVATE.git']) {
    const r = runHookDirect('public', u, { env: { PENTACLE_PREPUSH_TEST_MODE: '1', PENTACLE_ALLOWED_PUBLIC_REMOTES: u } });
    assert.doesNotMatch(r.stderr, /\(public repo/, `must never treat private as public: ${u}`);
    assert.match(r.stderr, /not the public HJK6\/pentacle/);
  }
});

// ── portability ──────────────────────────────────────────────────────────────
test('the hook is POSIX sh (shebang + `sh -n` clean)', () => {
  const src = fs.readFileSync(hookPath, 'utf8');
  assert.match(src.split('\n')[0], /^#!\/bin\/sh$/, 'hook must be #!/bin/sh');
  const chk = spawnSync('sh', ['-n', hookPath], { encoding: 'utf8' });
  assert.equal(chk.status, 0, `sh -n failed: ${chk.stderr}`);
});
