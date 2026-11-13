"""Public command line. Only explicitly projected facts reach output."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .releases import OFFICIAL_REMOTE


def common(parser):
    for flag in ("install", "config", "daemon-url", "web-url", "release-remote"):
        parser.add_argument("--" + flag, default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    common(result)
    commands = result.add_subparsers(dest="command", required=True)
    version = commands.add_parser("version", help="show installed and running identities")
    common(version)
    doctor = commands.add_parser("doctor", help="check the installation")
    common(doctor)
    doctor.add_argument("--live", action="store_true")
    doctor.add_argument("--for-transaction", help=argparse.SUPPRESS)
    service = commands.add_parser("service", help="adopt running services")
    common(service)
    service_commands = service.add_subparsers(dest="service_command", required=True)
    adopt = service_commands.add_parser("adopt", help="install service units from running processes")
    common(adopt)
    adopt.add_argument("--yes", action="store_true")
    update = commands.add_parser("update", help="update to a stable release")
    common(update)
    update.add_argument("--check", action="store_true")
    update.add_argument("--to")
    update.add_argument("--allow-untested-start", action="store_true")
    update.add_argument("--ack-required-actions", action="store_true")
    update.add_argument("--restart-now", action="store_true")
    update.add_argument("--no-live", action="store_true")
    update.add_argument("--boot-wait", type=float, default=180)
    update.add_argument("--store", action="append", default=[])
    update.add_argument("--restore-state")
    update.add_argument("--yes", action="store_true")
    update.add_argument("--transaction", help=argparse.SUPPRESS)
    return result


def version_text(value):
    installed = value["install"]
    version = installed["version"] or "development"
    print(f"Installed: {version} {installed['commit'] or 'unknown'}")
    print(f"Daemon: {value['daemon']['commit'] or 'unknown'}")
    print(f"Web: {value['web']['build_commit'] or 'unknown'} (build {value['web']['build_id'] or 'unknown'})")
    print(f"Latest stable: {value['latest']['version'] or 'unknown'}")
    print(f"Release source: {installed['release_remote']}; manifest {value['manifest']['source']}")
    stamp = value["manifest"]["fetched_at"]
    if stamp:
        print(f"Manifest fetched: {stamp}")
    if value["manifest"]["source"] == "cache":
        from datetime import datetime, timezone
        days = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() // 86400))
        print(f"Cached manifest age: {days} days; online release status unknown.")
    if value["behind_releases"] is not None:
        print(f"Releases behind recorded latest: {value['behind_releases']}")


def doctor_text(value):
    version_text(value["identity"])
    group = None
    for check in value["checks"]:
        prefix = check["id"].split(".")[0]
        if prefix != group:
            print(f"\n{prefix}")
            group = prefix
        blocked = f" (blocked by {', '.join(check['blocked_by'])})" if check["blocked_by"] else ""
        print(f"  {check['id']:<24} {check['status']:<4} {check['finding']}{blocked}")
        if check["fix"]:
            print(f"    Fix: {check['fix']}")
    print("\n" + ", ".join(f"{count} {status}" for status, count in value["summary"].items()))


def main(argv=None, *, default_install=None):
    args = parser().parse_args(argv)
    defaults = {"install": str(default_install or Path(__file__).resolve().parents[2]),
                "config": os.environ.get("PENTACLE_CONFIG") or "~/.config/pentacle/pentacle.config.js",
                "daemon_url": None, "web_url": "http://127.0.0.1:7795",
                "release_remote": OFFICIAL_REMOTE, "json": False}
    for name, value in defaults.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    try:
        if args.command in {"version", "doctor"}:
            from .doctor import Snapshot, report
            snapshot = Snapshot(args)
            if args.command == "version":
                value = snapshot.identity
                print(json.dumps(value)) if args.json else version_text(value)
                return 0
            value = report(snapshot, live=args.live, for_transaction=args.for_transaction)
            print(json.dumps(value)) if args.json else doctor_text(value)
            return 1 if value["summary"]["fail"] else 0
        if args.command == "service":
            from .services import adopt
            return adopt(args)
        from .update import update
        return update(args)
    except KeyboardInterrupt:
        print("pentacle: interrupted; run the same command to continue.", file=sys.stderr)
        return 130
    except Exception:
        # Subprocess output and exception text can contain credentials or config.
        print("pentacle: an internal error prevented the command from completing.", file=sys.stderr)
        return 70
