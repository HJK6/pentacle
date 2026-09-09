#!/usr/bin/env bash
# soak_gate.sh — the required full v2 soak with durable evidence.
set -euo pipefail

SERVICE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${V2_GATE_RUNNER_PYTHON:-python3}"
ARGS=(soak)
if [[ -n "${V2_GATE_EVIDENCE_DIR:-}" ]]; then
  ARGS+=(--evidence-dir "${V2_GATE_EVIDENCE_DIR}")
fi
if [[ -n "${V2_GATE_EVIDENCE_OUT:-}" ]]; then
  ARGS+=(--evidence-out "${V2_GATE_EVIDENCE_OUT}")
fi

# A pre-deploy/nightly invocation sets V2_SOAK_STRICT_FULL=1. In that mode the
# runner overwrites every duration/fleet/threshold override with the full preset
# and records the effective values, so ambient shell state cannot weaken it.
export SOAK_TIER="${SOAK_TIER:-full}"
exec "${RUNNER}" "${SERVICE_DIR}/tools/run_gate.py" "${ARGS[@]}"
