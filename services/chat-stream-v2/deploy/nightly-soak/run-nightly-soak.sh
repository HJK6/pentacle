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
START=$(date -u +%FT%TZ)
RUN=$ROOT/${START//[-:]/}
print "=== nightly-soak scheduled start $START run=$RUN"
mkdir -p "$RUN/evidence" || exit 70
decision=$($PY $TOOLS/nightly_journey.py admit --job nightly-soak --window "$WINDOW" --wait-within-window)
admission_rc=$?
$PY - "$RUN/evidence/receipt.json" "$decision" <<'PY'
import json, sys
from pathlib import Path
receipt, decision = sys.argv[1:]
record = {"admission": decision, "terminated_at_bound": False}
Path(receipt).write_text(json.dumps(record))
PY
(( $? == 0 )) || exit 70
if (( admission_rc != 0 )); then
  print "=== nightly-soak BLOCKED $(date -u +%FT%TZ) reason=$decision evidence=$RUN/evidence"
  cd "$MEMORY_ROOT" || exit 70
  $PY $TOOLS/nightly_journey.py report --job nightly-soak --date "${START%%T*}" --result BLOCKED --evidence "$RUN/evidence" --primary-error "$decision"
  exit 2
fi
cd "$CHECKOUT" || exit 70
$PY $TOOLS/scheduled_admission.py --envelope-s 2400 --receipt "$RUN/evidence/bound.json" -- \
  $PY $TOOLS/nightly_journey.py check-release --checkout "$CHECKOUT" --evidence "$RUN/evidence"
rc=$?
soak_bound=$($PY - "$WINDOW" <<'PYBOUND'
import json, sys
from datetime import datetime, timedelta, timezone
window = json.load(open(sys.argv[1]))
now = datetime.now(timezone.utc)
hour, minute = map(int, window['start_utc'].split(':'))
start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
if start > now:
    start -= timedelta(days=1)
end = start + timedelta(seconds=window['duration_s'])
print(max(1, min(7200, int((end - now).total_seconds()) - 600)))
PYBOUND
)
cd "$CHECKOUT/services/chat-stream-v2" || exit 70
PYTHONDONTWRITEBYTECODE=1 PENTACLE_FORCE_LIVE_DAEMON=1 $PY $TOOLS/scheduled_admission.py --envelope-s "$soak_bound" --receipt "$RUN/evidence/soak-bound.json" -- \
  $PY -m pytest tests/soak -m '' -p no:cacheprovider -q --junitxml="$RUN/evidence/soak.xml" > "$RUN/evidence/soak.log" 2>&1
soak_rc=$?
# Keep evidence for the newest 14 occurrences, using the existing retention policy.
old=($ROOT/2*(N/On))
for dir in ${old[15,-1]}; do rm -rf -- $dir; done
$PY - "$RUN/evidence" "$rc" "$soak_rc" <<'PY'
import json, sys
from pathlib import Path
import xml.etree.ElementTree as ET
root, rc, soak_rc = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
record = json.loads((root / 'receipt.json').read_text())
bound = json.loads((root / 'bound.json').read_text()) if (root / 'bound.json').exists() else {}
if bound.get('terminated_at_bound'):
    record.update(result='BLOCKED', primary_error='terminated_at_bound')
elif rc and record.get('result') != 'FAIL':
    record.update(result='FAIL', primary_error='post-release checks did not complete')
soak_bound = json.loads((root / 'soak-bound.json').read_text()) if (root / 'soak-bound.json').exists() else {}
soak_xml = root / 'soak.xml'
counts = None
try:
    suite = ET.parse(soak_xml).getroot().find('testsuite')
    counts = {key: int(suite.get(key, 0)) for key in ('tests', 'failures', 'errors', 'skipped')}
except (OSError, ET.ParseError, AttributeError, ValueError):
    pass
record['soak'] = {'result': 'timeout' if soak_bound.get('terminated_at_bound') else 'passed' if soak_rc == 0 else 'failed',
                  'returncode': soak_rc, 'counts': counts, 'duration_s': soak_bound.get('elapsed_s'),
                  'junit': str(soak_xml), 'log': str(root / 'soak.log'), 'bound': soak_bound}
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
$PY $TOOLS/nightly_journey.py report --job nightly-soak --date "${START%%T*}" --result "$result" --evidence "$RUN/evidence" --primary-error "$primary"
report_rc=$?
(( rc != 0 )) && exit $rc
[[ $result == PASS ]] || exit 1
exit $report_rc
