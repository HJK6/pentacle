#!/bin/zsh
# Strict-full nightly soak; installation paths belong to local config.zsh.
emulate -L zsh
setopt pipe_fail no_unset
CONFIG=${NIGHTLY_SOAK_CONFIG:-$HOME/.pentacle/nightly-soak/config.zsh}
source "$CONFIG" || exit 70
: ${CHECKOUT:?local config must set CHECKOUT} ${TOOLS_ROOT:?local config must set TOOLS_ROOT}
: ${PY:?local config must set PY} ${MEMORY_ROOT:?local config must set MEMORY_ROOT}
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export NIGHTLY_PUBLISHER_CMD=${NIGHTLY_PUBLISHER_CMD:-"$PY $MEMORY_ROOT/scripts/new_work_item.py"}
TOOLS=$TOOLS_ROOT/services/chat-stream-v2/tools
WINDOW=$HOME/.pentacle/nightly-journey/window.json
ROOT=$HOME/.pentacle/soak
RUN=$ROOT/$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p $RUN/home $RUN/tmp $RUN/tmux $RUN/evidence || exit 70
decision=$($PY $TOOLS/nightly_journey.py admit --job nightly-soak --window "$WINDOW")
admission_rc=$?
$PY -c 'import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps({"admission":sys.argv[2],"terminated_at_bound":False}))' "$RUN/evidence/receipt.json" "$decision" || exit 70
if (( admission_rc != 0 )); then
  print "=== nightly-soak BLOCKED $(date -u +%FT%TZ) reason=$decision evidence=$RUN/evidence"
  cd "$MEMORY_ROOT" || exit 70
  $PY $TOOLS/nightly_journey.py report --job nightly-soak --result BLOCKED --evidence "$RUN/evidence" --primary-error "$decision"
  exit 2
fi
print "=== nightly-soak start $(date -u +%FT%TZ) run=$RUN"
print "source=$(git -C $CHECKOUT rev-parse HEAD) dirty=$(git -C $CHECKOUT status --porcelain | wc -l | tr -d ' ')"
prod_pid() { launchctl print gui/$(id -u)/com.pentacle.chat-streamd-v2 2>/dev/null | awk '$1=="pid" {print $3; exit}'; }
pid_before=$(prod_pid)
print "prod_daemon_pid_before=$pid_before"
cd $CHECKOUT || exit 70
$PY $TOOLS/scheduled_admission.py --envelope-s 2400 --receipt "$RUN/evidence/bound.json" -- \
  /usr/bin/env -i HOME=$RUN/home TMPDIR=$RUN/tmp/ TMUX_TMPDIR=$RUN/tmux USER=$(id -un) LOGNAME=$(id -un) SHELL=/bin/zsh \
  LANG=en_US.UTF-8 PYTHONUNBUFFERED=1 SOAK_TIER=full V2_SOAK_STRICT_FULL=1 \
  PATH=$CHECKOUT/services/chat-stream-v2/.venv/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  V2_GATE_RUNNER_PYTHON=$PY V2_GATE_EVIDENCE_DIR=$RUN/evidence \
  /bin/bash services/chat-stream-v2/tools/soak_gate.sh
rc=$?
# Cleanup scoped to this run only: its private tmux servers and scratch dirs.
for sock in $RUN/tmux/tmux-*/*(N=); do
  /opt/homebrew/bin/tmux -S $sock kill-server 2>/dev/null
done
live_sockets=()
for sock in $RUN/tmux/tmux-*/*(N=); do
  /opt/homebrew/bin/tmux -S $sock list-sessions >/dev/null 2>&1 && live_sockets+=($sock)
done
leftover=$(pgrep -f "$RUN" | tr '\n' ' ')
pid_after=$(prod_pid)
host_ok=1
[[ -n $pid_before && $pid_before == $pid_after ]] || { host_ok=0; print "HOST CHECK FAIL: production daemon pid before=[$pid_before] after=[$pid_after]"; }
[[ -z ${leftover// /} ]] || { host_ok=0; print "HOST CHECK FAIL: run-owned processes remain: $leftover"; }
(( ${#live_sockets} == 0 )) || { host_ok=0; print "HOST CHECK FAIL: live run tmux servers: $live_sockets"; }
if (( host_ok )); then
  rm -rf $RUN/home $RUN/tmp $RUN/tmux
else
  print "run scratch kept for diagnosis: $RUN"
  (( rc == 0 )) && rc=75
fi
# Keep evidence for the newest 14 runs.
old=($ROOT/2*(N/On))
for dir in ${old[15,-1]}; do rm -rf -- $dir; done
print "prod_daemon_pid_after=$pid_after leftover_run_pids=[${leftover}] live_run_sockets=${#live_sockets} host_ok=$host_ok"
print "=== nightly-soak end $(date -u +%FT%TZ) rc=$rc evidence=$RUN/evidence/v2-soak-evidence.json"
# Preserve subject/host facts independently of reporting transport failures.
$PY - "$RUN/evidence" "$CHECKOUT" "$pid_before" "$pid_after" "$host_ok" "$rc" <<'PY'
import json, subprocess, sys
from pathlib import Path
evidence, checkout, before, after, host_ok, rc = sys.argv[1:]
root = Path(evidence)
receipt = json.loads((root / 'receipt.json').read_text())
receipt.update(source_sha=subprocess.check_output(['git', '-C', checkout, 'rev-parse', 'HEAD'], text=True).strip(),
               daemon_pid_before=before, daemon_pid_after=after, cleanup={'host_ok': host_ok == '1'},
               subprocess_returncode=int(rc))
try:
    subject = json.loads((root / 'v2-soak-evidence.json').read_text())
except (OSError, ValueError):
    subject = {}
receipt['subject'] = subject
bound = json.loads((root / 'bound.json').read_text()) if (root / 'bound.json').exists() else {}
if bound.get('terminated_at_bound'):
    result, primary = 'BLOCKED', 'terminated_at_bound'
elif int(rc) == 0 and subject.get('passed') is True:
    result, primary = 'PASS', ''
else:
    result = 'FAIL'
    failures = [str(t.get('reason') or 'soak_failure') for t in subject.get('tiers', []) if not t.get('passed')]
    primary = '; '.join(failures) or ('host_check_failure' if host_ok != '1' else 'missing_or_failed_soak_evidence')
receipt.update(result=result, primary_error=primary)
(root / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
(root / 'result.txt').write_text(result)
(root / 'primary-error.txt').write_text(primary)
PY
(( $? == 0 )) || exit 70
result=$(<$RUN/evidence/result.txt)
primary=$(<$RUN/evidence/primary-error.txt)
cd "$MEMORY_ROOT" || exit 70
$PY $TOOLS/nightly_journey.py report --job nightly-soak --result "$result" --evidence "$RUN/evidence" --primary-error "$primary"
report_rc=$?
(( rc != 0 )) && exit $rc
[[ $result == PASS ]] || exit 1
exit $report_rc
