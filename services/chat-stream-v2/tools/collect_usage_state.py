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
        default=Path.cwd() / "scripts",
    )
    args = parser.parse_args(argv)
    UsageStateCollector(
        state_path=args.state,
        provider_a_command=(sys.executable, str(args.shared_scripts / "check_provider_a_usage.py"), "--local-fallback", "--json"),
        provider_c_command=(sys.executable, str(args.shared_scripts / "check_provider_c_usage.py"), "--json"),
    ).run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
