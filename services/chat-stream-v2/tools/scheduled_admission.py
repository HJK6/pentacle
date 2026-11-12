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
    start = datetime.combine(now.date(), fire)
    if start > now:
        start -= timedelta(days=1)
    end = start + timedelta(seconds=window["duration_s"])
    if not start <= now < end:
        return "BLOCKED:outside_window"
    # A window must also fit before its authorization expires at UTC midnight.
    if (min(end, expiry) - now).total_seconds() < envelope_s + RESERVE_S:
        return "BLOCKED:insufficient_remaining"
    if held is not None and held > now:
        return "BLOCKED:held"
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
        # Inspect only the local executable's first three argv positions.
        try:
            tokens = shlex.split(columns[2])
        except ValueError:
            tokens = columns[2].split()
        if not tokens:
            continue
        executable = Path(tokens[0]).name.lower()
        if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
            script = tokens[2] if len(tokens) > 2 and tokens[1] in ("-u", "-B", "-I", "-s", "-S") else (tokens[1] if len(tokens) > 1 else "")
            if (Path(script).name in ("gate_env.py", "run_gate.py", "deploy.py")
                    or tokens[1:3] == ["-m", "deploy.deploy"]):
                return "BLOCKED:active_gate_or_deploy"
    return "ADMIT"


def process_table(timeout):
    """Birth-pinned ancestry, including descendants that create new sessions."""
    result = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,pgid=,lstart="],
                            text=True, capture_output=True, check=True, timeout=timeout,
                            env=dict(os.environ, TZ="UTC0", LC_ALL="C"))
    table = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            pid, parent, group = map(int, parts[:3])
            table[pid] = {"parent": parent, "group": group, "birth": parts[3]}
    return table


def owned_descendants(table, root, retained=None):
    owned = {pid: row for pid, row in (retained or {}).items()
             if pid in table and table[pid]["birth"] == row["birth"]}
    if retained is None and root in table:
        owned[root] = table[root]
    while True:
        extra = {pid: row for pid, row in table.items() if pid not in owned and row["parent"] in owned}
        if not extra:
            return owned
        owned.update(extra)


def bounded(command, envelope_s, receipt_path):
    """Bound the actual runner topology, including its nested sessions.

    TERM begins inside the envelope so run_gate can reap its own registry.
    Before the terminal cutoff, birth-verified descendants are killed too.
    Never discover ownership by argv text or signal a pre-existing group.
    """
    path = Path(receipt_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"envelope_s": envelope_s, "terminated_at_bound": False, "finished": False}
    path.write_text(json.dumps(record))
    started = clock.monotonic()
    deadline = started + envelope_s
    grace = min(30, envelope_s / 3)
    verify_reserve = min(5, envelope_s / 8)
    child = subprocess.Popen(command, start_new_session=True)
    def interrupted(signum, _frame):
        raise InterruptedError(f"bound supervisor received signal {signum}")
    prior = signal.signal(signal.SIGTERM, interrupted)
    owned = None
    def stop_owned():
        nonlocal owned
        # Snapshot while the leader still owns its descendants, before TERM
        # permits them to reparent. Retain birth identity across that change.
        try:
            table = process_table(max(0.01, min(5, (deadline - clock.monotonic()) / 3)))
            owned = owned_descendants(table, child.pid)
            record["owned_at_stop"] = owned
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            record.setdefault("cleanup_errors", []).append(type(exc).__name__)
            owned = {}
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=max(0, deadline - verify_reserve - clock.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        try:
            table = process_table(max(0.01, min(5, deadline - clock.monotonic())))
            survivors = owned_descendants(table, child.pid, owned)
            # Signal only processes whose retained birth still matches. A
            # verified private group leader also owns any new group members.
            for pid, row in survivors.items():
                try:
                    if row["group"] == pid:
                        os.killpg(pid, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            record["killed_owned_pids"] = list(survivors)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            record.setdefault("cleanup_errors", []).append(type(exc).__name__)
        # The Popen-owned leader is safe to signal while it is still unreaped.
        if child.poll() is None:
            child.kill()
        child.wait()
    try:
        rc = child.wait(timeout=max(0, deadline - grace - clock.monotonic()))
    except subprocess.TimeoutExpired:
        record["terminated_at_bound"] = True
        stop_owned()
        rc = 2
    except BaseException:
        stop_owned()
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
