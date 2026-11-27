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
if [[ -z "$PUTBACK_WORK" ]]; then
  PYTHONPATH="${0:A:h:h}:${0:A:h:h:h}" "$PY" -c '
import subprocess, sys
from deploy.coordinated_runtime import code_identity_at
run = lambda args, cwd: subprocess.run(args, cwd=cwd, capture_output=True, text=True)
sys.exit(code_identity_at(sys.argv[1], sys.argv[2], run) != code_identity_at(sys.argv[1], sys.argv[3], run))
' "$RT" "$PRIOR" "$TARGET" || refuse "needs the helper release"
fi
OLD_PID=$(pid_of)
[[ -n "$OLD_PID" ]] || refuse "no running daemon pid"
say "preflight ok: prior=$PRIOR target=$TARGET pid=$OLD_PID"
if [[ "$DRY" == --dry-run ]]; then
  say "DRY RUN: no checkout or restart"
  exit 0
fi

PREFLIGHT=$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)
STOP_TIME= SERVING_TIME= NEW_PID= BOOT_ID= PROBE_VERDICT=skipped PROBE_PID= PROBE_FILE=
finish() {
  local code=$? outcome
  trap - EXIT
  if [[ -n "$PROBE_PID" ]]; then
    wait "$PROBE_PID"; local probe_run=$?
    if "$PY" "${0:A:h}/restart_probe.py" verify "$PROBE_FILE" && (( probe_run == 0 )); then
      PROBE_VERDICT=passed
    else
      PROBE_VERDICT=failed
      (( code == 0 )) && code=6
    fi
    /bin/rm -f "$PROBE_FILE"
  fi
  case $code in 0|6) outcome=released;; 2) outcome=refused;; 4) outcome=put_back;; *) outcome=needs_human;; esac
  if [[ "$code" == 4 ]]; then
    NEW_PID=$(pid_of)
    SERVING_TIME=$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)
  fi
  if [[ -n "$NEW_PID" ]]; then
  BOOT_ID=$(launchctl print "$LABEL" | "$PY" -c '
import json, pathlib, re, sys
text = sys.stdin.read(); m = re.search(r"PENTACLE_DAEMON_STDOUT_PATH => (.+)", text) or re.search(r"stdout path = (.+)", text)
try:
    lines = pathlib.Path(m.group(1).strip()).read_text().splitlines()
    for line in reversed(lines):
        if line.startswith("generation=") and " chat_streamd_v2 listening on " in line:
            identity = json.loads(line.split(" ", 1)[0][11:])
            if identity.get("pid") == int(sys.argv[1]): print(identity["boot_uuid"]); break
except (OSError, ValueError, AttributeError, KeyError): pass
' "$NEW_PID")
  fi
  [[ -n "$STOP_TIME" && -z "$NEW_PID" ]] && NEW_PID=$(pid_of)
  if [[ -n "${PENTACLE_RELEASE_RECORD_DIR:-}" ]]; then
    "$PY" -c '
import json, pathlib, sys, uuid
keys = ("target", "prior", "outcome", "exit_code", "preflight", "stop", "serving", "old_pid", "new_pid", "boot_id", "probe_verdict")
values = [v or None for v in sys.argv[2:]]; values[3] = int(values[3])
path = pathlib.Path(sys.argv[1]) / (str(uuid.uuid4()) + ".json")
path.write_text(json.dumps(dict(zip(keys, values)), indent=2) + "\n")
' "$PENTACLE_RELEASE_RECORD_DIR" "$TARGET" "$PRIOR" "$outcome" "$code" "$PREFLIGHT" "$STOP_TIME" "$SERVING_TIME" "$OLD_PID" "$NEW_PID" "$BOOT_ID" "$PROBE_VERDICT" || say "WARNING: release record could not be written"
  else
    say "WARNING: PENTACLE_RELEASE_RECORD_DIR unset; no release record"
  fi
  exit "$code"
}
trap finish EXIT
if [[ -n "${PENTACLE_RELEASE_PROBE_TARGET:-}" ]]; then
  PROBE_FILE=$(mktemp) || refuse "could not create probe file"
  "$PY" "${0:A:h}/restart_probe.py" run "$PENTACLE_RELEASE_PROBE_TARGET" 10 "$PROBE_FILE" &
  PROBE_PID=$!
  sleep 5
fi
T0=$(date +%s)
if ! git -C "$RT" checkout -q --detach "$TARGET"; then
  git -C "$RT" checkout -q --detach "$PRIOR" || human "failed checkout and could not check out prior"
  refuse "checkout failed, nothing restarted"
fi
STOP_TIME=$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)
launchctl kickstart -k "$LABEL" || say "kickstart returned nonzero; checking what serves"
if wait_for "$TARGET" "$OLD_PID"; then
  NEW_PID=$(pid_of)
  SERVING_TIME=$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)

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
