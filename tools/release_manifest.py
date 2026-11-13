#!/usr/bin/env python3
"""Validate or append the authoritative stable-release manifest."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from pentacle.releases import ReleaseError, add, verify


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("verify")
    check.add_argument("--ref", required=True)
    check.add_argument("--base")
    append = commands.add_parser("add")
    append.add_argument("--version", required=True)
    append.add_argument("--commit", required=True)
    append.add_argument("--date", default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    append.add_argument("--changelog-section", action="append", default=[])
    append.add_argument("--tested-update-from", action="append", required=True)
    append.add_argument("--required-action", action="append", default=[])
    append.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            data = verify(args.root, args.ref, args.base)
            print(json.dumps({"ok": True, "releases": len(data["releases"])}))
        else:
            print(json.dumps(add(args.root, version=args.version, commit=args.commit, date=args.date,
                                 sections=args.changelog_section, starts=args.tested_update_from,
                                 actions=args.required_action, evidence=args.evidence)))
    except ReleaseError as exc:
        print(f"release_manifest: {exc}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError):
        print("release_manifest: release validation failed.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
