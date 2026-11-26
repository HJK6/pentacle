#!/bin/zsh
# Switch a clean runtime checkout, restart once, and prove or put back.
set -u

say() { print -r -- "$(date '+%H:%M:%S') $*"; }
refuse() { say "REFUSED: $*"; exit 2; }
human() { say "NEEDS A HUMAN: $*"; exit 5; }

(( $# == 1 || $# == 2 )) || refuse "usage: plain_release.sh TARGET [--dry-run]"
TARGET=$1
DRY=${2:-}
[[ $# == 1 || "$DRY" == --dry-run ]] || refuse "unknown argument: $DRY"
[[ "$TARGET" =~ '^[0-9a-f]{40}$' ]] || refuse "target must be a different full sha"

RT=${PENTACLE_RELEASE_RUNTIME_CHECKOUT:-}
UNIT=${PENTACLE_RELEASE_LAUNCHD_LABEL:-}
PY=${PENTACLE_RELEASE_RUNTIME_PYTHON:-}
DAEMON_URL=${PENTACLE_RELEASE_DAEMON_URL:-}
PROBE=${0:A:h}/welcome_probe.py
# Only the helper wrapper sets this; it is a path, never evaluated as code.
PUTBACK_WORK=${PENTACLE_RELEASE_PUTBACK_WORK:-}
[[ -n "$RT" && -n "$UNIT" && -n "$PY" && -n "$DAEMON_URL" ]] ||
  refuse "set PENTACLE_RELEASE_RUNTIME_CHECKOUT, PENTACLE_RELEASE_LAUNCHD_LABEL, PENTACLE_RELEASE_RUNTIME_PYTHON and PENTACLE_RELEASE_DAEMON_URL"
[[ "$UNIT" =~ '^[A-Za-z0-9_.-]+$' && "$PY" == /* && -f "$PY" && -x "$PY" ]] || refuse "invalid launchd label or interpreter"
LABEL=gui/$(id -u)/$UNIT
WAIT_S=90

pid_of() { launchctl print "$LABEL" 2>/dev/null | awk '$1=="pid" && $3 ~ /^[1-9][0-9]*$/ {print $3; exit}'; }
serving() { "$PY" "$PROBE" "$DAEMON_URL" 2>/dev/null; }
wait_for() {
  local deadline=$(( $(date +%s) + WAIT_S )) pid
  while (( $(date +%s) < deadline )); do
    pid=$(pid_of)
    if [[ -n "$pid" && "$pid" != "$2" && "$(serving)" == "$1" ]]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

PRIOR=$(git -C "$RT" rev-parse --verify HEAD) || refuse "runtime checkout unreadable"
[[ "$TARGET" != "$PRIOR" ]] || refuse "target must be a different full sha"
changes=$(git -C "$RT" status --porcelain) || refuse "runtime checkout unreadable"
[[ -z "$changes" ]] || refuse "runtime checkout is not clean"
[[ "$(serving)" == "$PRIOR" ]] || refuse "daemon is not serving the checked-out commit $PRIOR"
git -C "$RT" fetch -q --prune origin || refuse "fetch failed"
git -C "$RT" cat-file -e "$TARGET^{commit}" 2>/dev/null || refuse "target not in origin"
refs=$(git -C "$RT" for-each-ref --contains "$TARGET" refs/remotes/origin) ||
  refuse "origin reachability check failed"
[[ -n "$refs" ]] || refuse "target not contained in an origin ref"
git -C "$RT" merge-base --is-ancestor "$PRIOR" "$TARGET" || refuse "prior is not an ancestor of target"
git -C "$RT" diff --quiet "$PRIOR" "$TARGET" -- services/chat-stream-v2/requirements.txt ||
  refuse "requirements changed"
OLD_PID=$(pid_of)
[[ -n "$OLD_PID" ]] || refuse "no running daemon pid"
say "preflight ok: prior=$PRIOR target=$TARGET pid=$OLD_PID"
if [[ "$DRY" == --dry-run ]]; then
  say "DRY RUN: no checkout or restart"
  exit 0
fi

T0=$(date +%s)
if ! git -C "$RT" checkout -q --detach "$TARGET"; then
  git -C "$RT" checkout -q --detach "$PRIOR" || human "failed checkout and could not check out prior"
  refuse "checkout failed, nothing restarted"
fi
launchctl kickstart -k "$LABEL" || say "kickstart returned nonzero; checking what serves"
if wait_for "$TARGET" "$OLD_PID"; then
  say "RELEASED: pid $(pid_of) serves $TARGET after $(( $(date +%s) - T0 )) s"
  exit 0
fi

say "target not serving after ${WAIT_S}s (serving='$(serving)', pid='$(pid_of)'); restoring $PRIOR"
MID_PID=$(pid_of)
if [[ -n "$PUTBACK_WORK" ]]; then
  "$PY" "${0:A:h}/helper_release.py" restore-policy --work "$PUTBACK_WORK" ||
    human "pre-putback step failed"
fi
git -C "$RT" checkout -q --detach "$PRIOR" || human "could not check out prior"
launchctl kickstart -k "$LABEL" || say "kickstart returned nonzero; checking what serves"
if wait_for "$PRIOR" "${MID_PID:-$OLD_PID}"; then
  say "PRIOR RESTORED: pid $(pid_of) serves $PRIOR; release failed"
  exit 4
fi
human "neither build is serving (serving='$(serving)', pid='$(pid_of)', checkout=$(git -C "$RT" rev-parse HEAD))"
