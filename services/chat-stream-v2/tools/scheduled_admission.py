#!/usr/bin/env python3
"""FD-file admission and a terminal process-group bound (stdlib only)."""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import re
import shlex
import signal
import subprocess
import time as clock

RESERVE_S = 600
ENVELOPES = {"journey": 1200, "nightly-soak": 2400}


def utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs a timezone")
    return parsed.astimezone(timezone.utc)


def admit(window_path, job, envelope_s, now_utc):
    """Return ADMIT or one of the six fail-closed BLOCKED reasons.

    The FD maintains held_until for reservations, quiet orders and deploys.
    No other file or runtime lock participates in this decision.
    """
    try:
        window = json.loads(Path(window_path).read_text())
    except FileNotFoundError:
        return "BLOCKED:missing"
    except (OSError, ValueError):
        return "BLOCKED:invalid"
    try:
        now = utc(now_utc) if isinstance(now_utc, str) else now_utc.astimezone(timezone.utc)
        if (not isinstance(window, dict) or job not in ENVELOPES
                or type(envelope_s) is not int or envelope_s <= 0
                or window.get("host") != platform.node().split(".")[0].lower()
                or not isinstance(window.get("issued_by"), str) or not window["issued_by"].strip()
                or not re.fullmatch(r"\d{2}:\d{2}", window.get("start_utc", ""))
                or type(window.get("duration_s")) is not int or not 0 < window["duration_s"] <= 86400):
            raise ValueError("window shape")
        hour, minute = map(int, window["start_utc"].split(":"))
        fire = time(hour, minute, tzinfo=timezone.utc)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", window["valid_until"]):
            raise ValueError("expiry needs a calendar date")
        valid = datetime.strptime(window["valid_until"], "%Y-%m-%d").date()
        expiry = datetime.combine(valid + timedelta(days=1), time(tzinfo=timezone.utc))
        if valid > now.date() + timedelta(days=30):
            raise ValueError("renewal exceeds 30 days")
        held = window["held_until"]
        held = utc(held) if held is not None else None
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return "BLOCKED:invalid"
    if expiry <= now:
        return "BLOCKED:expired"
    if held is not None and held > now:
        return "BLOCKED:held"
    start = datetime.combine(now.date(), fire)
    if start > now:
        start -= timedelta(days=1)
    end = start + timedelta(seconds=window["duration_s"])
    if not start <= now < end:
        return "BLOCKED:outside_window"
    # A window must also fit before its authorization expires at UTC midnight.
    if (min(end, expiry) - now).total_seconds() < envelope_s + RESERVE_S:
        return "BLOCKED:insufficient_remaining"
    return "ADMIT"


def fire_guard():
    """FD's additional fire-time guard: no active gate or deployment process.

    Read failures are BLOCKED too. This is deliberately a single snapshot,
    with no lock discovery, polling, or signaling of another process.
    """
    try:
        result = subprocess.run(["/bin/ps", "-axo", "pid=,comm=,args="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "BLOCKED:process_table_unavailable"
    if result.returncode:
        return "BLOCKED:process_table_unavailable"
    for line in result.stdout.splitlines():
        columns = line.split(None, 2)
        if len(columns) != 3 or int(columns[0]) == os.getpid():
            continue
        # Match executable/script tokens, not prose in an agent's instructions.
        try:
            tokens = shlex.split(columns[2])
        except ValueError:
            tokens = columns[2].split()
        for token in tokens:
            name = Path(token).name
            if name in ("gate_env.py", "run_gate.py", "deploy.py") or name == "deploy":
                return "BLOCKED:active_gate_or_deploy"
    return "ADMIT"


def bounded(command, envelope_s, receipt_path):
    """Run a new, owned process group; SIGKILL that group at its hard bound.

    start_new_session makes the child's PID its group ID. Never signal any
    pre-existing group. Persist timeout evidence before the caller reports it.
    """
    path = Path(receipt_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"envelope_s": envelope_s, "terminated_at_bound": False, "finished": False}
    path.write_text(json.dumps(record))
    started = clock.monotonic()
    child = subprocess.Popen(command, start_new_session=True)
    def interrupted(signum, _frame):
        raise InterruptedError(f"bound supervisor received signal {signum}")
    prior = signal.signal(signal.SIGTERM, interrupted)
    try:
        rc = child.wait(timeout=envelope_s)
    except subprocess.TimeoutExpired:
        record["terminated_at_bound"] = True
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        rc = 2
    except BaseException:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        raise
    finally:
        signal.signal(signal.SIGTERM, prior)
        record.update(finished=True, returncode=child.returncode, elapsed_s=clock.monotonic() - started)
        path.write_text(json.dumps(record, indent=2) + "\n")
    return rc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--envelope-s", type=int, required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.envelope_s <= 0 or not command:
        parser.error("positive envelope and command required")
    return bounded(command, args.envelope_s, args.receipt)


if __name__ == "__main__":
    raise SystemExit(main())
