"""Real collector/runner/publish wrapper; isolate only Cosmo, RPC and clock counterparts."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sqlite3
from datetime import datetime

import pytest

import household_due_reminders as tool

DAY = "2026-11-12"


def row(ident=1, **kw):
    return {"id": ident, "list": "chores", "label": f"Chore {ident}", "scope": "owner",
            "done_at": None, "due_date": DAY, "routine_id": None, **kw}


def routine(ident=2, **kw):
    return {"id": ident, "list": "chores", "label": f"Routine {ident}", "scope": "owner",
            "paused": False, "next_due": DAY, "item_id": None, **kw}


def population(items=(), routines=()):
    lists = {name: [] for name in tool.LISTS}
    for item in items:
        lists[item["list"]].append(item)
    return {**{f"/lists/{name}/items": values for name, values in lists.items()}, "/routines": list(routines)}


class Sink:
    def __init__(self):
        self.published = {}
        self.told = {}
        self.publish_attempts = []
        self.tell_attempts = []
        self.fail_publish = False
        self.fail_tell = False
        self.ambiguous = False

    def publish(self, key, body):
        self.publish_attempts.append((key, body))
        if self.fail_publish:
            raise tool.Failure("publish_unconfirmed")
        if key in self.published:
            assert self.published[key] == body
        self.published[key] = body
        if self.ambiguous:
            self.ambiguous = False
            raise tool.Failure("rpc_unconfirmed")
        return {"type": "assistant.publish.ok", "request_id": key, "message_id": "fixture-message"}

    def tell(self, key, body):
        self.tell_attempts.append((key, body))
        if self.fail_tell:
            raise tool.Failure("tell_unconfirmed")
        if key in self.told:
            assert self.told[key] == body
        self.told[key] = body
        return {"type": "tell.ok", "tell_id": key, "ledger_row_id": 1, "action_committed": True}


@pytest.fixture
def config(tmp_path):
    return {"enabled": True, "not_before": DAY, "state_root": str(tmp_path / "production"),
            "self_person": "owner", "cosmo_url": "https://household.example.invalid",
            "cosmo_token_file": str(tmp_path / "cosmo.token"), "host_id": "fixture-host",
            "seat_cwd": str(tmp_path), "composite_stream_id": "fixture-chat:assistant",
            "ws_url": "ws://127.0.0.1:7799", "agent_orch": ["agent-orch"]}


def invoke(config, data=None, sink=None, at=DAY + "T08:00:00-06:00", **kwargs):
    return tool.run(config, datetime.fromisoformat(at), reader=(data or population()).__getitem__,
                    publisher=sink or Sink(), **kwargs)


def state(config):
    return json.loads((Path(config["state_root"]) / "delivery.json").read_text())


def test_complete_population_selection_sort_privacy_and_format():
    items = [row(1, list="tasks", due_date="2026-11-10"), row(2, list="grocery"), row(3, list="meals"),
             row(4), row(5, list="study"), row(6, due_date="2026-11-13"), row(7, due_date=None),
             row(8, done_at="2026-11-12T14:00:00Z"), row(9, scope="other"), row(10, scope="shared")]
    data = population(items, [routine(20), routine(21, paused=True), routine(22, next_due="2026-12-05")])
    calls = []
    def read(path):
        calls.append(path)
        return data[path]
    rows = tool.collect(read, "owner", DAY)
    assert calls == [f"/lists/{name}/items" for name in tool.LISTS] + ["/routines"]
    assert [r["id"] for r in rows] == [1, 2, 3, 4, 10, 20, 5]
    text = tool.format_message(rows, DAY)
    assert text.startswith("Due today · Thu, Nov 12\n• Chore 1 (overdue since Nov 10)")
    assert "Routine 20" in text and "Routine 21" not in text and "Routine 22" not in text
    assert "owner" not in text


def test_done_window_and_pausing_do_not_hide_existing_open_item():
    data = population([row(1, routine_id=2)], [routine(2, item_id=1, paused=True)])
    assert len(tool.collect(data.__getitem__, "owner", DAY)) == 1
    data["/lists/chores/items"][0]["done_at"] = "2026-11-12T14:00:00Z"
    assert tool.collect(data.__getitem__, "owner", DAY) == []
    data = population([], [routine(2, next_due="2026-11-19")])
    assert tool.collect(data.__getitem__, "owner", DAY) == []
    assert len(tool.collect(data.__getitem__, "owner", "2026-11-19")) == 1


@pytest.mark.parametrize("broken", [
    population([row(1, routine_id=2)], [routine(2)]),
    population([], [routine(2, item_id=1)]),
    population([row(1, routine_id=2)], []),
    population([row(1, routine_id=2), row(3, routine_id=2)], [routine(2, item_id=1)]),
])
def test_inconsistent_reads_retry_once_then_fail(broken):
    calls = []
    def read(route):
        calls.append(route)
        return broken[route]
    with pytest.raises(tool.Failure, match="inconsistent_cosmo"):
        tool.collect(read, "owner", DAY)
    assert len(calls) == 12


def test_one_reread_can_resolve_materialization_race():
    calls = 0
    bad = population([row(1, routine_id=2)], [routine(2)])
    good = population([row(1, routine_id=2)], [routine(2, item_id=1)])
    def read(route):
        nonlocal calls
        calls += 1
        return (bad if calls <= 6 else good)[route]
    assert len(tool.collect(read, "owner", DAY)) == 1
    assert calls == 12


@pytest.mark.parametrize("changes", [{"due_date": "bad"}, {"done_at": 3}, {"done_at": "not-a-time"}, {"done_at": "2026-11-12T08:00:00"}, {"label": ""}, {"id": True},
                                     {"routine_id": "3"}])
def test_malformed_visible_item_fails(changes):
    with pytest.raises(tool.Failure, match="malformed_cosmo"):
        tool.collect(population([row(**changes)]).__getitem__, "owner", DAY)


def test_empty_is_silent_and_closed_for_today(config):
    sink = Sink()
    assert invoke(config, sink=sink)["status"] == "empty"
    assert invoke(config, population([row()]), sink)["status"] == "empty"
    assert sink.publish_attempts == sink.tell_attempts == []


def test_once_each_morning_and_overdue_repeats_tomorrow(config):
    sink = Sink(); data = population([row()])
    assert invoke(config, data, sink)["status"] == "delivered"
    assert invoke(config, data, sink)["status"] == "delivered"
    assert len(sink.publish_attempts) == 1
    assert invoke(config, data, sink, at="2026-11-13T08:00:00-06:00")["status"] == "delivered"
    assert len(sink.published) == 2
    assert "overdue since Nov 12" in sink.published["household-due:2026-11-13"]


def test_ambiguous_publication_freezes_key_and_body_across_restart(config):
    sink = Sink(); sink.ambiguous = True
    assert invoke(config, population([row(label="Original")]), sink)["status"] == "failed"
    pending = state(config)["days"][DAY]
    assert pending["status"] == "pending"
    assert invoke(config, population([row(label="Changed")]), sink)["status"] == "delivered"
    assert len(sink.published) == 1
    assert sink.publish_attempts[0] == sink.publish_attempts[1]
    assert "Original" in sink.publish_attempts[-1][1]
    assert state(config)["days"][DAY]["receipt"]["type"] == "assistant.publish.ok"


def test_partial_read_fails_and_blocker_is_durable_until_ack(config):
    sink = Sink(); sink.fail_tell = True
    calls = []
    def read(route):
        calls.append(route)
        if route == "/lists/meals/items":
            raise tool.Failure("cosmo_read_failed")
        return []
    assert tool.run(config, datetime.fromisoformat(DAY+"T08:00:00-06:00"), reader=read, publisher=sink)["status"] == "failed"
    assert sink.published == {}
    assert state(config)["days"] == {}
    assert len(state(config)["alerts"]) == 1
    sink.fail_tell = False
    assert invoke(config, sink=sink)["status"] == "empty"
    assert len(sink.told) == 1
    assert len(sink.tell_attempts) == 2
    assert all(a["receipt"] for a in state(config)["alerts"].values())
    invoke(config, sink=sink)
    assert len(sink.tell_attempts) == 2


def test_publish_failure_never_marks_delivered(config):
    sink = Sink(); sink.fail_publish = True
    assert invoke(config, population([row()]), sink)["status"] == "failed"
    assert state(config)["days"][DAY]["status"] == "pending"
    assert len(sink.told) == 1
    assert "BLOCKER" in next(iter(sink.told.values()))


def test_cutoff_and_recovery_missed_days_never_publish_stale(config):
    sink = Sink()
    invoke(config, population([row()]), sink, at="2026-11-14T07:00:00-06:00")
    assert len(sink.told) == 2
    assert set(state(config)["days"]) == {"2026-11-12", "2026-11-13"}
    assert invoke(config, population([row()]), sink, at="2026-11-14T12:00:00-06:00")["status"] == "missed"
    assert sink.published == {}
    assert len(sink.told) == 3
    invoke(config, population([row()]), sink, at="2026-11-14T23:00:00-06:00")
    assert len(sink.told) == 3


def test_pending_at_noon_becomes_missed_and_never_replays(config):
    sink = Sink(); sink.fail_publish = True
    invoke(config, population([row()]), sink)
    sink.fail_publish = False
    assert invoke(config, population([row()]), sink, at=DAY+"T12:00:00-06:00")["status"] == "missed"
    assert len(sink.publish_attempts) == 1
    assert state(config)["days"][DAY]["body"]


def test_chicago_window_dst_and_midnight(config):
    config["not_before"] = "2026-11-01"
    sink = Sink()
    assert invoke(config, sink=sink, at="2026-11-01T13:59:00+00:00")["status"] == "before_window"
    assert invoke(config, sink=sink, at="2026-11-01T14:00:00+00:00")["status"] == "empty"
    config["not_before"] = "2027-03-14"
    assert invoke(config, sink=sink, at="2027-03-14T12:59:00+00:00")["status"] == "before_window"
    assert invoke(config, sink=sink, at="2027-03-14T13:00:00+00:00")["status"] == "empty"


def test_overlap_returns_without_any_delivery(config):
    root = Path(config["state_root"]); root.mkdir(mode=0o700)
    with (root / "lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sink = Sink()
        assert invoke(config, population([row()]), sink)["status"] == "locked"
        assert not sink.publish_attempts


def test_rehearsal_only_fd_and_separate_state(config, tmp_path):
    sink = Sink(); test_root = tmp_path / "rehearsal"
    assert invoke(config, population([row()]), sink, to_fd=True, state_root=str(test_root))["status"] == "delivered"
    invoke(config, population([row()]), sink, to_fd=True, state_root=str(test_root))
    assert sink.published == {} and len(sink.told) == 1
    assert not Path(config["state_root"]).exists()
    with pytest.raises(tool.Failure, match="test_state_required"):
        invoke(config, to_fd=True, state_root=config["state_root"])
    with pytest.raises(tool.Failure, match="production_state_override_refused"):
        invoke(config, state_root=str(test_root))
    assert tool.main(["--config", "missing", "--once", "--as-of", DAY+"T08:00:00-06:00"]) == 1


def test_disabled_and_not_before_prevent_delivery(config):
    sink = Sink(); config["enabled"] = False
    assert invoke(config, population([row()]), sink)["status"] == "disabled"
    config["enabled"] = True
    assert invoke(config, population([row()]), sink, at="2026-11-11T08:00:00-06:00")["status"] == "before_window"
    assert not sink.publish_attempts


def test_wrapper_resolves_fresh_binding_and_only_passes_token_path(config, monkeypatch, tmp_path):
    calls = []; current = ["fixture-host:desk-a"]
    config["sessions_db"] = str(tmp_path / "sessions.db"); config["binding_name"] = "assistant"
    with sqlite3.connect(config["sessions_db"]) as db:
        db.execute("CREATE TABLE v2_assistant_direct_binding (name TEXT, stream_id TEXT, generation TEXT, revision INTEGER)")
        db.execute("INSERT INTO v2_assistant_direct_binding VALUES ('assistant','fixture-host:desk-a','generation-1',1)")
    for stream in ("fixture-host:desk-a", "fixture-host:desk-b"):
        path = tmp_path / ".pentacle-stream-tokens" / (tool.digest(stream)[:24]+".token")
        path.parent.mkdir(exist_ok=True); path.write_text("synthetic-secret"); path.chmod(0o600)
    def child(argv, **kw):
        calls.append((argv, kw))
        if argv[1:3] == ["assistant", "binding"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"type": "assistant.binding.ok", "stream_id": current[0],
                "generation": "generation-1", "revision": 1, "composite_stream_id": config["composite_stream_id"]}))
        return subprocess.CompletedProcess(argv, 0, '{"type":"assistant.publish.ok"}')
    monkeypatch.setattr(tool.subprocess, "run", child)
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "fixture-worker:lead")
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "wrong-secret")
    wrapper = tool.Publisher(config)
    wrapper.publish("household-due:"+DAY, "Body")
    current[0] = "fixture-host:desk-b"
    with sqlite3.connect(config["sessions_db"]) as db:
        db.execute("UPDATE v2_assistant_direct_binding SET stream_id='fixture-host:desk-b'")
    wrapper.publish("household-due:"+DAY, "Body")
    assert len(calls) == 4
    assert calls[1][1]["env"]["AGENT_ORCH_STREAM_ID"] == "fixture-host:desk-a"
    assert calls[3][1]["env"]["AGENT_ORCH_STREAM_ID"] == "fixture-host:desk-b"
    assert "--response-state" in calls[1][0] and "final" in calls[1][0]
    assert all("synthetic-secret" not in str(c) and "wrong-secret" not in str(c) for c in calls)
    assert all(c[1]["timeout"] == 18 for c in calls)


def test_wrapper_missing_remote_permissive_and_symlink_tokens_fail_closed(config, tmp_path):
    wrapper = tool.Publisher(config)
    with pytest.raises(tool.Failure, match="binding_not_local"):
        wrapper.seat_env({"stream_id": "other-host:desk"})
    stream = "fixture-host:desk"; path = tmp_path / ".pentacle-stream-tokens" / (tool.digest(stream)[:24]+".token")
    with pytest.raises(tool.Failure, match="missing_private_file"):
        wrapper.seat_env({"stream_id": stream})
    path.parent.mkdir(); path.write_text("synthetic-secret"); path.chmod(0o644)
    with pytest.raises(tool.Failure, match="unsafe_private_file"):
        wrapper.seat_env({"stream_id": stream})
    path.unlink(); target = tmp_path / "secret"; target.write_text("synthetic-secret"); target.chmod(0o600); path.symlink_to(target)
    with pytest.raises(tool.Failure, match="unsafe_private_file"):
        wrapper.seat_env({"stream_id": stream})


def test_no_model_or_cosmo_mutation_surface():
    source = Path(tool.__file__).read_text()
    assert '"POST"' not in source and '"PATCH"' not in source and '"DELETE"' not in source
    assert '"spawn"' not in source and '"prompt"' not in source


def test_tell_ledger_without_committed_delivery_is_not_ack(config, monkeypatch):
    wrapper = tool.Publisher(config)
    monkeypatch.setattr(wrapper, "binding", lambda: {"stream_id": "fixture-host:desk"})
    monkeypatch.setattr(wrapper, "seat_env", lambda value: {})
    monkeypatch.setattr(wrapper, "rpc", lambda *args: {"type": "tell.ok", "ledger_row_id": 1, "delivery_status": "pasted_unsubmitted"})
    with pytest.raises(tool.Failure, match="tell_unconfirmed"):
        wrapper.tell("blocker-fixture", "BLOCKER Synthetic failure")


def test_fd_failure_rehearsals_cannot_poison_production_blocker_keys(config, tmp_path):
    sink = Sink()
    def failed_read(route):
        raise tool.Failure("cosmo_read_failed")
    now = datetime.fromisoformat(DAY+"T08:00:00-06:00")
    for name in ("rehearsal-a", "rehearsal-b"):
        result = tool.run(config, now, to_fd=True, state_root=str(tmp_path / name),
                          reader=failed_read, publisher=sink)
        assert result["status"] == "failed"
    assert tool.run(config, now, reader=failed_read, publisher=sink)["status"] == "failed"
    assert len(sink.told) == 3
    production_key = f"household-due:blocker:{DAY}:cosmo_read_failed"
    assert production_key in sink.told
    assert all(record["receipt"] for record in state(config)["alerts"].values())
    # Each namespace also preserves its own identical ID/body on a replay.
    before = set(sink.told)
    tool.run(config, now, to_fd=True, state_root=str(tmp_path / "rehearsal-a"), reader=failed_read, publisher=sink)
    tool.run(config, now, reader=failed_read, publisher=sink)
    assert set(sink.told) == before


def test_ready_digest_precedes_failed_pending_alert_retry(config):
    events = []
    class OrderedSink(Sink):
        def publish(self, key, body):
            events.append("publish")
            return super().publish(key, body)
        def tell(self, key, body):
            events.append("tell")
            return super().tell(key, body)
    sink = OrderedSink(); sink.fail_publish = True; sink.fail_tell = True
    assert invoke(config, population([row()]), sink)["status"] == "failed"
    sink.fail_publish = False; events.clear()
    assert invoke(config, population([row()]), sink)["status"] == "delivered"
    assert events[0] == "publish"
    assert state(config)["days"][DAY]["status"] == "delivered"
    assert all(record["receipt"] is None for record in state(config)["alerts"].values())


@pytest.mark.parametrize("bad", ["hash", "body", "tell_id", "sender", "status", "row_id", "read_only", "source", "outer_id"])
def test_blocker_reconciliation_rejects_unproved_or_foreign_ledger(config, monkeypatch, bad):
    from comms import payload_digest
    config["binding_name"] = "assistant"
    wrapper = tool.Publisher(config)
    monkeypatch.setattr(wrapper, "binding", lambda: {"stream_id": "fixture-host:desk-b"})
    monkeypatch.setattr(wrapper, "seat_env", lambda binding: {"AGENT_ORCH_STREAM_ID": binding["stream_id"], "AGENT_ORCH_TOKEN": ""})
    key = "household-due:blocker:fixture"; body = "BLOCKER Synthetic failure"; sender = "fixture-host:desk-a"
    record = {"tell_id": key, "text": body, "from_stream_id": sender, "ledger_row_id": 1,
              "delivery_status": "delivered", "request_payload_hash": payload_digest("front-desk:assistant\0"+sender, body)}
    receipt = {"type": "ledger_get.ok", "tell_id": key, "read_only": True, "source": "v2_tell_deliveries", "tell": record}
    if bad == "hash": record["request_payload_hash"] = "wrong"
    elif bad == "body": record["text"] = "Other body"
    elif bad == "tell_id": record["tell_id"] = "other-key"
    elif bad == "sender": record["from_stream_id"] = "other-host:desk"
    elif bad == "status": record["delivery_status"] = "pasted_unsubmitted"
    elif bad == "row_id": record["ledger_row_id"] = False
    elif bad == "read_only": receipt["read_only"] = False
    elif bad == "source": receipt["source"] = "other-table"
    else: receipt["tell_id"] = "other-key"
    reads = []
    def rpc(args, env=None):
        assert env["AGENT_ORCH_STREAM_ID"] == "fixture-host:desk-b" and env["AGENT_ORCH_TOKEN"] == ""
        if args[0] == "tell": raise tool.Failure("tell_id_conflict")
        reads.append(args)
        return receipt
    monkeypatch.setattr(wrapper, "rpc", rpc)
    with pytest.raises(tool.Failure, match="tell_unconfirmed"):
        wrapper.tell(key, body)
    assert reads == [["ledger", "get", key]]


def test_missing_committed_ledger_keeps_same_alert_unacknowledged(config, monkeypatch):
    wrapper = tool.Publisher(config)
    monkeypatch.setattr(wrapper, "binding", lambda: {"stream_id": "fixture-host:desk-b"})
    monkeypatch.setattr(wrapper, "seat_env", lambda binding: {})
    reads = []; attempts = []
    def rpc(args, env=None):
        if args[0] == "tell":
            attempts.append(args)
            raise tool.Failure("tell_id_conflict")
        reads.append(args)
        raise tool.Failure("rpc_refused")  # ledger_get.error: tell_not_found
    monkeypatch.setattr(wrapper, "rpc", rpc)
    def failed_read(route): raise tool.Failure("cosmo_read_failed")
    now = datetime.fromisoformat(DAY+"T08:00:00-06:00")
    for _ in range(2):
        assert tool.run(config, now, reader=failed_read, publisher=wrapper)["status"] == "failed"
    alerts = state(config)["alerts"]
    assert list(alerts) == [f"household-due:blocker:{DAY}:cosmo_read_failed"]
    assert next(iter(alerts.values()))["receipt"] is None
    assert len(reads) == 2 and len(attempts) == 2
    assert attempts[0] == attempts[1]
