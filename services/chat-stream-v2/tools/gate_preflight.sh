#!/usr/bin/env bash
# gate_preflight.sh — fail fast on the host prerequisite for v2's multi-bind gate.
set -euo pipefail

LOOPBACK_ALIAS="127.0.0.2"
SERVICE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMEDIATION="${SERVICE_DIR}/deploy/loopback-alias/README.md"

fail() {
  local reason="$1"
  echo "[v2-gate-preflight] FAIL: ${reason}" >&2
  echo "[v2-gate-preflight] remediation: install the durable launchd asset from ${REMEDIATION}" >&2
  echo "[v2-gate-preflight] then rerun ${SERVICE_DIR}/tools/gate_preflight.sh" >&2
  exit 2
}

if [[ "$(uname -s)" == "Darwin" ]]; then
  if ! /sbin/ifconfig lo0 2>/dev/null | awk -v address="${LOOPBACK_ALIAS}" '$1 == "inet" && $2 == address {found = 1} END {exit !found}'; then
    fail "loopback_alias_missing: ${LOOPBACK_ALIAS} is not configured on lo0"
  fi
fi

# The interface listing is necessary on macOS, but a bind/connect probe is the
# portable proof that the address can actually serve the gate's WebSocket.
PYTHON_BIN="${V2_PYTHON_BIN:-python3}"
if ! "${PYTHON_BIN}" - "${LOOPBACK_ALIAS}" <<'PY'
import socket
import sys

address = sys.argv[1]
listener = socket.socket()
client = socket.socket()
try:
    listener.settimeout(2.0)
    listener.bind((address, 0))
    listener.listen(1)
    client.settimeout(2.0)
    client.connect(listener.getsockname())
    connection, _ = listener.accept()
    connection.close()
finally:
    client.close()
    listener.close()
PY
then
  fail "loopback_alias_unusable: ${LOOPBACK_ALIAS} cannot bind and accept locally"
fi

echo "[v2-gate-preflight] PASS: ${LOOPBACK_ALIAS} is configured and usable"
