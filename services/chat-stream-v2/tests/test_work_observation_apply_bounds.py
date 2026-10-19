"""Delta apply keeps coherent file observations without rewriting irrelevant rows."""
import asyncio
import json
import threading

import pytest

from _shared.work_observations import advance_observation, timestamp
from store import Store
from test_work_lanes import FD, run


A = "spec_demo__alpha"
B = "spec_demo__beta"


def fresh(sid=A, *, checked=0, digest="first", path=None):
    return {"spec_id": sid, "quality": "fresh", "source_hash": digest,
            "path": path or "/fixture/work/in_progress/" + sid, "error": None,
            "facts": {"title": sid, "status": "in_progress", "terminal": None,
                      "ac_checked": checked, "ac_total": 2, "estimate": None,
                      "estimate_exempt": True, "status_text": "Fixture", "next_action_text": "Check"}}


def scan(at, *items, available=True, errors=None):
    candidates = {}
    for item in items:
        candidates.setdefault(item["spec_id"], []).append(item)
    return {"scanned_at": at, "available": available, "root_configured": True,
            "error": None if available else "work_root_unavailable",
            "candidates": candidates, "errors": errors or {}}


@pytest.mark.parametrize("count", [64, 640])
def test_file_store_unchanged_and_fixed_delta_do_not_decode_or_rewrite_population(tmp_path, monkeypatch, count):
    async def body():
        store = Store(str(tmp_path / "fixture.db"))
        store.start()
        try:
            items = [fresh(f"spec_demo__item_{i}") for i in range(count)]
            await store.reconcile_work_observations(scan(100, *items), settle_s=5, sweep=True)
            writer_id = await store.submit(lambda c: threading.get_ident())
            loads = json.loads
            decoded = []
            writes = []
            def counted(value, *args, **kw):
                result = loads(value, *args, **kw)
                if threading.get_ident() == writer_id and isinstance(result, dict) and "member" in result:
                    decoded.append(1)
                return result
            def trace(sql):
                if sql.upper().startswith(("INSERT INTO V2_WORK_ITEM_OBSERVATIONS", "UPDATE V2_WORK_ITEM_OBSERVATIONS")):
                    writes.append(sql)
            await store.submit(lambda c: c.set_trace_callback(trace))
            with monkeypatch.context() as patch:
                patch.setattr(json, "loads", counted)
                assert await store.reconcile_work_observations(scan(101, *items), settle_s=5, sweep=True) == {"changed": 0, "stale_scan": False}
                assert len(decoded) == 0
                assert len(writes) == 0
                items[0] = fresh("spec_demo__item_0", checked=1, digest="changed")
                assert await store.reconcile_work_observations(scan(102, *items), settle_s=5, sweep=True) == {"changed": 1, "stale_scan": False}
                assert len(decoded) == 0
                assert len(writes) == 1
        finally:
            store.stop()
    asyncio.run(body())


@pytest.mark.parametrize("file_backed", [False, True])
def test_public_members_match_monolithic_transition_oracle(tmp_path, file_backed):
    async def body(env):
        lane = (await env.adopt(state="paused", lead=False, owner="operator", members=[A, B]))["lane"]
        expected = {}
        transitions = []
        initial = fresh()
        changed = fresh(checked=1, digest="second")
        duplicate = fresh(path="/fixture/work/completed/copy")
        cases = [
            (scan(100, initial, fresh(B)), True),
            (scan(101, initial, fresh(B)), True),
            (scan(102, fresh(digest="source-only"), fresh(B)), False),
            (scan(103, changed, fresh(B)), True),
            (scan(104, fresh(B), errors={initial["path"]: {"quality": "error", "error": "work_yaml_invalid"}}), True),
            (scan(105, fresh(B)), False),
            (scan(106, fresh(B)), True),
            (scan(120, fresh(B)), True),
            (scan(121, fresh(B)), True),
            (scan(122, changed, duplicate, fresh(B)), True),
            (scan(140, changed, duplicate, fresh(B)), True),
            (scan(141, changed, duplicate, fresh(B)), True),
            (scan(142, available=False), True),
            (scan(143, changed, fresh(B)), True),
            (scan(144, fresh(checked=1, digest="second", path="/fixture/work/completed/moved"), fresh(B)), True),
        ]
        for observation, sweep in cases:
            # Deliberately use the unchanged public transition primitive as the oracle.
            for sid in (A, B):
                previous = expected.get(sid)
                choices = observation["candidates"].get(sid, [])
                if not observation["available"]:
                    candidate = {"quality": "stale", "error": observation["error"]}
                elif len(choices) > 1:
                    candidate = {"quality": "ambiguous", "error": "work_declared_id_ambiguous"}
                elif choices:
                    candidate = choices[0]
                elif previous and previous.get("path") in observation["errors"]:
                    candidate = observation["errors"][previous["path"]]
                else:
                    candidate = {"quality": "missing", "error": "work_file_missing"}
                expected[sid], transition = advance_observation(sid, previous, candidate,
                    now=observation["scanned_at"], settle_s=10, sweep=sweep)
                if transition:
                    transitions.append(transition)
            await env.store.reconcile_work_observations(observation, settle_s=10, sweep=sweep)
            shown = await env.store.get_work_lane(lane["lane_id"])
            assert shown["members"] == [expected[sid]["member"] for sid in (A, B)]
            events = [e["payload"] for e in shown["events"] if e["operation"] == "item_change"]
            assert events == transitions
            rows = await env.store.work_lane_rows()
            assert rows[0]["_members"] == shown["members"]
    run(body, str(tmp_path / "fixture.db") if file_backed else ":memory:")


def test_fresh_clock_survives_restart_pending_and_legacy_rollback(tmp_path):
    path = str(tmp_path / "fixture.db")
    lane_id = None
    async def seed(env):
        nonlocal lane_id
        lane_id = (await env.adopt(state="paused", lead=False, owner="operator", members=[A]))["lane"]["lane_id"]
        await env.store.reconcile_work_observations(scan(100, fresh()), settle_s=10, sweep=True)
        await env.store.reconcile_work_observations(scan(200, fresh()), settle_s=10, sweep=True)
    run(seed, path)
    async def reopen():
        store = Store(path)
        store.start()
        try:
            await store.mark_work_index_pending()
            shown = (await store.get_work_lane(lane_id))["members"][0]
            assert shown["observation"]["observed_at"] == timestamp(200)
            assert shown["source_changed_at"] == timestamp(100)
            # The documented rollback maintenance restores the legacy raw-row view.
            def materialize(conn):
                conn.execute("BEGIN IMMEDIATE")
                state = json.loads(conn.execute("SELECT state_json FROM v2_work_index_state").fetchone()[0])
                conn.execute("UPDATE v2_work_item_observations SET record_json=json_set(record_json, "
                    "'$.member.observation.observed_at', ?) WHERE "
                    "json_extract(record_json,'$.member.observation.quality')='fresh'", (state["snapshot_at"],))
                conn.commit()
                return json.loads(conn.execute("SELECT record_json FROM v2_work_item_observations WHERE spec_id=?", (A,)).fetchone()[0])["member"]
            assert await store.submit(materialize) == shown
            await store.reconcile_work_observations(scan(300, available=False), settle_s=10, sweep=True)
            assert (await store.get_work_lane(lane_id))["members"][0]["observation"]["observed_at"] == timestamp(200)
        finally:
            store.stop()
    asyncio.run(reopen())


def hold_preparation(monkeypatch, at):
    import store_work_index
    original = store_work_index._prepare_file
    entered, release = threading.Event(), threading.Event()
    def prepare(path, observation, **kwargs):
        result = original(path, observation, **kwargs)
        if observation["scanned_at"] == at:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release preparation")
        return result
    monkeypatch.setattr(store_work_index, "_prepare_file", prepare)
    return entered, release


@pytest.mark.parametrize("held_at", [200, 300])
def test_competing_scans_validate_snapshot_and_reject_reverse_order(tmp_path, monkeypatch, held_at):
    async def body(env):
        lane = (await env.adopt(state="paused", lead=False, owner="operator", members=[A]))["lane"]
        await env.store.reconcile_work_observations(scan(100, fresh()), settle_s=10, sweep=True)
        entered, release = hold_preparation(monkeypatch, held_at)
        pending = asyncio.create_task(env.store.reconcile_work_observations(
            scan(held_at, fresh(checked=1 if held_at == 200 else 2, digest=str(held_at))), settle_s=10, sweep=True))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            assert (await env.store.get_work_lane(lane["lane_id"]))["members"][0]["ac_checked"] == 0
            other = 500 - held_at
            await env.store.reconcile_work_observations(
                scan(other, fresh(checked=1 if other == 200 else 2, digest=str(other))), settle_s=10, sweep=True)
        finally:
            release.set()
        result = await pending
        assert result["stale_scan"] is (held_at == 200)
        shown = await env.store.get_work_lane(lane["lane_id"])
        member = shown["members"][0]
        assert member["ac_checked"] == 2
        assert member["observation"]["observed_at"] == timestamp(300)
        assert member["obs_rev"] == (2 if held_at == 200 else 3)
        assert len([e for e in shown["events"] if e["operation"] == "item_change"]) == member["obs_rev"] - 1
    run(body, str(tmp_path / "fixture.db"))


def test_commit_uses_current_members_and_fanout_across_composites(tmp_path, monkeypatch):
    async def body(env):
        lane = (await env.adopt(state="paused", lead=False, owner="operator", members=[A]))["lane"]
        await env.store.reconcile_work_observations(scan(100, fresh(), fresh(B)), settle_s=10, sweep=True)
        entered, release = hold_preparation(monkeypatch, 200)
        pending = asyncio.create_task(env.store.reconcile_work_observations(
            scan(200, fresh(checked=1, digest="second"), fresh(B)), settle_s=10, sweep=True))
        others = []
        unknown = "spec_demo__unknown"
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            for i in range(3):
                out = await env.store.apply_work_lane_operation(
                    stream_id=f"other-{i}:assistant", request_id=f"other-adopt-{i}", operation="adopt", lane_id=None,
                    expected_lane_version=None, payload={"adoption_key": f"request:other-{i}", "title": "Other composite",
                        "owner_kind": "operator", "work_state": "paused", "visible_chat": {"stream_id": f"other-{i}:assistant"},
                        "members": [A]}, actor_stream_id=FD, actor_generation=env.gen, binding_name="primary",
                    env_binding=env.composite._env_binding())
                others.append(out["lane"]["lane_id"])
            await env.op("set_members", {"members": [B, unknown]}, lane=lane["lane_id"], version=lane["version"])
        finally:
            release.set()
        assert await pending == {"changed": 1, "stale_scan": False}
        shown = await env.store.get_work_lane(lane["lane_id"])
        assert not [e for e in shown["events"] if e["operation"] == "item_change"]
        assert shown["members"][1]["spec_id"] == unknown
        assert shown["members"][1]["observation"]["quality"] == "missing"
        for lid in others:
            shown = await env.store.get_work_lane(lid)
            events = [e for e in shown["events"] if e["operation"] == "item_change"]
            assert len(events) == 1 and events[0]["event_id"] == f"item:{lid}:{A}:2"
            assert shown["members"][0]["ac_checked"] == 1
        # Repeated content refresh must not fan out again.
        await env.store.reconcile_work_observations(scan(201, fresh(checked=1, digest="second"), fresh(B)), settle_s=10, sweep=True)
        assert len([e for e in (await env.store.get_work_lane(others[0]))["events"] if e["operation"] == "item_change"]) == 1
    run(body, str(tmp_path / "fixture.db"))


@pytest.mark.parametrize("action", ["cancel", "restart"])
def test_late_preparation_cannot_write_after_cancellation_or_store_restart(tmp_path, monkeypatch, action):
    async def body():
        store = Store(str(tmp_path / "fixture.db"))
        store.start()
        pending = None
        release = threading.Event()
        try:
            await store.reconcile_work_observations(scan(100, fresh()), settle_s=10, sweep=True)
            entered, release = hold_preparation(monkeypatch, 200)
            pending = asyncio.create_task(store.reconcile_work_observations(
                scan(200, fresh(checked=1, digest="second")), settle_s=10, sweep=True))
            assert await asyncio.to_thread(entered.wait, 3)
            if action == "cancel":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                assert store.stop()
                store.start()
            release.set()
            if action == "restart":
                with pytest.raises(RuntimeError, match="Store changed during preparation"):
                    await pending
            assert (await store.work_index_status())["snapshot_at"] == timestamp(100)
            assert (await store.reconcile_work_observations(scan(300, fresh(checked=2, digest="third")), settle_s=10, sweep=True))["changed"] == 1
        finally:
            release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            store.stop()
    asyncio.run(body())


def test_delta_rows_events_and_clock_roll_back_together(tmp_path):
    async def body(env):
        lane = (await env.adopt(state="paused", lead=False, owner="operator", members=[A, B]))["lane"]
        await env.store.reconcile_work_observations(scan(100, fresh(), fresh(B)), settle_s=10, sweep=True)
        before = await env.store.get_work_lane(lane["lane_id"])
        before_status = await env.store.work_index_status()
        calls = []
        def fault():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("second delta fault")
        env.store._work_index_fault = fault
        changed = scan(200, fresh(checked=1, digest="second"), fresh(B, checked=1, digest="second"))
        with pytest.raises(RuntimeError, match="second delta fault"):
            await env.store.reconcile_work_observations(changed, settle_s=10, sweep=True)
        assert await env.store.get_work_lane(lane["lane_id"]) == before
        assert await env.store.work_index_status() == before_status
        env.store._work_index_fault = None
        assert (await env.store.reconcile_work_observations(changed, settle_s=10, sweep=True))["changed"] == 2
        shown = await env.store.get_work_lane(lane["lane_id"])
        assert [m["ac_checked"] for m in shown["members"]] == [1, 1]
        assert len([e for e in shown["events"] if e["operation"] == "item_change"]) == 2
    run(body, str(tmp_path / "fixture.db"))


def test_settled_missing_rows_stop_writing_without_losing_last_observed_time(tmp_path):
    async def body(env):
        lane = (await env.adopt(state="paused", lead=False, owner="operator", members=[A]))["lane"]
        await env.store.reconcile_work_observations(scan(100, fresh()), settle_s=10, sweep=True)
        for at, sweep in ((101, False), (102, True), (120, True)):
            await env.store.reconcile_work_observations(scan(at), settle_s=10, sweep=sweep)
        writes = []
        await env.store.submit(lambda c: c.set_trace_callback(lambda sql: writes.append(sql)
            if sql.upper().startswith("INSERT INTO V2_WORK_ITEM_OBSERVATIONS") else None))
        await env.store.reconcile_work_observations(scan(121), settle_s=10, sweep=True)
        assert not writes
        shown = (await env.store.get_work_lane(lane["lane_id"]))["members"][0]
        assert shown["status"] == "missing"
        assert shown["observation"]["observed_at"] == timestamp(100)
        assert shown["obs_rev"] == 1
    run(body, str(tmp_path / "fixture.db"))


def test_readers_see_complete_snapshot_while_delta_commit_is_held(tmp_path):
    import sqlite3
    path = str(tmp_path / "fixture.db")
    async def body(env):
        lane = (await env.adopt(state="paused", lead=False, owner="operator", members=[A, B]))["lane"]
        await env.store.reconcile_work_observations(scan(100, fresh(), fresh(B)), settle_s=10, sweep=True)
        entered, release = threading.Event(), threading.Event()
        def hold():
            if not entered.is_set():
                entered.set()
                assert release.wait(5)
        env.store._work_index_fault = hold
        apply = asyncio.create_task(env.store.reconcile_work_observations(
            scan(200, fresh(checked=1, digest="second"), fresh(B, checked=1, digest="second")), settle_s=10, sweep=True))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            # A real concurrent SQLite reader sees the prior committed generation.
            conn = sqlite3.connect(path)
            try:
                conn.execute("BEGIN")
                records = [json.loads(r[0]) for r in conn.execute("SELECT record_json FROM v2_work_item_observations ORDER BY spec_id")]
                state = json.loads(conn.execute("SELECT state_json FROM v2_work_index_state").fetchone()[0])
                assert [r["member"]["ac_checked"] for r in records] == [0, 0]
                assert state["snapshot_at"] == timestamp(100)
            finally:
                conn.close()
            pending_read = asyncio.create_task(env.store.get_work_lane(lane["lane_id"]))
            await asyncio.sleep(0)
            assert not pending_read.done()
        finally:
            release.set()
        await apply
        shown = await pending_read
        assert [m["ac_checked"] for m in shown["members"]] == [1, 1]
        assert {m["observation"]["observed_at"] for m in shown["members"]} == {timestamp(200)}
    run(body, path)
