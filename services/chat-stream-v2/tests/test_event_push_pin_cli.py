"""The pin writer consumes canonical local evidence before enforcing a release."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("event_push_pin", SERVICE_DIR / "tools" / "event_push_pin.py")
assert SPEC and SPEC.loader
event_push_pin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(event_push_pin)
TARGET = "b" * 40
PREVIOUS = "a" * 40


def evidence(sha=TARGET):
    return {"schema": "pentacle.v2.gate-evidence.v1", "gate": "merge", "sha": sha,
            "source": {"sha_before": sha, "sha_after": sha, "clean_before": True, "clean_after": True},
            "tiers": [{"tier": "unit", "passed": True}, {"tier": "smoke", "passed": True}], "passed": True}


def seed(db, *, previous=PREVIOUS, stage=False):
    async def go():
        store = event_push_pin.Store(str(db))
        store.start()
        try:
            if previous is not None:
                await store.put("event_push.target_sha", previous)
            if stage:
                await store.stage_event_push_target_sha(TARGET)
            for host in event_push_pin.SATELLITE_HOSTS:
                await store.put(f"event_push.runtime.{host}", json.dumps({
                    "sha": TARGET, "pid": 4321, "observed_at": "2030-01-01T00:00:00Z",
                    "observed_at_epoch": time.time() + 60,
                }))
        finally:
            store.stop()
    asyncio.run(go())


def pins(db):
    with sqlite3.connect(db) as conn:
        return dict(conn.execute("SELECT k,v FROM kv WHERE k IN "
                                "('event_push.target_sha','event_push.target_sha.previous')"))


def test_stage_uses_local_evidence_and_never_queries_remote_tags(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(event_push_pin, "SATELLITE_HOSTS", ("workstation",))
    db = tmp_path / "sessions.db"
    seed(db)
    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps(evidence()))

    def smoke_only(command, **kwargs):
        assert command[1].endswith("spawn_fleet_smoke.py"), command
        return subprocess.CompletedProcess(command, 0, json.dumps({
            "ok": True, "status": "PASS", "failures": [], "untested": [],
        }), "")

    monkeypatch.setattr(event_push_pin.subprocess, "run", smoke_only)
    assert event_push_pin.main(["--db", str(db), "--stage", TARGET, "--gate-evidence", str(gate)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["previous"] == PREVIOUS and result["readback"] == TARGET
    assert pins(db) == {"event_push.target_sha": TARGET, "event_push.target_sha.previous": PREVIOUS}


@pytest.mark.parametrize("bad", ["missing", "unreadable", "malformed", "nonpassing", "wrong-sha",
                                    "wrong-source", "dirty-before", "dirty-after", "missing-tier", "wrong-gate"])
@pytest.mark.parametrize("rollback", [False, True])
def test_invalid_evidence_refuses_before_pin_mutation(tmp_path, monkeypatch, bad, rollback):
    monkeypatch.setattr(event_push_pin, "SATELLITE_HOSTS", ("workstation",))
    db = tmp_path / "sessions.db"
    seed(db, stage=rollback)
    before = pins(db)
    gate = tmp_path / "gate.json"
    payload = copy.deepcopy(evidence(PREVIOUS if rollback else TARGET))
    if bad == "nonpassing": payload["passed"] = False
    if bad == "wrong-sha": payload = evidence("c" * 40)
    if bad == "wrong-source": payload["source"]["sha_after"] = "c" * 40
    if bad == "dirty-before": payload["source"]["clean_before"] = False
    if bad == "dirty-after": payload["source"]["clean_after"] = False
    if bad == "missing-tier": payload["tiers"] = [{"tier": "unit", "passed": True}]
    if bad == "wrong-gate": payload["gate"] = "unit"
    if bad != "unreadable": gate.write_text("{broken" if bad == "malformed" else json.dumps(payload))
    args = ["--db", str(db), *( ["--rollback"] if rollback else ["--stage", TARGET])]
    if bad != "missing": args += ["--gate-evidence", str(gate)]
    with pytest.raises(SystemExit) as raised:
        event_push_pin.main(args)
    assert raised.value.code == 2
    assert pins(db) == before


def test_nonempty_rollback_requires_evidence_for_captured_previous_sha(tmp_path, capsys):
    db = tmp_path / "sessions.db"
    seed(db, stage=True)
    gate = tmp_path / "previous-gate.json"
    gate.write_text(json.dumps(evidence(PREVIOUS)))
    assert event_push_pin.main(["--db", str(db), "--rollback", "--gate-evidence", str(gate)]) == 0
    assert json.loads(capsys.readouterr().out)["readback"] == PREVIOUS
    assert pins(db)["event_push.target_sha"] == PREVIOUS


def test_rollback_to_initial_absent_pin_survives_store_restart(tmp_path, capsys):
    """No release artifact is activated when undoing first-time enforcement."""
    db = tmp_path / "sessions.db"
    seed(db, previous=None, stage=True)
    assert event_push_pin.main(["--db", str(db), "--rollback"]) == 0
    assert json.loads(capsys.readouterr().out) == {"action": "rollback", "target_sha": None, "readback": ""}
    assert pins(db)["event_push.target_sha"] == ""
