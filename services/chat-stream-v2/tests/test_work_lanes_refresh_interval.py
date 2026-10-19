"""Minimum interval between work-lane drain passes; latest state wins; a trailing pass always runs."""
import asyncio
import json
import math
import time
from types import SimpleNamespace

import pytest

import store_work_lanes
from _shared.specs_service import SpecsSubsystem
from store import Store
from work_lanes_projection import WorkLanesInventory
from test_assistant_prose_mirror import ASSISTANT
from test_work_lanes import run

NO_SESSIONS = SimpleNamespace(list_open=lambda: [])


async def _burst(inv, seconds, gap):
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        inv.refresh()
        await asyncio.sleep(gap)
    await inv._task


def _count_passes(store):
    passes = []
    real = store.reconcile_work_lane_episodes

    async def counted(*args, **kwargs):
        passes.append(time.monotonic())
        return await real(*args, **kwargs)
    store.reconcile_work_lane_episodes = counted
    return passes


def test_burst_route_evaluations_bounded_by_interval(monkeypatch):
    """Measured shape: 17 lanes, 928 resolved routes of ~3 KB each, one inventory change every 20 ms."""
    monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", "1")
    evaluations = []
    real = store_work_lanes.lead_reported_done_conn
    monkeypatch.setattr(store_work_lanes, "lead_reported_done_conn",
                        lambda conn, lane: (evaluations.append(1), real(conn, lane))[1])

    async def body(env):
        lane_ids = [(await env.adopt(f"stream:l{i}"))["lane"]["lane_id"] for i in range(17)]
        pad = "x" * 2835

        def seed(conn):
            for i in range(928):
                conn.execute(
                    "INSERT INTO v2_assistant_composite_routes(route_id,stream_id,input_identity,payload_digest,"
                    "input_request_id,body,routing_state,delivery_state,dispatch_id,route_json,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,'resolved','landed',?,?,?,?)",
                    (f"r{i}", ASSISTANT, f"in{i}", "d", f"req{i}", "b", f"disp{i}",
                     json.dumps({"lane_id": lane_ids[i % 17], "pad": pad}),
                     "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"))
            conn.commit()
        await env.store.submit(seed)

        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(env.store, NO_SESSIONS, broadcast)
        passes = _count_passes(env.store)
        evaluations.clear()
        await _burst(inv, 2.0, 0.02)
        # 2 s at one pass per second: the leading pass, at most two spaced passes, one trailing pass.
        assert 2 <= len(passes) <= 4, passes
        assert len(evaluations) <= 4 * 2 * 17, len(evaluations)
    run(body)


def test_trailing_pass_publishes_latest_state_only(monkeypatch):
    monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", "0.3")

    async def body(env):
        lane_id = (await env.adopt("stream:t", state="paused", lead=False, owner="operator"))["lane"]["lane_id"]

        async def retitle(title):
            await env.store.submit(lambda c: (c.execute(
                "UPDATE v2_assistant_composite_lanes SET title=? WHERE lane_id=?", (title, lane_id)), c.commit()))
        frames = []

        async def broadcast(frame):
            frames.append(frame)
        inv = WorkLanesInventory(env.store, NO_SESSIONS, broadcast)
        inv.refresh()
        await asyncio.sleep(0.05)
        assert [f["lanes"][0]["title"] for f in frames] == ["Lane stream:t"]
        await retitle("second")
        inv.refresh()
        await asyncio.sleep(0.05)
        await retitle("third")
        inv.refresh()
        await inv._task
        # "second" was superseded inside the interval; the trailing pass published the latest state.
        assert [f["lanes"][0]["title"] for f in frames] == ["Lane stream:t", "third"]
    run(body)


def test_first_pass_is_immediate_and_next_waits_for_interval(monkeypatch):
    monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", "0.5")

    async def body(env):
        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(env.store, NO_SESSIONS, broadcast)
        passes = _count_passes(env.store)
        started = time.monotonic()
        inv.refresh()
        await asyncio.sleep(0.1)
        assert len(passes) == 1 and passes[0] - started < 0.1
        inv.refresh()
        await asyncio.sleep(0.2)
        assert len(passes) == 1
        await inv._task
        assert len(passes) == 2 and passes[1] - passes[0] >= 0.5
    run(body)


def test_stop_cancels_a_pending_wait(monkeypatch):
    monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", "30")

    async def body(env):
        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(env.store, NO_SESSIONS, broadcast)
        passes = _count_passes(env.store)
        inv.start()
        await asyncio.sleep(0.1)
        inv.refresh()
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await inv.stop()
        assert time.monotonic() - started < 1.0
        assert len(passes) == 1
    run(body)


def test_core_ready_start_is_not_delayed_by_interval(tmp_path, monkeypatch):
    """Composes with the deferred boot scan: pre-ready refreshes stay idle, start() scans at once."""
    from test_work_index_persistence import write_item
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(tmp_path))
    write_item(tmp_path)
    monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", "30")

    async def go():
        store = Store(":memory:")
        store.start()
        specs = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
        scans = []
        original = specs.scan_work_observations
        specs.scan_work_observations = lambda: (scans.append(1), original())[1]

        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(store, NO_SESSIONS, broadcast, specs=specs)
        try:
            for _ in range(5):
                inv.refresh()
            await asyncio.sleep(0.05)
            assert not scans
            started = time.monotonic()
            inv.start()
            await asyncio.wait_for(inv._task, 5)
            assert len(scans) == 1 and time.monotonic() - started < 5
            assert (await store.work_index_status())["available"] is True
        finally:
            await inv.stop()
            store.stop()
    asyncio.run(go())


def test_interval_configuration(monkeypatch):
    monkeypatch.delenv("WORK_LANES_REFRESH_MIN_S", raising=False)
    assert WorkLanesInventory(None, NO_SESSIONS, None).refresh_min_s == 2.0
    assert WorkLanesInventory(None, NO_SESSIONS, None, refresh_min_s=0).refresh_min_s == 0.0
    monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", "0.75")
    assert WorkLanesInventory(None, NO_SESSIONS, None).refresh_min_s == 0.75
    for value in ("nan", "inf", "-1"):
        monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", value)
        with pytest.raises(ValueError, match="work_lanes_refresh_interval_invalid"):
            WorkLanesInventory(None, NO_SESSIONS, None)
    for value in (math.nan, math.inf, -0.5):
        with pytest.raises(ValueError, match="work_lanes_refresh_interval_invalid"):
            WorkLanesInventory(None, NO_SESSIONS, None, refresh_min_s=value)


def test_zero_interval_keeps_back_to_back_passes(monkeypatch):
    monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", "0")

    async def body(env):
        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(env.store, NO_SESSIONS, broadcast)
        passes = _count_passes(env.store)
        inv.refresh()
        await inv._task
        inv.refresh()
        await inv._task
        assert len(passes) == 2
    run(body)
