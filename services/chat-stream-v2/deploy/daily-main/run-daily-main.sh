#!/bin/zsh
# Source-only launcher. The operator supplies the schedule and local config.
emulate -L zsh
setopt pipe_fail no_unset
ROOT=$HOME/.pentacle/daily-main
RUN=
detail=reason=setup
finish() {
  local rc=$1 result=NOT_LAUNCHED
  if [[ -n $RUN && -s $RUN/launch ]]; then
    detail=$(<"$RUN/launch")
    if [[ $detail == source=* ]]; then
      if (( rc == 0 )); then result=PASS; else result=FAIL; fi
    fi
  fi
  print -r -- "=== daily-main result=$result $detail rc=$rc evidence=$RUN utc=$(date -u +%FT%TZ)" | tee -a "$ROOT/results.log"
}
if [[ ${1:-} != --execute ]]; then
  trap 'finish $?' EXIT
  mkdir -p "$ROOT/runs" || exit 70
fi
source "${DAILY_MAIN_CONFIG:-$ROOT/config.zsh}" || exit 70
: ${CHECKOUT:?set CHECKOUT to the service checkout}
: ${REPOSITORY:?set REPOSITORY to the public repository URL}
PY=$CHECKOUT/services/chat-stream-v2/.venv/bin/python
TOOLS=$CHECKOUT/services/chat-stream-v2/tools
WINDOW=$HOME/.pentacle/nightly-journey/window.json
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [[ ${1:-} == --execute ]]; then
  RUN=$2
  WORK=$(mktemp -d "${TMPDIR:-/tmp}/daily-main.XXXXXXXX") || exit 70
  trap 'rm -rf -- "$WORK"' EXIT
  print reason=checkout > "$RUN/launch" || exit 70
  git clone --quiet --no-local --branch main "$REPOSITORY" "$WORK/checkout" || exit 2
  cd "$WORK/checkout" || exit 70
  SHA=$(git rev-parse origin/main) && git checkout --quiet --detach "$SHA" || exit 70
  [[ -r services/chat-stream-v2/tools/run_gate.py ]] || exit 70
  print reason=checkout_overhead > "$RUN/launch" || exit 70
  "$PY" -c 'import os, sys, time; sys.exit(float(os.environ["PENTACLE_SCHEDULED_TERM_AT"]) - time.monotonic() < 10805)' || exit 2
  print "source=$SHA comparison=$SHA profile=merge" > "$RUN/launch" || exit 70
  "$PY" services/chat-stream-v2/tools/run_gate.py merge --deadline-seconds 10800 --evidence-dir "$RUN"
  exit $?
fi
detail=reason=admission
decision=$("$PY" "$TOOLS/nightly_journey.py" admit --job daily-main --window "$WINDOW" --wait-within-window) || {
  [[ $decision == BLOCKED:* ]] && detail="reason=${decision#BLOCKED:}"
  exit 2
}
detail=reason=free_space
(( $(df -Pk "$ROOT" | awk 'NR==2 {print $4}') >= 62914560 )) || exit 2
detail=reason=setup
RUN=$(mktemp -d "$ROOT/runs/attempt.XXXXXXXX") || exit 70
"$PY" "$TOOLS/scheduled_admission.py" --envelope-s 11400 --receipt "$RUN/bound.json" -- /bin/zsh "$0" --execute "$RUN"
