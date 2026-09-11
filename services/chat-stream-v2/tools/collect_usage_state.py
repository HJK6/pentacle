#!/usr/bin/env python3
"""Run the external usage CLIs once and persist their combined state."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from usage_collector import UsageStateCollector


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument(
        "--shared-scripts",
        type=Path,
        # Resolve relative to this file, not the process CWD. Under launchd the
        # collector runs with CWD="/", which turned a CWD-relative default into
        # "/scripts/check_claude_usage.py" and broke every usage probe.
        default=Path(__file__).resolve().parents[3] / "scripts",
    )
    parser.add_argument("--skip-codex", action="store_true", help="collect only Claude; preserve prior Codex state without probing")
    args = parser.parse_args(argv)
    UsageStateCollector(
        state_path=args.state,
        claude_command=(sys.executable, str(args.shared_scripts / "check_claude_usage.py"), "--local-fallback", "--json"),
        codex_command=((sys.executable, "-c", 'print(\'{"status":"no_update"}\')') if args.skip_codex
                       else (sys.executable, str(args.shared_scripts / "check_codex_usage.py"), "--json")),
    ).run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
