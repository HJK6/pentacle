#!/bin/sh
# Disposable-only test of pushguard-hook / pushguard-install.
# Mocks curl, ssh and the git transport (remote.<n>.vcs helper -> a local bare repo) via PATH; GIT_ALLOW_PROTOCOL blocks
# every real network transport, so no test can reach GitHub. No real public (or private) writes.
# usage: sh tests/run.sh [CANDIDATE_DIR]      RED mode: PUSHGUARD_RED=1 skips install -> guard assertions must FAIL
set -u
CAND=$(cd "${1:-$(dirname "$0")/..}" && pwd -P)
T=$(mktemp -d "${TMPDIR:-/tmp}/pgt.XXXXXX") || exit 2
trap 'rm -rf "${T:?}"' EXIT
pass=0; fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
bad() { fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want [$3] got [$2])"; fi; }
contains() { case $2 in *"$3"*) ok "$1";; *) bad "$1 [got: $2]";; esac; }

# ---- mock tools on PATH ----
mkdir -p "$T/mock"
cat > "$T/mock/curl" <<'M'
#!/bin/sh
printf '%s\n' "$*" >> "${STUB_LOG:-/dev/null}"
slug=$(printf '%s' "$*" | sed -n 's#.*https://github.com/\([^ ]*\)\.git/info/refs.*#\1#p')
code=$(sed -n "s#^$slug ##p" "${STUB_VIS:-/dev/null}" | head -n 1)
printf '%s' "${code:-000}"
[ "${code:-000}" = 000 ] && exit 6
exit 0
M
cat > "$T/mock/ssh" <<'M'
#!/bin/sh
h=$(printf '%s\n' "$@" | tail -n 1)
real=$(sed -n "s#^$h ##p" "${STUB_SSH:-/dev/null}" | head -n 1)
printf 'user git\nhostname %s\nport 22\n' "${real:-$h}"
M
# fake git transport: any remote with remote.<n>.vcs=pgfake talks to the local bare repo $PG_BARE (fails closed if unset)
cat > "$T/mock/git-remote-pgfake" <<'M'
#!/bin/sh
exec git remote-ext "$1" "git %s ${PG_BARE:?}"
M
chmod +x "$T/mock/curl" "$T/mock/ssh" "$T/mock/git-remote-pgfake"
export PATH="$T/mock:$PATH" STUB_LOG="$T/curl.log" STUB_VIS="$T/vis" STUB_SSH="$T/sshmap"
export GIT_ALLOW_PROTOCOL=file:pgfake GIT_TERMINAL_PROMPT=0
printf 'pg-owner/pub-fixture 200\npg-owner/priv-fixture 401\npg-owner/flaky 503\npg-owner/ratelimited 403\npg-owner/gone 404\n' > "$T/vis"
printf 'gh-alias github.com\ngh-other example.org\npeer 192.0.2.7\n' > "$T/sshmap"

# ---- isolated git env ----
export HOME="$T/home"; mkdir -p "$HOME"; export GIT_CONFIG_NOSYSTEM=1 XDG_CONFIG_HOME="$T/home/.config"
unset GIT_DIR GIT_WORK_TREE
git config --global user.name t; git config --global user.email t@t; git config --global init.defaultBranch main
export PUSHGUARD_STATE="$T/state"
GD="$T/guard dir"                       # space in the path on purpose
H="$CAND/pushguard-hook"

# ===== A. classification matrix (no git) =====
cl() { out=$(sh "$H" classify "$2"); check "classify: $1" "$out" "$3"; }
cl https-plain          https://github.com/o/r                                  "github o/r"
cl https-dotgit-slash   https://github.com/o/r.git/                             "github o/r"
cl https-userinfo       https://x-access-token:SECRET@github.com/o/r.git        "github o/r"
cl https-case           https://GitHub.com/O/R                                  "github O/R"
cl ssh-url              ssh://git@github.com/o/r.git                            "github o/r"
cl ssh-port443          ssh://git@ssh.github.com:443/o/r.git                    "github o/r"
cl scp-like             git@github.com:o/r.git                                  "github o/r"
cl scp-no-user          github.com:o/r                                          "github o/r"
cl scp-bracket-host     '[github.com]:o/r.git'                                  "github o/r"
cl scp-bracket-user     'git@[github.com]:o/r.git'                              "github o/r"
cl scp-trailing-dot     'git@github.com.:o/r'                                   "github o/r"
cl https-trailing-dot   https://github.com./o/r                                 "github o/r"
cl ssh-alias-github     git@gh-alias:o/r.git                                    "github o/r"
cl ssh-alias-other      git@gh-other:o/r.git                                    "non-github"
cl inter-host           peer:repos/foo.git                                      "non-github"
cl gitlab               git@gitlab.com:g/r.git                                  "non-github"
cl other-https          https://example.com/o/r.git                             "non-github"
cl gist                 https://gist.github.com/o/abcdef.git                    "unclassifiable-github"
cl one-segment          https://github.com/o                                    "unclassifiable-github"
cl three-segments       https://github.com/o/r/extra                            "unclassifiable-github"
cl weird-chars          'https://github.com/o/r;x'                              "unclassifiable-github"
cl dotdot               https://github.com/o/..                                 "unclassifiable-github"
cl dash-host            'ssh://-oProxyCommand=x/o/r'                            "unclassifiable-github"
cl abs-path             /srv/git/repo.git                                       "non-github"
cl rel-path             ../r.git                                                "non-github"
cl file-url             file:///srv/git/repo.git                                "non-github"
cl windows-backslash    'C:\repos\x.git'                                        "non-github"
cl windows-slash        'C:/repos/x.git'                                        "non-github"

# ===== B. visibility mapping =====
vis() { out=$(sh "$H" check "$2"); check "visibility: $1" "$out" "$3"; }
vis public        https://github.com/pg-owner/pub-fixture     "github pg-owner/pub-fixture public"
vis private       git@github.com:pg-owner/priv-fixture.git    "github pg-owner/priv-fixture notpublic"
vis nonexistent   https://github.com/pg-owner/gone            "github pg-owner/gone unknown"
vis 5xx           https://github.com/pg-owner/flaky           "github pg-owner/flaky unknown"
vis ratelimit     https://github.com/pg-owner/ratelimited     "github pg-owner/ratelimited unknown"
vis network-error https://github.com/pg-owner/unmapped        "github pg-owner/unmapped unknown"

export HOOK_EXIT=0
# ===== helpers =====
newrepo() { # $1=name: work repo + bare "GitHub" backend
  rm -rf "${T:?}/${1:?}" "${T:?}/${1:?}.git"; git init -q --bare "$T/$1.git"; git init -q "$T/$1"
  ( cd "$T/$1" && echo a > f && git add f && git commit -qm c1 ); }
ghremote() { # $1=repo $2=remote name $3=github url ; transport goes to the repo's bare backend
  ( cd "$T/$1" && git remote add "$2" "$3" && git config remote."$2".vcs pgfake ); }
hooklog() { # $1=hooks dir $2=hook name
  mkdir -p "$1"
  printf '#!/bin/sh\n{ printf "%s args=[%%s] stdin=[" "$*"; cat | tr "\\n" ";"; echo "]"; } >> "%s"\nexit ${HOOK_EXIT:-0}\n' "$2" "$T/hook.log" > "$1/$2"
  chmod +x "$1/$2"; }
bare() { git --git-dir="$T/$1.git" rev-parse -q --verify refs/heads/main 2>/dev/null | cut -c1-7; }

# ===== C. install, real-git destination enforcement =====
before=$(git config --global --list | sort)
if [ "${PUSHGUARD_RED:-0}" != 1 ]; then
  out=$(sh "$CAND/pushguard-install" install --hint "use the designated relay" "$GD"); check "install reports" "${out%%:*}" "installed"
  out=$(sh "$CAND/pushguard-install" install "$GD"); check "install idempotent" "${out%%:*}" "installed"
fi
newrepo w1
ghremote w1 pub   https://github.com/pg-owner/pub-fixture.git
ghremote w1 priv  https://github.com/pg-owner/priv-fixture.git
ghremote w1 flaky https://github.com/pg-owner/flaky.git
( cd "$T/w1" && git remote add loc "$T/w1.git" )
: > "$T/hook.log"; hooklog "$T/w1/.git/hooks" pre-push

PG_BARE="$T/w1.git"; export PG_BARE
out=$(cd "$T/w1" && git push pub main 2>&1); rc=$?
contains "public: refused with reason" "$out" "REFUSED direct push: pg-owner/pub-fixture is a public repository"
contains "public: refusal carries the installed hint" "$out" "pushguard: use the designated relay"
ghremote w1 weird "https://u:p@ss@gist.github.com/o/r.git"
out=$(cd "$T/w1" && git push weird main 2>&1 | grep "^pushguard:"); case $out in *ss@*|*p@ss*) bad "refusal message leaks userinfo [$out]";; *REFUSED*) ok "refusal message redacts userinfo up to the last @";; *) bad "weird url [$out]";; esac
[ "$rc" -ne 0 ] && ok "public: non-zero exit" || bad "public: non-zero exit"
check "public: remote ref not updated" "$(bare w1)" ""
check "public: original hook NOT run when guard refuses" "$(cat "$T/hook.log")" ""
out=$(cd "$T/w1" && git push flaky main 2>&1); contains "unknown visibility: refused" "$out" "could not be verified"
check "unknown: remote ref not updated" "$(bare w1)" ""
out=$(cd "$T/w1" && git push priv main 2>&1); rc=$?
check "private: push succeeds" "$rc" "0"
check "private: remote ref updated" "$([ -n "$(bare w1)" ] && echo yes)" "yes"
contains "private: original pre-push ran with same args" "$(cat "$T/hook.log")" "pre-push args=[priv https://github.com/pg-owner/priv-fixture.git] stdin=[refs/heads/main "
( cd "$T/w1" && git tag v1 )
out=$(cd "$T/w1" && git push pub v1 2>&1);               contains "public: tag push refused" "$out" "REFUSED"
out=$(cd "$T/w1" && git push pub :refs/heads/main 2>&1); contains "public: deletion refused" "$out" "REFUSED"
out=$(cd "$T/w1" && git push pub 2>&1);                  contains "public: bare 'git push' refused" "$out" "REFUSED"
out=$(cd "$T/w1" && git push --dry-run pub main 2>&1);   contains "public: --dry-run refused (local, mock transport)" "$out" "REFUSED"
: > "$T/hook.log"; HOOK_EXIT=3; hooklog "$T/w1/.git/hooks" pre-push; ( cd "$T/w1" && echo b >> f && git commit -qam c2 )
out=$(cd "$T/w1" && git push priv main 2>&1); rc=$?; HOOK_EXIT=0; hooklog "$T/w1/.git/hooks" pre-push
check "chain: original hook exit 3 blocks the push" "$rc" "1"
check "chain: ref NOT advanced when original hook fails" "$(git --git-dir="$T/w1.git" rev-parse -q --verify refs/heads/main | cut -c1-7)" "$(git -C "$T/w1" rev-parse -q --verify HEAD~1 | cut -c1-7)"
: > "$T/curl.log"; out=$(cd "$T/w1" && git push loc main 2>&1); check "local remote pushes" "$?" "0"
check "guard made no curl call for a local-path remote" "$(grep -c . "$T/curl.log")" "0"
grep -qiE 'authorization|bearer|token|netrc|-u |--user' "$T/curl.log" && bad "curl argv carries no credential material" || ok "curl argv carries no credential material"
cargs=$(sh "$H" check https://github.com/pg-owner/priv-fixture >/dev/null; tail -n 1 "$T/curl.log")
case $cargs in "-q "*) ok "curl invoked with -q (ignores curlrc)";; *) bad "curl -q [$cargs]";; esac

# ===== C2. live-proof (live mode) must not write a global identity =====
H3="$T/h3"; mkdir -p "$H3"; HOME="$H3" git config --global core.hooksPath "$GD"
out=$(HOME="$H3" PUSHGUARD_PROOF_PUBLIC=pg-owner/pub-fixture PUSHGUARD_PROOF_PRIVATE=pg-owner/priv-fixture sh "$CAND/tests/live-proof.sh" live 2>&1)
contains "live-proof live passes against the installed guard (mocked lookups)" "$out" "RESULT: 8 passed, 0 failed"
check "live-proof live leaves the global git identity untouched" "$(HOME="$H3" git config --global --get user.name || echo unset)" "unset"

# ===== D0. default install is pre-push only: no hook-existence side effects (push-to-checkout / updateInstead) =====
rm -rf "${T:?}/upd" "${T:?}/updc"; git init -q -b main "$T/upd"; ( cd "$T/upd" && echo a > f && git add f && git commit -qm a && git config receive.denyCurrentBranch updateInstead )
git clone -q "$T/upd" "$T/updc"; ( cd "$T/updc" && echo b > f && git commit -qam b && git push -q origin main >/dev/null 2>&1 )
check "updateInstead push updates the target work tree" "$(cat "$T/upd/f")" "b"
check "updateInstead target work tree stays clean" "$(git -C "$T/upd" status --porcelain)" ""
check "default install creates only the pre-push stub" "$(ls "$GD" | tr '\n' ' ')" "pre-push pushguard-hook "

# ===== D. non-pre-push hooks preserved under the global hooksPath (extra stubs requested explicitly) =====
newrepo w2; sh "$CAND/pushguard-install" coverage "$T" >/dev/null 2>&1   # baseline run must not crash
printf '#!/bin/sh\nexit 0\n' > "$T/w2/.git/hooks/pre-commit"; chmod +x "$T/w2/.git/hooks/pre-commit"
sh "$CAND/pushguard-install" coverage "$T" > "$T/cov.txt" 2>&1; rc=$?
check "coverage flags an original hook that has no stub (HOOK-GAP, exit 1)" "$rc:$(grep -c "HOOK-GAP $T/w2 " "$T/state/coverage.last")" "1:1"
sh "$CAND/pushguard-install" install --hooks "pre-commit commit-msg post-commit post-receive" "$GD" >/dev/null
sh "$CAND/pushguard-install" coverage "$T/w2" >/dev/null 2>&1; check "no HOOK-GAP once the stub exists" "$(grep -c HOOK-GAP "$T/state/coverage.last")" "0"
newrepo w2; : > "$T/hook.log"; for hk in pre-commit commit-msg post-commit; do hooklog "$T/w2/.git/hooks" "$hk"; done
( cd "$T/w2" && echo x >> f && git commit -qam c2 )
check "original pre-commit/commit-msg/post-commit each ran once via guard dir" "$(grep -c . "$T/hook.log")" "3"
grep -q '^commit-msg args=\[.*COMMIT_EDITMSG\]' "$T/hook.log" && ok "commit-msg receives its message-file argument" || bad "commit-msg argument"
HOOK_EXIT=1; hooklog "$T/w2/.git/hooks" pre-commit; ( cd "$T/w2" && echo y >> f && git commit -qam c3 >/dev/null 2>&1 ); rc=$?; HOOK_EXIT=0
check "original pre-commit failure still blocks commit" "$rc" "1"
rm -rf "${T:?}/srv.git"; git init -q --bare "$T/srv.git"; hooklog "$T/srv.git/hooks" post-receive; : > "$T/hook.log"
( cd "$T/w2" && git reset -q --hard && git push -q "$T/srv.git" main >/dev/null 2>&1 )
grep -q '^post-receive' "$T/hook.log" && ok "server-side post-receive in a bare repo chained" || bad "post-receive chain"

# ----- D2. stale stubs, arguments, coverage cwd, unsupported originals -----
( cd "$T/w2" && sh "$CAND/pushguard-install" coverage "$T/w2" >/dev/null 2>&1 ); check "coverage run from inside a repo gives no false HOOK-GAP" "$(grep -c HOOK-GAP "$T/state/coverage.last")" "0"
sh "$CAND/pushguard-install" install "$GD" >/dev/null
check "reinstall with the default list removes stale stubs" "$(ls "$GD" | tr '\n' ' ')" "pre-push pushguard-hook "
sh "$CAND/pushguard-install" install --hooks >/dev/null 2>&1; check "install --hooks without a value is a usage error (64)" "$?" "64"
sh "$CAND/pushguard-install" install "$GD" extra >/dev/null 2>&1; check "install with two directories is a usage error (64)" "$?" "64"
sh "$CAND/pushguard-install" coverage >/dev/null 2>&1; check "coverage without roots is a usage error (64)" "$?" "64"
mkdir -p "$T/emptyroot"; sh "$CAND/pushguard-install" coverage "$T/emptyroot" >/dev/null 2>&1; check "coverage with no checkout found is not clean (exit 1)" "$?" "1"
newrepo w4; printf '#!/bin/sh\nexit 0\n' > "$T/w4/.git/hooks/push-to-checkout"; chmod +x "$T/w4/.git/hooks/push-to-checkout"
sh "$CAND/pushguard-install" coverage "$T/w4" >/dev/null 2>&1; check "coverage reports an original push-to-checkout as UNSUPPORTED (exit 1)" "$?:$(grep -c '^UNSUPPORTED' "$T/state/coverage.last")" "1:1"
sh "$CAND/pushguard-install" install --hooks push-to-checkout "$GD" >/dev/null 2>&1; check "install refuses to stub push-to-checkout" "$?" "1"

# ===== E. fresh clone + linked worktree covered =====
git clone -q "$T/w2" "$T/clone" 2>/dev/null
ghremote clone pub https://github.com/pg-owner/pub-fixture.git
out=$(cd "$T/clone" && git push pub HEAD:refs/heads/z 2>&1); contains "fresh clone covered" "$out" "REFUSED"
( cd "$T/w2" && git worktree add -q "$T/wt2" -b wtb )
( cd "$T/wt2" && git remote add pub2 https://github.com/pg-owner/pub-fixture.git && git config remote.pub2.vcs pgfake )
out=$(cd "$T/wt2" && git push pub2 wtb 2>&1); contains "linked worktree covered" "$out" "REFUSED"

# ===== F. local hooksPath override: coverage detects, integrate fixes, project hook kept, rollback exact =====
newrepo w3; mkdir -p "$T/w3/scripts/hooks"; : > "$T/hook.log"; hooklog "$T/w3/scripts/hooks" pre-push
( cd "$T/w3" && git config core.hooksPath scripts/hooks )
ghremote w3 pub  https://github.com/pg-owner/pub-fixture.git
ghremote w3 priv https://github.com/pg-owner/priv-fixture.git
PG_BARE="$T/w3.git"; export PG_BARE
out=$(cd "$T/w3" && git push pub main 2>&1); case $out in *REFUSED*) bad "override repo should be uncovered before integrate";; *) ok "override repo bypasses the global guard (the documented gap)";; esac
sh "$CAND/pushguard-install" coverage "$T" > "$T/cov.txt" 2>&1; rc=$?
check "coverage exits 1 and flags exactly the override repo" "$rc:$(grep -c "UNCOVERED $T/w3 " "$T/state/coverage.last"):$(grep -c UNCOVERED "$T/state/coverage.last")" "1:1:1"
sh "$CAND/pushguard-install" integrate "$T/w3" > /dev/null
out=$(cd "$T/w3" && git push pub main 2>&1); contains "integrated override repo refuses public" "$out" "REFUSED"
: > "$T/hook.log"; ( cd "$T/w3" && git push priv main >/dev/null 2>&1 )
contains "integrated repo: project hook (relative scripts/hooks) runs for private" "$(cat "$T/hook.log")" "pre-push args=[priv "
sh "$CAND/pushguard-install" coverage "$T/w3" > /dev/null 2>&1; check "coverage clean after integrate" "$?" "0"
( cd "$T/w3" && git config pushguard.originalHooksPath "$GD" && git push priv main >/dev/null 2>&1 ); ok "no recursion when original == guard dir (returns)"
( cd "$T/w3" && git config pushguard.originalHooksPath scripts/hooks )

# ----- lost-state guard (guard still global here) -----
mv "$T/state/preimage.log" "$T/state/preimage.log.hidden"
sh "$CAND/pushguard-install" install "$GD" >/dev/null 2>&1; check "reinstall with lost preimage log refuses (exit 1)" "$?" "1"
mv "$T/state/preimage.log.hidden" "$T/state/preimage.log"

# ===== F2. absolute preimages, failing restore keeps the log =====
mkdir -p "$T/pp/my repo"; git init -q -b main "$T/pp/my repo"; ( cd "$T/pp/my repo" && git config core.hooksPath scripts/hooks )
( cd "$T/pp" && sh "$CAND/pushguard-install" integrate "my repo" >/dev/null )
PHY=$(cd "$T/pp/my repo" && pwd -P); TABC=$(printf '\t'); check "integrate with a spaced relative path records an absolute preimage" "$(grep -c "^repo${TABC}${PHY}${TABC}local${TABC}scripts/hooks" "$T/state/preimage.log" | tr -d ' ')" "1"
mkdir -p "$T/gone"; git init -q -b main "$T/gone/r"; ( cd "$T/gone/r" && git config core.hooksPath scripts/hooks ); sh "$CAND/pushguard-install" integrate "$T/gone/r" >/dev/null
mv "$T/gone/r" "$T/gone/r.moved"
: > "$T/pp/my repo/.git/config.lock"
( cd / && sh "$CAND/pushguard-install" uninstall >/dev/null 2>"$T/un.err" ); rc=$?
check "uninstall with a failing restore exits non-zero" "$rc" "1"
[ -s "$T/state/preimage.log" ] && [ -d "$GD" ] && ok "failed uninstall keeps the log and the guard dir" || bad "failed uninstall must keep log + guard dir"
rm -f "$T/pp/my repo/.git/config.lock"

# ===== F3. multi-value local override integrates =====
newrepo w5; ( cd "$T/w5" && git config core.hooksPath scripts/hooks && git config --add core.hooksPath other/hooks )
sh "$CAND/pushguard-install" integrate "$T/w5" >/dev/null 2>&1; check "integrate handles a repo with two local hooksPath values" "$?:$(git -C "$T/w5" config --get-all core.hooksPath | grep -c .)" "0:1"

# ===== G. rollback restores config exactly =====
git config --global --add core.hooksPath "$T/extra-second-value"
( cd / && sh "$CAND/pushguard-install" uninstall > /dev/null 2>&1 ); check "uninstall succeeds from another cwd after the failure is repaired (even with a second global hooksPath value)" "$?" "0"
check "uninstall skips a vanished repo without failing" "$(ls "$T/gone/r.moved" >/dev/null 2>&1 && echo ok)" "ok"
check "uninstall restored the spaced repo" "$(git -C "$T/pp/my repo" config --get core.hooksPath)" "scripts/hooks"
after=$(git config --global --list | sort)
check "uninstall restores global git config exactly" "$after" "$before"
check "uninstall restores integrated repo hooksPath" "$(git -C "$T/w3" config --get core.hooksPath)" "scripts/hooks"
check "uninstall removes pushguard.originalHooksPath" "$(git -C "$T/w3" config --get pushguard.originalHooksPath 2>/dev/null || echo unset)" "unset"
check "uninstall removes guard dir" "$([ -d "$GD" ] && echo present || echo gone)" "gone"

printf '\nRESULT: %s passed, %s failed\n' "$pass" "$fail"; [ "$fail" -eq 0 ]
