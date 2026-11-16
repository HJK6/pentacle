#!/usr/bin/env python3
"""Run the v2-certified replacement for the retired v1 live gate."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parents[2]
V2_GATE = REPO_ROOT / "services" / "chat-stream-v2" / "tools" / "run_gate.py"
STAGE_GATE = {"A": "unit", "B": "merge"}


def command(stage: str) -> tuple[str, ...]:
    return (sys.executable, str(V2_GATE), STAGE_GATE[stage])


def run(stage: str) -> int:
    return subprocess.run(command(stage), cwd=REPO_ROOT, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=sorted(STAGE_GATE), type=str.upper)
    args = parser.parse_args()
    return run(args.stage)


if __name__ == "__main__":
    raise SystemExit(main())
