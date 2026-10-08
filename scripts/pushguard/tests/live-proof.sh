#!/bin/sh
# Host proof for the installed guard. Disposable repos only; the git transport is a LOCAL fake (remote.<n>.vcs helper ->
# local bare repo) and GIT_ALLOW_PROTOCOL blocks every real network transport, so nothing can reach GitHub for writing.
# The only network use is the guard's own anonymous read-only visibility lookups (real curl) for the two repos named below.
# usage: sh tests/live-proof.sh live            (current HOME; guard must already be installed)
#        sh tests/live-proof.sh sim [CAND_DIR]  (temp HOME + temp install: rehearsal of this script)
set -u
MODE=${1:-live}
PUB=${PUSHGUARD_PROOF_PUBLIC:-}     # owner/repo of a known PUBLIC repo (read-only lookup), supplied by the operator of the host
PRIV=${PUSHGUARD_PROOF_PRIVATE:-}   # owner/repo of a known PRIVATE repo (lookup only; nothing is pushed to it)
[ -n "$PUB" ] && [ -n "$PRIV" ] || { echo "set PUSHGUARD_PROOF_PUBLIC and PUSHGUARD_PROOF_PRIVATE (owner/repo)"; exit 2; }
T=$(mktemp -d "${TMPDIR:-/tmp}/pgl.XXXXXX") || exit 2
trap 'rm -rf "${T:?}"' EXIT
pass=0; fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
bad() { fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }
contains() { case $2 in *"$3"*) ok "$1";; *) bad "$1 [got: $2]";; esac; }

if [ "$MODE" = sim ]; then
  CAND=$(cd "${2:-$(dirname "$0")/..}" && pwd -P)
  export HOME="$T/home"; mkdir -p "$HOME"; export GIT_CONFIG_NOSYSTEM=1 PUSHGUARD_STATE="$T/state"
  git config --global user.name t; git config --global user.email t@t
  sh "$CAND/pushguard-install" install "$T/guard" >/dev/null || { echo "sim install failed"; exit 2; }
fi
git config --global --get core.hooksPath >/dev/null || { echo "guard not installed (no global core.hooksPath)"; exit 2; }

mkdir -p "$T/mock"
printf '#!/bin/sh\nexec git remote-ext "$1" "git %%s ${PG_BARE:?}"\n' > "$T/mock/git-remote-pgfake"; chmod +x "$T/mock/git-remote-pgfake"
export PATH="$T/mock:$PATH" GIT_ALLOW_PROTOCOL=file:pgfake GIT_TERMINAL_PROMPT=0
git init -q --bare -b main "$T/b.git"; git init -q -b main "$T/w"; ( cd "$T/w" && git config user.name t && git config user.email t@t && echo a > f && git add f && git commit -qm c1 )
export PG_BARE="$T/b.git"
( cd "$T/w" && git remote add pub "https://github.com/$PUB.git" && git config remote.pub.vcs pgfake \
             && git remote add priv "git@github.com:$PRIV.git" && git config remote.priv.vcs pgfake && git remote add loc "$T/b.git" )
mkdir -p "$T/w/.git/hooks"; printf '#!/bin/sh\necho "ORIGINAL-PRE-PUSH ran" >> "%s"\n' "$T/orig.log" > "$T/w/.git/hooks/pre-push"; chmod +x "$T/w/.git/hooks/pre-push"
head_ref() { git --git-dir="$T/b.git" rev-parse -q --verify refs/heads/main 2>/dev/null | cut -c1-7; }

out=$(cd "$T/w" && git push pub main 2>&1); contains "public destination refused" "$out" "REFUSED direct push: $PUB is a public repository"
[ -z "$(head_ref)" ] && ok "public: no ref created on the (local fake) remote" || bad "public: ref was created"
[ ! -s "$T/orig.log" ] && ok "public: original pre-push not run after a refusal" || bad "public: original ran"
out=$(cd "$T/w" && git push priv main 2>&1); rc=$?
[ "$rc" -eq 0 ] && ok "private destination allowed (exit 0)" || bad "private allowed [$out]"
[ -n "$(head_ref)" ] && ok "private: ref updated on the local fake remote" || bad "private: ref not updated"
contains "private: original pre-push ran after the guard" "$(cat "$T/orig.log" 2>/dev/null)" "ORIGINAL-PRE-PUSH ran"
( cd "$T/w" && echo b >> f && git commit -qam c2 ); out=$(cd "$T/w" && git push loc main 2>&1); [ $? -eq 0 ] && ok "local-path remote unaffected" || bad "local remote [$out]"
git clone -q "$T/b.git" "$T/c" 2>/dev/null; ( cd "$T/c" && git remote add pub "https://github.com/$PUB.git" && git config remote.pub.vcs pgfake )
out=$(cd "$T/c" && git push pub HEAD:refs/heads/probe 2>&1); contains "fresh clone refused" "$out" "REFUSED"

printf '\nRESULT: %s passed, %s failed (mode=%s, git %s)\n' "$pass" "$fail" "$MODE" "$(git --version | sed 's/git version //')"; [ "$fail" -eq 0 ]
