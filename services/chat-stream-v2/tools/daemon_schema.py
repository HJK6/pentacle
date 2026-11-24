#!/usr/bin/env python3
"""Inspect or prepare schema metadata on an explicitly named disposable DB."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import daemon_migrations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "prepare"):
        command = sub.add_parser(name)
        command.add_argument("database", type=Path, help="explicit disposable SQLite database path")
    args = parser.parse_args()
    try:
        result = (daemon_migrations.inspect(args.database) if args.command == "inspect"
                  else daemon_migrations.prepare(args.database))
    except Exception as exc:
        print(json.dumps({"prepared": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if args.command == "inspect" and result["prepared"] or args.command == "prepare" else 1


if __name__ == "__main__":
    raise SystemExit(main())
