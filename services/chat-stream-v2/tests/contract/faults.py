"""Prove each contract case detects a source fault in a scratch checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[2]
REPO = SERVICE.parents[1]


@dataclass(frozen=True)
class Fault:
    node: str
    source: str
    before: str
    after: str
    assertion: str


FAULTS = {
    "C1": Fault(
        "test_protocol.py::test_c1_handshake_and_errors", "server.py",
        '\n            "error_code": "unsupported_in_v2",',
        '\n            "error_code": "unsupported",',
        "C1 unknown verb error",
    ),
    "C2": Fault(
        "test_lifecycle.py::test_c2_session_lifecycle", "sessions.py",
        'code = "close_live_children" if children else "close_pending_spawn"',
        'code = "close_pending_spawn"',
        "C2 open child did not fence parent closure",
    ),
    "C3": Fault(
        "test_send.py::test_c3_send_lands_once", "comms.py",
        '"receipt_id": receipt_id, "delivery": delivery,\n            "submission_confirmed": submission_confirmed,',
        '"receipt_id": receipt_id, "delivery": delivery,\n            "submission_confirmed": False,',
        "C3 send reply lost confirmation",
    ),
    "C4": Fault(
        "test_send.py::test_c4_overlapping_send_and_disconnected_initiator", "store.py",
        "OR (? != '' AND optimistic_id=? AND display_text=?",
        "OR (? = '' AND optimistic_id=? AND display_text=?",
        "C4 overlapping physical request did not coalesce",
    ),
    "C5": Fault(
        "test_send.py::test_c5_honest_send_failure", "comms.py",
        'if confirmation_pending:\n                result.update({\n                    "do_not_resubmit": True,',
        'if confirmation_pending:\n                result.update({\n                    "do_not_resubmit": False,',
        "C5 send was not honestly marked unconfirmed",
    ),
    "C6": Fault(
        "test_events.py::test_c6_append_and_live_identity", "ingest.py",
        'return json.dumps(list(_jsonl_event_identity(payload)), separators=(",", ":"))',
        'return json.dumps([*_jsonl_event_identity(payload), id(payload)], separators=(",", ":"))',
        "C6 live event identity or provider record deduplication changed",
    ),
    "C7": Fault(
        "test_events.py::test_c7_history_and_reconnect", "server.py",
        "remaining = limit\n        cursor = before_daemon_seq",
        "remaining = limit + 1\n        cursor = before_daemon_seq",
        "C7 history total exceeded the requested limit",
    ),
    "C8": Fault(
        "test_lifecycle.py::test_c8_restart_continuity", "server.py",
        '"sessions": snapshot_sessions,',
        '"sessions": [],',
        "C8 first snapshot did not restore exactly the surviving fixture seat",
    ),
    "C9": Fault(
        "test_send.py::test_c9_send_receipts_across_restart", "comms.py",
        '"delivery": winner_delivery,\n            "submission_confirmed": winner_confirmed,',
        '"delivery": "accepted",\n            "submission_confirmed": winner_confirmed,',
        "C9 acknowledged replay lost its landed state",
    ),
    "C10": Fault(
        "test_seat_auth.py::test_c10_retired_seat_is_fenced", "server.py",
        'raise VerbError("compact_self_only", "compact requires the calling seat\'s token")',
        'raise VerbError("unknown_session", "compact requires the calling seat\'s token")',
        "C10 a retired token could still compact",
    ),
    "C11": Fault(
        "test_seat_auth.py::test_c11_authenticated_tell_is_deduplicated", "comms.py",
        '\n            return {**prior["reply"], "duplicate": True}',
        '\n            return {**prior["reply"], "duplicate": False}',
        "C11 identical tell was not recognized as a duplicate",
    ),
    "C12": Fault(
        "test_pressure.py::test_c12_slow_consumer_isolation", "server.py",
        'self._close_ws(websocket, 1011, "slow_consumer")',
        'self._close_ws(websocket, 1012, "service_restart")',
        "C12 blocked subscriber did not receive the slow-consumer close",
    ),
}


def _git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args])


def _run(service, output, name, nodes, workers):
    junit = output / f"{name}.xml"
    log = output / f"{name}.log"
    env = dict(os.environ)
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(key, None)
    with log.open("wb") as stream:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=short", "--timeout-method=signal",
             "-p", "no:rerunfailures", "-n", str(workers), "--max-worker-restart=0",
             f"--junitxml={junit}", *nodes],
            cwd=service, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=180,
        )
    return result.returncode, ET.parse(junit).findall(".//testcase")


def _assert_baseline(result, selected):
    code, cases = result
    names = {FAULTS[case].node.split("::")[1] for case in selected}
    if code != 0 or len(cases) != len(names) or {node.get("name") for node in cases} != names:
        raise RuntimeError("the unchanged contract cases did not all pass")
    if any(len(node) for node in cases):
        raise RuntimeError("the unchanged contract cases included a failure, error or skip")


def _fault(service, output, case, fault, identity):
    path = service / fault.source
    original = path.read_bytes()
    source = original.decode("utf-8")
    if source.count(fault.before) != 1:
        raise RuntimeError(f"{case}: fault anchor does not apply exactly once in {fault.source}")
    path.write_bytes(source.replace(fault.before, fault.after, 1).encode("utf-8"))
    try:
        patch = _git(service, "diff", "--", fault.source)
        if not patch:
            raise RuntimeError(f"{case}: fault did not change the subject")
        (output / f"{case}.diff").write_bytes(patch)
        code, cases = _run(service, output, case, [f"tests/contract/{fault.node}"], 0)
    finally:
        path.write_bytes(original)
    failure = cases[0].find("failure") if len(cases) == 1 else None
    detected = (
        code == 1 and failure is not None
        and cases[0].get("name") == fault.node.split("::")[1]
        and cases[0].find("error") is None
        and fault.assertion in failure.get("message", "")
        and failure.get("message", "").startswith("AssertionError")
    )
    return {**identity, "case": case, "node": f"tests/contract/{fault.node}",
            "patch_sha256": hashlib.sha256(patch).hexdigest(), "detected": detected,
            "assertion": failure.get("message") if failure is not None else None, "returncode": code}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True, help="new directory outside the checkout")
    parser.add_argument("--case", choices=FAULTS, action="append", help="select a fault (default: all)")
    args = parser.parse_args()
    output = args.evidence.resolve()
    if output.is_relative_to(REPO):
        parser.error("--evidence must be outside the checkout")
    if _git(REPO, "status", "--porcelain"):
        parser.error("the subject checkout must be clean")
    selected = args.case or list(FAULTS)
    if len(selected) != len(set(selected)):
        parser.error("each --case must be unique")
    output.mkdir(parents=True)
    commit = _git(REPO, "rev-parse", "HEAD").decode().strip()
    identity = {"base_commit": commit, "runner": platform.platform(), "python": platform.python_version()}
    lines = output / "detections.jsonl"
    with tempfile.TemporaryDirectory(prefix="chat-contract-faults-") as temporary, lines.open("w") as receipt:
        checkout = Path(temporary) / "checkout"
        _git(REPO, "clone", "--quiet", "--local", "--no-checkout", str(REPO), str(checkout))
        _git(checkout, "checkout", "--quiet", "--detach", commit)
        service = checkout / SERVICE.relative_to(REPO)
        nodes = [f"tests/contract/{FAULTS[case].node}" for case in selected]
        _assert_baseline(_run(service, output, "unchanged", nodes, 2), selected)
        green = True
        for case in selected:
            record = _fault(service, output, case, FAULTS[case], identity)
            receipt.write(json.dumps(record) + "\n")
            receipt.flush()
            print(f"{case}: {'detected' if record['detected'] else 'failed detection'}", flush=True)
            green &= record["detected"]
    return 0 if green else 1


if __name__ == "__main__":
    sys.exit(main())
