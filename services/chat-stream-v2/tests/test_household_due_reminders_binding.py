"""Untrusted local hint must agree with an authenticated current-seat binding read."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

import household_due_reminders as tool
from test_household_due_reminders import DAY, config, population, row, state


@pytest.fixture
def setup(config, tmp_path):
    config["sessions_db"] = str(tmp_path / "sessions.db")
    config["binding_name"] = "assistant"
    with sqlite3.connect(config["sessions_db"]) as db:
        db.execute("CREATE TABLE v2_assistant_direct_binding (name TEXT PRIMARY KEY,stream_id TEXT,generation TEXT,revision INTEGER)")
        db.execute("INSERT INTO v2_assistant_direct_binding VALUES(?,?,?,?)", ("assistant", "fixture-host:desk-a", "gen-a", 1))
    for stream in ("fixture-host:desk-a", "fixture-host:desk-b"):
        path = tmp_path / ".pentacle-stream-tokens" / (tool.digest(stream)[:24]+".token")
        path.parent.mkdir(exist_ok=True); path.write_text("synthetic-token"); path.chmod(0o600)
    return config


def reply(config, **changes):
    return {"type": "assistant.binding.ok", "stream_id": "fixture-host:desk-a", "generation": "gen-a",
            "revision": 1, "composite_stream_id": config["composite_stream_id"], **changes}


@pytest.mark.parametrize("changes", [{"stream_id": "fixture-host:desk-b"}, {"generation": "gen-b"},
                                      {"revision": 2}, {"composite_stream_id": "other:assistant"}])
def test_hint_rpc_disagreement_refuses_every_outbound(setup, monkeypatch, changes):
    wrapper = tool.Publisher(setup)
    calls = []
    def rpc(args, env=None):
        calls.append((args, env))
        return reply(setup, **changes)
    monkeypatch.setattr(wrapper, "rpc", rpc)
    with pytest.raises(tool.Failure, match="binding_disagreement"):
        wrapper.publish("household-due:"+DAY, "Body")
    assert len(calls) == 1 and calls[0][0] == ["assistant", "binding"]


def test_closed_seat_hint_keeps_blocker_unacknowledged_until_recovery(setup, monkeypatch):
    config = setup; wrapper = tool.Publisher(config); valid = False; calls = []
    def rpc(args, env=None):
        calls.append((args, env))
        if args == ["assistant", "binding"]:
            if not valid:
                raise tool.Failure("rpc_refused")
            return reply(config, stream_id="fixture-host:desk-b", generation="gen-b", revision=2)
        if args[:2] == ["assistant", "publish"]:
            return {"type": "assistant.publish.ok", "event_id": 1}
        return {"type": "tell.ok", "action_committed": True, "ledger_row_id": 1}
    monkeypatch.setattr(wrapper, "rpc", rpc)
    from datetime import datetime
    now = datetime.fromisoformat(DAY+"T08:00:00-06:00")
    assert tool.run(config, now, reader=population([row(label="Frozen")]).__getitem__, publisher=wrapper)["status"] == "failed"
    pending = state(config)
    assert pending["days"][DAY]["status"] == "pending"
    assert all(record["receipt"] is None for record in pending["alerts"].values())
    assert all(args == ["assistant", "binding"] for args, _ in calls)
    with sqlite3.connect(config["sessions_db"]) as db:
        db.execute("UPDATE v2_assistant_direct_binding SET stream_id=?,generation=?,revision=2", ("fixture-host:desk-b", "gen-b"))
    valid = True
    assert tool.run(config, now, reader=population([row(label="Changed")]).__getitem__, publisher=wrapper)["status"] == "delivered"
    assert all(record["receipt"] for record in state(config)["alerts"].values())
    published = [args for args, _ in calls if args[:2] == ["assistant", "publish"]]
    assert len(published) == 1 and "Frozen" in published[0][-1] and "Changed" not in published[0][-1]
    assert len([args for args, _ in calls if args[0] == "tell"]) == 1


def test_hint_connection_is_read_only_query_only_and_suppresses_credentials(setup, monkeypatch):
    config = setup; connects = []; queries = []
    original = sqlite3.connect
    def connect(*args, **kwargs):
        connects.append((args, kwargs))
        db = original(*args, **kwargs); db.set_trace_callback(queries.append)
        return db
    monkeypatch.setattr(tool.sqlite3, "connect", connect)
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "fixture-worker:lead")
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "wrong-token")
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN_FILE", "/wrong/token")
    monkeypatch.setenv("AGENT_ORCH_INTERNAL_LEADER_STREAM_ID", "fixture-worker:parent")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture-worker:other")
    monkeypatch.setenv("AGENT_ORCH_TOKEN", "wrong-operator-token")
    wrapper = tool.Publisher(config)
    def rpc(args, env=None):
        assert env["AGENT_ORCH_STREAM_ID"] == "fixture-host:desk-a"
        assert env["AGENT_ORCH_TOKEN"] == ""
        assert "wrong" not in str(env)
        assert "PENTACLE_STREAM_ID" not in env and "AGENT_ORCH_INTERNAL_LEADER_STREAM_ID" not in env
        return reply(config)
    monkeypatch.setattr(wrapper, "rpc", rpc)
    assert wrapper.binding()["stream_id"] == "fixture-host:desk-a"
    assert connects[0][1]["uri"] is True and connects[0][1]["timeout"] <= 2
    assert "mode=ro" in connects[0][0][0]
    assert any("query_only" in query.lower() for query in queries)
    reads = [query for query in queries if query.lower().startswith("select")]
    assert len(reads) == 1 and "stream_id, generation, revision" in reads[0]
    assert "v2_assistant_direct_binding" in reads[0] and "kv" not in reads[0]


@pytest.mark.parametrize("case", ["missing_db", "missing_table", "missing_column", "missing_row", "remote", "malformed", "missing_token"])
def test_unusable_hint_fails_closed_and_never_runs_cli(setup, monkeypatch, case, tmp_path):
    config = setup
    if case == "missing_db":
        config["sessions_db"] = str(tmp_path / "absent.db")
    else:
        with sqlite3.connect(config["sessions_db"]) as db:
            if case == "missing_table": db.execute("DROP TABLE v2_assistant_direct_binding")
            elif case == "missing_column": db.execute("ALTER TABLE v2_assistant_direct_binding RENAME COLUMN generation TO obsolete")
            elif case == "missing_row": db.execute("DELETE FROM v2_assistant_direct_binding")
            elif case == "remote": db.execute("UPDATE v2_assistant_direct_binding SET stream_id='other-host:desk'")
            elif case == "malformed": db.execute("UPDATE v2_assistant_direct_binding SET revision=-1")
    if case == "missing_token":
        (tmp_path / ".pentacle-stream-tokens" / (tool.digest("fixture-host:desk-a")[:24]+".token")).unlink()
    wrapper = tool.Publisher(config)
    def never(*args, **kwargs):
        pytest.fail("CLI must not run without a valid local hint and protected seat token file")
    monkeypatch.setattr(wrapper, "rpc", never)
    with pytest.raises(tool.Failure):
        wrapper.binding()
    if case == "missing_db":
        assert not Path(config["sessions_db"]).exists()
