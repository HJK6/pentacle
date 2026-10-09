"""R6 lane facts and durable, silent-clear episodes on disposable stores."""
import asyncio
from dataclasses import asdict
import json

import pytest

from work_lane_progress import lane_progress
from test_work_lanes import run


NOW = "2026-01-03T00:00:00.000Z"
OLD = "2026-01-01T00:00:00.000Z"


def member(status="completed", quality="fresh"):
    return {"spec_id": "spec_demo__span", "status": status,
            "terminal": status if status in ("completed", "deprecated") else None,
            "ac_checked": None, "ac_total": None, "estimate": None,
            "source_changed_at": OLD, "observation": {"quality": quality}, "obs_rev": 1}


@pytest.mark.parametrize("members,available,state,expected", [
    ([member()], True, "paused", True),
    ([member(), member("deprecated")], True, "blocked", True),
    ([member("deprecated")], True, "paused", False),
    ([], True, "paused", False),
    ([member(), member("in_progress")], True, "paused", False),
    ([member(), member("missing", "missing")], True, "paused", False),
    ([member(), member("ambiguous", "ambiguous")], True, "paused", False),
    ([member(quality="stale")], True, "paused", None),
    ([member(quality="error")], True, "paused", None),
    ([member()], False, "paused", None),
    ([member()], True, "done", False),
    ([member(quality="stale")], False, "done", False),
    ([member(), member("in_progress"), member("in_progress", "stale"), member("missing", "missing")],
     True, "paused", None),
])
def test_completion_pending_tristate(members, available, state, expected):
    result = lane_progress({"_members": members, "_work_index_available": available,
                            "work_state": state})
    assert result["completion_pending"] is expected
    assert result["lead_reported_done"] is None


@pytest.mark.parametrize("state,qualifies,stamp,expected", [
    ("active", True, OLD, True),
    ("active", False, OLD, False),
    ("paused", True, OLD, False),
    ("blocked", True, OLD, False),
    ("done", True, OLD, False),
    ("active", True, "2026-01-02T00:00:00.000Z", False),
    ("active", True, NOW, False),
    ("active", True, None, False),
])
def test_stale_uses_presented_active_and_strict_threshold(state, qualifies, stamp, expected):
    result = lane_progress({"work_state": state, "_qualifies": qualifies,
                            "_fd_updated_at": stamp}, now_iso=NOW)
    assert result["stale"] is expected


def test_stale_threshold_configuration(monkeypatch):
    monkeypatch.setenv("WORK_LANE_STALE_H", "72")
    row = {"work_state": "active", "_qualifies": True, "_fd_updated_at": OLD}
    assert lane_progress(row, now_iso=NOW)["stale"] is False
    monkeypatch.setenv("WORK_LANE_STALE_H", "12")
    assert lane_progress(row, now_iso=NOW)["stale"] is True


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1", "bad"])
def test_stale_threshold_rejects_invalid_configuration(monkeypatch, value):
    monkeypatch.setenv("WORK_LANE_STALE_H", value)
    with pytest.raises(ValueError, match="work_lane_stale_interval_invalid"):
        lane_progress({"work_state": "paused"}, now_iso=NOW)


class RecordingSink:
    def __init__(self):
        self.calls = []
        self.committed = {}
        self.front_desk_generation = "initial-fd-generation"
        self.lines = []
        self.raise_after_commit = False
        self.unavailable = False

    async def emit(self, fact):
        self.calls.append(asdict(fact))
        if self.unavailable:
            return None
        identity = (fact.family, fact.principal, fact.episode_id)
        value = (asdict(fact), "recorded:" + fact.episode_id)
        if identity not in self.committed:
            self.lines.append((identity, self.front_desk_generation))
        assert self.committed.setdefault(identity, value) == value
        if self.raise_after_commit:
            raise RuntimeError("synthetic uncertain sink commit")
        return value[1]


async def seed_observations(store, members, *, available=True):
    def write(conn):
        conn.execute("BEGIN IMMEDIATE")
        for item in members:
            conn.execute("INSERT OR REPLACE INTO v2_work_item_observations VALUES (?,?)",
                         (item["spec_id"], json.dumps({"member": item})))
        conn.execute("INSERT OR REPLACE INTO v2_work_index_state VALUES (1,?)", (json.dumps({
            "available": available, "root_configured": True, "snapshot_at": OLD,
            "last_sweep_at": OLD, "error": None}),))
        conn.commit()
    await store.submit(write)


async def episodes(store):
    return await store.submit(lambda conn: [dict(r) for r in conn.execute(
        "SELECT * FROM v2_work_lane_episodes ORDER BY lane_id,kind,sequence")])


def test_completion_null_hold_silent_clear_recur_and_no_version_bump():
    async def body(env):
        lane = (await env.adopt(state="paused", lead=False, owner="operator", members=["spec_demo__span"]))["lane"]
        sink = RecordingSink()
        await seed_observations(env.store, [member()])
        await env.store.reconcile_work_lane_episodes(sink, now_iso=NOW)
        await env.store.reconcile_work_lane_episodes(sink, now_iso=NOW)
        assert len(sink.calls) == 1
        first = (await episodes(env.store))[0]
        assert first["episode_id"] == f"{lane['lane_id']}:completed:1"
        assert first["emitted_ref"] == "recorded:" + first["episode_id"]
        await seed_observations(env.store, [member(quality="stale")])
        await env.store.reconcile_work_lane_episodes(sink, now_iso=NOW)
        assert (await episodes(env.store))[0]["cleared_at"] is None
        await seed_observations(env.store, [member("in_progress")])
        await env.store.reconcile_work_lane_episodes(sink, now_iso=NOW)
        assert (await episodes(env.store))[0]["cleared_at"] == NOW
        assert len(sink.calls) == 1
        await seed_observations(env.store, [member()])
        await env.store.reconcile_work_lane_episodes(sink, now_iso=NOW)
        assert [r["episode_id"] for r in await episodes(env.store)] == [
            f"{lane['lane_id']}:completed:1", f"{lane['lane_id']}:completed:2"]
        assert len(sink.calls) == 2
        assert (await env.store.get_work_lane(lane["lane_id"]))["lane"]["version"] == lane["version"]
    run(body)


def test_unknown_does_not_open_and_unavailable_retries_same_fact():
    async def body(env):
        await env.adopt(state="paused", lead=False, owner="operator", members=["spec_demo__span"])
        sink = RecordingSink()
        await seed_observations(env.store, [member(quality="error")])
        await env.store.reconcile_work_lane_episodes(sink, now_iso=NOW)
        assert await episodes(env.store) == []
        await seed_observations(env.store, [member()])
        sink.unavailable = True
        await env.store.reconcile_work_lane_episodes(sink, now_iso=NOW)
        assert (await episodes(env.store))[0]["emitted_ref"] is None
        sink.unavailable = False
        await env.store.reconcile_work_lane_episodes(sink, now_iso="2026-01-04T00:00:00.000Z")
        assert sink.calls[0] == sink.calls[1]
        assert len(sink.committed) == 1
    run(body)


def test_stale_clear_on_leave_active_and_immediate_recur():
    async def body(env):
        lane = (await env.adopt())["lane"]
        sink = RecordingSink()
        stamp = "2030-01-01T00:00:00.000Z"
        await env.store.reconcile_work_lane_episodes(sink, now_iso=stamp)
        assert [r["kind"] for r in await episodes(env.store)] == ["stale"]
        await env.op("set_state", {"to": "paused"}, lane=lane["lane_id"], version=1)
        await env.store.reconcile_work_lane_episodes(sink, now_iso=stamp)
        assert len(sink.calls) == 1
        await env.op("set_state", {"to": "active"}, lane=lane["lane_id"], version=2)
        await env.store.reconcile_work_lane_episodes(sink, now_iso=stamp)
        assert [r["sequence"] for r in await episodes(env.store)] == [1, 2]
        assert len(sink.calls) == 2
    run(body)


def test_concurrent_replayed_sweeps_open_once_and_use_same_writer_without_deadlock():
    async def body(env):
        await env.adopt(state="paused", lead=False, owner="operator", members=["spec_demo__span"])
        await seed_observations(env.store, [member()])
        sink = RecordingSink()
        original = sink.emit
        async def emit(fact):
            # The real core also commits through this Store: no transaction may span this await.
            await env.store.put("synthetic-sink", fact.episode_id)
            return await original(fact)
        sink.emit = emit
        async with asyncio.timeout(3):
            await asyncio.gather(*(env.store.reconcile_work_lane_episodes(sink, now_iso=NOW) for _ in range(8)))
        rows = await episodes(env.store)
        assert len(rows) == 1 and rows[0]["emitted_ref"]
        assert len(sink.committed) == 1
    run(body)


@pytest.mark.parametrize("crash_after_commit", [False, True])
def test_restart_rehands_identical_fact_after_crash(tmp_path, crash_after_commit):
    from store import Store
    path = str(tmp_path / "episodes.db")
    sink = RecordingSink()
    async def seed(env):
        await env.adopt(state="paused", lead=False, owner="operator", members=["spec_demo__span"])
        await seed_observations(env.store, [member()])
        if crash_after_commit:
            sink.raise_after_commit = True
            await env.store.reconcile_work_lane_episodes(sink, now_iso=NOW)
        else:
            class CrashBeforeSink:
                async def emit(self, fact):
                    raise asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await env.store.reconcile_work_lane_episodes(CrashBeforeSink(), now_iso=NOW)
        row = (await episodes(env.store))[0]
        assert row["emitted_ref"] is None
        assert json.loads(row["fact_json"])["episode_id"] == row["episode_id"]
    run(seed, path)
    async def restart():
        store = Store(path)
        store.start()
        try:
            sink.raise_after_commit = False
            await store.reconcile_work_lane_episodes(sink, now_iso="2026-01-04T00:00:00.000Z")
            await store.reconcile_work_lane_episodes(sink, now_iso="2026-01-05T00:00:00.000Z")
            assert len(sink.committed) == 1
            assert len(await episodes(store)) == 1
            assert all(call == sink.calls[0] for call in sink.calls)
            assert (await episodes(store))[0]["emitted_ref"]
        finally:
            store.stop()
    asyncio.run(restart())


@pytest.mark.parametrize("membership_change", [False, True])
def test_clear_recur_while_sink_awaits_latches_only_exact_opening(membership_change):
    async def body(env):
        lane = (await env.adopt(state="paused", lead=False, owner="operator", members=["spec_demo__span"]))["lane"]
        await seed_observations(env.store, [member()])
        sink = RecordingSink()
        entered, release = asyncio.Event(), asyncio.Event()
        original = sink.emit
        async def held(fact):
            entered.set()
            await release.wait()
            return await original(fact)
        sink.emit = held
        task = asyncio.create_task(env.store.reconcile_work_lane_episodes(sink, now_iso=NOW))
        await asyncio.wait_for(entered.wait(), 3)
        if membership_change:
            await env.op("set_members", {"members": ["spec_demo__other"]}, lane=lane["lane_id"], version=1)
        else:
            await seed_observations(env.store, [member("in_progress")])
        await env.store.reconcile_work_lane_episodes(None, now_iso=NOW)
        current_member = dict(member(), spec_id="spec_demo__other") if membership_change else member()
        await seed_observations(env.store, [current_member])
        await env.store.reconcile_work_lane_episodes(None, now_iso=NOW)
        assert len(await episodes(env.store)) == 2
        release.set()
        await asyncio.wait_for(task, 3)
        rows = await episodes(env.store)
        assert rows[0]["cleared_at"] is not None and rows[0]["emitted_ref"]
        assert rows[1]["emitted_ref"] is None
        await env.store.reconcile_work_lane_episodes(sink, now_iso=NOW)
        assert len(sink.committed) == 2
        assert [r["emitted_ref"] for r in await episodes(env.store)] == [
            f"recorded:{lane['lane_id']}:completed:1", f"recorded:{lane['lane_id']}:completed:2"]
    run(body)


@pytest.mark.parametrize("quality", ["fresh", "stale"])
def test_membership_change_clears_even_true_or_unknown_and_replay_is_silent(quality):
    async def body(env):
        first = member()
        second = dict(member(quality=quality), spec_id="spec_demo__other")
        lane = (await env.adopt(state="paused", lead=False, owner="operator", members=[first["spec_id"]]))["lane"]
        await seed_observations(env.store, [first, second])
        sink = RecordingSink()
        await env.store.reconcile_work_lane_episodes(sink, now_iso=NOW)
        payload = {"members": [second["spec_id"]]}
        await env.op("set_members", payload, lane=lane["lane_id"], version=1, request_id="new-members")
        assert (await episodes(env.store))[0]["cleared_at"] is not None
        await asyncio.gather(*(env.store.reconcile_work_lane_episodes(sink, now_iso=NOW) for _ in range(3)))
        assert len(await episodes(env.store)) == (2 if quality == "fresh" else 1)
        second["observation"] = {"quality": "fresh"}
        await seed_observations(env.store, [second])
        await env.store.reconcile_work_lane_episodes(sink, now_iso=NOW)
        assert len(await episodes(env.store)) == 2
        assert (await env.op("set_members", payload, lane=lane["lane_id"], version=1,
                             request_id="new-members"))["duplicate"]
        with pytest.raises(ValueError, match="work_lane_members_unchanged"):
            await env.op("set_members", payload, lane=lane["lane_id"], version=2)
        await env.store.reconcile_work_lane_episodes(sink, now_iso=NOW)
        assert len(await episodes(env.store)) == 2
        assert (await episodes(env.store))[-1]["cleared_at"] is None
        assert len(sink.committed) == 2
    run(body)


def test_drain_rereads_membership_after_index_scan_and_membership_clear_is_atomic():
    from types import SimpleNamespace
    from work_lanes_projection import WorkLanesInventory
    async def body(env):
        lane = (await env.adopt(state="paused", lead=False, owner="operator", members=["spec_demo__span"]))["lane"]
        await seed_observations(env.store, [member()])
        sink = RecordingSink()
        await env.store.reconcile_work_lane_episodes(sink, now_iso=NOW)
        entered, release = asyncio.Event(), asyncio.Event()
        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(env.store, SimpleNamespace(list_open=lambda: []), broadcast, episode_sink=sink)
        async def held_scan():
            entered.set()
            await release.wait()
        inv.reconcile_index = held_scan
        inv.refresh()
        await asyncio.wait_for(entered.wait(), 3)
        def fail(operation):
            if operation == "set_members":
                raise RuntimeError("synthetic membership rollback")
        env.store._work_lane_fault = fail
        with pytest.raises(RuntimeError, match="membership rollback"):
            await env.op("set_members", {"members": ["spec_demo__unknown"]}, lane=lane["lane_id"], version=1)
        assert (await episodes(env.store))[0]["cleared_at"] is None
        env.store._work_lane_fault = None
        await env.op("set_members", {"members": ["spec_demo__unknown"]}, lane=lane["lane_id"], version=1)
        release.set()
        await inv._task
        assert len(await episodes(env.store)) == 1
        assert (await episodes(env.store))[0]["cleared_at"] is not None
        assert len(sink.calls) == 1
        assert (await inv.current())["lanes"][0]["members"][0]["spec_id"] == "spec_demo__unknown"
        await inv.stop()
    run(body)


def test_periodic_drain_and_session_inventory_emit_trigger_episodes():
    from inventory import InventoryEmitter
    from sessions import Sessions
    from work_lanes_projection import WorkLanesInventory
    async def body(env):
        await env.adopt(state="paused", lead=False, owner="operator", members=["spec_demo__span"])
        await seed_observations(env.store, [member("in_progress")])
        sessions = Sessions(env.store, tmux=None, local_host="fixture-root")
        await sessions.refresh()
        frames = []
        async def broadcast(frame):
            frames.append(frame)
        sink = RecordingSink()
        inv = WorkLanesInventory(env.store, sessions, broadcast, episode_sink=sink)
        inv.start(interval_s=0.02)
        try:
            await inv._task
            await seed_observations(env.store, [member()])
            async with asyncio.timeout(3):
                while not sink.committed:
                    await asyncio.sleep(0.005)
            assert len(sink.committed) == 1
        finally:
            await inv.stop()
        await seed_observations(env.store, [member("in_progress")])
        await env.store.reconcile_work_lane_episodes(sink)
        await seed_observations(env.store, [member()])
        # Exercise the existing main.py session-emitter wrapper, including its
        # deduped path, without booting any daemon or adding another cadence.
        import ast
        from pathlib import Path
        tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
        wrapper = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                       and n.name == "_emit_sessions_and_lanes")
        emitter = InventoryEmitter(sessions, broadcast, min_interval_s=0)
        sessions.set_inventory_emitter(emitter)
        namespace = {"_session_emit": emitter.emit_if_changed, "work_lanes": inv}
        exec(compile(ast.Module(body=[wrapper], type_ignores=[]), "main.py", "exec"), namespace)
        emitter.emit_if_changed = namespace["_emit_sessions_and_lanes"]
        await emitter.emit_if_changed(immediate=True)
        task = inv._task
        for _ in range(20):
            inv.refresh()
            assert inv._task is task
        await task
        assert len(sink.committed) == 2
        assert any(f["type"] == "session.inventory" for f in frames)
        assert any(f["type"] == "work_lanes.inventory" for f in frames)
        await inv.stop()
    run(body)


def test_pending_fact_survives_fd_rebind_and_crash_after_sink_return():
    async def body(env):
        await env.adopt(state="paused", lead=False, owner="operator", members=["spec_demo__span"])
        await seed_observations(env.store, [member()])
        sink = RecordingSink()
        original = env.store.submit
        async def crash_latch(fn):
            if fn.__name__ == "latch":
                raise asyncio.CancelledError()
            return await original(fn)
        env.store.submit = crash_latch
        with pytest.raises(asyncio.CancelledError):
            await env.store.reconcile_work_lane_episodes(sink, now_iso=NOW)
        env.store.submit = original
        assert len(sink.committed) == 1 and (await episodes(env.store))[0]["emitted_ref"] is None
        # The stub's delivery recipient changes independently of producer identity.
        sink.front_desk_generation = "replacement-fd-generation"
        await env.store.reconcile_work_lane_episodes(sink)
        assert len(sink.committed) == 1 and sink.calls[0] == sink.calls[1]
        assert len(sink.lines) == 1 and sink.lines[0][1] == "initial-fd-generation"
        assert (await episodes(env.store))[0]["emitted_ref"]
    run(body)


def test_renewed_freshness_clears_stale_without_recovery_fact():
    async def body(env):
        lane = (await env.adopt(members=["spec_demo__span"]))["lane"]
        await seed_observations(env.store, [member("in_progress")])
        sink = RecordingSink()
        await env.store.reconcile_work_lane_episodes(sink, now_iso="2030-01-01T00:00:00Z")
        assert len(sink.calls) == 1 and sink.calls[0]["kind"] == "stale"
        fresh = dict(member("in_progress"), source_changed_at="2030-01-01T00:00:00Z")
        await seed_observations(env.store, [fresh])
        await env.store.reconcile_work_lane_episodes(sink, now_iso="2030-01-01T00:00:01Z")
        assert (await episodes(env.store))[0]["cleared_at"] is not None
        assert len(sink.calls) == 1
        assert (await env.store.get_work_lane(lane["lane_id"]))["lane"]["version"] == lane["version"]
    run(body)
