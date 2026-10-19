"""Unexpected lead loss is a repaired fault, never a stored pause."""
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from store_work_lane_episodes import LaneEpisodeFact, ensure_work_lane_episodes_schema
from test_work_lanes import FD, code, run
from work_lanes_projection import project_lane


async def lose_lead(env, key="stream:ll", name="ll-lead"):
    lead = await env.seat(name, role="lead", parent_stream_id=FD)
    lid = (await env.adopt(key=key, lead=lead))["lane"]["lane_id"]
    await env.store.mark_closed("host-a", name, closed_at="2026-10-07T20:00:00Z",
                                pane_status="pane_dead", close_kind="manager_close")
    return lid


async def episodes(store, lane_id, kind="lead_lost"):
    def q(conn):
        return [dict(r) for r in conn.execute(
            "SELECT * FROM v2_work_lane_episodes WHERE lane_id=? AND kind=? ORDER BY sequence",
            (lane_id, kind))]
    return await store.submit(q)


async def drain(store, sink=None):
    # The production drain order (work_lanes_projection._drain) minus the file index.
    if hasattr(store, "reconcile_work_lanes"):  # removed by C1; present only on the RED baseline
        await store.reconcile_work_lanes()
    await store.reconcile_work_lane_episodes(sink)


def presented(row):
    return project_lane(row, None, "2026-10-08T00:00:00.000Z")


def test_lead_loss_keeps_active_and_opens_one_episode():
    async def body(env):
        lid = await lose_lead(env)
        before = (await env.store.get_work_lane(lid))["lane"]
        await drain(env.store)
        await drain(env.store)
        shown = await env.store.get_work_lane(lid)
        assert shown["lane"]["work_state"] == "active"
        assert shown["lane"]["version"] == before["version"]
        assert "lead_lost" not in [e["operation"] for e in shown["events"]]
        row = [r for r in await env.store.work_lane_rows() if r["lane_id"] == lid][0]
        lane = presented(row)
        assert (lane["state"], lane["state_reason"]) == ("active", "lead_lost")
        assert lane["lead"]["qualifies"] is False
        opened = await episodes(env.store, lid)
        assert [(e["sequence"], e["cleared_at"]) for e in opened] == [(1, None)]
        assert json.loads(opened[0]["fact_json"])["kind"] == "lead_lost"
    run(body)


def test_qualifying_set_lead_clears_and_ack_does_not():
    class Sink:
        def __init__(self):
            self.calls = []

        async def emit(self, fact):
            self.calls.append(fact)
            return "notification:" + fact.episode_id

    async def body(env):
        lid = await lose_lead(env)
        sink = Sink()
        await drain(env.store, sink)
        await drain(env.store, sink)  # delivered once; a delivery/ack is never a repair
        assert [f.code for f in sink.calls] == ["lane_lead_lost"]
        assert (await episodes(env.store, lid))[0]["cleared_at"] is None
        fresh = await env.seat("ll-fresh", role="lead", parent_stream_id=FD)
        version = (await env.store.get_work_lane(lid))["lane"]["version"]
        await env.op("set_lead", {"lead": {"stream_id": fresh[0], "generation": fresh[1]}},
                     lane=lid, version=version)
        await drain(env.store, sink)
        assert (await episodes(env.store, lid))[0]["cleared_at"] is not None
        row = [r for r in await env.store.work_lane_rows() if r["lane_id"] == lid][0]
        assert presented(row)["state"] == "active" and presented(row)["state_reason"] != "lead_lost"
    run(body)


def test_deliberate_pause_requires_reason_and_clears():
    async def body(env):
        lid = await lose_lead(env)
        await drain(env.store)
        version = (await env.store.get_work_lane(lid))["lane"]["version"]
        with pytest.raises(ValueError) as exc:
            await env.op("set_state", {"to": "paused"}, lane=lid, version=version)
        assert "work_lane_reason_required" in code(exc)
        await env.op("set_state", {"to": "paused", "reason": "Waiting for the owner window."},
                     lane=lid, version=version)
        await drain(env.store)
        assert (await episodes(env.store, lid))[0]["cleared_at"] is not None
        lane = (await env.store.get_work_lane(lid))["lane"]
        assert (lane["work_state"], lane["work_state_reason"]) == ("paused", "fd")
    run(body)


def test_legacy_stored_pause_is_a_fault_restated_with_reason():
    async def body(env):
        lid = await lose_lead(env)

        def legacy(conn):
            conn.execute("UPDATE v2_assistant_composite_lanes SET work_state='paused',"
                         " work_state_reason='lead_lost', version=version+1 WHERE lane_id=?", (lid,))
            conn.commit()
        await env.store.submit(legacy)
        await drain(env.store)
        assert [e["cleared_at"] for e in await episodes(env.store, lid)] == [None]
        row = [r for r in await env.store.work_lane_rows() if r["lane_id"] == lid][0]
        assert (presented(row)["state"], presented(row)["state_reason"]) == ("active", "lead_lost")
        version = (await env.store.get_work_lane(lid))["lane"]["version"]
        with pytest.raises(ValueError) as exc:
            await env.op("set_state", {"to": "paused"}, lane=lid, version=version)
        assert "work_lane_reason_required" in code(exc)
        await env.op("set_state", {"to": "paused", "reason": "Deliberate hold."}, lane=lid, version=version)
        await drain(env.store)
        assert (await episodes(env.store, lid))[0]["cleared_at"] is not None
        lane = (await env.store.get_work_lane(lid))["lane"]
        assert (lane["work_state"], lane["work_state_reason"]) == ("paused", "fd")
    run(body)


def test_legacy_stored_pause_clears_on_qualifying_successor():
    async def body(env):
        lid = await lose_lead(env)
        await drain(env.store)

        def legacy(conn):
            conn.execute("UPDATE v2_assistant_composite_lanes SET work_state='paused',"
                         " work_state_reason='lead_lost', version=version+1 WHERE lane_id=?", (lid,))
            conn.commit()
        await env.store.submit(legacy)
        await drain(env.store)
        assert [e["cleared_at"] for e in await episodes(env.store, lid)] == [None]
        fresh = await env.seat("ll-successor", role="lead", parent_stream_id=FD)
        version = (await env.store.get_work_lane(lid))["lane"]["version"]
        await env.op("set_lead", {"lead": {"stream_id": fresh[0], "generation": fresh[1]}},
                     lane=lid, version=version)
        await drain(env.store)
        assert (await episodes(env.store, lid))[0]["cleared_at"] is not None
        row = [r for r in await env.store.work_lane_rows() if r["lane_id"] == lid][0]
        # Reconnected: the fault is cleared; the stored pause is resumed only by an explicit set-state.
        assert (presented(row)["state"], presented(row)["state_reason"]) == ("paused", "lead_lost")
        version = (await env.store.get_work_lane(lid))["lane"]["version"]
        assert (await env.op("set_state", {"to": "active"}, lane=lid, version=version))["lane"]["work_state"] == "active"
    run(body)


def test_same_state_is_still_unchanged_without_fault():
    async def body(env):
        lid = await lose_lead(env)
        version = (await env.store.get_work_lane(lid))["lane"]["version"]
        await env.op("set_state", {"to": "paused", "reason": "Hold."}, lane=lid, version=version)
        with pytest.raises(ValueError) as exc:
            await env.op("set_state", {"to": "paused", "reason": "Again."}, lane=lid, version=version + 1)
        assert "work_lane_state_unchanged" in code(exc)
    run(body)


@pytest.mark.parametrize("state", ["paused", "blocked"])
def test_held_lanes_never_open_lead_lost(state):
    async def body(env):
        lead = await env.seat("held-lead", role="lead", parent_stream_id=FD)
        extra = {"blocker": "keys"} if state == "blocked" else {}
        lid = (await env.adopt(key="stream:held", state=state, lead=lead, **extra))["lane"]["lane_id"]
        await env.store.mark_closed("host-a", "held-lead", closed_at="2026-10-07T20:00:00Z",
                                    pane_status="pane_dead")
        await drain(env.store)
        assert await episodes(env.store, lid) == []
        assert (await env.store.get_work_lane(lid))["lane"]["work_state"] == state
    run(body)


def test_episode_survives_restart_without_duplicate():
    state = {}

    async def first(env):
        state["lid"] = await lose_lead(env)
        await drain(env.store)

    async def second(env):
        await drain(env.store)
        opened = await episodes(env.store, state["lid"])
        assert [(e["sequence"], e["cleared_at"]) for e in opened] == [(1, None)]

    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "ll.db")
        run(first, path)
        run(second, path)


def test_check_widened_for_estimate_missing_is_rewidened():
    # R8: a database already carrying the estimate_missing CHECK must accept lead_lost.
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE v2_work_lane_episodes (
        episode_id TEXT PRIMARY KEY, lane_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('completed','stale','estimate_missing')),
        sequence INTEGER NOT NULL CHECK(sequence >= 1), opened_at TEXT NOT NULL, cleared_at TEXT,
        emitted_ref TEXT, fact_json TEXT NOT NULL, UNIQUE(lane_id,kind,sequence))""")
    conn.execute("INSERT INTO v2_work_lane_episodes VALUES ('l:stale:1','l','stale',1,'t',NULL,'ref','{}')")
    ensure_work_lane_episodes_schema(conn)
    conn.execute("INSERT INTO v2_work_lane_episodes VALUES ('l:lead_lost:1','l','lead_lost',1,'t',NULL,NULL,'{}')")
    assert conn.execute("SELECT emitted_ref FROM v2_work_lane_episodes WHERE kind='stale'").fetchone() == ("ref",)
    ensure_work_lane_episodes_schema(conn)  # idempotent
    assert LaneEpisodeFact("l", "lead_lost", "l:lead_lost:1", "t").code == "lane_lead_lost"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "record-only", "on"])
async def test_lead_lost_alerts_the_fd_directly_with_repair(tmp_path, monkeypatch, mode):
    # A lost lead is repaired, not reviewed later: it bypasses the Work update digest.
    from test_work_lane_alert_sink import subject, adopt as adopt_sink, drain as drain_sink, facts, notices
    monkeypatch.setenv("PENTACLE_ERROR_ALERTS_MODE", mode)
    monkeypatch.setenv("PENTACLE_VOICE_MODE", "off")
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    async with subject(tmp_path) as s:
        lane = await adopt_sink(s, kind="stale")  # active with a qualifying lead
        await s.store.mark_closed("fixture", "v2-lead", closed_at="2026-10-07T20:00:00Z", pane_status="pane_dead")
        await drain_sink(s)
        await drain_sink(s)
        kinds = [r["kind"] for r in await episodes(s.store, lane["lane_id"])]
        assert kinds == ["lead_lost"]
        codes = [f["error_context"]["code"] for f in await facts(s)]
        assert codes.count("lane_lead_lost") == 1
        await s.server.error_alerts.reconcile()
        (alert,) = [r for r in await notices(s) if "lane_lead_lost" in r["body"]]
        assert alert["kind"] == "error_alert" and json.loads(alert["metadata"])["policy_mode"] == "immediate"
        await s.queue.drain_once()
        (paste,) = [p for p in s.provider.pastes if "lane_lead_lost" in str(p)]
        assert "fixture:v2-lead" in str(paste) and "work-lane set-lead " + lane["lane_id"] in str(paste)
        assert not [r for r in await notices(s) if json.loads(r["metadata"]).get("typed_digest")]
        assert (await s.store.get_work_lane(lane["lane_id"]))["lane"]["work_state"] == "active"