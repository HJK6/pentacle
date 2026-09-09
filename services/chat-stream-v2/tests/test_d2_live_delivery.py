"""A journey report enumerates failures even after the journey aborts."""
from types import SimpleNamespace
import json

import pytest

from tools import d2_live_delivery as d2


@pytest.fixture
def journey(tmp_path, monkeypatch):
    args = SimpleNamespace(candidate_sha="candidate", pid=123, gate_tell="check",
                           gate_ledger_row="check", ci_run="check", qa_report="check",
                           qa_digest="check", deadline_seconds=1800,
                           artifact_root=tmp_path, runtime_checkout=tmp_path,
                           url="ws://isolated", parent_stream_id="hosta:lead")
    j = d2.D2Journey(args)
    monkeypatch.setattr(d2.subprocess, "check_output", lambda *a, **kw: "candidate")
    monkeypatch.setattr(j, "_runtime_identity", lambda phase: j._check("runtime_" + phase, True))
    monkeypatch.setattr(j, "_operator", lambda *a, **kw: ({"active": []}, {"type": "snapshot", "capabilities": {"close_expected_generation": True}}))
    return j


def test_failed_journey_still_runs_all_final_observations(journey, monkeypatch):
    """A failed catalog admission still inspects cleanup and inventory."""
    with pytest.raises(Exception):
        journey.run()
    receipt = json.loads((journey.out / "receipt.json").read_text())["d2"]
    assert "runtime_after" in receipt["checks"]
    names = {item["check"] for item in receipt["failures"]}
    assert "journey" in names
    assert "teardown_closed_expected" in names
    assert "foreign_inventory_unmutated_by_lane" in receipt["checks"]


def test_organic_foreign_inventory_change_allowed(journey, monkeypatch):
    before = [{"stream_id": "hosta:foreign", "session_generation": "old"}]
    journey.foreign_inventory_before = journey._foreign_inventory_projection(before)
    monkeypatch.setattr(journey, "_operator", lambda *a, **kw: ({"active": []}, None))
    journey._check_foreign_inventory({"sessions": []})
    proof = journey.result["checks"]["foreign_inventory_unmutated_by_lane"]
    assert proof["passed"] is True
    assert proof["inventory_equal"] is False
    assert proof["before_sha256"] != proof["after_sha256"]


def test_foreign_cleanup_identity_rejected(journey):
    journey.foreign_inventory_before = []
    with pytest.raises(AssertionError, match="foreign"):
        journey._check_foreign_inventory({"sessions": [{"stream_id": "hosta:foreign"}]})


def test_collector_preserves_every_failure_and_later_check(journey):
    def fail():
        raise AssertionError("bad predicate")
    journey._observe("first", fail)
    journey._observe("second", fail)
    journey._observe("last", lambda: journey._check("last", True))
    assert [x["check"] for x in journey.result["failures"]] == ["first", "second"]
    assert journey.result["checks"]["last"] is True


@pytest.mark.parametrize("urgent,delivery,end_at,passes", [
    (True, 120, 150, True), (True, 160, 150, False),
    (False, 120, 150, False), (False, 160, 150, True),
])
def test_busy_delivery_uses_delivery_time_not_later_proof(journey, monkeypatch, urgent, delivery, end_at, passes):
    from datetime import datetime, timezone
    stamp = lambda seconds: datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    start, end = journey.out / "busy.start", journey.out / "busy.end"
    start.write_text("100")
    end.write_text(str(end_at))
    monkeypatch.setattr(d2.time, "time", lambda: 250)
    journey.busy_observations["hosta:owned"] = [{"at": 115, "working": True}]
    notice = {"created_at": stamp(110), "delivered_at": stamp(delivery), "kind": "wake_urgent" if urgent else "wake"}
    check = lambda: journey._prove_busy_delivery("hosta:owned", start, end, notice, "delivery", require_during_busy=urgent, require_queue_drain=not urgent)
    if passes:
        check()
        assert journey.result["checks"]["delivery"]["delivered_at"] == delivery
    else:
        with pytest.raises(AssertionError):
            check()


def test_runtime_identity_can_differ_from_helper_source(journey, monkeypatch):
    monkeypatch.setattr(d2.subprocess, "check_output", lambda argv, **kw: "candidate" if argv[0] == "git" else "/workspace/example/services/chat-stream-v2/main.py")
    journey.args.runtime_sha = "candidate"
    d2.D2Journey._runtime_identity(journey, "after")
    journey.args.runtime_sha = "wrong"
    with pytest.raises(RuntimeError, match="identity drift"):
        d2.D2Journey._runtime_identity(journey, "after")


@pytest.mark.parametrize("query_fails", [False, True])
@pytest.mark.parametrize("reader", ["journey", "closed_row"])
def test_sql_polling_releases_handles_without_garbage_collection(journey, monkeypatch, tmp_path, query_fails, reader):
    import gc
    import os
    import sqlite3
    db = tmp_path / "polling.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE sample (value INTEGER)")
    if not query_fails:
        connection.execute("CREATE TABLE sessions (host TEXT, session_name TEXT, status TEXT)")
        connection.execute("INSERT INTO sessions VALUES ('fixture', 'owned', 'closed')")
        connection.commit()
    connection.close()
    monkeypatch.setattr(d2, "LIVE_DB", db)
    gc.collect()
    enabled = gc.isenabled()
    gc.disable()
    try:
        before = len(os.listdir("/dev/fd"))
        for _ in range(20):
            if reader == "closed_row":
                window = SimpleNamespace(session_db=db)
                owned = SimpleNamespace(host="fixture", session_name="owned")
                assert d2.LiveWindow._closed_row(window, owned) is (not query_fails)
            elif query_fails:
                with pytest.raises(sqlite3.OperationalError):
                    journey._sql("SELECT * FROM absent_table")
            else:
                assert journey._sql("SELECT 1 AS value") == [{"value": 1}]
        after = len(os.listdir("/dev/fd"))
        assert after <= before + 2, f"SQL polling retained {after - before} file descriptors"
    finally:
        gc.collect()
        if enabled:
            gc.enable()
