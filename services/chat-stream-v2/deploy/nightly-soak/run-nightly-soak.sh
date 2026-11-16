#!/bin/zsh
# The existing nightly slot checks the deployed release, never a frozen soak copy.
emulate -L zsh
setopt pipe_fail no_unset
CONFIG=${NIGHTLY_SOAK_CONFIG:-$HOME/.pentacle/nightly-soak/config.zsh}
source "$CONFIG" || exit 70
: ${CHECKOUT:?local config must set CHECKOUT to the deployed runtime checkout}
: ${MEMORY_ROOT:?local config must set MEMORY_ROOT}
PY=$CHECKOUT/services/chat-stream-v2/.venv/bin/python
TOOLS=$CHECKOUT/services/chat-stream-v2/tools
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export NIGHTLY_PUBLISHER_CMD=${NIGHTLY_PUBLISHER_CMD:-"$PY $MEMORY_ROOT/scripts/new_work_item.py"}
WINDOW=$HOME/.pentacle/nightly-journey/window.json
ROOT=$HOME/.pentacle/soak
RUN=$ROOT/$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$RUN/evidence" || exit 70
decision=$($PY $TOOLS/nightly_journey.py admit --job nightly-soak --window "$WINDOW" --wait-within-window)
admission_rc=$?
$PY -c 'import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps({"admission":sys.argv[2],"terminated_at_bound":False}))' "$RUN/evidence/receipt.json" "$decision" || exit 70
if (( admission_rc != 0 )); then
  print "=== nightly-soak BLOCKED $(date -u +%FT%TZ) reason=$decision evidence=$RUN/evidence"
  cd "$MEMORY_ROOT" || exit 70
  $PY $TOOLS/nightly_journey.py report --job nightly-soak --result BLOCKED --evidence "$RUN/evidence" --primary-error "$decision"
  exit 2
fi
print "=== nightly-soak start $(date -u +%FT%TZ) run=$RUN"
cd "$CHECKOUT" || exit 70
$PY $TOOLS/scheduled_admission.py --envelope-s 2400 --receipt "$RUN/evidence/bound.json" -- \
  $PY $TOOLS/nightly_journey.py check-release --checkout "$CHECKOUT" --evidence "$RUN/evidence"
rc=$?
# Keep evidence for the newest 14 occurrences, using the existing retention policy.
old=($ROOT/2*(N/On))
for dir in ${old[15,-1]}; do rm -rf -- $dir; done
$PY - "$RUN/evidence" "$rc" <<'PY'
import json, sys
from pathlib import Path
root, rc = Path(sys.argv[1]), int(sys.argv[2])
record = json.loads((root / 'receipt.json').read_text())
bound = json.loads((root / 'bound.json').read_text()) if (root / 'bound.json').exists() else {}
if bound.get('terminated_at_bound'):
    record.update(result='BLOCKED', primary_error='terminated_at_bound')
elif rc and record.get('result') != 'FAIL':
    record.update(result='FAIL', primary_error='post-release checks did not complete')
record['subprocess_returncode'] = rc
(root / 'receipt.json').write_text(json.dumps(record, indent=2) + '\n')
(root / 'result.txt').write_text(record.get('result', 'FAIL'))
(root / 'primary-error.txt').write_text(record.get('primary_error', 'missing post-release check result'))
PY
(( $? == 0 )) || exit 70
result=$(<$RUN/evidence/result.txt)
primary=$(<$RUN/evidence/primary-error.txt)
print "=== nightly-soak end $(date -u +%FT%TZ) rc=$rc result=$result evidence=$RUN/evidence"
cd "$MEMORY_ROOT" || exit 70
$PY $TOOLS/nightly_journey.py report --job nightly-soak --result "$result" --evidence "$RUN/evidence" --primary-error "$primary"
report_rc=$?
(( rc != 0 )) && exit $rc
[[ $result == PASS ]] || exit 1
exit $report_rc
