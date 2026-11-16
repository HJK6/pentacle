#!/usr/bin/env bash
# smoke_gate.sh — the chat_streamd v2 smoke tier as a mechanical gate.
#
# Single source of truth for "what the v2 smoke gate runs". Invoked by:
#   - .github/workflows/chat-stream-v2-smoke.yml   (CI: any PR touching
#     services/chat-stream-v2/** — meant to be a REQUIRED status check)
#   - deploy-mac.sh                                (local pre-deploy gate)
#   - a developer, by hand, before pushing
#
# The merge gate is `merge_gate.sh`; this wrapper remains the single-tier entry
# point for developers and callers that only need the smoke surface.
set -euo pipefail

SERVICE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${V2_GATE_RUNNER_PYTHON:-python3}"
ARGS=(smoke)
if [[ -n "${V2_GATE_EVIDENCE_DIR:-}" ]]; then
  ARGS+=(--evidence-dir "${V2_GATE_EVIDENCE_DIR}")
fi
if [[ -n "${V2_GATE_EVIDENCE_OUT:-}" ]]; then
  ARGS+=(--evidence-out "${V2_GATE_EVIDENCE_OUT}")
fi

exec "${RUNNER}" "${SERVICE_DIR}/tools/run_gate.py" "${ARGS[@]}"
