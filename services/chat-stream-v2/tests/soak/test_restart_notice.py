"""Front-desk restart notice on a real daemon restart
(spec_pentacle__fd_restart_resume_notice_2026_11).

Real `main.py` processes on restart_matrix's disposable harness (isolated DB,
run-owned tmux socket, stub Claude) with the `assistant` composite bound to a
stub front-desk seat through the real spawn verb. Explicitly run, never part of
the unit gate:

    RESTART_MATRIX_EVIDENCE=$TMPDIR/pentacle-fd-restart-notice-evidence \\
        PENTACLE_FORCE_LIVE_DAEMON=1 python3 -m pytest tests/soak/test_restart_notice.py -q

Cell A (`daemon_restart`): a child pane is killed, the daemon is SIGKILLed and
restarted on the same DB; the new process sends exactly one notice naming the
killed child `gone`. Cell B (`deploy`): the isolated DB's marker `daemon_sha` is
rewritten (a marker stand-in, not a production deployment) and the daemon
restarted with the same boot time. Each cell writes the notice text and its
sha256 to the evidence directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.soak import restart_matrix as rm
from tests.soak.fd_resume_matrix import FdCell, sid

pytestmark = [pytest.mark.live_daemon, pytest.mark.timeout(900)]

MARKER_KEY = "daemon.restart_notice.last"
STAND_IN_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def evidence(tmp_path, request):
    root = os.environ.get("RESTART_MATRIX_EVIDENCE")
    path = Path(root) / request.node.name if root else tmp_path / "evidence"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _marker(cell: rm.Cell) -> dict[str, Any] | None:
    rows = cell.sql("SELECT v FROM kv WHERE k=?", (MARKER_KEY,))
    return json.loads(rows[0]["v"]) if rows else None


def _new_marker(cell: rm.Cell, previous_instance: str | None, timeout: float = 60) -> dict[str, Any] | None:
    return rm.wait_for(lambda: (lambda m: m if m and m.get("instance_id") != previous_instance else None)(
        _marker(cell)), timeout, 0.2)


def _notice_rows(cell: rm.Cell, tell_id: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "queued": cell.sql("SELECT tell_id, from_stream_id, body FROM v2_assistant_composite_tell_queue "
                           "WHERE tell_id=?", (tell_id,)),
        "delivered": cell.sql("SELECT tell_id, reply FROM v2_tell_deliveries WHERE tell_id=?", (tell_id,)),
        "all_notice_tells": cell.sql(
            "SELECT tell_id FROM v2_tell_deliveries WHERE tell_id LIKE 'restart-notice:%' "
            "UNION ALL SELECT tell_id FROM v2_assistant_composite_tell_queue WHERE tell_id LIKE 'restart-notice:%'"),
    }


def _notice_text(cell: rm.Cell, fd_name: str, marker: dict[str, Any], rows: dict[str, list]) -> str:
    if rows["queued"]:
        return rows["queued"][0]["body"]
    # Delivered: the stub front desk recorded the pasted text. The wire carries
    # no tell_id, so match this process's header (kind + its own start time).
    header = f"BLOCKER restart from daemon: {marker['kind']} — "
    started = f"; daemon started {marker['started_at']} sha "
    matches = [item["text"] for item in cell.inputs(fd_name)
               if header in item["text"] and started in item["text"]]
    if not matches:
        return ""
    text = matches[-1]
    return text[text.find(header):]


def _keep_notice(evidence: Path, label: str, text: str, extra: dict[str, Any]) -> str:
    digest = hashlib.sha256(text.encode()).hexdigest()
    (evidence / f"notice-{label}.txt").write_text(text)
    (evidence / f"notice-{label}.json").write_text(json.dumps({"sha256": digest, **extra}, indent=1, default=str))
    return digest


def _wait_notice(cell: rm.Cell, fd_name: str, marker: dict[str, Any], timeout: float = 30) -> tuple[dict, str]:
    tell_id = marker["notice_tell_id"]
    found = rm.wait_for(lambda: (lambda r: r if (r["queued"] or r["delivered"]) else None)(
        _notice_rows(cell, tell_id)), timeout, 0.2) or _notice_rows(cell, tell_id)
    text = rm.wait_for(lambda: _notice_text(cell, fd_name, marker, _notice_rows(cell, tell_id)), timeout, 0.2) or ""
    return found, text


def _bound_cell(cell: rm.Cell) -> FdCell:
    fd = FdCell(cell, assistant_role=None)
    cell.daemon.extra_env["PENTACLE_FD_CHECKPOINT_PATH"] = str(cell.root / "checkpoint-current.md")
    (cell.root / "checkpoint-current.md").write_text("checkpoint body never copied\n")
    cell.daemon.start()  # composite disabled: no notice
    fd.seed("fd", role="lead")
    fd.seed("daff", role="daff-assistant")
    fd.seed("childa", parent="fd")
    fd.seed("childb", parent="fd")
    fd.configure_composites("fd", "daff")  # SIGTERM + start bound: first_instrumented_start
    return fd


def _classify(record: dict[str, Any]) -> dict[str, Any]:
    failed = [k for k, v in record["checks"].items() if not v]
    record["failed_checks"] = failed
    record["classification"] = rm.PASS if not failed else rm.PRODUCT_FAIL
    return record


def run_cells(root: Path, evidence: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with rm.cell_env(root, evidence) as cell:
        fd = _bound_cell(cell)
        first = _new_marker(cell, None)
        first_rows, first_text = _wait_notice(cell, "fd", first) if first else ({}, "")
        first_sha = _keep_notice(evidence, "first-start", first_text, {"marker": first, "accounted": "separately"})

        # Cell A: kill one child's pane, SIGKILL the daemon, restart on the same DB.
        cell.namespace.run("kill-session", "-t", "=childb")
        gone = rm.wait_for(lambda: "childb" not in cell.namespace.session_names(), 10, 0.05)
        stop_a = cell.daemon.stop(signal.SIGKILL)
        cell.daemon.start()
        marker_a = _new_marker(cell, (first or {}).get("instance_id"))
        rows_a, text_a = _wait_notice(cell, "fd", marker_a) if marker_a else ({"all_notice_tells": []}, "")
        tell_a = (marker_a or {}).get("notice_tell_id", "")
        sha_a = _keep_notice(evidence, "cell-A-daemon_restart", text_a, {"marker": marker_a, "stop": stop_a})
        lines_a = text_a.splitlines()
        rec_a = {"cell": "A", "expected_kind": "daemon_restart", "marker": marker_a, "notice_sha256": sha_a,
                 "first_start": {"marker": first, "notice_sha256": first_sha}, "stop": stop_a,
                 "notice_rows": {k: len(v) for k, v in rows_a.items()}}
        rec_a["checks"] = {
            "first_start_marker": bool(first) and first.get("kind") == "first_instrumented_start",
            "first_start_notice": first_text.startswith("BLOCKER restart from daemon: first_instrumented_start"),
            "child_pane_killed": bool(gone),
            "new_instance_marker": bool(marker_a) and "error" not in marker_a,
            "kind_daemon_restart": (marker_a or {}).get("kind") == "daemon_restart",
            "tell_id_is_instance": tell_a == f"restart-notice:{(marker_a or {}).get('instance_id')}",
            "exactly_one_tell_for_instance": len(rows_a.get("queued", [])) + len(rows_a.get("delivered", [])) == 1,
            "header_kind": text_a.startswith("BLOCKER restart from daemon: daemon_restart — "),
            "killed_child_gone": any(l.startswith(f"  {sid('childb')} ") and " gone parent=FD" in l for l in lines_a),
            "live_child_alive": any(l.startswith(f"  {sid('childa')} ") and " alive parent=FD" in l for l in lines_a),
            "front_desk_alive": f"Front desk: {sid('fd')} generation " in text_a and " pane alive;" in text_a,
            "checkpoint_path_only": "Checkpoint: " in text_a and "checkpoint body never copied" not in text_a,
        }
        records.append(_classify(rec_a))

        # Cell B: marker stand-in for a deploy (rewrite only the isolated DB's sha).
        stop_b = cell.daemon.stop(signal.SIGTERM)
        conn = sqlite3.connect(cell.daemon.db, timeout=10)
        try:
            stored = json.loads(conn.execute("SELECT v FROM kv WHERE k=?", (MARKER_KEY,)).fetchone()[0])
            real_sha = stored.get("daemon_sha")
            stored["daemon_sha"] = STAND_IN_SHA
            conn.execute("UPDATE kv SET v=? WHERE k=?", (json.dumps(stored), MARKER_KEY))
            conn.commit()
        finally:
            conn.close()
        cell.daemon.start()
        marker_b = _new_marker(cell, (marker_a or {}).get("instance_id"))
        rows_b, text_b = _wait_notice(cell, "fd", marker_b) if marker_b else ({"all_notice_tells": []}, "")
        sha_b = _keep_notice(evidence, "cell-B-deploy", text_b, {
            "marker": marker_b, "stop": stop_b,
            "stand_in": f"marker daemon_sha rewritten to {STAND_IN_SHA} in the isolated DB only; "
                        "not a production deployment"})
        rec_b = {"cell": "B", "expected_kind": "deploy", "marker": marker_b, "notice_sha256": sha_b,
                 "stand_in_sha": STAND_IN_SHA, "real_sha": real_sha,
                 "notice_rows": {k: len(v) for k, v in rows_b.items()}}
        rec_b["checks"] = {
            "new_instance_marker": bool(marker_b) and "error" not in marker_b,
            "kind_deploy": (marker_b or {}).get("kind") == "deploy",
            "boot_time_unchanged": (marker_b or {}).get("boot_time") == (marker_a or {}).get("boot_time"),
            "exactly_one_tell_for_instance": len(rows_b.get("queued", [])) + len(rows_b.get("delivered", [])) == 1,
            "header_names_previous_sha": text_b.startswith("BLOCKER restart from daemon: deploy — ")
                                         and text_b.splitlines()[0].endswith(f"sha {STAND_IN_SHA[:7]}"),
            "one_notice_per_process": len(rows_b.get("all_notice_tells", [])) == 3,
        }
        records.append(_classify(rec_b))
        (evidence / "records.json").write_text(json.dumps(records, indent=1, default=str))
        fd.keep_db()
    return records


def test_restart_notice_cells(tmp_path, evidence):
    records = run_cells(tmp_path / "env", evidence)
    assert [r["cell"] for r in records] == ["A", "B"]
    for record in records:
        detail = {k: record.get(k) for k in ("cell", "classification", "failed_checks", "marker")}
        assert record["classification"] == rm.PASS, json.dumps(detail, default=str)
