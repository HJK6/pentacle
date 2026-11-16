#!/usr/bin/env bash
#
# Canonical editable installer for services/agent-orch.
#
# The script chooses a supported Python for the local platform, refuses old pip
# versions that cannot handle this pyproject-only package, installs agent-orch
# with a user editable pip install, and verifies that importing agent_orch
# resolves back to this checkout. Linux and Homebrew Python get
# --break-system-packages for PEP 668-managed environments; python.org
# Framework Python on macOS does not.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install.sh [--dry-run] [--python PATH] [--help]

Install agent-orch from this checkout using a user editable pip install.

Options:
  --dry-run       Print the selected interpreter and pip command without installing.
  --python PATH   Use an explicit Python interpreter instead of platform discovery.
  --help          Show this help.

Python discovery:
  macOS: /Library/Frameworks/Python.framework/Versions/3.13/bin/python3,
         /opt/homebrew/opt/python@3.13/bin/python3.13,
         /opt/homebrew/bin/python3.13
  Linux: python3.13, python3.12, python3 with version >= 3.12
EOF
}

die() {
  printf 'install.sh: error: %s\n' "$*" >&2
  exit 1
}

print_cmd() {
  local arg
  for arg in "$@"; do
    printf '%q ' "$arg"
  done
  printf '\n'
}

python_version_ok() {
  local python=$1
  local major=$2
  local minor=$3
  "$python" -c 'import sys; req=(int(sys.argv[1]), int(sys.argv[2])); sys.exit(0 if sys.version_info >= req else 1)' "$major" "$minor" >/dev/null 2>&1
}

pip_version() {
  local python=$1
  "$python" -c 'import pip; print(pip.__version__)'
}

pip_version_ok() {
  local python=$1
  "$python" -c '
import pip
import re
import sys
parts = [int(p) for p in re.findall(r"\d+", pip.__version__)[:2]]
while len(parts) < 2:
    parts.append(0)
sys.exit(0 if tuple(parts) >= (23, 0) else 1)
'
}

find_linux_python() {
  local candidate path
  for candidate in python3.13 python3.12 python3; do
    path=$(command -v "$candidate" 2>/dev/null || true)
    if [[ -n "$path" ]] && python_version_ok "$path" 3 12; then
      printf '%s\n' "$path"
      return 0
    fi
  done
  return 1
}

find_darwin_python() {
  local candidate
  for candidate in \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    /opt/homebrew/opt/python@3.13/bin/python3.13 \
    /opt/homebrew/bin/python3.13
  do
    if [[ -x "$candidate" ]] && python_version_ok "$candidate" 3 13; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

DRY_RUN=0
PYTHON_OVERRIDE=

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --python)
      [[ $# -ge 2 ]] || die "--python requires a path"
      PYTHON_OVERRIDE=$2
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PLATFORM=$(uname -s)

case "$PLATFORM" in
  Linux)
    MIN_MAJOR=3
    MIN_MINOR=12
    ;;
  Darwin)
    MIN_MAJOR=3
    MIN_MINOR=13
    ;;
  *)
    die "unsupported platform '$PLATFORM'; expected Linux or Darwin"
    ;;
esac

if [[ -n "$PYTHON_OVERRIDE" ]]; then
  [[ -x "$PYTHON_OVERRIDE" ]] || die "--python path is not executable: $PYTHON_OVERRIDE"
  PYTHON=$PYTHON_OVERRIDE
  python_version_ok "$PYTHON" "$MIN_MAJOR" "$MIN_MINOR" || die "$PYTHON must be Python $MIN_MAJOR.$MIN_MINOR or newer"
else
  if [[ "$PLATFORM" == "Darwin" ]]; then
    PYTHON=$(find_darwin_python) || die "no usable Python 3.13+ found; install python.org Python 3.13 or Homebrew python@3.13"
  else
    PYTHON=$(find_linux_python) || die "no usable Python 3.12+ found; install Python 3.12 or newer"
  fi
fi

if ! PIP_VERSION=$(pip_version "$PYTHON" 2>/dev/null); then
  die "$PYTHON cannot import pip; install pip for this interpreter"
fi
pip_version_ok "$PYTHON" || die "$PYTHON has pip $PIP_VERSION; pip 23.0 or newer is required"

INSTALL_CMD=("$PYTHON" -m pip install --user --force-reinstall --no-deps)
if [[ "$PLATFORM" == "Linux" || "$PYTHON" == /opt/homebrew/* ]]; then
  INSTALL_CMD+=(--break-system-packages)
fi
INSTALL_CMD+=(-e "$SCRIPT_DIR")

CLI_DIR=$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))') || die "could not resolve user script directory"
CLI_PATH="$CLI_DIR/agent-orch"

printf 'Python: %s\n' "$PYTHON"
printf 'pip: %s\n' "$PIP_VERSION"
printf 'install command: '
print_cmd "${INSTALL_CMD[@]}"

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'dry run: no install performed\n'
  exit 0
fi

"${INSTALL_CMD[@]}"

EXPECTED=$("$PYTHON" -c 'from pathlib import Path; import sys; print((Path(sys.argv[1]).resolve() / "agent_orch" / "__init__.py").resolve())' "$SCRIPT_DIR")
if ! ACTUAL=$("$PYTHON" -c 'from pathlib import Path; import agent_orch; print(Path(agent_orch.__file__).resolve())'); then
  die "installed package could not be imported with $PYTHON"
fi

if [[ "$ACTUAL" != "$EXPECTED" ]]; then
  die "editable install verification failed: imported $ACTUAL, expected $EXPECTED"
fi

printf 'verified editable import: %s\n' "$ACTUAL"
printf 'agent-orch CLI: %s\n' "$CLI_PATH"
if [[ ! -x "$CLI_PATH" ]]; then
  printf 'install.sh: warning: CLI path is not executable yet; check user-script PATH/install output\n' >&2
fi
