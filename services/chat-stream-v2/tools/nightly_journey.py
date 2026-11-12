#!/usr/bin/env python3
"""Standing-window admission, deployed release checks and nightly reporting."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time

from scheduled_admission import ENVELOPES, admit, fire_guard

SERVICE = Path(__file__).resolve().parents[1]


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".tmp")
    scratch.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    scratch.replace(path)


def job_name(job):
    return job.removeprefix("nightly-")


def signature(job, row, primary):
    # Paths, timestamps, opaque ids and numbers are volatile; class text is not.
    normalized = primary.lower()
    normalized = re.sub(r"(?:[a-z]:)?[/\\][^\s,;]*", " PATH ", normalized)
    normalized = re.sub(r"\b\d{4}-\d\d-\d\d[t\s][\d:.+z-]+", " TIME ", normalized)
    normalized = re.sub(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b|\b[0-9a-f]{8,}\b", " ID ", normalized)
    normalized = re.sub(r"\d+", " N ", normalized)
    normalized = " ".join(normalized.lower().split())
    return hashlib.sha256("\0".join((job_name(job), row, normalized)).encode()).hexdigest()[:8]


def command_json(command, *args, payload=None, timeout=60):
    result = subprocess.run(shlex.split(command) + list(args), input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, timeout=timeout)
    try:
        value = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError(f"transport exit={result.returncode}: {result.stderr[-500:]}") from None
    if result.returncode or value.get("ok") is False:
        raise RuntimeError(json.dumps(value))
    return value


def operator_rpc(payload):
    for path in (SERVICE, SERVICE.parent):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from tools.live_window import authenticated_operator_connection
    url = os.environ.get("AGENT_ORCH_WS_URL", "ws://127.0.0.1:7791")
    token = Path(os.environ.get("PENTACLE_OPERATOR_TOKEN_PATH", str(Path.home() / ".config/pentacle-stream/token")))
    with authenticated_operator_connection(url, token, 20) as connection:
        return connection.rpc(payload)


def landed(response):
    return response.get("delivery") == "landed" or response.get("state") == "landed"


def deliver_report(record, path, rpc):
    """Retain exact intent; reconcile and retry once with the same key/body."""
    delivery = record.setdefault("report_delivery", {})
    if delivery.get("landed"):
        return
    if "payload" not in delivery:
        binding = rpc({"type": "assistant.binding"})
        if binding.get("type") != "assistant.binding.ok":
            raise RuntimeError("assistant binding unavailable")
        target = binding["stream_id"]
        host, session = target.split(":", 1)
        key = "nightly-report-" + hashlib.sha256((record["job"] + record["date"]).encode()).hexdigest()[:32]
        delivery.update(target=target, payload={"type": "send", "host": host, "session_name": session,
                                                "request_id": key, "optimistic_id": key, "text": record["report_line"]})
        atomic(path, record)
    payload = delivery["payload"]
    for attempt in range(2):
        receipts = rpc({"type": "send.receipt.get", "to_stream_id": delivery["target"], "request_id": payload["request_id"]})
        if receipts.get("type") != "send.receipt.get.ok":
            raise RuntimeError("REPORT reconciliation unavailable")
        if any(landed(r) for r in receipts.get("receipts", [])):
            delivery.update(landed=True, receipt=receipts)
            atomic(path, record)
            return
        response = rpc(payload)
        delivery.setdefault("attempts", []).append(response)
        delivery["landed"] = landed(response)
        atomic(path, record)
        if delivery["landed"]:
            return
    # A final receipt may prove the second send without resubmitting.
    receipts = rpc({"type": "send.receipt.get", "to_stream_id": delivery["target"], "request_id": payload["request_id"]})
    delivery.update(landed=receipts.get("type") == "send.receipt.get.ok" and any(landed(r) for r in receipts.get("receipts", [])), receipt=receipts)
    atomic(path, record)
    if not delivery["landed"]:
        raise RuntimeError("REPORT committed_pending_proof; not delivered")


def notice_proof(notification_id, notifications_db, notices_db):
    """Only the outbound notice's delivered_at proves delivery of a card."""
    with sqlite3.connect(Path(notifications_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
        ids = [r[0] for r in db.execute("SELECT notification_id FROM notifications WHERE json_extract(error_context,'$.source_notification_id')=?", (notification_id,))]
    with sqlite3.connect(Path(notices_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
        for nid in ids:
            row = db.execute("SELECT notice_id,delivered_at FROM v2_outbound_notices WHERE json_extract(metadata,'$.notification_id')=? AND delivered_at IS NOT NULL LIMIT 1", (nid,)).fetchone()
            if row:
                return {"notice_id": row[0], "delivered_at": row[1]}
    return None


def report(args):
    path = Path(args.evidence) / "receipt.json"
    record = json.loads(path.read_text()) if path.exists() else {}
    day = args.date or datetime.now(timezone.utc).date().isoformat()
    identity = {"job": args.job, "date": day, "result": args.result, "primary_error": args.primary_error}
    if record.get("report_line") and any(record.get(k) != v for k, v in identity.items()):
        raise ValueError("receipt already contains a different report intent")
    record.update(identity)
    bound_path = path.parent / "bound.json"
    if bound_path.exists():
        record.update(json.loads(bound_path.read_text()))
    record["item"] = record.get("item", "none")
    errors = []
    if args.result == "FAIL" and record["item"] == "none":
        stem = f"pentacle__nightly_{job_name(args.job)}_{signature(args.job, args.row, args.primary_error)}"
        topic = stem.removeprefix("pentacle__")
        try:
            response = command_json(args.publisher_cmd, "pentacle", topic, "--kind", "defect", "--title", f"{args.job}: {args.primary_error[:100]}",
                                    "--summary", f"Scheduled {args.job} failed on {day}; row={args.row}; primary={args.primary_error}; receipt={path}", timeout=260)
            record["publisher"] = response
            record["item"] = stem
        except Exception as exc:
            # The publisher owns cross-status uniqueness; only its explicit
            # duplicate refusal counts as the existing same-signature item.
            if "already exists" in str(exc).lower():
                record["item"] = stem
                record["publisher"] = {"existing": True}
            else:
                errors.append(f"publisher: {exc}")
    label = "nightly-" + job_name(args.job)
    record["report_line"] = f"REPORT {label} date={day} result={args.result} receipt={path} item={record['item']}"
    atomic(path, record)
    rpc = (lambda payload: command_json(args.report_send_cmd, payload=payload)) if args.report_send_cmd else operator_rpc
    try:
        deliver_report(record, path, rpc)
    except Exception as exc:
        errors.append(f"report: {exc}")
    if args.result != "PASS":
        try:
            if "notification" not in record:
                response = command_json(args.notify_cmd, "--severity", "critical", "--dedup-key", f"{label}:{day}", "--title", f"{label} {args.result}",
                                        "--message", f"{args.primary_error} {path} {record['item']}")
                if response.get("type") != "notification.create.ok" or not response.get("notification", {}).get("notification_id"):
                    raise RuntimeError("critical notification was not accepted")
                record["notification"] = response["notification"]
                atomic(path, record)
            deadline = time.monotonic() + args.notice_wait_s
            while not record.get("notice_delivery"):
                proof = notice_proof(record["notification"]["notification_id"], args.notifications_db, args.notices_db)
                if proof:
                    record["notice_delivery"] = proof
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("critical notice has no delivered_at proof")
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        except Exception as exc:
            errors.append(f"critical: {exc}")
    record["reporting_errors"] = errors
    atomic(path, record)
    print(record["report_line"])
    return 1 if errors else 0


def wait_for_admission(window, job, *, now=None, sleep=time.sleep):
    """Wait only for host contention while this standing window still fits."""
    now = now or (lambda: datetime.now(timezone.utc))
    occurrence_end = None
    while True:
        instant = now().astimezone(timezone.utc)
        if occurrence_end is not None and instant >= occurrence_end:
            return "BLOCKED:outside_window"
        decision = admit(window, job, ENVELOPES[job], instant)
        if decision == "ADMIT":
            decision = fire_guard()
        if decision not in {"BLOCKED:held", "BLOCKED:active_gate_or_deploy"}:
            return decision
        if occurrence_end is None:
            value = json.loads(Path(window).read_text())
            hour, minute = map(int, value["start_utc"].split(":"))
            start = instant.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if start > instant:
                start -= timedelta(days=1)
            occurrence_end = start + timedelta(seconds=value["duration_s"])
        sleep(min(60, max(0, (occurrence_end - instant).total_seconds())))


def check_release(args, *, deploy_module=None):
    """Exercise the same release checks without installing or restarting anything."""
    if deploy_module is None:
        if str(SERVICE) not in sys.path:
            sys.path.insert(0, str(SERVICE))
        from deploy import deploy as deploy_module
    d = deploy_module
    repo = Path(args.checkout).resolve()
    record = {"checkout": str(repo)}
    try:
        service = d.SERVICES[args.service]
        stamp = dict(d._read_stamp(repo, service))
        stamp["release_exclusions"] = d.release_exclusions(service, repo)
        record["release_exclusions"] = stamp["release_exclusions"]
        sha = stamp["sha"]
        record["source_sha"] = sha
        record["identity_basis"] = "deployment stamp target; not independent loaded-runtime proof"
        checks = d.run_post_release_checks(service, repo, sha, stamp, runner=d._run)
        passed = bool(checks) and all(row.get("outcome") == "passed" for row in checks.values())
        record.update(checks=checks, result="PASS" if passed else "FAIL",
                      primary_error="" if passed else "post-release checks failed")
    except Exception as exc:
        record.update(result="FAIL", primary_error=f"post-release checks: {type(exc).__name__}: {exc}")
    path = Path(args.evidence) / "receipt.json"
    prior = json.loads(path.read_text()) if path.exists() else {}
    atomic(path, {**prior, **record})
    print(json.dumps(record, sort_keys=True))
    return 0 if record["result"] == "PASS" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="subcommand", required=True)
    check = subs.add_parser("check-release")
    check.add_argument("--checkout", required=True, help="deployed runtime checkout")
    check.add_argument("--evidence", required=True)
    check.add_argument("--service", default="chat-streamd-v2")
    for name in ("admit", "report"):
        sub = subs.add_parser(name)
        sub.add_argument("--job", required=True, choices=ENVELOPES)
        sub.add_argument("--notify-cmd", default=os.environ.get("NIGHTLY_NOTIFY_CMD", "agent-orch notify"))
        sub.add_argument("--publisher-cmd", default=os.environ.get("NIGHTLY_PUBLISHER_CMD", "python3 scripts/new_work_item.py"))
        sub.add_argument("--report-send-cmd", default=os.environ.get("NIGHTLY_REPORT_SEND_CMD"), help="JSON RPC on stdin/stdout; tests only")
        if name == "admit":
            sub.add_argument("--window", required=True)
            sub.add_argument("--now-utc")
            sub.add_argument("--wait-within-window", action="store_true")
        else:
            sub.add_argument("--result", choices=("PASS", "FAIL", "BLOCKED"), required=True)
            sub.add_argument("--evidence", required=True)
            sub.add_argument("--primary-error", default="")
            sub.add_argument("--row", default="full")
            sub.add_argument("--date")
            sub.add_argument("--notifications-db", default=str(Path.home() / ".local/share/pentacle-stream/notifications.db"))
            sub.add_argument("--notices-db", default=str(Path.home() / ".local/share/pentacle-stream/sessions.db"))
            sub.add_argument("--notice-wait-s", type=float, default=30)
    args = parser.parse_args(argv)
    try:
        if args.subcommand == "check-release":
            return check_release(args)
        if args.subcommand == "admit":
            if args.wait_within_window:
                if args.now_utc:
                    parser.error("--now-utc cannot be combined with waiting")
                decision = wait_for_admission(args.window, args.job)
                print(decision)
                return 0 if decision == "ADMIT" else 2
            decision = admit(args.window, args.job, ENVELOPES[args.job], args.now_utc or datetime.now(timezone.utc))
            if decision == "ADMIT":
                decision = fire_guard()
            print(decision)
            return 0 if decision == "ADMIT" else 2
        return report(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
