#!/bin/bash
set -e

# Emits exactly one readiness state for deploy gates:
# - ok: provider binary exists and its auth-status command reports logged in.
# - missing: provider binary cannot be found.
# - unauthed: provider exists but auth-status reports no usable login.
# Do not use "<provider> --version" for readiness; it succeeds without credentials.
# After auth succeeds, the same preflight emits provider-signal drift WARNs on
# stderr while preserving stdout as the single readiness state.
# Claude can recover from macOS Keychain, so require its local state file first
# to keep a missing ~/.claude.json from being collapsed into "ok".

provider=${1:-}

case "$provider" in
  claude|codex)
    ;;
  *)
    echo "usage: $0 {claude|codex}" >&2
    exit 2
    ;;
esac

resolve_provider_cmd() {
  local name=$1

  if command -v "$name" >/dev/null 2>&1; then
    command -v "$name"
    return 0
  fi

  if [[ "$name" == "claude" && -x "$HOME/.local/bin/claude" ]]; then
    echo "$HOME/.local/bin/claude"
    return 0
  fi

  return 1
}

provider_cmd=$(resolve_provider_cmd "$provider") || {
  echo "missing"
  exit 0
}

# provider_signals.py uses module-level PEP 604 unions (tuple[str, dict] | None),
# which need Python >= 3.10. Bare `python3` on some hosts (e.g. hosta's Xcode 3.9.6)
# is too old and crashes the drift import. Resolve a >=3.10 interpreter instead.
# Probe with a sanitized env: a PYTHONHOME/PYTHONPATH inherited from the caller
# (deploy.py's gate carries the invoker's PYTHONHOME) can make an otherwise-good
# venv python fatal on startup ("Failed to import encodings") when its base prefix
# differs from that PYTHONHOME, which would wrongly reject it. `env -u` strips that
# pollution so the version probe reflects the interpreter, not the caller's env.
_py_is_310plus() {
  env -u PYTHONHOME -u PYTHONPATH "$1" \
    -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

# Print a Python >=3.10 interpreter for the drift check, else return 1 (caller skips).
# Order: (1) v2 repo venv python; (2) python3.13..python3.10 by name; (3) python3 iff >=3.10.
_resolve_py310() {
  local repo_root=$1 candidate
  local venv_py="$repo_root/services/chat-stream-v2/.venv/bin/python"

  if [[ -x "$venv_py" ]] && _py_is_310plus "$venv_py"; then
    echo "$venv_py"
    return 0
  fi

  for candidate in python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1 && _py_is_310plus "$candidate"; then
      command -v "$candidate"
      return 0
    fi
  done

  if command -v python3 >/dev/null 2>&1 && _py_is_310plus python3; then
    command -v python3
    return 0
  fi

  return 1
}

has_provider_auth_state() {
  case "$1" in
    claude)
      [[ -s "$HOME/.claude.json" ]]
      ;;
    codex)
      [[ -s "${CODEX_HOME:-$HOME/.codex}/auth.json" ]]
      ;;
  esac
}

if ! has_provider_auth_state "$provider"; then
  echo "unauthed"
  exit 0
fi

case "$provider" in
	  claude)
	    if "$provider_cmd" auth status >/dev/null 2>&1; then
	      echo "ok"
	      exit 0
	    fi
	    ;;
	  codex)
	    if "$provider_cmd" login status >/dev/null 2>&1; then
	      echo "ok"
	      exit 0
	    fi
    ;;
esac

echo "unauthed"
